export async function mount(el, sdk = {}) {
  const pluginId = sdk.pluginId || 'mteam-plugin'
  const pluginFetch = (path, init) => sdk.api?.plugin
    ? sdk.api.plugin(path, init)
    : fetch(`/api/plugins/${pluginId}${path}`, init)
  const state = {
    activeTab: 'rss',
    rssItems: [],
    albums: [],
    activeAlbumId: '',
    page: 1,
    pageSize: 12,
    loading: false,
    pushing: new Set(),
    addOpen: false,
    addValue: '',
    adding: false,
    destroyed: false,
  }

  el.innerHTML = `
    <div class="mteam-page">
      <div class="mteam-toolbar">
        <div><h1 class="mteam-title">M-Team</h1><div class="mteam-sub"><span data-role="summary">加载中</span><span data-role="cacheInfo"></span></div></div>
        <div class="mteam-actions"><button data-role="refresh" class="mteam-btn mteam-btn--primary">刷新</button></div>
      </div>
      <div class="mteam-tabs">
        <button data-tab="rss" class="mteam-tab is-active">RSS</button>
        <button data-tab="albums" class="mteam-tab">片单</button>
      </div>
      <div data-role="albumBar" class="mteam-album-bar" style="display:none"></div>
      <div data-role="empty" class="mteam-empty">加载中...</div>
      <div data-role="grid" class="mteam-grid"></div>
      <div data-role="pager" class="mteam-pager"></div>
      <div data-role="modalRoot"></div>
    </div>
  `

  const $ = role => el.querySelector(`[data-role="${role}"]`)
  const grid = $('grid'), empty = $('empty'), pager = $('pager'), summary = $('summary'), cacheInfo = $('cacheInfo'), refreshBtn = $('refresh'), albumBar = $('albumBar'), modalRoot = $('modalRoot')
  const tabButtons = Array.from(el.querySelectorAll('[data-tab]'))
  const fmtDate = v => { if (!v) return ''; const d = new Date(v); return Number.isNaN(d.getTime()) ? v : d.toLocaleString('zh-CN', { hour12: false }) }
  const fmtSize = n => { n = Number(n || 0); if (!n) return ''; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ } return `${n.toFixed(i >= 3 ? 2 : 1)} ${u[i]}` }
  const keyOf = it => String(it.guid || it.download_url || it.enclosure_url || it.link || it.title || '')
  const escapeHtml = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]))
  const notify = (type, msg) => (sdk.toast?.[type] || sdk.toast?.info || sdk.toast?.success)?.(msg)

  function currentItems() {
    if (state.activeTab === 'rss') return state.rssItems
    const album = state.albums.find(x => x.id === state.activeAlbumId) || state.albums[0]
    return album?.items || []
  }

  function currentAlbum() {
    return state.albums.find(x => x.id === state.activeAlbumId) || state.albums[0] || null
  }

  function estimate() {
    const w = grid.clientWidth || el.clientWidth || innerWidth
    const top = grid.getBoundingClientRect().top
    const h = Math.max(360, innerHeight - top - 76)
    const gap = 12
    const min = w >= 1800 ? 360 : w >= 1280 ? 330 : w >= 760 ? 300 : 240
    const cols = Math.max(1, Math.floor((w + gap) / (min + gap)))
    const rows = Math.max(1, Math.floor((h + gap) / (min * 9 / 16 + 90 + gap)))
    state.pageSize = Math.max(cols, cols * rows)
    if (state.page > pages()) state.page = pages()
  }

  function pages() { return Math.max(1, Math.ceil(currentItems().length / state.pageSize)) }
  function button(text, fn, disabled) { const b = document.createElement('button'); b.className = 'mteam-btn'; b.textContent = text; b.disabled = disabled; b.onclick = fn; return b }

  function renderTabs() {
    for (const btn of tabButtons) btn.classList.toggle('is-active', btn.dataset.tab === state.activeTab)
  }

  function renderAlbumBar() {
    albumBar.style.display = state.activeTab === 'albums' ? 'flex' : 'none'
    albumBar.innerHTML = ''
    if (state.activeTab !== 'albums') return
    for (const album of state.albums) {
      const b = document.createElement('button')
      b.className = 'mteam-badge' + (album.id === currentAlbum()?.id ? ' is-active' : '')
      b.textContent = `${album.title || `片单 ${album.id}`} · ${album.count ?? album.items?.length ?? 0}`
      b.onclick = () => { state.activeAlbumId = album.id; state.page = 1; render() }
      albumBar.appendChild(b)
    }
    const add = document.createElement('button')
    add.className = 'mteam-add'
    add.textContent = '+'
    add.title = '添加片单'
    add.onclick = () => { state.addOpen = true; state.addValue = ''; render() }
    albumBar.appendChild(add)
  }

  function renderModal() {
    modalRoot.innerHTML = ''
    if (!state.addOpen) return
    const input = document.createElement('input')
    input.className = 'mteam-input'
    input.placeholder = '粘贴 M-Team 片单地址或 albumId'
    input.value = state.addValue
    const cancel = document.createElement('button')
    cancel.className = 'mteam-btn'
    cancel.textContent = '取消'
    const submit = document.createElement('button')
    submit.className = 'mteam-btn mteam-btn--primary'
    submit.textContent = state.adding ? '添加中' : '添加'
    submit.disabled = state.adding
    input.oninput = e => { state.addValue = e.target.value }
    input.onkeydown = e => { if (e.key === 'Enter') addAlbum() }
    const closeModal = () => { if (!state.adding) { state.addOpen = false; render() } }
    cancel.onclick = closeModal
    submit.onclick = () => addAlbum()
    if (sdk.ui?.modal) {
      const modal = sdk.ui.modal({ title: '添加片单', content: input, footer: [cancel, submit], closeOnMask: false, onClose: closeModal })
      modalRoot.appendChild(modal.el)
    } else {
      modalRoot.innerHTML = `<div class="mteam-modal-mask"><div class="mteam-modal"><div class="mteam-modal-title">添加片单</div><div class="mteam-modal-fallback"></div><div class="mteam-modal-actions"></div></div></div>`
      modalRoot.querySelector('.mteam-modal-fallback').appendChild(input)
      const actions = modalRoot.querySelector('.mteam-modal-actions')
      actions.appendChild(cancel)
      actions.appendChild(submit)
    }
    input.focus()
  }

  function render() {
    if (state.destroyed) return
    estimate()
    renderTabs()
    renderAlbumBar()
    renderModal()
    const items = currentItems()
    const album = currentAlbum()
    if (state.activeTab === 'rss') summary.textContent = state.loading ? '加载中' : `${state.rssItems.length} 个作品`
    else summary.textContent = state.loading ? '加载中' : (album ? `${album.title} · ${items.length} 个作品` : '未添加片单')
    grid.innerHTML = ''
    const totalPages = pages()
    const start = (state.page - 1) * state.pageSize
    const visible = items.slice(start, start + state.pageSize)
    empty.style.display = state.loading || !items.length ? 'block' : 'none'
    empty.textContent = state.loading ? '加载中...' : (state.activeTab === 'albums' ? '暂无片单内容，点击 + 添加片单。' : '暂无内容')
    for (const it of visible) {
      const k = keyOf(it)
      const a = document.createElement('a')
      a.className = 'mteam-card'
      a.href = it.link || '#'
      a.target = '_blank'
      a.rel = 'noopener noreferrer'
      a.innerHTML = `<div class="mteam-cover">${it.image_url ? `<img src="${escapeHtml(it.image_url)}" loading="lazy" alt="cover">` : '<div class="mteam-placeholder">NO IMAGE</div>'}</div><div class="mteam-body"><div class="mteam-name">${escapeHtml(it.title || '-')}</div><div class="mteam-meta"><span>${escapeHtml(fmtDate(it.pubDate))}</span><span>${escapeHtml(fmtSize(it.size_bytes))}</span></div><div class="mteam-meta"><span>${escapeHtml(it.category || it.image_source || '')}</span>${(it.download_url || it.enclosure_url) ? `<button class="mteam-push" data-key="${escapeHtml(k)}">${state.pushing.has(k) ? '推送中' : '推送'}</button>` : ''}</div></div>`
      const btn = a.querySelector('.mteam-push')
      if (btn) { btn.disabled = state.pushing.has(k); btn.onclick = e => { e.preventDefault(); e.stopPropagation(); pushItem(it) } }
      grid.appendChild(a)
    }
    pager.innerHTML = ''
    if (totalPages > 1) {
      pager.appendChild(button('上一页', () => { state.page--; render() }, state.page <= 1))
      for (let i = 1; i <= totalPages; i++) {
        if (totalPages > 10 && Math.abs(i - state.page) > 2 && i !== 1 && i !== totalPages) continue
        const b = button(String(i), () => { state.page = i; render() }, false)
        if (i === state.page) b.classList.add('is-active')
        pager.appendChild(b)
      }
      pager.appendChild(button('下一页', () => { state.page++; render() }, state.page >= totalPages))
    }
  }

  async function loadRss(force = false) {
    state.loading = true
    render()
    try {
      const r = await pluginFetch(`/rss/items?limit=300${force ? '&refresh=true' : ''}`)
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || '加载失败')
      state.rssItems = Array.isArray(data) ? data : (data.items || [])
      cacheInfo.textContent = data.image_cache_days ? `图片缓存 ${data.image_cache_days} 天 · 已缓存 ${data.image_cached_count || 0} 张` : ''
      state.page = 1
    } catch (e) {
      empty.style.display = 'block'
      empty.textContent = e.message || '加载失败'
      state.rssItems = []
    } finally {
      state.loading = false
      render()
    }
  }

  async function loadAlbums() {
    state.loading = true
    render()
    try {
      const r = await pluginFetch(`/actions/albums`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: {} }) })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || '片单加载失败')
      state.albums = data.albums || []
      if (!state.activeAlbumId && state.albums[0]) state.activeAlbumId = state.albums[0].id
      state.page = 1
    } catch (e) {
      notify('error', e.message || '片单加载失败')
      state.albums = []
    } finally {
      state.loading = false
      render()
    }
  }

  async function addAlbum() {
    const value = state.addValue.trim()
    if (!value || state.adding) return
    state.adding = true
    render()
    try {
      const r = await pluginFetch(`/actions/add_album`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: { url: value } }) })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || '添加片单失败')
      const album = data.album
      if (album) {
        const idx = state.albums.findIndex(x => x.id === album.id)
        if (idx >= 0) state.albums.splice(idx, 1, album)
        else state.albums.push(album)
        state.activeAlbumId = album.id
      }
      state.addOpen = false
      notify('success', '片单已添加')
    } catch (e) {
      notify('error', e.message || '添加片单失败')
    } finally {
      state.adding = false
      render()
    }
  }

  async function pushItem(item) {
    const k = keyOf(item)
    if (state.pushing.has(k)) return
    state.pushing.add(k)
    render()
    try {
      const r = await pluginFetch(`/rss/push`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ item }) })
      const data = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(data.detail || '推送失败')
      notify('success', '已推送至下载器')
    } catch (e) {
      notify('error', e.message || '推送失败')
    } finally {
      state.pushing.delete(k)
      render()
    }
  }

  const onResize = () => render()
  refreshBtn.onclick = () => state.activeTab === 'rss' ? loadRss(true) : loadAlbums()
  for (const btn of tabButtons) btn.onclick = () => { state.activeTab = btn.dataset.tab; state.page = 1; state.activeTab === 'albums' && !state.albums.length ? loadAlbums() : render() }
  addEventListener('resize', onResize)
  await loadRss(false)

  return () => {
    state.destroyed = true
    removeEventListener('resize', onResize)
    el.innerHTML = ''
  }
}
