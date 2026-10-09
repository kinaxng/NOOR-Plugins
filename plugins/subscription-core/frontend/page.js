function h(tag, cls = '', text = '') {
  const el = document.createElement(tag)
  if (cls) el.className = cls
  if (text) el.textContent = text
  return el
}
function fmtDate(value) {
  if (!value) return '未检测'
  const raw = String(value)
  const date = new Date(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(raw) ? raw : raw + 'Z')
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleString('zh-CN', { hour12: false })
}
function fmtSize(bytes) {
  const n = Number(bytes || 0)
  if (!n) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n, i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`
}
function fmtSpeed(bytes) {
  const value = fmtSize(bytes)
  return value ? `${value}/s` : ''
}
function downloaderLabel(value) {
  if (value === 'qbittorrent') return 'qBittorrent'
  if (value === 'xunlei-remote') return '迅雷远程'
  if (value === '115') return '115 离线'
  return value || '下载器'
}
function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}
function imageCandidates(...values) {
  const out = []
  const push = value => {
    if (!value) return
    if (Array.isArray(value)) {
      value.forEach(push)
      return
    }
    const text = String(value || '').trim()
    if (text && !out.includes(text)) out.push(text)
  }
  values.forEach(push)
  return out
}
function pluginFetch(sdk, path, init) {
  return sdk.api?.plugin
    ? sdk.api.plugin(path, init)
    : fetch(`/api/plugins/${sdk.pluginId || 'subscription-core'}${path}`, init)
}
async function refreshStoredImageCandidates(sdk, item) {
  const code = item?.code || item?.number || item?.search_code
  const text = String(code || '').trim()
  const id = String(item?.id || '').trim()
  if (!text && !id) return []
  try {
    const response = await pluginFetch(sdk, '/actions/refresh_cover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ payload: { id, code: text } }),
    })
    const payload = await response.json()
    return imageCandidates(payload?.image_candidates, payload?.cover_url, payload?.thumb_url, payload?.fanart_url)
  } catch {
    return []
  }
}
function loadImageUrl(url) {
  return new Promise(resolve => {
    const img = new Image()
    img.decoding = 'async'
    img.onload = () => resolve(url)
    img.onerror = () => resolve('')
    img.src = url
  })
}
async function firstLoadableImage(urls) {
  for (const url of imageCandidates(urls)) {
    const loaded = await loadImageUrl(url)
    if (loaded) return loaded
  }
  return ''
}
async function renderFallbackImage(host, candidates, placeholder = 'NO IMAGE', onExhausted) {
  const urls = imageCandidates(candidates)
  const token = Symbol('image-load')
  host.__imageLoadToken = token
  host.innerHTML = ''
  host.classList.add('is-loading')
  let loaded = await firstLoadableImage(urls)
  if (!loaded && typeof onExhausted === 'function') {
    const fresh = imageCandidates(await onExhausted())
    loaded = await firstLoadableImage(fresh.filter(url => !urls.includes(url)))
  }
  if (host.__imageLoadToken !== token) return
  host.classList.remove('is-loading')
  host.innerHTML = ''
  if (!loaded) {
    host.textContent = placeholder
    return
  }
  const img = document.createElement('img')
  img.alt = ''
  img.loading = 'lazy'
  img.src = loaded
  host.appendChild(img)
}
function badge(label, tone = 'info') {
  const b = h('span', `sub-badge sub-badge--${tone}`, label)
  return b
}

export async function mount(root, sdk = {}) {
  const pluginId = sdk.pluginId || 'subscription-core'
  const state = {
    loading: true,
    checking: false,
    rules: null,
    error: '',
    stats: {},
    defaults: { mode: 'loose', require_cracked: false, require_subtitle: false },
    items: [],
    events: [],
    filter: 'all',
    keyword: '',
    expanded: new Set(),
    activeEventId: '',
    filterTabs: null,
    searchInput: null,
    formOpen: false,
    editingId: '',
    form: { code: '', title: '', mode: 'loose', require_cracked: false, require_subtitle: false },
  }
  const apiPost = async (action, payload = {}) => {
    const response = await pluginFetch(sdk, `/actions/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload }) })
    return response.json()
  }

  root.innerHTML = `
    <div class="sub-page">
      <div data-role="topbar"></div>
      <div data-role="notice"></div>
      <div class="sub-layout">
        <section data-role="items" class="sub-list"></section>
        <aside class="sub-sidebar">
          <div data-role="stats"></div>
          <section data-role="events" class="sub-events"></section>
        </aside>
      </div>
    </div>
  `
  const $ = role => root.querySelector(`[data-role="${role}"]`)

  function notify(type, message) {
    if (!message) return
    const fn = type === 'error' ? 'error' : type === 'success' ? 'success' : 'info'
    sdk.toast?.[fn]?.(message)
  }

  function renderTopbar() {
    const host = $('topbar')
    state.filterTabs?.dispose?.()
    host.innerHTML = ''
    state.filterTabs = sdk.ui.tabs({
      value: state.filter,
      tabs: [
        { value: 'all', label: '全部' },
        { value: 'subscribe', label: '订阅' },
        { value: 'upgrade', label: '洗版' },
        { value: 'matched', label: '待提交' },
        { value: 'downloading', label: '下载中' },
        { value: 'downloaded', label: '待入库' },
      ],
      onChange: value => { state.filter = value; renderItems() },
    })
    state.searchInput = sdk.ui.input({ type: 'search', className: 'sub-filter__search', value: state.keyword, placeholder: '搜索番号、标题或来源', onInput: value => { state.keyword = String(value || '').trim(); renderItems() } })
    const checkBtn = sdk.ui?.button ? sdk.ui.button({ label: state.checking ? '检测中…' : '手动检测', disabled: state.checking, onClick: checkAll }) : h('button', '', state.checking ? '检测中…' : '手动检测')
    if (!sdk.ui?.button) checkBtn.onclick = checkAll
    const rules = state.rules
    const rulesBtn = sdk.ui.button({ label: rules ? `${rules.default_mode === 'strict' ? '严格' : '宽松'} · ${[rules.default_require_cracked && '破解', rules.default_require_subtitle && '中字'].filter(Boolean).join('+') || '不限'} · 提升 ≥${rules.upgrade_score_threshold}分` : '编辑规则', disabled: !rules, onClick: editRules })
    const bar = sdk.ui?.topBar ? sdk.ui.topBar({ tabs: state.filterTabs, actions: [state.searchInput, checkBtn, rulesBtn] }) : null
    if (bar) host.appendChild(bar.el || bar)
    else {
      const wrap = h('div', 'noor-plugin-topbar')
      const left = h('div', 'noor-plugin-topbar__tabs')
      left.appendChild(state.filterTabs)
      const right = h('div', 'noor-plugin-topbar__actions')
      right.append(state.searchInput, checkBtn, rulesBtn)
      wrap.append(left, right)
      host.appendChild(wrap)
    }
  }

  function renderNotice() {
    const host = $('notice')
    host.innerHTML = ''
    if (state.error) host.append(sdk.ui?.notice ? sdk.ui.notice({ text: state.error, tone: 'error' }) : h('div', 'sub-notice is-error', state.error))
  }

  function editRules() {
    const draft = { ...state.rules, version_scores: { ...state.rules.version_scores }, version_enabled: { ...state.rules.version_enabled } }
    const versions = [
      ['new_model_subtitle', '新模型破解＋中字', true, true, true],
      ['cracked_subtitle', '破解＋中字', true, true, false],
      ['new_model', '新模型破解', true, false, true],
      ['cracked', '仅破解', true, false, false],
      ['subtitle', '仅中字', false, true, false],
      ['normal', '普通', false, false, false],
    ]
    const scoreChain = h('div')
    scoreChain.setAttribute('role', 'table')
    scoreChain.setAttribute('aria-label', '版本优先级')
    const renderScoreChain = () => {
      scoreChain.replaceChildren()
      const sorted = [...versions].sort((a, b) => draft.version_scores[b[0]] - draft.version_scores[a[0]])
      const header = h('div')
      header.style.cssText = 'display:grid;grid-template-columns:40px 44px minmax(0,1fr) 68px;align-items:center;gap:8px;padding:10px 0'
      for (const label of ['优先级', '启用', '版本类型', '分数']) header.appendChild(h('strong', '', label))
      scoreChain.appendChild(header)
      sorted.forEach(([key, label], index) => {
        const input = scoreInput(draft.version_scores[key], value => { draft.version_scores[key] = value })
        input.setAttribute('aria-label', label + '分数')
        input.style.width = '100%'
        input.onchange = renderScoreChain
        input.disabled = draft.version_enabled[key] === false
        const enabled = sdk.ui.settingsSwitch({ checked: draft.version_enabled[key] !== false })
        enabled.input.setAttribute('aria-label', '启用' + label)
        enabled.input.onchange = () => {
          draft.version_enabled[key] = enabled.input.checked
          input.disabled = !enabled.input.checked
        }
        const line = h('div')
        line.style.cssText = header.style.cssText
        line.setAttribute('role', 'row')
        line.append(h('span', '', String(index + 1)), enabled, h('span', '', label), input)
        scoreChain.appendChild(line)
      })
    }
    const scoreInput = (value, onChange, max = 500) => {
      const input = h('input', 'noor-plugin-input')
      input.type = 'number'; input.min = '0'; input.max = String(max); input.step = '1'; input.value = String(value)
      input.setAttribute('aria-label', '分数')
      input.oninput = () => { onChange(input.value === '' ? NaN : Number(input.value)) }
      return input
    }
    const row = (label, control, description) => sdk.ui.settingRow({ label, control, description })
    const content = sdk.ui.settingsCard({ title: '版本优先级', description: '高分优先；关闭即排除。严格订阅还须满足该订阅的全部条件。', children: [
      scoreChain,
      row('洗版至少提升（分）', scoreInput(draft.upgrade_score_threshold, value => { draft.upgrade_score_threshold = value }, 100)),
    ] })
    renderScoreChain()
    const save = sdk.ui.button({ label: '保存', tone: 'primary', onClick: async () => {
      if (Object.values(draft.version_scores).some(v => !Number.isInteger(v) || v < 0 || v > 500) || !Number.isInteger(draft.upgrade_score_threshold) || draft.upgrade_score_threshold < 0 || draft.upgrade_score_threshold > 100) {
        notify('error', '版本分数须为 0–500 的整数，提升分数须为 0–100 的整数'); return
      }
      save.disabled = true
      try {
        const data = await apiPost('save_rules', draft)
        state.rules = data.rules
        modal.close(); notify('success', '规则已保存'); await load()
      } catch (error) { notify('error', error.message || '保存失败') }
      finally { save.disabled = false }
    } })
    const modal = sdk.ui.modal({ title: '编辑规则', content, width: 'lg', footer: [sdk.ui.button({ label: '取消', onClick: () => modal.close() }), save] })
  }

  function renderFilter() {
    state.filterTabs?.__noorSetValue?.(state.filter)
    if (state.searchInput && state.searchInput.value !== state.keyword) state.searchInput.value = state.keyword
  }

  function openCreateForm() {
    state.formOpen = true
    state.editingId = ''
    state.form = { code: '', title: '', mode: state.defaults.mode || 'loose', require_cracked: !!state.defaults.require_cracked, require_subtitle: !!state.defaults.require_subtitle }
    render()
  }

  function openEdit(item) {
    const draft = {
      code: item.code || '',
      title: item.title || '',
      mode: item.mode || 'loose',
      require_cracked: !!item.require_cracked,
      require_subtitle: !!item.require_subtitle,
      allow_normal: item.allow_normal === true,
    }
    const title = sdk.ui.input({ value: draft.title, placeholder: '可选', onInput: value => { draft.title = value } })
    let normalRow
    const mode = sdk.ui.select({ value: draft.mode, options: [
      { value: 'loose', label: '宽松订阅' },
      { value: 'strict', label: '严格订阅' },
    ], onChange: value => { draft.mode = value; if (normalRow) normalRow.hidden = value !== 'loose' } })
    const cracked = sdk.ui.settingsSwitch({ checked: draft.require_cracked })
    cracked.input.onchange = () => { draft.require_cracked = cracked.input.checked }
    const subtitle = sdk.ui.settingsSwitch({ checked: draft.require_subtitle })
    subtitle.input.onchange = () => { draft.require_subtitle = subtitle.input.checked }
    const normal = sdk.ui.settingsSwitch({ checked: draft.allow_normal })
    normal.input.onchange = () => { draft.allow_normal = normal.input.checked }
    normalRow = sdk.ui.settingRow({ label: '普通', control: normal, description: '关闭后，不再采用无破解、无中字的普通版本。' })
    normalRow.hidden = draft.mode !== 'loose'
    const content = sdk.ui.settingsCard({ title: item.code || '订阅', children: [
      sdk.ui.settingRow({ label: '标题', control: title }),
      sdk.ui.settingRow({ label: '模式', control: mode }),
      sdk.ui.settingRow({ label: '破解', control: cracked }),
      sdk.ui.settingRow({ label: '中字', control: subtitle }),
      normalRow,
    ] })
    const save = sdk.ui.button({ label: '保存', tone: 'primary', onClick: async () => {
      save.disabled = true
      try {
        await apiPost('update', { id: item.id, ...draft })
        modal.close()
        notify('success', '订阅已更新')
        await load()
      } catch (e) {
        notify('error', e?.message || '更新失败')
      } finally {
        save.disabled = false
      }
    } })
    const modal = sdk.ui.modal({ title: '编辑订阅', content, footer: [sdk.ui.button({ label: '取消', onClick: () => modal.close() }), save] })
    requestAnimationFrame(() => title.focus())
  }

  function renderForm() {
    const host = $('form')
    host.innerHTML = ''
    if (!state.formOpen) return
    const panel = h('section', 'sub-panel sub-form')
    const heading = state.editingId ? '编辑订阅' : '新建订阅 / 洗版'
    const hint = state.editingId ? '修改监控规则后保存，媒体库已有则自动归类为洗版。' : '媒体库已有则自动归类为洗版，否则为订阅。'
    panel.innerHTML = `
      <div class="sub-panel__head"><strong>${esc(heading)}</strong><span>${esc(hint)}</span></div>
      <div class="sub-form-grid">
        <label><span>番号</span><input data-field="code" placeholder="TEST-001" value="${esc(state.form.code)}" ${state.editingId ? 'disabled' : ''}></label>
        <label><span>标题</span><input data-field="title" placeholder="可选" value="${esc(state.form.title)}"></label>
        <label><span>模式</span><select data-field="mode"><option value="loose">宽松订阅</option><option value="strict">严格订阅</option></select></label>
        <div class="sub-checks">
          <label><input data-field="require_cracked" type="checkbox" ${state.form.require_cracked ? 'checked' : ''}> 破解</label>
          <label><input data-field="require_subtitle" type="checkbox" ${state.form.require_subtitle ? 'checked' : ''}> 中字</label>
        </div>
      </div>
      <div class="sub-form-actions">
        <button type="button" data-action="cancel">取消</button>
        <button type="button" data-action="save">保存订阅</button>
      </div>
    `
    panel.querySelector('[data-field="mode"]').value = state.form.mode
    panel.querySelectorAll('[data-field]').forEach(input => {
      if (input.disabled) return
      input.oninput = input.onchange = () => {
        const key = input.dataset.field
        state.form[key] = input.type === 'checkbox' ? input.checked : input.value
      }
    })
    panel.querySelector('[data-action="cancel"]').onclick = () => { state.formOpen = false; state.editingId = ''; render() }
    panel.querySelector('[data-action="save"]').onclick = state.editingId ? updateSubscription : createSubscription
    host.appendChild(panel)
  }

  function itemBadges(item) {
    const out = []
    out.push(badge(item.type === 'upgrade' ? '洗版' : '订阅', item.type === 'upgrade' ? 'warning' : 'primary'))
    out.push(badge(item.mode === 'strict' ? '严格' : '宽松', 'info'))
    if (item.require_cracked) out.push(badge('破解', 'warning'))
    if (item.require_subtitle) out.push(badge('中字', 'success'))
    if (item.mode === 'loose' && item.allow_normal === true) out.push(badge('普通', 'info'))
    const download = item.download_status || null
    if (download) out.push(badge(download.label || '已提交', download.tone || 'info'))
    else if (item.status === 'submitted') out.push(badge('已提交', 'primary'))
    else if (item.status === 'matched') out.push(badge('待提交', 'success'))
    else if (item.status === 'active') out.push(badge('监控中', 'info'))
    if ((item.cleanup_suggestion || {}).status === 'pending') out.push(badge('待处理旧版', 'danger'))
    return out
  }

  function sourceText(item) {
    const source = item?.last_source || item?.source || {}
    const parts = []
    if (source?.page === 'library') parts.push('媒体库')
    else if (source?.page === 'detail') parts.push('作品详情')
    else if (source?.page === 'resource') parts.push('资源搜索')
    else if (source?.page === 'recommend') parts.push('推荐中心')
    else if (source?.page === 'manual') parts.push('手动添加')
    if (source?.provider) parts.push(String(source.provider))
    if (source?.action) parts.push(String(source.action))
    if (source?.url) parts.push(String(source.url))
    return parts.join(' · ')
  }

  function qualityText(item, best) {
    if (!best) return ''
    const improvement = Number(best.improvement ?? item.candidate_profile?.improvement ?? 0)
    const required = Number(best.required_improvement ?? item.candidate_profile?.required_improvement ?? 0)
    if (item.type === 'upgrade') {
      return improvement >= required ? '达到洗版条件' : `还需提升 ${Math.max(0, required - improvement)} 分`
    }
    if (best.score) return `匹配度 ${Number(best.score || 0)} 分`
    return '可用候选'
  }

  function renderCompare(item) {
    const current = item.current_profile || {}
    const candidate = item.candidate_profile || {}
    if (!current.path && !candidate.provider) return ''
    const box = h('div', 'sub-compare')
    const currentBlock = h('div', 'sub-compare__col')
    currentBlock.appendChild(h('strong', '', '当前版本'))
    currentBlock.appendChild(row('评分', current.score != null ? `${current.score} 分` : '-'))
    currentBlock.appendChild(row('规格', resolutionLabel(current.resolution_rank)))
    currentBlock.appendChild(row('大小', current.size_bytes ? fmtSize(current.size_bytes) : '-'))
    currentBlock.appendChild(row('中字', current.has_subtitle ? '是' : '否'))
    currentBlock.appendChild(row('破解', current.is_cracked ? '是' : '否'))
    if (current.path) currentBlock.appendChild(row('路径', current.path))
    const candidateBlock = h('div', 'sub-compare__col')
    candidateBlock.appendChild(h('strong', '', '最佳候选'))
    if (candidate.provider) {
      candidateBlock.appendChild(row('来源', candidate.provider))
      candidateBlock.appendChild(row('评分', candidate.score != null ? `${candidate.score} 分` : '-'))
      if (item.type === 'upgrade') {
        candidateBlock.appendChild(row('提升', `${Number(candidate.score || 0) - Number(current.score || 0)} 分`))
        candidateBlock.appendChild(row('分数门槛', `≥ ${state.rules?.upgrade_score_threshold ?? 20} 分`))
      }
      candidateBlock.appendChild(row('规格', resolutionLabel(candidate.resolution_rank)))
      candidateBlock.appendChild(row('大小', candidate.size_bytes ? fmtSize(candidate.size_bytes) : '-'))
      candidateBlock.appendChild(row('中字', candidate.has_subtitle ? '是' : '否'))
      candidateBlock.appendChild(row('破解', candidate.is_cracked ? '是' : '否'))
      candidateBlock.appendChild(row('说明', candidate.reason || candidate.title || ''))
    } else {
      candidateBlock.appendChild(h('span', 'sub-compare__empty', '暂无匹配资源'))
    }
    box.append(currentBlock, candidateBlock)
    return box.outerHTML
  }

  function renderDownload(item) {
    const status = item.download_status
    if (!status && item.push_status !== 'submitted') return ''
    const downloader = downloaderLabel(status?.downloader_id || item.submitted_downloader_id)
    const progress = Math.round(Number(status?.progress || 0) * 100)
    const details = [downloader]
    if (status?.state) details.push(status.state)
    if (status?.speed) details.push(fmtSpeed(status.speed))
    if (status?.savepath) details.push(status.savepath)
    if (status?.message) details.push(status.message)
    return `<div class="sub-download is-${esc(status?.tone || 'info')}">
      <div class="sub-download__head"><strong>${esc(status?.label || '已提交下载器')}</strong><span>${progress}%</span></div>
      <div class="sub-download__track"><i style="width:${progress}%"></i></div>
      <div class="sub-download__meta">${esc(details.join(' · '))}</div>
    </div>`
  }

  function renderCandidates(item) {
    const candidates = Array.isArray(item.recent_candidates) ? item.recent_candidates : []
    if (!candidates.length) return ''
    return `<div class="sub-candidates"><div class="sub-section-title">本次候选 <span>${candidates.length}</span></div>${candidates.map((candidate, index) => `
      <div class="sub-candidate${index === 0 ? ' is-best' : ''}">
        <span>${index + 1}</span><strong>${esc(candidate.title || candidate.id || '未知资源')}</strong>
        <small>${esc(candidate.provider_label || candidate.provider || '')}${candidate.kind === 'ed2k' ? ' · ED2K' : ''}${candidate.size_bytes ? ` · ${fmtSize(candidate.size_bytes)}` : ''} · ${Number(candidate.score || 0)} 分</small>
      </div>`).join('')}</div>`
  }

  function row(label, value) {
    const line = h('div', 'sub-compare__row')
    line.innerHTML = `<span>${esc(label)}</span><strong>${esc(String(value || '-'))}</strong>`
    return line
  }

  function resolutionLabel(rank) {
    const labels = ['未知', '480p', '720p', '1080p', '2160p', '4K']
    const n = Number(rank || 0)
    return labels[n] || '未知'
  }

  function renderItems() {
    const host = $('items')
    host.innerHTML = ''
    if (state.loading) {
      host.append(h('div', 'sub-empty', '加载订阅中…'))
      return
    }
    if (!state.items.length) {
      host.append(h('div', 'sub-empty', '暂无订阅。请从 JavDB 作品页、详情页或资源搜索结果中接入订阅。'))
      return
    }
    const visibleItems = state.items.filter(item => {
      if (state.filter === 'subscribe' && item.type !== 'subscribe') return false
      if (state.filter === 'upgrade' && item.type !== 'upgrade') return false
      if (state.filter === 'matched' && item.status !== 'matched') return false
      if (state.filter === 'downloading' && !['queued', 'downloading', 'paused'].includes(item.download_status?.stage)) return false
      if (state.filter === 'downloaded' && item.download_status?.stage !== 'completed') return false
      if (state.keyword) {
        const text = `${item.code || ''} ${item.title || ''} ${sourceText(item)} ${item.current_file_path || ''}`.toLowerCase()
        if (!text.includes(state.keyword.toLowerCase())) return false
      }
      return true
    })
    if (!visibleItems.length) {
      host.append(h('div', 'sub-empty', '当前筛选条件下没有订阅。'))
      return
    }
    for (const item of visibleItems) {
      const isExpanded = state.expanded.has(item.id)
      const hasExpanded = state.expanded.size > 0
      const card = h('article', `sub-card${isExpanded ? ' is-expanded' : ''}${hasExpanded && !isExpanded ? ' is-dimmed' : ''}`)
      card.dataset.subscriptionId = item.id
      card.tabIndex = 0
      const best = item.best_resource || null
      const cover = h('div', 'sub-card__cover')
      renderFallbackImage(
        cover,
        imageCandidates(item.image_candidates, item.fanart_url, item.cover_url, item.thumb_url, item.image, best?.image_candidates, best?.fanart_url, best?.cover_url, best?.thumb_url, best?.image),
        item.code || 'NO IMAGE',
        () => refreshStoredImageCandidates(sdk, item),
      )
      card.innerHTML = `
        <div class="sub-card__head">
          <div class="sub-card__title"><strong>${esc(item.code)}</strong><span>${esc(item.title || '')}</span></div>
          <div class="sub-card__actions">${isExpanded && (item.cleanup_suggestion || {}).status === 'pending' ? '<button data-action="cleanup-old" type="button">一键处理旧版</button>' : ''}${isExpanded ? '<button data-action="edit" type="button">编辑</button><button data-action="delete" type="button">取消订阅</button>' : ''}<button data-action="check" type="button">检测</button></div>
        </div>
        <div class="sub-card__main">
          <div class="sub-card__badges"></div>
          <div class="sub-card__meta">上次检测：${fmtDate(item.last_checked_at)}${sourceText(item) ? ` · 来源：${esc(sourceText(item))}` : ''}${item.current_file_path ? ` · 当前：${esc(item.current_file_path)}` : ''}</div>
          ${best ? `<div class="sub-best"><strong>最佳候选</strong><span>${esc(best.provider_label || best.provider)}${best.kind === 'ed2k' ? ' · ED2K' : ''} · ${esc(best.title || '')} · ${fmtSize(best.size_bytes)}</span><small>${esc(qualityText(item, best))}</small></div>` : '<div class="sub-best is-empty">暂无匹配资源</div>'}
          ${renderDownload(item)}
          ${item.last_submit_error ? `<div class="sub-best is-error"><strong>${item.last_submit_error_kind === 'downloader_quota_limited' ? '等待重试' : item.last_submit_error_kind === 'upgrade_not_improved' ? '未达洗版条件' : '推送异常'}</strong><span>${esc(item.last_submit_error)}${item.retry_after_at ? ` · 下次尝试：${fmtDate(item.retry_after_at)}` : ''}</span></div>` : ''}
          ${isExpanded ? `${renderCompare(item)}${renderCandidates(item)}` : ''}
        </div>
      `
      card.prepend(cover)
      const bHost = card.querySelector('.sub-card__badges')
      itemBadges(item).forEach(x => bHost.appendChild(x))
      const toggleCard = () => {
        const fromHeight = card.getBoundingClientRect().height
        state.expanded = state.expanded.has(item.id) ? new Set() : new Set([item.id])
        state.activeEventId = ''
        renderItems()
        const nextCard = Array.from($('items').children).find(entry => entry.dataset.subscriptionId === item.id)
        if (nextCard && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
          const toHeight = nextCard.getBoundingClientRect().height
          nextCard.classList.add('is-resizing')
          const animation = nextCard.animate(
            [{ height: `${fromHeight}px` }, { height: `${toHeight}px` }],
            { duration: 220, easing: 'cubic-bezier(.2,.8,.2,1)' },
          )
          animation.onfinish = () => nextCard.classList.remove('is-resizing')
          animation.oncancel = () => nextCard.classList.remove('is-resizing')
        }
        renderEvents()
      }
      card.onclick = event => {
        if (event.target.closest('button, input, select, a')) return
        toggleCard()
      }
      card.onkeydown = event => {
        if ((event.key === 'Enter' || event.key === ' ') && event.target === card) { event.preventDefault(); toggleCard() }
      }
      const cleanupOldBtn = card.querySelector('[data-action="cleanup-old"]')
      if (cleanupOldBtn) cleanupOldBtn.onclick = event => { event.stopPropagation(); cleanupOld(item.id) }
      const editBtn = card.querySelector('[data-action="edit"]')
      if (editBtn) editBtn.onclick = event => { event.stopPropagation(); openEdit(item) }
      card.querySelector('[data-action="check"]').onclick = event => { event.stopPropagation(); checkOne(item.id) }
      const deleteBtn = card.querySelector('[data-action="delete"]')
      if (deleteBtn) deleteBtn.onclick = event => { event.stopPropagation(); deleteOne(item.id) }
      host.appendChild(card)
    }
  }

  function renderEvents() {
    const host = $('events')
    host.replaceChildren()
    const stats = h('div', 'sub-filter')
    stats.append(badge(`监控 ${state.stats.total || 0}`, 'primary'), badge(`待提交 ${state.stats.matched || 0}`, 'info'), badge(`下载中 ${state.stats.downloading || 0}`, 'warning'), badge(`待入库 ${state.stats.downloaded || 0}`, 'success'))
    $('stats').replaceChildren(sdk.ui.settingsCard({ title: '订阅概览', children: [stats] }))
    host.append(h('div', 'sub-events__head', '最近事件'))
    if (!state.events.length) {
      host.append(h('div', 'sub-events__empty', '暂无事件'))
      return
    }
    for (const ev of state.events.slice(0, 20)) {
      const row = h('div', `sub-event is-${ev.level || 'info'}${state.activeEventId && state.activeEventId !== ev.id ? ' is-dimmed' : ''}`)
      const item = state.items.find(item => item.id === ev.subscription_id)
      const code = ev.code || item?.code || '系统'
      const meta = h('div', 'sub-event__meta')
      meta.append(sdk.ui.badge({ label: code, tone: item ? 'primary' : 'info' }), h('time', '', fmtDate(ev.created_at)))
      row.append(meta, h('strong', '', ev.message || ''))
      if (item) {
        const locate = () => {
          const closing = state.activeEventId === ev.id
          state.activeEventId = closing ? '' : ev.id
          state.expanded = closing ? new Set() : new Set([item.id])
          state.filter = 'all'
          state.keyword = ''
          renderFilter()
          renderItems()
          renderEvents()
          const target = Array.from($('items').children).find(card => card.dataset.subscriptionId === item.id)
          if (!target) return
          target.focus({ preventScroll: true })
          target.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' })
        }
        row.classList.add('is-linked')
        row.tabIndex = 0
        row.setAttribute('role', 'button')
        row.setAttribute('aria-label', `定位到 ${code}：${ev.message || ''}`)
        row.onclick = locate
        row.onkeydown = event => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); locate() }
        }
      }
      host.appendChild(row)
    }
  }

  function render() {
    renderTopbar()
    renderNotice()
    renderFilter()
    renderItems()
    renderEvents()
  }

  async function load(quiet = false) {
    state.loading = !quiet
    state.error = ''
    render()
    try {
      const [data, rulesData] = await Promise.all([apiPost('overview'), apiPost('rules')])
      state.rules = rulesData.rules
      state.stats = data.stats || {}
      state.items = data.items || []
      state.events = data.events || []
      state.defaults = data.defaults || state.defaults
    } catch (e) {
      state.error = e?.message || '读取订阅失败'
    } finally {
      state.loading = false
      render()
    }
  }

  async function createSubscription() {
    try {
      const data = await apiPost('create', state.form)
      state.formOpen = false
      state.editingId = ''
      state.form = { code: '', title: '', mode: 'loose', require_cracked: false, require_subtitle: false }
      notify('success', data.created ? '订阅已创建' : '订阅已存在')
      await load()
    } catch (e) {
      notify('error', e?.message || '创建失败')
    }
  }

  async function updateSubscription() {
    try {
      await apiPost('update', { id: state.editingId, ...state.form })
      state.formOpen = false
      state.editingId = ''
      notify('success', '订阅已更新')
      await load()
    } catch (e) {
      notify('error', e?.message || '更新失败')
    }
  }

  async function checkAll() {
    state.checking = true
    renderTopbar()
    try {
      await apiPost('check_once')
      notify('success', '检测完成')
      await load()
    } catch (e) {
      notify('error', e?.message || '检测失败')
    } finally {
      state.checking = false
      renderTopbar()
    }
  }

  async function checkOne(id) {
    try {
      await apiPost('check_once', { id, force: true })
      notify('success', '检测完成')
      await load()
    } catch (e) {
      notify('error', e?.message || '检测失败')
    }
  }

  async function cleanupOld(id) {
    const item = state.items.find(entry => entry.id === id)
    const suggestion = item?.cleanup_suggestion || {}
    const ok = await sdk.ui.confirm({ title: `处理 ${item?.code || ''} 旧版`, message: `将删除本地旧版及其硬链接源链：\n${suggestion.old_path || ''}\n\n115 新版保持不变。此操作不可撤销。`, confirmText: '删除旧版', danger: true })
    if (!ok) return
    try {
      await apiPost('cleanup_old_version', { id })
      notify('success', '旧版本已清理')
      await load()
    } catch (e) {
      notify('error', e?.message || '旧版本清理失败')
    }
  }

  async function deleteOne(id) {
    const item = state.items.find(entry => entry.id === id)
    const ok = sdk.ui?.confirm
      ? await sdk.ui.confirm({ title: '取消订阅', message: `确认取消 ${item?.code || '这个订阅'}？`, confirmText: '取消订阅', danger: true })
      : true
    if (!ok) return
    try {
      await apiPost('delete', { id })
      notify('success', '订阅已删除')
      await load()
    } catch (e) {
      notify('error', e?.message || '删除失败')
    }
  }

  await load()
  let polling = false
  const timer = setInterval(async () => {
    if (polling || document.hidden || state.loading || state.checking || state.formOpen) return
    polling = true
    try { await load(true) } finally { polling = false }
  }, 30000)
  return () => { clearInterval(timer); state.filterTabs?.dispose?.() }
}
