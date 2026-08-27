export async function mount(el, sdk = {}) {
  const pluginId = sdk.pluginId || 'xunlei-remote'
  const api = (path, init) => sdk.api?.plugin ? sdk.api.plugin(path, init) : fetch(`/api/plugins/${pluginId}${path}`, init)
  const toast = (type, msg) => sdk.toast?.[type]?.(msg)
  const state = {
    loading: false,
    error: '',
    tasks: [],
    device: null,
    about: null,
    config: null,
    dailyLimit: null,
    trySpeed: null,
    submitModal: null,
    submitStatus: 'idle',
    submitProgress: 0,
    filter: 'active',
    query: '',
    page: 1,
    pageSize: 10,
    actioning: new Set(),
    timer: null,
    destroyed: false,
    renderedTabsKey: '',
    tableSignature: '',
  }

  el.innerHTML = `
    <div class="xunlei-remote-page">
      <div class="xunlei-remote-toolbar">
        <div data-role="tabs"></div>
        <div class="xunlei-remote-actions">
          <span data-role="usage" class="xunlei-remote-badge xunlei-remote-badge--info">额度读取中</span>
          <span data-role="device" class="xunlei-remote-badge">未连接</span>
          <button data-role="newTask" class="xunlei-remote-btn xunlei-remote-btn--primary">新建任务</button>
          <button data-role="residual" class="xunlei-remote-btn">残留处理</button>
          <button data-role="refresh" class="xunlei-remote-btn xunlei-remote-btn--primary">刷新</button>
        </div>
      </div>
      <div class="xunlei-remote-stats" data-role="stats">
        <div class="xunlei-remote-stat"><span>任务</span><strong data-role="statTotal">0</strong></div>
        <div class="xunlei-remote-stat"><span>进行中</span><strong data-role="statActive">0</strong></div>
        <div class="xunlei-remote-stat"><span>已完成</span><strong data-role="statDone">0</strong></div>
        <div class="xunlei-remote-stat"><span>速度</span><strong data-role="statSpeed">0 B/s</strong></div>
        <label class="xunlei-remote-stat xunlei-remote-stat--search">
          <span>搜索任务</span>
          <input data-role="search" placeholder="任务名 / 路径 / 链接" autocomplete="off">
        </label>
      </div>
      <div data-role="state" class="xunlei-remote-state" style="display:none"></div>
      <div class="xunlei-remote-table-card">
        <div class="xunlei-remote-table-wrap">
          <table class="xunlei-remote-table">
            <thead><tr><th>名称</th><th>状态</th><th>进度</th><th>速度</th><th>大小</th><th>时间</th><th class="xunlei-remote-right">操作</th></tr></thead>
            <tbody data-role="tbody"></tbody>
          </table>
        </div>
      </div>
      <div data-role="pager" class="xunlei-remote-pager"></div>
    </div>
  `

  const $ = role => el.querySelector(`[data-role="${role}"]`)
  const tabsHost = $('tabs')
  const usageBadge = $('usage')
  const device = $('device')
  const refresh = $('refresh')
  const newTask = $('newTask')
  const residual = $('residual')
  const statTotal = $('statTotal')
  const statActive = $('statActive')
  const statDone = $('statDone')
  const statSpeed = $('statSpeed')
  const search = $('search')
  const stateBox = $('state')
  const tbody = $('tbody')
  const pager = $('pager')

  const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]))
  const fmtBytes = n => { n = Number(n || 0); if (!n) return '0 B'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ } return `${n.toFixed(i >= 3 ? 2 : 1)} ${u[i]}` }
  const fmtSpeed = n => Number(n || 0) ? `${fmtBytes(n)}/s` : '0 B/s'
  const fmtTime = s => { if (!s) return '-'; const d = new Date(s); return Number.isNaN(d.getTime()) ? s : `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` }
  const pct = n => `${Math.round(Number(n || 0) * 1000) / 10}%`
  const dayKey = value => {
    const date = value instanceof Date ? value : new Date(value)
    if (Number.isNaN(date.getTime())) return ''
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
  }
  const phaseText = p => ({ PHASE_TYPE_PENDING: '等待中', PHASE_TYPE_RUNNING: '下载中', PHASE_TYPE_PAUSED: '暂停', PHASE_TYPE_COMPLETE: '完成', PHASE_TYPE_ERROR: '错误' }[p] || p || '-')
  const phaseTone = p => p === 'PHASE_TYPE_COMPLETE' ? 'success' : p === 'PHASE_TYPE_ERROR' ? 'error' : p === 'PHASE_TYPE_PAUSED' ? 'muted' : p === 'PHASE_TYPE_RUNNING' ? 'info' : 'warning'
  function modalApi(options) {
    if (sdk.ui?.modal) return sdk.ui.modal(options)
    const mask = document.createElement('div')
    mask.className = 'noor-plugin-modal-mask'
    const panel = document.createElement('div')
    panel.className = `noor-plugin-modal noor-plugin-modal--${options.width || 'md'}`
    panel.innerHTML = `<div class="noor-plugin-modal__head"><div class="noor-plugin-modal__title">${esc(options.title || '')}</div><button type="button" class="noor-plugin-btn noor-plugin-modal__close">关闭</button></div><div class="noor-plugin-modal__body"></div>`
    const body = panel.querySelector('.noor-plugin-modal__body')
    const footer = document.createElement('div')
    footer.className = 'noor-plugin-modal__actions'
    if (Array.isArray(options.content)) for (const x of options.content) body.appendChild(x)
    else if (options.content) body.appendChild(options.content)
    if (Array.isArray(options.footer)) for (const x of options.footer) footer.appendChild(x)
    else if (options.footer) footer.appendChild(options.footer)
    if (footer.childNodes.length) panel.appendChild(footer)
    mask.appendChild(panel)
    const close = () => { mask.remove(); options.onClose?.() }
    panel.querySelector('.noor-plugin-modal__close').onclick = close
    mask.onclick = e => { if (e.target === mask && options.closeOnMask !== false) close() }
    document.body.appendChild(mask)
    return { el: mask, body, close }
  }

  function createSubmitButton(options) {
    return sdk.ui?.submitButton
      ? sdk.ui.submitButton(options)
      : Object.assign(document.createElement('button'), { type: 'button', className: `xunlei-remote-btn xunlei-remote-btn--primary`, textContent: options.label || options.idleLabel || '提交' })
  }

  function textarea(options = {}) {
    if (sdk.ui?.textarea) return sdk.ui.textarea(options)
    const el = document.createElement('textarea')
    el.className = options.className ? `noor-plugin-input xunlei-remote-textarea ${options.className}` : 'noor-plugin-input xunlei-remote-textarea'
    el.placeholder = options.placeholder || ''
    el.value = options.value || ''
    el.rows = options.rows || 5
    return el
  }

  function makePathCombo() {
    const root = document.createElement('div')
    root.className = 'xunlei-path-combo'
    const head = document.createElement('div')
    head.className = 'xunlei-path-combo__head'
    const badge = document.createElement('span')
    badge.className = 'xunlei-path-combo__badge'
    badge.textContent = '路径'
    const input = document.createElement('input')
    input.className = 'xunlei-path-combo__input'
    input.placeholder = '/mnt/downloads/...'
    const toggle = document.createElement('button')
    toggle.type = 'button'
    toggle.className = 'xunlei-path-combo__toggle'
    toggle.setAttribute('aria-label', '展开路径')
    const menu = document.createElement('div')
    menu.className = 'xunlei-path-combo__menu'
    menu.style.display = 'none'
    head.append(badge, input, toggle)
    root.append(head, menu)
    root.dataset.currentPath = ''
    const close = () => { menu.style.display = 'none' }
    const open = () => { menu.style.display = 'flex' }
    const setPath = (path, label = '') => {
      path = String(path || '').trim()
      root.dataset.currentPath = path
      input.value = path
      badge.textContent = label || path.split('/').filter(Boolean).pop() || '路径'
    }
    input.oninput = () => { root.dataset.currentPath = input.value }
    input.onfocus = () => open()
    toggle.onclick = e => { e.stopPropagation(); menu.style.display === 'none' ? open() : close() }
    document.addEventListener('click', event => { if (!root.contains(event.target)) close() })
    root.__setPath = setPath
    root.__menu = menu
    root.__input = input
    return root
  }

  async function postAction(action, payload = {}) {
    const r = await api(`/actions/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload }) })
    const data = await r.json().catch(() => ({}))
    if (!r.ok || data.ok === false) throw new Error(data.detail || data.message || data.error || '请求失败')
    return data
  }

  async function openResidualModal() {
    const box = document.createElement('div')
    box.className = 'xunlei-restore'
    box.innerHTML = '<div class="xunlei-restore-state">扫描中...</div>'
    const closeBtn = sdk.ui?.button ? sdk.ui.button({ label: '关闭', onClick: () => modal.close() }) : document.createElement('button')
    if (!sdk.ui?.button) { closeBtn.type = 'button'; closeBtn.className = 'xunlei-remote-btn'; closeBtn.textContent = '关闭'; closeBtn.onclick = () => modal.close() }
    const modal = modalApi({ title: '残留文件处理', width: 'lg', content: box, footer: [closeBtn] })
    try {
      const data = await postAction('restore_candidates', { limit: 300 })
      const items = Array.isArray(data.items) ? data.items : []
      if (!items.length) {
        box.innerHTML = `<div class="xunlei-restore-state">${data.roots?.length ? '没有发现残留文件' : '请先在插件设置中填写残留文件扫描目录'}</div>`
        return
      }
      box.innerHTML = items.map((item, index) => `
        <div class="xunlei-restore-item">
          <div class="xunlei-restore-item__main"><strong>${esc(item.name)}</strong><small>${esc(item.path)}</small></div>
          <button type="button" class="xunlei-remote-btn" data-residual-index="${index}">删除并搜索</button>
        </div>
      `).join('')
      for (const button of box.querySelectorAll('[data-residual-index]')) {
        button.onclick = async () => {
          const item = items[Number(button.dataset.residualIndex)]
          const ok = await (sdk.ui?.confirm ? sdk.ui.confirm({ title: '删除残留文件', message: item.path, confirmText: '删除并搜索', danger: true }) : Promise.resolve(confirm(`删除 ${item.path}？`)))
          if (!ok) return
          button.disabled = true
          try {
            const result = await postAction('delete_restore_file', { path: item.path })
            const query = result.code || item.code || item.name.replace(/\.(?:xltd|xtld)$/i, '')
            modal.close()
            if (sdk.navigate) await sdk.navigate({ path: '/search/resources', query: { q: query } })
            else location.assign(`/search/resources?q=${encodeURIComponent(query)}`)
          } catch (error) {
            toast('error', error.message || '删除失败')
            button.disabled = false
          }
        }
      }
    } catch (error) {
      box.innerHTML = `<div class="xunlei-restore-state is-error">${esc(error.message || '扫描失败')}</div>`
    }
  }

  function openNewTaskModal() {
    if (state.submitModal) state.submitModal.close()
    state.submitStatus = 'idle'
    state.submitProgress = 0
    const form = document.createElement('div')
    form.className = 'xunlei-remote-form'
    if (state.dailyLimit?.title) {
      const warning = document.createElement('div')
      warning.className = 'xunlei-remote-quota-warning'
      warning.textContent = state.dailyLimit.title + '，推送可能失败。建议优先使用 qBittorrent。'
      form.appendChild(warning)
    }
    const urlInput = textarea({ placeholder: '每行一个 magnet / BT URL / 普通 URL，最多 50 条', rows: 6 })
    const defaultPath = state.config?.default_savepath || state.config?.savepath || '/downloads/xunlei/'
    const pathCombo = makePathCombo()
    pathCombo.__setPath(defaultPath, '默认')
    const indicesSelect = sdk.ui?.select ? sdk.ui.select({
      value: 'auto',
      options: [
        { label: '智能选择：跳过广告小文件，保留字幕', value: 'auto' },
        { label: '下载全部文件', value: '--1' },
      ],
    }) : document.createElement('select')
    if (!sdk.ui?.select) {
      indicesSelect.className = 'noor-plugin-input noor-plugin-select'
      indicesSelect.innerHTML = '<option value="auto">智能选择：跳过广告小文件，保留字幕</option><option value="--1">下载全部文件</option>'
    }
    const fields = [
      sdk.ui?.field ? sdk.ui.field({ label: '下载链接', hint: '支持批量添加：一行一条，最多 50 条。', control: urlInput }) : urlInput,
      sdk.ui?.field ? sdk.ui.field({ label: '下载路径', hint: '选择历史路径后可直接在输入框继续补子目录。', control: pathCombo }) : pathCombo,
      sdk.ui?.field ? sdk.ui.field({ label: '文件选择', control: indicesSelect }) : indicesSelect,
    ]
    for (const field of fields) form.appendChild(field)
    let submitBtn
    const setSubmitButton = () => {
      const next = createSubmitButton({
        idleLabel: '推送下载',
        submittingLabel: state.submitProgress ? `${Math.round(state.submitProgress)}%` : '推送中',
        successLabel: '推送成功',
        errorLabel: '推送失败',
        status: state.submitStatus,
        progress: state.submitProgress,
        className: 'xunlei-remote-submit',
        onClick: submitTask,
      })
      if (!sdk.ui?.submitButton) next.onclick = submitTask
      submitBtn?.replaceWith(next)
      submitBtn = next
    }
    const cancelBtn = sdk.ui?.button ? sdk.ui.button({ label: '取消', onClick: () => state.submitModal?.close() }) : document.createElement('button')
    if (!sdk.ui?.button) { cancelBtn.type = 'button'; cancelBtn.className = 'xunlei-remote-btn'; cancelBtn.textContent = '取消'; cancelBtn.onclick = () => state.submitModal?.close() }
    async function submitTask() {
      const urls = String(urlInput.value || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean).slice(0, 50)
      if (!urls.length) { toast('error', '请填写下载链接'); urlInput.focus?.(); return }
      if (String(urlInput.value || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean).length > 50) {
        toast('error', '单次最多添加 50 条链接')
        return
      }
      const savepath = String(pathCombo.dataset.currentPath || pathCombo.__input?.value || '').trim()
      if (!savepath) { toast('error', '请选择下载路径'); return }
      state.submitStatus = 'running'
      state.submitProgress = 1
      setSubmitButton()
      let okCount = 0
      let failCount = 0
      const failures = []
      try {
        for (let i = 0; i < urls.length; i += 1) {
          state.submitProgress = Math.max(2, Math.round((i / urls.length) * 92))
          setSubmitButton()
          const payload = {
            url: urls[i],
            urls: urls[i],
            savepath,
            file_indices: String(indicesSelect.value || 'auto').trim() || 'auto',
          }
          try {
            const r = await api('/downloads', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload }) })
            const data = await r.json().catch(() => ({}))
            if (!r.ok || data.ok === false) throw new Error(data.detail || data.message || data.error || data.title || '推送失败')
            okCount += 1
          } catch (e) {
            failCount += 1
            failures.push({ url: urls[i], message: e?.message || '推送失败' })
          }
        }
        state.submitProgress = 100
        state.submitStatus = failCount ? 'error' : 'success'
        setSubmitButton()
        if (okCount) toast('success', `已推送 ${okCount} 条任务`)
        if (failCount) {
          const first = failures[0]
          toast('error', `${failCount} 条任务推送失败：${first?.message || '未知错误'}`)
          showBatchFailureModal(failures)
        }
        await load({ silent: true })
      } catch (e) {
        state.submitStatus = 'error'
        state.submitProgress = 100
        setSubmitButton()
        toast('error', e.message || '推送失败')
      }
    }
    setSubmitButton()
    state.submitModal = modalApi({ title: '新建迅雷任务', width: 'md', content: form, footer: [cancelBtn, submitBtn], onClose: () => { state.submitModal = null } })
    loadDownloadOptions(pathCombo)
    setTimeout(() => urlInput.focus?.(), 0)
  }


  function showBatchFailureModal(failures) {
    if (!failures?.length) return
    const box = document.createElement('div')
    box.className = 'xunlei-remote-failure-list'
    box.innerHTML = failures.map((item, index) => `<div class="xunlei-remote-failure-item"><strong>#${index + 1} ${esc(item.message || '推送失败')}</strong><small>${esc(item.url || '')}</small></div>`).join('')
    const closeBtn = sdk.ui?.button ? sdk.ui.button({ label: '知道了', tone: 'primary', onClick: () => modal.close() }) : document.createElement('button')
    if (!sdk.ui?.button) { closeBtn.type = 'button'; closeBtn.className = 'xunlei-remote-btn xunlei-remote-btn--primary'; closeBtn.textContent = '知道了'; closeBtn.onclick = () => modal.close() }
    const modal = modalApi({ title: '失败任务明细', width: 'md', content: box, footer: [closeBtn] })
  }

  function openFolderPicker(pathCombo) {
    const box = document.createElement('div')
    box.className = 'xunlei-folder-picker'
    const current = document.createElement('div')
    current.className = 'xunlei-folder-picker__current'
    const list = document.createElement('div')
    list.className = 'xunlei-folder-picker__list'
    box.append(current, list)
    const stack = [{ id: '', name: '根目录', path: '' }]
    let selected = { id: '', path: pathCombo.dataset.currentPath || '' }
    const renderFolders = async () => {
      const parent = stack[stack.length - 1]
      current.textContent = parent.path || parent.name || '根目录'
      list.innerHTML = '<div class="xunlei-folder-picker__empty">读取中...</div>'
      try {
        const res = await api('/actions/browse_folders', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: { parent_id: parent.id || '', limit: 200 } }) })
        const data = await res.json().catch(() => ({}))
        if (!res.ok || data.ok === false) throw new Error(data.detail || data.message || '读取失败')
        const folders = Array.isArray(data.folders) ? data.folders : []
        list.innerHTML = ''
        if (stack.length > 1) {
          const up = document.createElement('button')
          up.type = 'button'
          up.className = 'xunlei-folder-picker__item'
          up.textContent = '← 返回上级'
          up.onclick = () => { stack.pop(); renderFolders() }
          list.appendChild(up)
        }
        if (!folders.length && stack.length <= 1) {
          list.innerHTML = '<div class="xunlei-folder-picker__empty">未读取到可选文件夹</div>'
          return
        }
        for (const folder of folders) {
          const item = document.createElement('button')
          item.type = 'button'
          item.className = 'xunlei-folder-picker__item'
          item.innerHTML = `<span>${esc(folder.name || folder.path || '文件夹')}</span><small>${esc(folder.path || '')}</small>`
          item.onclick = () => { selected = folder; stack.push(folder); renderFolders() }
          list.appendChild(item)
        }
      } catch (e) {
        list.innerHTML = `<div class="xunlei-folder-picker__empty">${esc(e.message || '读取失败')}</div>`
      }
    }
    const useSelectedFolder = async () => {
      const path = stack[stack.length - 1]?.path || selected.path
      if (!path) { toast('error', '请选择文件夹'); return }
      await saveSelectedFolder(path, pathCombo)
      modal.close()
    }
    const cancel = sdk.ui?.button ? sdk.ui.button({ label: '取消', onClick: () => modal.close() }) : document.createElement('button')
    const use = sdk.ui?.button ? sdk.ui.button({ label: '使用当前文件夹', tone: 'primary', onClick: useSelectedFolder }) : document.createElement('button')
    if (!sdk.ui?.button) {
      cancel.type = 'button'; cancel.textContent = '取消'; cancel.className = 'xunlei-remote-btn'; cancel.onclick = () => modal.close()
      use.type = 'button'; use.textContent = '使用当前文件夹'; use.className = 'xunlei-remote-btn xunlei-remote-btn--primary'; use.onclick = useSelectedFolder
    }
    const modal = modalApi({ title: '选择下载文件夹', width: 'lg', content: box, footer: [cancel, use] })
    renderFolders()
  }

  function appendBrowseItem(menu, pathCombo) {
    const sep = document.createElement('div')
    sep.className = 'xunlei-path-combo__sep'
    const browse = document.createElement('button')
    browse.type = 'button'
    browse.className = 'xunlei-path-combo__item xunlei-path-combo__item--browse'
    browse.textContent = '选择文件夹…'
    browse.onclick = () => { menu.style.display = 'none'; openFolderPicker(pathCombo) }
    menu.append(sep, browse)
  }

  async function saveSelectedFolder(path, pathCombo) {
    path = String(path || '').trim()
    if (!path) return
    try {
      const res = await api('/actions/create_download_path', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: { path } }) })
      const data = await res.json().catch(() => ({}))
      if (!res.ok || data.ok === false) throw new Error(data.detail || data.message || '保存失败')
      toast('success', '已保存为迅雷历史下载路径')
    } catch (e) {
      toast('error', e.message || '保存失败')
    } finally {
      await loadDownloadOptions(pathCombo, path)
      pathCombo.__setPath(path, path.split('/').filter(Boolean).pop() || '路径')
    }
  }

  async function loadDownloadOptions(pathCombo, preferredPath = '') {
    const menu = pathCombo.__menu
    try {
      const res = await api('/actions/download_options', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: {} }) })
      const data = await res.json().catch(() => ({}))
      const paths = Array.isArray(data.paths) ? data.paths.filter(p => p && p.path) : []
      menu.innerHTML = ''
      if (!paths.length) {
        const empty = document.createElement('div')
        empty.className = 'xunlei-path-combo__empty'
        empty.textContent = data.warning ? '路径读取失败' : '未读取到历史路径'
        menu.appendChild(empty)
        appendBrowseItem(menu, pathCombo)
        return
      }
      for (const p of paths) {
        const item = document.createElement('button')
        item.type = 'button'
        item.className = 'xunlei-path-combo__item'
        item.innerHTML = `<span class="xunlei-remote-path-combo__item-badge">${esc(p.name || p.path)}</span><small>${esc(p.path)}</small>`
        item.onclick = () => { pathCombo.__setPath(p.path, p.name || p.path); menu.style.display = 'none'; pathCombo.__input.focus() }
        menu.appendChild(item)
      }
      appendBrowseItem(menu, pathCombo)
      const preferred = preferredPath || data.default_savepath || pathCombo.dataset.currentPath || paths[0].path
      const hit = paths.find(p => p.path === preferred) || paths[0]
      pathCombo.__setPath(hit.path, hit.name || hit.path)
    } catch (e) {
      menu.innerHTML = '<div class="xunlei-path-combo__empty">路径读取失败</div>'
      appendBrowseItem(menu, pathCombo)
    }
  }


  function currentItems() {
    const q = state.query.trim().toLowerCase()
    return state.tasks.filter(t => {
      if (q && !`${t.name} ${t.savepath} ${t.url}`.toLowerCase().includes(q)) return false
      if (state.filter === 'all') return true
      if (state.filter === 'active') return ['PHASE_TYPE_PENDING', 'PHASE_TYPE_RUNNING'].includes(t.phase)
      if (state.filter === 'done') return t.phase === 'PHASE_TYPE_COMPLETE'
      if (state.filter === 'running') return ['PHASE_TYPE_PENDING', 'PHASE_TYPE_RUNNING'].includes(t.phase)
      if (state.filter === 'paused') return t.phase === 'PHASE_TYPE_PAUSED'
      if (state.filter === 'error') return t.phase === 'PHASE_TYPE_ERROR'
      return true
    })
  }

  function renderTabs(force = false) {
    const tabs = [
      { key: 'active', label: '进行中' },
      { key: 'all', label: '全部' },
      { key: 'done', label: '已完成' },
      { key: 'paused', label: '暂停' },
      { key: 'error', label: '错误' },
    ]
    const key = state.filter
    if (!force && state.renderedTabsKey === key && tabsHost.childNodes.length) return
    const oldDispose = tabsHost.__noorDispose
    if (typeof oldDispose === 'function') oldDispose()
    tabsHost.__noorDispose = null
    tabsHost.innerHTML = ''
    state.renderedTabsKey = key
    const onChange = next => {
      if (state.filter === next) return
      state.filter = next
      state.page = 1
      state.tableSignature = ''
      renderTabs(true)
      render()
    }
    if (sdk.ui?.tabs) {
      const ui = sdk.ui.tabs({ value: state.filter, tabs, onChange })
      tabsHost.__noorDispose = ui.dispose
      tabsHost.appendChild(ui)
      return
    }
    tabsHost.innerHTML = `<div class="xunlei-remote-tabs">${tabs.map(t => `<button class="${t.key === state.filter ? 'is-active' : ''}" data-tab="${t.key}">${t.label}</button>`).join('')}</div>`
    for (const b of tabsHost.querySelectorAll('[data-tab]')) b.onclick = () => onChange(b.dataset.tab)
  }

  function renderStats() {
    const active = state.tasks.filter(t => ['PHASE_TYPE_PENDING', 'PHASE_TYPE_RUNNING'].includes(t.phase)).length
    const done = state.tasks.filter(t => t.phase === 'PHASE_TYPE_COMPLETE').length
    const speed = state.tasks.reduce((s, t) => s + Number(t.speed || 0), 0)
    statTotal.textContent = String(state.tasks.length)
    statActive.textContent = String(active)
    statDone.textContent = String(done)
    statSpeed.textContent = fmtSpeed(speed)
  }

  function visibleItems() {
    const items = currentItems()
    const pages = Math.max(1, Math.ceil(items.length / state.pageSize))
    if (state.page > pages) state.page = pages
    if (state.page < 1) state.page = 1
    return { items, pages, visible: items.slice((state.page - 1) * state.pageSize, state.page * state.pageSize) }
  }


  function rowActionForTask(t) {
    if (!t || !t.id) return null
    if (t.phase === 'PHASE_TYPE_ERROR') return { action: 'retry_task', label: '重试', icon: '↻', tone: 'warning' }
    if (t.phase === 'PHASE_TYPE_PAUSED') return { action: 'resume_task', label: '开始', icon: '▶', tone: 'default' }
    if (t.phase === 'PHASE_TYPE_RUNNING' || t.phase === 'PHASE_TYPE_PENDING') return { action: 'pause_task', label: '暂停', icon: 'Ⅱ', tone: 'default' }
    return null
  }

  function actionHost(meta, id, extraClass = '') {
    if (!meta) return `<span class="xunlei-remote-action-slot ${extraClass}" aria-hidden="true"></span>`
    return `<span class="xunlei-remote-action-slot ${extraClass}" data-act-host="${esc(meta.action)}" data-id="${esc(id)}" data-label="${esc(meta.label)}" data-icon="${esc(meta.icon)}" data-tone="${esc(meta.tone)}"></span>`
  }

  function rowActionsHtml(t) {
    const primary = rowActionForTask(t)
    const del = { action: 'delete_tasks', label: '删除', icon: '×', tone: 'danger' }
    return `${actionHost(primary, t.id, 'xunlei-remote-action-slot--primary')}${actionHost(del, t.id, 'xunlei-remote-action-slot--delete')}`
  }

  function rowHtml(t) {
    return `<tr data-id="${esc(t.id)}"><td><div class="xunlei-remote-name">${esc(t.name)}</div><div class="xunlei-remote-sub">${esc(t.savepath || t.url || '')}</div></td><td data-cell="phase"><span class="xunlei-remote-status xunlei-remote-status--${phaseTone(t.phase)}">${phaseText(t.phase)}</span></td><td data-cell="progress"><div class="xunlei-remote-progress"><i style="width:${Math.max(0, Math.min(100, Number(t.progress || 0) * 100))}%"></i></div><small>${pct(t.progress)}</small></td><td data-cell="speed">${fmtSpeed(t.speed)}</td><td>${esc(t.size_formatted || fmtBytes(t.size))}</td><td>${fmtTime(t.created_time)}</td><td class="xunlei-remote-right"><div class="xunlei-remote-row-actions">${t.phase === 'PHASE_TYPE_PAUSED' ? `<button data-act="resume_task" data-id="${esc(t.id)}">▶</button>` : `<button data-act="pause_task" data-id="${esc(t.id)}">⏸</button>`}<button class="is-danger" data-act="delete_tasks" data-id="${esc(t.id)}">删</button></div></td></tr>`
  }

  function bindRowActions(root = tbody) {
    for (const b of root.querySelectorAll('[data-act]')) b.onclick = e => { e.stopPropagation(); operate(b.dataset.act, b.dataset.id) }
  }

  function patchRows(visible) {
    for (const t of visible) {
      const row = tbody.querySelector(`tr[data-id="${CSS.escape(String(t.id))}"]`)
      if (!row) return false
      const phase = row.querySelector('[data-cell="phase"]')
      const progress = row.querySelector('[data-cell="progress"]')
      const speed = row.querySelector('[data-cell="speed"]')
      if (phase) phase.innerHTML = `<span class="xunlei-remote-status xunlei-remote-status--${phaseTone(t.phase)}">${phaseText(t.phase)}</span>`
      if (progress) progress.innerHTML = `<div class="xunlei-remote-progress"><i style="width:${Math.max(0, Math.min(100, Number(t.progress || 0) * 100))}%"></i></div><small>${pct(t.progress)}</small>`
      if (speed) speed.textContent = fmtSpeed(t.speed)
      const actions = row.querySelector('.xunlei-remote-row-actions')
      if (actions) {
        actions.innerHTML = `${t.phase === 'PHASE_TYPE_PAUSED' ? `<button data-act="resume_task" data-id="${esc(t.id)}">▶</button>` : `<button data-act="pause_task" data-id="${esc(t.id)}">⏸</button>`}<button class="is-danger" data-act="delete_tasks" data-id="${esc(t.id)}">删</button>`
        bindRowActions(actions)
      }
    }
    return true
  }

  function renderPager(pages) {
    pager.innerHTML = ''
    if (sdk.ui?.pagination) {
      const ui = sdk.ui.pagination({ page: state.page, totalPages: pages, onChange: p => { state.page = p; state.tableSignature = ''; renderTable() } })
      ui.style.justifyContent = 'center'
      pager.appendChild(ui)
    } else {
      pager.innerHTML = `<button ${state.page <= 1 ? 'disabled' : ''}>上一页</button><span>${state.page}/${pages}</span><button ${state.page >= pages ? 'disabled' : ''}>下一页</button>`
    }
  }

  function renderTable() {
    const { visible, pages } = visibleItems()
    const signature = `${state.filter}|${state.query}|${state.page}|${pages}|${visible.map(t => t.id).join(',')}`
    if (signature === state.tableSignature && patchRows(visible)) return
    state.tableSignature = signature
    tbody.innerHTML = visible.map(rowHtml).join('')
    bindRowActions()
    renderPager(pages)
  }

  function render() {
    if (state.destroyed) return
    renderTabs()
    renderStats()
    const limitMatch = String(state.dailyLimit?.title || '').match(/今日(\d+)个/)
    const limitTotal = Number(limitMatch?.[1] || 3)
    const today = dayKey(new Date())
    const limitUsed = state.tasks.filter(task => dayKey(task.created_time) === today).length
    const speedUsed = Number(state.trySpeed?.usage_used || 0)
    const speedTotal = Number(state.trySpeed?.usage_total || 0)
    usageBadge.textContent = `任务额度 ${limitUsed}/${limitTotal} · 加速 ${speedUsed}/${speedTotal || '—'}`
    usageBadge.title = state.dailyLimit?.title || `试用加速剩余 ${Number(state.trySpeed?.usage_remaining || 0)} 次`
    const connected = !!state.device?.user?.name
    device.textContent = connected ? `已连接 · ${state.device.user.name}` : '未连接'
    device.classList.toggle('xunlei-remote-badge--success', connected)
    refresh.disabled = state.loading
    refresh.textContent = state.loading ? '刷新中' : '刷新'
    stateBox.style.display = state.error || (!state.loading && !currentItems().length) ? 'flex' : 'none'
    stateBox.className = 'xunlei-remote-state' + (state.error ? ' is-error' : '')
    stateBox.textContent = state.error || '暂无任务'
    renderTable()
  }

  async function load(options = {}) {
    const silent = !!options.silent
    if (!silent) {
      state.loading = true
      state.error = ''
      render()
    }
    try {
      const [dev, tasks, about, cfg] = await Promise.all([
        api('/actions/device_info', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: {} }) }).then(r => r.json()),
        api('/actions/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: { phase: 'all', limit: 200 } }) }).then(r => r.json()),
        api('/actions/about', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: {} }) }).then(r => r.json()).catch(() => null),
        api('/actions/device_config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: {} }) }).then(r => r.json()).catch(() => null),
      ])
      if (dev.ok === false) throw new Error(dev.detail || dev.message || '设备连接失败')
      if (tasks.ok === false) throw new Error(tasks.detail || tasks.message || '任务读取失败')
      state.error = ''
      state.device = dev.info || dev
      state.dailyLimit = dev.task_daily_limit || null
      state.trySpeed = dev.try_speed || null
      state.tasks = Array.isArray(tasks.tasks) ? tasks.tasks : []
      state.about = about?.about || null
      state.config = cfg?.config || null
    } catch (e) {
      if (!silent) state.error = e.message || '加载失败'
    } finally {
      state.loading = false
      render()
    }
  }

  async function operate(action, id) {
    if (!id || state.actioning.has(id)) return
    state.actioning.add(id)
    try {
      const ok = action === 'delete_tasks'
        ? await (sdk.ui?.confirm ? sdk.ui.confirm({ title: '删除任务', message: '只删除迅雷任务，不删除已下载文件。', confirmText: '删除', danger: true }) : Promise.resolve(confirm('删除任务？')))
        : true
      if (!ok) return
      const r = await api(`/actions/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: { id, ids: [id] } }) })
      const data = await r.json().catch(() => ({}))
      if (!r.ok || data.ok === false) throw new Error(data.detail || data.message || '操作失败')
      toast('success', '操作已提交')
      state.tableSignature = ''
      await load({ silent: true })
    } catch (e) {
      toast('error', e.message || '操作失败')
    } finally {
      state.actioning.delete(id)
    }
  }

  refresh.onclick = () => load()
  newTask.onclick = openNewTaskModal
  residual.onclick = openResidualModal
  search.oninput = e => { state.query = e.target.value; state.page = 1; state.tableSignature = ''; render() }
  await load()
  state.timer = setInterval(() => { if (!state.destroyed && !state.loading) load({ silent: true }) }, 10000)
  return () => {
    state.destroyed = true
    if (state.timer) clearInterval(state.timer)
    const oldDispose = tabsHost.__noorDispose
    if (typeof oldDispose === 'function') oldDispose()
    el.innerHTML = ''
  }
}
