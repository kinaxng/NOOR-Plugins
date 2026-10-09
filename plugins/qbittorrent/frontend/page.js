export async function mount(el, sdk = {}) {
  const pluginId = sdk.pluginId || 'qbittorrent'
  const api = (path, init) => sdk.api?.plugin ? sdk.api.plugin(path, init) : fetch(`/api/plugins/${pluginId}${path}`, init)
  const toast = (type, msg) => sdk.toast?.[type]?.(msg)
  const state = {
    loading: false,
    error: '',
    version: '',
    apiMode: '',
    authMode: '',
    transfer: {},
    showNoorOnly: false,
    noorTag: 'noor',
    minFileSizeMb: 0,
    categories: {},
    downloadOptions: null,
    settingsOpen: false,
    categoryEditor: { open: false, mode: 'create', original: '', name: '', savePath: '', deleting: '' },
    torrents: [],
    filter: 'all',
    query: '',
    searchOpen: false,
    sortKey: 'added_on',
    sortDir: 'desc',
    selected: '',
    detail: null,
    detailOpen: false,
    confirmDeleteHash: '',
    actioning: new Set(),
    filteringNoor: false,
    destroyed: false,
    refreshTimer: null,
    ws: null,
    wsFallback: false,
    liveMode: 'connecting',
    page: 1,
    pageSize: 10,
    renderedFilterTab: '',
    tableSignature: '',
    submitModal: null,
    submitStatus: 'idle',
    submitProgress: 0,
  }

  el.innerHTML = `
    <div class="qb-page">
      <div class="noor-plugin-topbar qb-toolbar">
        <div data-role="filterTabs" class="noor-plugin-topbar__tabs"></div>
        <div class="noor-plugin-topbar__actions qb-actions">
          <span data-role="smallFilterHost"></span>
          <span data-role="connection" class="qb-connection"><i></i><span>未连接</span></span>
          <span data-role="newTaskHost"></span>
          <span data-role="settingsHost"></span>
          <span data-role="refreshHost"></span>
        </div>
        <span data-role="version" hidden></span>
        <span data-role="scope" hidden></span>
      </div>
      <div class="qb-overview-row">
        <div class="qb-summary" data-role="summary"></div>
        <div class="qb-task-tools">
          <div data-role="searchToggle" class="qb-search-card qb-stat" title="点击搜索任务名称 / 分类 / 保存路径">
            <span>搜索</span>
            <strong data-role="searchLabel">任务</strong>
            <input data-role="search" class="qb-search-inline" placeholder="输入关键词" style="display:none">
            <span data-role="clearSearchHost"></span>
          </div>
        </div>
      </div>
      <div data-role="state" class="qb-state" style="display:none"></div>
      <div class="qb-table-card">
        <div class="qb-table-wrap">
          <table class="qb-table">
            <thead><tr>
              <th><button data-sort="name">名称 <span></span></button></th>
              <th><button data-sort="state">状态 <span></span></button></th>
              <th><button data-sort="progress">进度 <span></span></button></th>
              <th><button data-sort="speed">速度 <span></span></button></th>
              <th><button data-sort="size">大小 <span></span></button></th>
              <th><button data-sort="ratio">分享率 <span></span></button></th>
              <th><button data-sort="added_on">时间 <span></span></button></th>
              <th class="qb-right">操作</th>
            </tr></thead>
            <tbody data-role="tbody"></tbody>
          </table>
        </div>
      </div>
      <div data-role="detail" class="qb-detail" style="display:none"></div>
      <div data-role="pager" class="qb-pager"></div>
      <div data-role="confirm" class="qb-modal-root"></div>
    </div>
  `

  const $ = role => el.querySelector(`[data-role="${role}"]`)
  let tbody = $('tbody'), stateBox = $('state'), summary = $('summary'), version = $('version'), scope = $('scope'),
    smallFilterHost = $('smallFilterHost'), newTaskHost = $('newTaskHost'), settingsHost = $('settingsHost'), refreshHost = $('refreshHost'),
    connection = $('connection'), smallFilter = null, newTask = null, settings = null, refresh = null,
    search = $('search'), searchToggle = $('searchToggle'), searchLabel = $('searchLabel'), clearSearchHost = $('clearSearchHost'), clearSearch = null,
    detail = $('detail'), pager = $('pager'), confirmRoot = $('confirm'), filterTabs = $('filterTabs')
  const sortButtons = Array.from(el.querySelectorAll('[data-sort]'))
  const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]))
  const fmtBytes = n => { n = Number(n || 0); if (!n) return '0 B'; const u = ['B','KB','MB','GB','TB']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++} return `${n.toFixed(i>=3?2:1)} ${u[i]}` }
  const fmtSpeed = n => Number(n || 0) ? `${fmtBytes(n)}/s` : '0 B/s'
  const fmtEta = n => { n = Number(n || 0); if (n < 0 || n >= 8640000) return '∞'; const h=Math.floor(n/3600), m=Math.floor((n%3600)/60); return h ? `${h}h ${m}m` : `${m}m` }
  const fmtTime = n => { n = Number(n || 0); if (!n) return '-'; const d = new Date(n * 1000); return `${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}` }
  const pct = n => `${Math.round(Number(n || 0) * 1000) / 10}%`
  const stateText = s => ({ downloading:'下载中', stalledDL:'等待下载', uploading:'做种', stalledUP:'等待做种', pausedDL:'暂停', pausedUP:'暂停', stoppedDL:'停止', stoppedUP:'停止', error:'错误', missingFiles:'文件缺失', queuedDL:'排队', queuedUP:'排队', checkingDL:'校验中', checkingUP:'校验中', checkingResumeData:'校验中', metaDL:'获取元数据', forcedDL:'强制下载', forcedUP:'强制做种', moving:'移动中' }[s] || s || '-')
  const stateTone = s => /error|missing/i.test(s) ? 'error' : /paused|stopped/i.test(s) ? 'muted' : /upload|stalledUP|forcedUP/i.test(s) ? 'success' : /check|queue|meta|moving/i.test(s) ? 'warning' : 'info'
  const priorityText = p => Number(p || 0) === 0 ? '已跳过' : Number(p || 0) >= 7 ? '最高' : Number(p || 0) >= 6 ? '高' : Number(p || 0) >= 1 ? '下载' : '默认'
  const hasTag = (torrent, tag) => String(torrent?.tags || '').split(',').map(x => x.trim().toLowerCase()).includes(String(tag || '').toLowerCase())

  function qbButton(label, className = '', onClick = null) {
    const b = document.createElement('button')
    b.type = 'button'
    b.className = ['qb-btn', className].filter(Boolean).join(' ')
    b.textContent = label
    if (onClick) b.onclick = onClick
    return b
  }

  function modalApi(options) {
    if (sdk.ui?.modal) {
      const modal = sdk.ui.modal({ ...options, closeOnMask: false })
      confirmRoot.appendChild(modal.el)
      return modal
    }
    const mask = document.createElement('div')
    mask.className = 'qb-modal-mask'
    const panel = document.createElement('div')
    panel.className = ['qb-modal', options.width ? `qb-modal--${options.width}` : ''].filter(Boolean).join(' ')
    const head = document.createElement('div')
    head.className = 'qb-modal-title'
    const h = document.createElement('h3')
    h.textContent = options.title || ''
    const closeBtn = qbButton('关闭', 'qb-icon-btn', close)
    head.append(h, closeBtn)
    const body = document.createElement('div')
    body.className = 'qb-modal-body'
    const children = Array.isArray(options.content) ? options.content : [options.content].filter(Boolean)
    children.forEach(node => body.appendChild(node))
    panel.append(head, body)
    if (Array.isArray(options.footer) && options.footer.length) {
      const actions = document.createElement('div')
      actions.className = 'qb-modal-actions'
      options.footer.forEach(node => actions.appendChild(node))
      panel.appendChild(actions)
    }
    mask.appendChild(panel)
    confirmRoot.appendChild(mask)
    function close() {
      mask.remove()
      options.onClose?.()
    }
    closeBtn.onclick = close
    mask.onclick = event => { if (event.target === mask) close() }
    return { el: mask, body, close }
  }

  function initUiKitTopbar() {
    const mkBtn = (label, cls, tone, title) => {
      if (sdk.ui?.button) return sdk.ui.button({ label, className: cls, tone, title })
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.className = cls
      btn.textContent = label
      if (title) btn.title = title
      return btn
    }
    settings = mkBtn('分类设置', 'qb-btn', 'default', '管理 qBittorrent 分类与保存路径')
    settings.dataset.role = 'settings'
    settingsHost.replaceWith(settings)
    newTask = mkBtn('新建任务', 'qb-btn qb-btn--primary', 'primary')
    newTask.dataset.role = 'newTask'
    newTaskHost.replaceWith(newTask)
    refresh = mkBtn('刷新', 'qb-btn', 'default')
    refresh.dataset.role = 'refresh'
    refreshHost.replaceWith(refresh)
    smallFilter = mkBtn('', 'qb-badge qb-badge-btn', 'default', '对 NOOR 推送到 qB 的任务跳过小于阈值的非字幕文件。点击可立即对当前任务执行一次过滤。')
    smallFilter.dataset.role = 'smallFilter'
    smallFilter.style.display = 'none'
    smallFilterHost.replaceWith(smallFilter)
    clearSearch = mkBtn('×', 'qb-search-clear', 'default', '清除搜索')
    clearSearch.dataset.role = 'clearSearch'
    clearSearch.style.display = 'none'
    clearSearchHost.replaceWith(clearSearch)
  }
  initUiKitTopbar()

  function renderFilterTabs(force = false) {
    const tabs = [
      { key: 'all', label: '全部' },
      { key: 'downloading', label: '下载中' },
      { key: 'seeding', label: '做种' },
      { key: 'completed', label: '已完成' },
      { key: 'paused', label: '暂停' },
      { key: 'errored', label: '错误' },
    ]
    if (!force && state.renderedFilterTab === state.filter && filterTabs.childNodes.length) return
    const oldDispose = filterTabs.__noorDispose
    if (typeof oldDispose === 'function') oldDispose()
    filterTabs.__noorDispose = null
    filterTabs.innerHTML = ''
    state.renderedFilterTab = state.filter
    const onChange = next => {
      if (state.filter === next) return
      state.filter = next
      state.page = 1
      state.tableSignature = ''
      renderFilterTabs(true)
      render()
    }
    if (sdk.ui?.tabs) {
      const ui = sdk.ui.tabs({ value: state.filter, tabs, onChange })
      filterTabs.__noorDispose = ui.dispose
      filterTabs.appendChild(ui)
      return
    }
    filterTabs.innerHTML = `<div class="qb-tabs">${tabs.map(t => `<button type="button" class="qb-tab ${t.key === state.filter ? 'is-active' : ''}" data-filter="${t.key}">${t.label}</button>`).join('')}</div>`
    for (const b of filterTabs.querySelectorAll('[data-filter]')) b.onclick = () => onChange(b.dataset.filter)
  }

  function matchesFilter(t) {
    const s = String(t.state || '')
    if (state.filter === 'all') return true
    if (state.filter === 'downloading') return ['downloading','stalledDL','queuedDL','forcedDL','metaDL'].includes(s)
    if (state.filter === 'seeding') return ['uploading','stalledUP','queuedUP','forcedUP'].includes(s)
    if (state.filter === 'completed') return Number(t.progress || 0) >= 1
    if (state.filter === 'paused') return /paused|stopped/i.test(s)
    if (state.filter === 'errored') return /error|missing/i.test(s)
    return true
  }

  function sortValue(t, key) {
    if (key === 'name') return String(t.name || '').toLowerCase()
    if (key === 'state') return stateText(t.state)
    if (key === 'progress') return Number(t.progress || 0)
    if (key === 'speed') return Number(t.dlspeed || 0) + Number(t.upspeed || 0)
    if (key === 'size') return Number(t.size || 0)
    if (key === 'ratio') return Number(t.ratio || 0)
    return Number(t.added_on || 0)
  }

  function currentItems() {
    const q = state.query.trim().toLowerCase()
    const items = state.torrents.filter(t => {
      if (state.showNoorOnly && state.noorTag && !hasTag(t, state.noorTag)) return false
      if (!matchesFilter(t)) return false
      if (!q) return true
      return `${t.name} ${t.category} ${t.save_path}`.toLowerCase().includes(q)
    })
    const dir = state.sortDir === 'asc' ? 1 : -1
    return items.sort((a,b) => {
      const av = sortValue(a, state.sortKey)
      const bv = sortValue(b, state.sortKey)
      if (typeof av === 'string' || typeof bv === 'string') return String(av).localeCompare(String(bv), 'zh-CN') * dir
      return ((av > bv) - (av < bv)) * dir
    })
  }

  function visibleItems() {
    const items = currentItems()
    const pages = Math.max(1, Math.ceil(items.length / state.pageSize))
    if (state.page > pages) state.page = pages
    if (state.page < 1) state.page = 1
    return { items, pages, visible: items.slice((state.page - 1) * state.pageSize, state.page * state.pageSize) }
  }

  function totalPages(count) {
    return Math.max(1, Math.ceil(count / state.pageSize))
  }

  function clampCurrentPage(count) {
    const pages = totalPages(count)
    if (state.page > pages) state.page = pages
    if (state.page < 1) state.page = 1
  }

  function visibleSignature(items) {
    return `${state.filter}|${state.query}|${state.page}|${items.map(t => t.hash).join(',')}`
  }

  function renderSummary() {
    const tr = state.transfer || {}
    const items = state.torrents
    const downloading = items.filter(t => ['downloading','stalledDL','queuedDL','forcedDL','metaDL'].includes(String(t.state || ''))).length
    const seeding = items.filter(t => ['uploading','stalledUP','queuedUP','forcedUP'].includes(String(t.state || ''))).length
    summary.innerHTML = `
      <div class="qb-stat"><span>任务</span><strong>${items.length}</strong></div>
      <div class="qb-stat"><span>下载中</span><strong>${downloading}</strong></div>
      <div class="qb-stat"><span>做种</span><strong>${seeding}</strong></div>
      <div class="qb-stat"><span>下载</span><strong>${fmtSpeed(tr.dl_info_speed)}</strong></div>
      <div class="qb-stat"><span>上传</span><strong>${fmtSpeed(tr.up_info_speed)}</strong></div>
    `
  }

  function renderChrome() {
    renderFilterTabs()
    for (const btn of sortButtons) {
      const active = btn.dataset.sort === state.sortKey
      btn.classList.toggle('is-active', active)
      btn.querySelector('span').textContent = active ? (state.sortDir === 'asc' ? '↑' : '↓') : '↕'
    }
    if (state.version) {
      version.textContent = [state.version, state.apiMode, state.authMode].filter(Boolean).join(' · ')
      connection.title = version.textContent
    } else connection.title = ''
    scope.textContent = state.showNoorOnly ? `范围：${state.noorTag}` : '范围：全部'
    if (Number(state.minFileSizeMb || 0) > 0) {
      smallFilter.style.display = 'inline-flex'
      smallFilter.disabled = state.filteringNoor
      smallFilter.textContent = state.filteringNoor ? '过滤中' : `过滤：${state.minFileSizeMb}MB`
    } else smallFilter.style.display = 'none'
    const connected = !state.error && String(state.transfer?.connection_status || '').toLowerCase() !== 'disconnected'
    connection.classList.toggle('is-online', connected)
    connection.classList.toggle('is-offline', !connected)
    connection.querySelector('span').textContent = connected ? '已连接' : '未连接'
    refresh.disabled = state.loading
    refresh.textContent = state.loading ? '刷新中' : '刷新'
    newTask.disabled = !!state.loading
    searchLabel.textContent = state.query ? '已筛选' : '任务'
    search.style.display = state.searchOpen ? 'block' : 'none'
    searchToggle.classList.toggle('is-search-open', state.searchOpen)
    clearSearch.style.display = state.query ? 'inline-flex' : 'none'
  }

  function renderState(items) {
    const show = state.loading || state.error || !items.length
    stateBox.style.display = show ? 'flex' : 'none'
    stateBox.className = 'qb-state' + (state.error ? ' is-error' : '')
    stateBox.innerHTML = state.loading ? '<span class="qb-spinner"></span><span>加载 qBittorrent...</span>' : state.error ? `<span>${esc(state.error)}</span>` : '<span>暂无符合条件的下载任务</span>'
  }

  function rowHtml(t) {
    return `<tr data-hash="${esc(t.hash)}" class="qb-row ${state.selected === t.hash ? 'is-selected' : ''}">
      <td><div class="qb-name">${esc(t.name)}</div><div class="qb-sub">${esc(t.category || '未分类')} · ${esc(t.save_path || '-')}</div></td>
      <td data-cell="state"><span class="qb-state-badge qb-state-badge--${stateTone(t.state)}">${esc(stateText(t.state))}</span></td>
      <td data-cell="progress"><div class="qb-progress"><span style="width:${Math.max(0, Math.min(100, Number(t.progress || 0) * 100))}%"></span></div><div class="qb-sub">${pct(t.progress)} · ETA ${fmtEta(t.eta)}</div></td>
      <td data-cell="speed"><div class="qb-speed">↓ ${fmtSpeed(t.dlspeed)}</div><div class="qb-sub">↑ ${fmtSpeed(t.upspeed)}</div></td>
      <td><div>${fmtBytes(t.size)}</div><div class="qb-sub">剩余 ${fmtBytes(t.amount_left)}</div></td>
      <td>${Number(t.ratio || 0).toFixed(2)}</td>
      <td><div class="qb-sub">${fmtTime(t.added_on)}</div></td>
      <td class="qb-right"><div class="qb-row-actions"><button data-act="resume">开始</button><button data-act="pause">暂停</button><button data-act="recheck">校验</button><button data-act="delete" class="is-danger">删除</button></div></td>
    </tr>`
  }

  function renderRows(visible) {
    tbody.innerHTML = visible.map(rowHtml).join('')
    bindRowActions()
  }

  function bindRowActions(root = tbody) {
    for (const tr of root.querySelectorAll('tr')) {
      tr.onclick = e => { if (e.target.closest('button')) return; openDetail(tr.dataset.hash) }
      for (const b of tr.querySelectorAll('[data-act]')) b.onclick = e => { e.stopPropagation(); runAction(b.dataset.act, tr.dataset.hash) }
    }
  }

  function patchRows(visible) {
    for (const t of visible) {
      const row = tbody.querySelector(`tr[data-hash="${CSS.escape(String(t.hash))}"]`)
      if (!row) return false
      const stateCell = row.querySelector('[data-cell="state"]')
      const progressCell = row.querySelector('[data-cell="progress"]')
      const speedCell = row.querySelector('[data-cell="speed"]')
      if (stateCell) stateCell.innerHTML = `<span class="qb-state-badge qb-state-badge--${stateTone(t.state)}">${esc(stateText(t.state))}</span>`
      if (progressCell) progressCell.innerHTML = `<div class="qb-progress"><span style="width:${Math.max(0, Math.min(100, Number(t.progress || 0) * 100))}%"></span></div><div class="qb-sub">${pct(t.progress)} · ETA ${fmtEta(t.eta)}</div>`
      if (speedCell) speedCell.innerHTML = `<div class="qb-speed">↓ ${fmtSpeed(t.dlspeed)}</div><div class="qb-sub">↑ ${fmtSpeed(t.upspeed)}</div>`
      const actions = row.querySelector('.qb-row-actions')
      if (actions) {
        actions.innerHTML = `<button data-act="resume">开始</button><button data-act="pause">暂停</button><button data-act="recheck">校验</button><button data-act="delete" class="is-danger">删除</button>`
        bindRowActions(row)
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
      pager.innerHTML = `<button type="button" ${state.page <= 1 ? 'disabled' : ''}>上一页</button><span>${state.page}/${pages}</span><button type="button" ${state.page >= pages ? 'disabled' : ''}>下一页</button>`
      const [prev, next] = pager.querySelectorAll('button')
      prev.onclick = () => { if (state.page > 1) { state.page--; state.tableSignature=''; renderTable() } }
      next.onclick = () => { if (state.page < pages) { state.page++; state.tableSignature=''; renderTable() } }
    }
  }

  function renderTable() {
    const { visible, pages } = visibleItems()
    const signature = `${state.filter}|${state.query}|${state.page}|${visible.map(t => t.hash).join(',')}`
    if (signature === state.tableSignature && patchRows(visible)) return
    state.tableSignature = signature
    renderRows(visible)
    renderPager(pages)
  }

  function renderDetail() {
    if (!state.detailOpen || !state.detail) { detail.style.display = 'none'; detail.innerHTML = ''; return }
    const d = state.detail
    const props = d.properties || {}
    const files = Array.isArray(d.files) ? d.files : []
    const skipped = files.filter(f => Number(f.priority || 0) === 0).length
    const downloading = files.length - skipped
    const visibleFiles = files.slice(0,120)
    const currentHash = state.selected || props.hash || ''
    detail.style.display = 'block'
    detail.innerHTML = `
      <div class="qb-detail-head">
        <div><h3>${esc(props.name || '任务详情')}</h3><p>${esc(props.save_path || '')}</p></div>
        <div class="qb-detail-actions">
          ${Number(state.minFileSizeMb || 0) > 0 ? `<button data-role="filterThis" class="qb-btn">过滤此任务小文件</button>` : ''}
          <button data-role="closeDetail" class="qb-btn">关闭</button>
        </div>
      </div>
      <div class="qb-detail-grid">
        <div>总大小 <strong>${fmtBytes(props.total_size)}</strong></div>
        <div>已下载 <strong>${fmtBytes(props.total_downloaded)}</strong></div>
        <div>分享率 <strong>${Number(props.share_ratio || 0).toFixed(2)}</strong></div>
        <div>文件 <strong>${downloading} 下载 / ${skipped} 跳过</strong>${Number(state.minFileSizeMb || 0) > 0 ? `<small>阈值 ${esc(state.minFileSizeMb)} MB，字幕保留</small>` : ''}</div>
      </div>
      <div class="qb-files">${visibleFiles.map(f=>{
        const p = Number(f.priority || 0)
        const isSkipped = p === 0
        return `<div class="qb-file ${isSkipped ? 'is-skipped' : ''}"><div><span>${esc(f.name)}</span><small>${pct(f.progress || 0)} · ${esc(priorityText(p))}</small></div><em>${fmtBytes(f.size)}</em></div>`
      }).join('') || '<div class="qb-file">暂无文件信息</div>'}</div>
      ${files.length > visibleFiles.length ? `<div class="qb-file-note">仅显示前 ${visibleFiles.length} 个文件，共 ${files.length} 个。</div>` : ''}
    `
    detail.querySelector('[data-role="closeDetail"]').onclick = () => { state.detailOpen = false; render() }
    const filterThis = detail.querySelector('[data-role="filterThis"]')
    if (filterThis) filterThis.onclick = () => applyFilter(currentHash)
  }

  function categoryList() {
    return Object.entries(state.categories || {}).map(([key, value]) => {
      const v = value && typeof value === 'object' ? value : {}
      return { name: String(v.name || key), savePath: String(v.savePath || v.save_path || '') }
    }).sort((a,b)=>a.name.localeCompare(b.name,'zh-CN'))
  }

  function categoryManagerHtml() {
    const items = categoryList()
    return `
      <div class="qb-category-toolbar"><button data-role="newCategory" class="qb-btn qb-btn--primary">新建分类</button></div>
      <div class="qb-category-list">
        ${items.length ? '<div class="qb-category-head"><span>分类</span><span>保存路径</span><span>操作</span></div>' : ''}
        ${items.map(c => {
          const hasPath = Boolean(c.savePath)
          return `<div class="qb-category-row"><div class="qb-category-name">${esc(c.name)}</div><div class="qb-category-path ${hasPath ? '' : 'is-empty'}">${esc(c.savePath || '未绑定路径')}</div><div class="qb-category-actions"><button data-act="edit" data-name="${esc(c.name)}" data-path="${esc(c.savePath)}">编辑</button><button data-act="delete" data-name="${esc(c.name)}" class="is-danger">删除</button></div></div>`
        }).join('') || '<div class="qb-state">暂无分类</div>'}
      </div>`
  }

  function bindCategoryManager(root) {
    root.querySelector('[data-role="newCategory"]')?.addEventListener('click', () => openCategoryEditor('create'))
    for (const b of root.querySelectorAll('[data-act="edit"]')) b.onclick = () => openCategoryEditor('edit', b.dataset.name || '', b.dataset.path || '')
    for (const b of root.querySelectorAll('[data-act="delete"]')) b.onclick = () => { state.categoryEditor.deleting = b.dataset.name || ''; render() }
  }

  function openCategoryEditor(mode, name = '', savePath = '') {
    state.categoryEditor = { open: true, mode, original: name, name, savePath, deleting: '' }
    render()
  }

  function renderCategoryEditor() {
    const ce = state.categoryEditor
    if (ce.deleting) {
      const content = document.createElement('div')
      content.innerHTML = `<p>只删除 qB 分类，不删除下载任务和文件。</p><div class="qb-modal-name">${esc(ce.deleting)}</div>`
      const close = () => { ce.deleting = ''; render() }
      renderModal({ title: '删除分类', content, footer: [qbButton('取消', '', close), qbButton('删除', 'qb-btn--danger', () => removeCategory(ce.deleting))], onClose: close })
      return true
    }
    if (!ce.open) return false
    const content = document.createElement('div')
    content.innerHTML = `<label class="qb-modal-field"><span>分类名称</span><input data-role="catName" class="qb-input" value="${esc(ce.name)}" ${ce.mode === 'edit' ? 'readonly' : ''}></label><label class="qb-modal-field"><span>保存路径</span><input data-role="catPath" class="qb-input" value="${esc(ce.savePath)}" placeholder="/downloads/av"></label>`
    const name = content.querySelector('[data-role="catName"]')
    const path = content.querySelector('[data-role="catPath"]')
    name.oninput = e => { ce.name = e.target.value }
    path.oninput = e => { ce.savePath = e.target.value }
    const close = () => { ce.open = false; render() }
    renderModal({ title: ce.mode === 'edit' ? '编辑分类路径' : '新建分类', content, footer: [qbButton('取消', '', close), qbButton('保存', 'qb-btn--primary', () => saveCategory())], onClose: close })
    return true
  }

  function renderSettings() {
    if (!state.settingsOpen) return false
    const content = document.createElement('div')
    content.innerHTML = `<div class="qb-setting-section"><h4>分类路径</h4>${categoryManagerHtml()}</div>`
    bindCategoryManager(content)
    const close = () => { state.settingsOpen = false; render() }
    renderModal({ title: 'qB 设置', content, onClose: close })
    return true
  }

  function renderModal(options) {
    confirmRoot.innerHTML = ''
    return modalApi(options)
  }

  function renderConfirm() {
    confirmRoot.innerHTML = ''
    if (renderCategoryEditor()) return
    if (renderSettings()) return
    if (!state.confirmDeleteHash) return
    const torrent = state.torrents.find(t => t.hash === state.confirmDeleteHash)
    const content = document.createElement('div')
    content.innerHTML = `<p>将从 qBittorrent 中移除任务，不删除已下载文件。</p><div class="qb-modal-name">${esc(torrent?.name || state.confirmDeleteHash)}</div>`
    const close = () => { state.confirmDeleteHash = ''; render() }
    renderModal({ title: '移除下载任务', content, footer: [qbButton('取消', '', close), qbButton('移除任务', 'qb-btn--danger', () => {
      const hash = state.confirmDeleteHash
      state.confirmDeleteHash = ''
      runAction('delete', hash, true)
    })], onClose: close })
  }

  function render() {
    if (state.destroyed) return
    renderSummary()
    renderChrome()
    const items = currentItems()
    clampCurrentPage(items.length)
    renderState(items)
    renderTable()
    renderDetail()
    renderConfirm()
  }

  async function call(action, payload = {}) {
    const r = await api(`/actions/${action}`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ payload }) })
    const data = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(data.detail || `${action} failed`)
    if (data?.ok === false) throw new Error(data.message || data.detail || `${action} failed`)
    return data
  }

  function applyOverviewData(data, options = {}) {
    const oldItems = currentItems()
    const oldVisible = visibleSignature(oldItems)
    const oldPages = totalPages(oldItems.length)
    state.version = data.version ? `qB ${data.version}` : ''
    state.apiMode = data.api_mode === 'start_stop' ? 'start/stop' : data.api_mode === 'pause_resume' ? 'pause/resume' : ''
    state.authMode = data.auth_mode === 'api_key' ? 'API Key' : data.auth_mode === 'cookie' ? 'Cookie' : ''
    state.transfer = data.transfer || {}
    state.showNoorOnly = !!data.show_noor_only
    state.noorTag = data.noor_tag || 'noor'
    state.minFileSizeMb = data.min_file_size_mb || 0
    state.categories = data.categories || {}
    state.torrents = data.torrents || []
    state.error = ''
    if (options.live) {
      const items = currentItems()
      clampCurrentPage(items.length)
      const sameVisible = oldVisible === visibleSignature(items)
      const samePages = oldPages === totalPages(items.length)
      renderSummary(); renderChrome(); renderState(items)
      if (sameVisible && samePages && !state.detailOpen) {
        if (patchRows(items.slice((state.page - 1) * state.pageSize, state.page * state.pageSize))) return
      }
      renderTable()
    }
  }

  async function load(options = {}) {
    const silent = !!options.silent
    if (!silent) { state.loading = true; state.error = ''; render() }
    try {
      const data = await call('overview')
      applyOverviewData(data, { live: silent })
    } catch (e) {
      state.error = e.message || 'qBittorrent 加载失败'
      state.torrents = []
    } finally {
      state.loading = false
      if (!silent) render()
    }
  }

  function createSubmitButton(options) {
    if (sdk.ui?.submitButton) return sdk.ui.submitButton(options)
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'qb-btn qb-btn--primary qb-submit'
    btn.textContent = options.idleLabel || '提交'
    btn.onclick = options.onClick
    return btn
  }

  async function openNewTaskModal() {
    if (state.submitModal) state.submitModal.close()
    state.submitStatus = 'idle'
    state.submitProgress = 0
    const form = document.createElement('div')
    form.className = 'qb-task-form'
    try {
      state.downloadOptions = await call('download_options')
    } catch {}
    const defaultPath = state.downloadOptions?.default_savepath || '/downloads/av'
    const defaultCategory = state.downloadOptions?.default_category || ''
    const urlInput = sdk.ui?.textarea
      ? sdk.ui.textarea({ placeholder: '每行一个 magnet / BT URL / 普通 URL，最多 50 条', rows: 6, className: 'qb-textarea' })
      : (() => {
          const input = document.createElement('textarea')
          input.className = 'noor-plugin-input qb-textarea'
          input.rows = 6
          input.placeholder = '每行一个 magnet / BT URL / 普通 URL，最多 50 条'
          return input
        })()
    const pathInput = document.createElement('input')
    pathInput.className = 'noor-plugin-input qb-input'
    pathInput.value = defaultPath
    const renameInput = document.createElement('input')
    renameInput.className = 'noor-plugin-input qb-input'
    renameInput.placeholder = '可选，重命名任务'
    const categorySelect = document.createElement('select')
    categorySelect.className = 'noor-plugin-input qb-select'
    const categories = Array.isArray(state.downloadOptions?.categories) ? state.downloadOptions.categories : categoryList()
    categorySelect.innerHTML = '<option value="">默认分类</option>' + categories.map(c => {
      const name = String(c && typeof c === 'object' ? c.name || c.save_path || '' : c || '')
      return `<option value="${esc(name)}" ${name === defaultCategory ? 'selected' : ''}>${esc(name)}</option>`
    }).join('')
    const fields = [
      sdk.ui?.field ? sdk.ui.field({ label: '下载链接', hint: '支持批量添加：一行一条，最多 50 条。', control: urlInput }) : urlInput,
      sdk.ui?.field ? sdk.ui.field({ label: '保存路径', control: pathInput }) : pathInput,
      sdk.ui?.field ? sdk.ui.field({ label: '分类', control: categorySelect }) : categorySelect,
      sdk.ui?.field ? sdk.ui.field({ label: '任务名称', hint: '可选，用于自动过滤和识别新任务。', control: renameInput }) : renameInput,
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
        className: 'qb-submit',
        onClick: submitTask,
      })
      if (!sdk.ui?.submitButton) next.onclick = submitTask
      submitBtn?.replaceWith(next)
      submitBtn = next
    }
    const cancelBtn = sdk.ui?.button ? sdk.ui.button({ label: '取消', onClick: () => state.submitModal?.close() }) : qbButton('取消', '', () => state.submitModal?.close())
    async function submitTask() {
      const urls = String(urlInput.value || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean).slice(0, 50)
      if (!urls.length) { toast('error', '请填写下载链接'); urlInput.focus?.(); return }
      if (String(urlInput.value || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean).length > 50) {
        toast('error', '单次最多添加 50 条链接')
        return
      }
      state.submitStatus = 'running'
      state.submitProgress = 2
      setSubmitButton()
      try {
        const payload = {
          urls: urls.join('\n'),
          savepath: String(pathInput.value || '').trim(),
          category: String(categorySelect.value || '').trim(),
          rename: String(renameInput.value || '').trim(),
          tag: state.noorTag,
        }
        const r = await api('/downloads', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ payload }) })
        const data = await r.json().catch(() => ({}))
        if (!r.ok || data.ok === false) throw new Error(data.detail || data.message || '推送失败')
        state.submitProgress = 100
        state.submitStatus = 'success'
        setSubmitButton()
        toast('success', data.message || '任务已推送')
        await load()
        state.submitModal?.close()
      } catch (e) {
        state.submitStatus = 'error'
        setSubmitButton()
        toast('error', e.message || '推送失败')
      }
    }
    setSubmitButton()
    state.submitModal = renderModal({ title: '新建 qBittorrent 任务', content: form, footer: [cancelBtn, submitBtn], onClose: () => { state.submitModal = null } })
  }

  async function saveCategory() {
    const ce = state.categoryEditor
    const name = ce.name.trim()
    if (!name) { toast('error', '分类名称不能为空'); return }
    try {
      await call(ce.mode === 'edit' ? 'edit_category' : 'create_category', { name, save_path: ce.savePath.trim() })
      toast('success', '分类已保存')
      ce.open = false
      state.settingsOpen = true
      await load()
    } catch (e) {
      toast('error', e.message || '分类保存失败')
    } finally { render() }
  }

  async function removeCategory(name) {
    try {
      await call('remove_categories', { categories: [name] })
      toast('success', '分类已删除')
      state.categoryEditor.deleting = ''
      state.settingsOpen = true
      await load()
    } catch (e) {
      toast('error', e.message || '分类删除失败')
    } finally { render() }
  }

  async function runAction(action, hash, confirmed = false) {
    if (state.actioning.has(`${action}:${hash}`)) return
    if (action === 'delete' && !confirmed) { state.confirmDeleteHash = hash; render(); return }
    state.actioning.add(`${action}:${hash}`)
    try {
      await call(action, { hash, deleteFiles: false })
      toast('success', '操作已提交')
      await load()
    } catch (e) {
      toast('error', e.message || '操作失败')
    } finally {
      state.actioning.delete(`${action}:${hash}`)
    }
  }

  async function openDetail(hash) {
    state.selected = hash; state.detailOpen = true; render()
    try {
      state.detail = await call('properties', { hash })
    } catch (e) {
      toast('error', e.message || '详情加载失败')
      state.detailOpen = false
    } finally { render() }
  }

  async function applyFilter(hash = '') {
    if (state.filteringNoor) return
    state.filteringNoor = true; render()
    try {
      const data = await call('apply_noor_filter', hash ? { hash } : {})
      toast('success', `过滤完成：已跳过 ${data.changed || 0} 个小文件`)
      if (hash) await openDetail(hash)
      else await load()
    } catch (e) {
      toast('error', e.message || '过滤失败')
    } finally {
      state.filteringNoor = false; render()
    }
  }

  function startFallbackLiveUpdates() {
    state.wsFallback = true
    state.liveMode = 'polling'
    if (state.refreshTimer) return
    state.refreshTimer = setInterval(() => {
      if (!state.destroyed && !state.settingsOpen && !state.categoryEditor.open && !state.categoryEditor.deleting && !state.submitModal) load({ silent: true })
    }, 8000)
  }

  function startLiveUpdates() {
    if (state.destroyed || state.wsFallback || !sdk.net?.webSocket) {
      startFallbackLiveUpdates()
      return
    }
    try {
      const ws = sdk.net.webSocket('/ws/overview?interval=4')
      ws.onopen = () => { state.liveMode = 'live' }
      ws.onmessage = event => {
        if (state.destroyed) return
        try {
          const data = JSON.parse(event.data || 'null')
          if (!data || data.ok === false) throw new Error(data?.error || data?.message || 'live update failed')
          applyOverviewData(data, { live: true })
        } catch (e) {
          state.wsFallback = true
          try { ws.close() } catch {}
          startFallbackLiveUpdates()
        }
      }
      ws.onerror = () => {
        state.wsFallback = true
        try { ws.close() } catch {}
        startFallbackLiveUpdates()
      }
      ws.onclose = () => {
        if (!state.destroyed && state.liveMode === 'live') {
          state.wsFallback = true
          startFallbackLiveUpdates()
        }
      }
      state.ws = ws
    } catch {
      startFallbackLiveUpdates()
    }
  }

  refresh.onclick = () => load()
  newTask.onclick = () => openNewTaskModal()
  smallFilter.onclick = () => applyFilter()
  settings.onclick = () => { state.settingsOpen = true; render() }
  searchToggle.onclick = () => { state.searchOpen = !state.searchOpen; render(); if (state.searchOpen) search.focus?.() }
  clearSearch.onclick = () => { state.query = ''; state.page = 1; state.tableSignature = ''; render(); search.focus?.() }
  search.oninput = e => { state.query = e.target.value; state.page = 1; state.tableSignature = ''; render() }
  for (const btn of sortButtons) btn.onclick = () => {
    const key = btn.dataset.sort
    if (state.sortKey === key) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc'
    else { state.sortKey = key; state.sortDir = key === 'name' ? 'asc' : 'desc' }
    render()
  }
  await load()
  startLiveUpdates()
  return () => {
    state.destroyed = true
    if (state.refreshTimer) clearInterval(state.refreshTimer)
    if (state.ws) { try { state.ws.close() } catch {} }
    const oldDispose = filterTabs.__noorDispose
    if (typeof oldDispose === 'function') oldDispose()
    if (state.submitModal) state.submitModal.close()
    el.innerHTML = ''
  }
}
