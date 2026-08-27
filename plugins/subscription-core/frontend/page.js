function h(tag, cls = '', text = '') {
  const el = document.createElement(tag)
  if (cls) el.className = cls
  if (text) el.textContent = text
  return el
}
function fmtDate(value) {
  if (!value) return '未检测'
  return String(value).replace('T', ' ').slice(0, 16)
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
    error: '',
    stats: {},
    defaults: { mode: 'loose', require_cracked: false, require_subtitle: false },
    items: [],
    events: [],
    filter: 'all',
    keyword: '',
    expanded: new Set(),
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
      <div data-role="filter"></div>
      <div data-role="form"></div>
      <div class="sub-layout">
        <section data-role="items" class="sub-list"></section>
        <aside data-role="events" class="sub-events"></aside>
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
    host.innerHTML = ''
    const stats = [
      badge(`监控 ${state.stats.total || 0}`, 'primary'),
      badge(`待提交 ${state.stats.matched || 0}`, 'info'),
      badge(`下载中 ${state.stats.downloading || 0}`, 'warning'),
      badge(`待入库 ${state.stats.downloaded || 0}`, 'success'),
    ]
    const createBtn = sdk.ui?.button ? sdk.ui.button({ label: '新建订阅', tone: 'primary', onClick: () => { state.formOpen = !state.formOpen; state.editingId = ''; state.form = { code: '', title: '', mode: state.defaults.mode || 'loose', require_cracked: !!state.defaults.require_cracked, require_subtitle: !!state.defaults.require_subtitle }; render() } }) : h('button', '', '新建订阅')
    if (!sdk.ui?.button) createBtn.onclick = () => { state.formOpen = !state.formOpen; state.editingId = ''; state.form = { code: '', title: '', mode: state.defaults.mode || 'loose', require_cracked: !!state.defaults.require_cracked, require_subtitle: !!state.defaults.require_subtitle }; render() }
    const checkBtn = sdk.ui?.button ? sdk.ui.button({ label: state.checking ? '检测中…' : '手动检测', disabled: state.checking, onClick: checkAll }) : h('button', '', state.checking ? '检测中…' : '手动检测')
    if (!sdk.ui?.button) checkBtn.onclick = checkAll
    const bar = sdk.ui?.topBar ? sdk.ui.topBar({ tabs: stats, actions: [createBtn, checkBtn] }) : null
    if (bar) host.appendChild(bar.el || bar)
    else {
      const wrap = h('div', 'noor-plugin-topbar')
      const left = h('div', 'noor-plugin-topbar__tabs')
      stats.forEach(x => left.appendChild(x))
      const right = h('div', 'noor-plugin-topbar__actions')
      right.append(createBtn, checkBtn)
      wrap.append(left, right)
      host.appendChild(wrap)
    }
  }

  function renderNotice() {
    const host = $('notice')
    host.innerHTML = ''
    if (state.error) host.append(sdk.ui?.notice ? sdk.ui.notice({ text: state.error, tone: 'error' }) : h('div', 'sub-notice is-error', state.error))
  }

  function renderFilter() {
    const host = $('filter')
    host.innerHTML = ''
    const modes = [
      ['all', '全部'],
      ['subscribe', '订阅'],
      ['upgrade', '洗版'],
      ['matched', '待提交'],
      ['downloading', '下载中'],
      ['downloaded', '待入库'],
    ]
    const filterWrap = h('div', 'sub-filter')
    const modesWrap = h('div', 'sub-filter__modes')
    for (const [value, label] of modes) {
      const btn = h('button', `sub-filter__mode${state.filter === value ? ' is-active' : ''}`, label)
      btn.onclick = () => { state.filter = value; renderItems() }
      modesWrap.appendChild(btn)
    }
    const search = h('input', 'sub-filter__search')
    search.type = 'search'
    search.placeholder = '搜索番号、标题或来源'
    search.value = state.keyword
    search.oninput = () => { state.keyword = String(search.value || '').trim(); renderItems() }
    filterWrap.append(modesWrap, search)
    host.appendChild(filterWrap)
  }

  function openCreateForm() {
    state.formOpen = true
    state.editingId = ''
    state.form = { code: '', title: '', mode: state.defaults.mode || 'loose', require_cracked: !!state.defaults.require_cracked, require_subtitle: !!state.defaults.require_subtitle }
    render()
  }

  function openEdit(item) {
    state.formOpen = true
    state.editingId = item.id
    state.form = {
      code: item.code || '',
      title: item.title || '',
      mode: item.mode || 'loose',
      require_cracked: !!item.require_cracked,
      require_subtitle: !!item.require_subtitle,
    }
    render()
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
        <small>${esc(candidate.provider_label || candidate.provider || '')}${candidate.size_bytes ? ` · ${fmtSize(candidate.size_bytes)}` : ''} · ${Number(candidate.score || 0)} 分</small>
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
      const card = h('article', 'sub-card')
      const best = item.best_resource || null
      const cover = h('button', 'sub-card__cover')
      cover.type = 'button'
      cover.onclick = () => item.code && sdk.navigate?.(`/plugins/javdb?code=${encodeURIComponent(item.code)}`)
      renderFallbackImage(
        cover,
        imageCandidates(item.image_candidates, item.fanart_url, item.cover_url, item.thumb_url, item.image, best?.image_candidates, best?.fanart_url, best?.cover_url, best?.thumb_url, best?.image),
        item.code || 'NO IMAGE',
        () => refreshStoredImageCandidates(sdk, item),
      )
      card.innerHTML = `
        <div class="sub-card__head">
          <div class="sub-card__title"><strong>${esc(item.code)}</strong><span>${esc(item.title || '')}</span></div>
          <div class="sub-card__actions"><button data-action="detail" type="button">${state.expanded.has(item.id) ? '收起' : '展开'}</button><button data-action="check" type="button">检测</button><button data-action="more" type="button">•••</button></div>
        </div>
        <div class="sub-card__main">
          <div class="sub-card__badges"></div>
          <div class="sub-card__meta">上次检测：${fmtDate(item.last_checked_at)}${sourceText(item) ? ` · 来源：${esc(sourceText(item))}` : ''}${item.current_file_path ? ` · 当前：${esc(item.current_file_path)}` : ''}</div>
          ${best ? `<div class="sub-best"><strong>最佳候选</strong><span>${esc(best.provider_label || best.provider)} · ${esc(best.title || '')} · ${fmtSize(best.size_bytes)}</span><small>${esc(qualityText(item, best))}</small></div>` : '<div class="sub-best is-empty">暂无匹配资源</div>'}
          ${renderDownload(item)}
          ${item.last_submit_error ? `<div class="sub-best is-error"><strong>${item.last_submit_error_kind === 'downloader_quota_limited' ? '等待重试' : item.last_submit_error_kind === 'upgrade_not_improved' ? '未达洗版条件' : '推送异常'}</strong><span>${esc(item.last_submit_error)}${item.retry_after_at ? ` · 下次尝试：${fmtDate(item.retry_after_at)}` : ''}</span></div>` : ''}
          ${state.expanded.has(item.id) ? `${renderCompare(item)}${renderCandidates(item)}<div class="sub-expanded-actions">${(item.cleanup_suggestion || {}).status === 'pending' ? '<button data-action="ack-cleanup" type="button">已处理旧版</button>' : ''}<button data-action="edit" type="button">编辑规则</button><button data-action="delete" type="button">取消订阅</button></div>` : ''}
        </div>
      `
      card.prepend(cover)
      const bHost = card.querySelector('.sub-card__badges')
      itemBadges(item).forEach(x => bHost.appendChild(x))
      card.querySelector('[data-action="detail"]').onclick = () => {
        if (state.expanded.has(item.id)) state.expanded.delete(item.id)
        else state.expanded.add(item.id)
        renderItems()
      }
      card.querySelector('[data-action="more"]').onclick = () => {
        state.expanded.add(item.id)
        renderItems()
      }
      const ackCleanupBtn = card.querySelector('[data-action="ack-cleanup"]')
      if (ackCleanupBtn) ackCleanupBtn.onclick = () => ackCleanup(item.id)
      const editBtn = card.querySelector('[data-action="edit"]')
      if (editBtn) editBtn.onclick = () => openEdit(item)
      card.querySelector('[data-action="check"]').onclick = () => checkOne(item.id)
      const deleteBtn = card.querySelector('[data-action="delete"]')
      if (deleteBtn) deleteBtn.onclick = () => deleteOne(item.id)
      host.appendChild(card)
    }
  }

  function renderEvents() {
    const host = $('events')
    host.innerHTML = '<div class="sub-events__head">最近事件</div>'
    if (!state.events.length) {
      host.append(h('div', 'sub-events__empty', '暂无事件'))
      return
    }
    for (const ev of state.events.slice(0, 20)) {
      const row = h('div', `sub-event is-${ev.level || 'info'}`)
      row.innerHTML = `<span>${fmtDate(ev.created_at)}</span><strong>${esc(ev.message || '')}</strong>`
      host.appendChild(row)
    }
  }

  function render() {
    renderTopbar()
    renderNotice()
    renderFilter()
    renderForm()
    renderItems()
    renderEvents()
  }

  async function load() {
    state.loading = true
    state.error = ''
    render()
    try {
      const data = await apiPost('overview')
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

  async function ackCleanup(id) {
    try {
      await apiPost('ack_cleanup', { id })
      notify('success', '已确认旧版本处理建议')
      await load()
    } catch (e) {
      notify('error', e?.message || '确认失败')
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
  return () => {}
}
