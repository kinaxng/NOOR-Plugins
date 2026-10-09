export async function mount(root, sdk) {
  const ui = sdk.ui
  const state = { authors: [], authorId: '', branch: [], listing: null, filter: '', request: 0, autoMatch: null, refreshArtwork: false }
  const images = new Map(), imageValues = new Map(), folders = new Map(), dialogs = new Set()
  let activeWorkMenu = null, activeWorkMenuOwner = null, pendingWorksRedraw = null
  let disposed = false
  let loadingLibrary = false, checkingCache = false, cacheWarningShown = false
  const cacheTokens = new Map()
  const listCacheKey = a => `noor:featured-list:${a.id}:${a.root_folder_id}`
  const cachedListing = a => {
    try { const data = JSON.parse(sessionStorage.getItem(listCacheKey(a)) || 'null'); return data ? { ...data, works: data.works.filter(w => Number(w.size) >= state.minVideoMb * 1048576) } : null } catch (_) { return null }
  }
  const readQueue = []
  let activeReads = 0
  const pumpReads = () => {
    while (activeReads < 2 && readQueue.length) {
      const task = readQueue.shift(); activeReads++
      task.run().then(task.resolve, task.reject).finally(() => { activeReads--; pumpReads() })
    }
  }
  const post = async (action, payload = {}) => {
    const run = () => sdk.api.post(`/plugins/featured-performers/actions/${action}`, { payload })
    try {
      const response = await (['image', 'library_image', 'library_title', 'library_folder_cover', 'source_image'].includes(action)
        ? new Promise((resolve, reject) => { readQueue.push({ run, resolve, reject }); pumpReads() }) : run())
      return response?.data ?? response
    } catch (error) {
      const detail = error.response?.data?.detail
      throw new Error(typeof detail === 'string' ? detail : error.message || '请求失败')
    }
  }
  const el = (tag, cls = '', text = '') => { const n = document.createElement(tag); n.className = cls; n.textContent = text; return n }
  const button = (label, onClick) => ui.button({ label, onClick })
  const input = (value = '', type = 'text') => ui.input({ value: value ?? '', type })
  const field = (label, control) => ui.field({ label, control })
  const author = () => state.authors.find(a => a.id === state.authorId)
  const message = (text, error = false) => { status.textContent = text; status.hidden = !text; status.dataset.error = String(error) }
  const replace = a => { state.authors = state.authors.map(old => old.id === a.id ? a : old) }
  const modal = (title, body, width = 'lg') => {
    const d = ui.modal({ title, content: body, width, closeOnMask: false, beforeClose: () => d.beforeClose?.() ?? true, onClose: () => { d.cleanup?.(); dialogs.delete(d) } })
    dialogs.add(d); return d
  }
  const footer = (dialog, controls) => {
    let n = dialog.el.querySelector('.noor-plugin-modal__actions')
    if (!n) { n = el('div', 'noor-plugin-modal__actions'); dialog.body.parentElement.append(n) }
    n.replaceChildren(...controls)
  }
  const editorFooter = (dialog, note, controls) => {
    note.classList.add('fp-editor-feedback')
    note.setAttribute('role', 'status')
    const layout = el('div', 'fp-editor-footer')
    layout.append(note, ui.actionRow({ children: controls })); footer(dialog, [layout])
  }
  const editorTabs = (parent, dialog, entries, onChange = () => {}) => {
    const panels = Object.fromEntries(entries.map(([key]) => [key, el('section', 'fp-editor-tab')]))
    const show = key => {
      for (const [id, panel] of Object.entries(panels)) panel.hidden = id !== key
      onChange(key)
    }
    const tabs = ui.tabs({ value: entries[0][0], tabs: entries.map(([key, label]) => ({ key, label })), onChange: show })
    parent.append(tabs, ...Object.values(panels)); show(entries[0][0])
    dialog.cleanup = () => tabs.dispose?.()
    return panels
  }
  const photo = (url, alt, cls = '') => { const img = el('img', cls); img.src = url; img.alt = alt; img.loading = 'lazy'; return img }
  const visible = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) { visible.unobserve(entry.target); entry.target.loadArtwork?.() }
  }, { rootMargin: '100px' })
  const whenVisible = (node, load) => { node.loadArtwork = load; visible.observe(node) }
  const cachedImage = (key, load) => {
    if (!images.has(key)) images.set(key, load().then(url => {
      if (url) imageValues.set(key, url); else imageValues.delete(key)
      return url
    }).catch(() => { images.delete(key); return imageValues.get(key) || '' }))
    return images.get(key)
  }
  const shell = el('div', 'fp-page'), status = el('div', 'fp-status'), main = el('main', 'fp-main')
  status.hidden = true; status.setAttribute('role', 'status'); shell.append(status, main); root.replaceChildren(shell)
  const closeWorkMenu = () => {
    activeWorkMenu?.remove(); activeWorkMenuOwner?.classList.remove('has-context-menu')
    activeWorkMenu = null; activeWorkMenuOwner = null
    const pending = pendingWorksRedraw; pendingWorksRedraw = null
    if (pending?.isConnected) queueMicrotask(() => drawWorks(pending))
  }
  shell.addEventListener('pointerdown', event => { if (!activeWorkMenu?.contains(event.target)) closeWorkMenu() })
  async function overview() {
    const result = await post('overview'); state.authors = result.authors || []; state.backendReady = result.library_index_version === 1; state.cleanupReady = result.cleanup_version >= 2; state.autoMatchReady = result.auto_match_version >= 2; state.layoutReady = result.emby_layout_version >= 1; state.minVideoMb = result.min_video_mb ?? 100; state.checkMinutes = result.check_minutes ?? 0; state.storage = result.storage || {}; state.cacheReady = result.persistent_cache_version === 1
    if (!state.backendReady) message('新版界面已就绪，后端尚待热重载。现有绑定与文件不受影响。')
  }
  async function artwork(a, role, extra = {}) {
    if (!a.root_folder_id) return ''
    const key = JSON.stringify([a.id, a.root_folder_id, role, extra])
    return cachedImage(key, () => post('image', { author_id: a.id, role, ...extra }).then(r => r.image?.data_url || ''))
  }
  function enter(a) {
    if (!state.backendReady) return message('后端尚未加载新版，请在热重载后刷新此页。')
    loadingLibrary = false; state.authorId = a.id; state.branch = []; state.filter = ''; state.listing = cachedListing(a); render(); loadFolder()
  }
  function render() {
    if (disposed) return
    visible.disconnect()
    main.replaceChildren()
    if (!author()) return renderAuthors()
    renderAuthor()
  }
  function renderAuthors() {
    const head = el('header', 'fp-toolbar'), title = el('div')
    title.append(el('h1', '', '精选女优'), el('p', 'fp-muted', '你的作者与作品集 · CD2'))
    const add = button('添加作者', createAuthor); add.disabled = !state.backendReady
    head.append(title, button('插件设置', storageSettings), button('作品筛选', librarySettings), add); main.append(head)
    const grid = el('div', 'fp-authors'); main.append(grid)
    for (const a of state.authors) {
      const wrap = el('div'); grid.append(wrap)
      const draw = (image = '', logoUrl = '') => {
        if (disposed) return
        wrap.replaceChildren(ui.mediaCard({ title: logoUrl ? '' : a.name, image, logoUrl, presentation: 'cinematic', sharp: true, placeholder: a.name,
          meta: a.root_folder_id ? '查看作品集' : '待绑定 CD2 目录', onClick: () => enter(a) }))
      }
      draw()
      whenVisible(wrap, () => Promise.all([artwork(a, 'fanart').then(url => url || artwork(a, 'poster')), artwork(a, 'clearlogo')]).then(([bg, logo]) => draw(bg, logo)))
    }
    if (!state.authors.length) main.append(el('p', 'fp-empty', '添加一位作者，绑定她在 CD2 中的文件夹，开始整理作品。'))
  }
  function renderAuthor() {
    const a = author(), hero = el('section', 'fp-hero'), nav = el('div', 'fp-hero-nav')
    nav.append(button('‹ 作者', () => { state.request++; state.authorId = ''; message(''); render() }), button('··· 作者设置', () => authorSettings(a)))
    const content = el('div', 'fp-hero-content'), brand = el('div', 'fp-brand'); content.append(brand)
    if (a.bio) content.append(el('p', 'fp-bio', a.bio))
    hero.append(nav, content); main.append(hero)
    artwork(a, 'fanart').then(url => { if (url && hero.isConnected) hero.prepend(photo(url, '', 'fp-hero-image')) })
    artwork(a, 'clearlogo').then(url => {
      if (!brand.isConnected) return
      brand.replaceChildren(url ? photo(url, a.name, 'fp-logo') : el('h1', '', a.name))
      if (!url && a.aliases) brand.after(el('p', 'fp-aliases', a.aliases))
    })
    if (!a.root_folder_id) { main.append(button('绑定 CD2 作者目录', () => browseAuthor(a))); return }
    const bar = el('div', 'fp-toolbar'), breadcrumbs = el('div', 'fp-breadcrumbs'), actions = el('div', 'fp-actions')
    breadcrumbs.append(button('全部作品', () => goBranch([])))
    state.branch.forEach((folder, index) => breadcrumbs.append(button(folder.name, () => goBranch(state.branch.slice(0, index + 1)))))
    const years = (a.years || []).filter(y => !y.virtual_unknown_year && y.folder_id)
    if (years.length) actions.append(ui.select({ value: state.branch[0]?.id || '', options: [{ value: '', label: '全部年份' }, ...years.map(y => ({ value: y.folder_id, label: y.display_title || y.title }))], onChange: value => { const y = years.find(y => y.folder_id === value); goBranch(y ? [{ id: y.folder_id, name: y.display_title || y.title }] : []) } }))
    actions.append(button('插件设置', storageSettings), button('作品筛选', librarySettings), button('刷新缓存', () => loadFolder(true)))
    const organize = button('整理 Emby', () => embyLayoutDialog(a)); organize.classList.add('fp-organize'); organize.disabled = !state.layoutReady || !state.listing?.complete
    organize.title = state.listing?.complete ? '预览作者、年份季与剧集结构' : '等待作品目录读取完成'
    actions.append(organize)
    const cleanup = button('清理垃圾', () => cleanupDialog(a, [...state.branch])); cleanup.disabled = !state.cleanupReady
    if (!state.cleanupReady) cleanup.title = '清理功能等待后端热重载'
    actions.append(cleanup)
    const group = years.find(y => y.folder_id === state.branch.at(-1)?.id)
    if (group) actions.append(button('年份设置', () => yearSettings(a, group)))
    bar.append(breadcrumbs, actions); main.append(bar)
    main.append(el('p', 'fp-muted fp-library-summary', librarySummaryText()))
    const search = input(state.filter, 'search'); search.placeholder = '筛选作品标题或文件名'; search.setAttribute('aria-label', search.placeholder)
    search.oninput = () => { state.filter = search.value; drawWorks(grid) }; main.append(search)
    const grid = el('div', 'fp-works'); main.append(grid); drawWorks(grid)
  }
  function librarySummaryText() {
    const auto = state.autoMatch?.counts || {}, autoText = state.autoMatchReady
      ? `自动整理：${state.autoMatch?.active || 0} 待处理 · ${auto.completed || 0} 已完成 · ${auto.manual || 0} 待手动${auto.failed ? ` · ${auto.failed} 异常重试` : ''}`
      : '自动整理等待后端加载'
    return `视频 ≥ ${state.minVideoMb} MB · 每个视频一部作品 · ${state.listing?.works?.length || 0} 部已读取 · ${autoText}`
  }
  function updateAuthorChrome() {
    const summary = main.querySelector('.fp-library-summary')
    if (summary) summary.textContent = librarySummaryText()
    const organize = main.querySelector('.fp-organize')
    if (organize) {
      organize.disabled = !state.layoutReady || !state.listing?.complete
      organize.title = state.listing?.complete ? '预览作者、年份季与剧集结构' : '等待作品目录读取完成'
    }
  }
  function drawWorks(grid) {
    if (activeWorkMenu?.isConnected) {
      pendingWorksRedraw = grid
      return
    }
    pendingWorksRedraw = null
    const next = document.createDocumentFragment()
    if (!state.listing) {
      next.append(el('p', 'fp-empty', '正在读取本地缓存…'))
      for (const n of grid.children) visible.unobserve(n)
      grid.replaceChildren(next)
      return
    }
    const a = author(), needle = state.filter.toLowerCase(), refreshArtwork = Boolean(state.refreshArtwork)
    for (const work of state.listing.works || []) {
      const branch = work.branch || []
      if (state.branch.some((part, i) => part.id !== branch[i]?.id)) continue
      if (!(work.title + work.name).toLowerCase().includes(needle)) continue
      const wrap = el('div', 'fp-work-card'); next.append(wrap)
      const key = JSON.stringify(['work', a.id, branch, work.file_id, work.name])
      if (!work.image_names?.length) imageValues.delete(key)
      if (refreshArtwork) images.delete(key)
      let cover = imageValues.get(key) || ''
      const metaText = () => `${work.nfo_error ? 'NFO 读取异常' : work.nfo_exists ? '已有 NFO' : '未创建 NFO'} · ${(Number(work.size || 0) / 1048576).toFixed(0)} MB`
      const card = ui.mediaCard({ title: work.title || work.name, image: cover, sharp: true, placeholder: '待设置封面', coverAspectRatio: '16/9', meta: metaText(), onClick: () => editWork(a, branch, work) })
      const coverNode = card.querySelector('.noor-plugin-media-card__cover')
      const applyCover = url => {
        cover = url || ''
        if (cover) {
          const img = photo(cover, work.title || work.name); img.loading = 'eager'
          coverNode.replaceChildren(img)
        } else if (!coverNode.querySelector('.noor-plugin-media-card__placeholder')) {
          coverNode.replaceChildren(el('div', 'noor-plugin-media-card__placeholder', '待设置封面'))
        }
      }
      const applyText = () => {
        const titleNode = card.querySelector('.noor-plugin-media-card__title')
        const metaNode = card.querySelector('.noor-plugin-media-card__meta span')
        if (titleNode) titleNode.textContent = work.title || work.name
        if (metaNode) metaNode.textContent = metaText()
        wrap.title = work.nfo_error || work.path || work.name
      }
      coverNode?.addEventListener('contextmenu', event => {
        event.preventDefault(); event.stopPropagation(); closeWorkMenu()
        const menu = el('div', 'fp-work-menu'), remove = button('删除作品', async () => {
          closeWorkMenu()
          const title = work.title || work.name
          if (!await ui.confirm({ title: '永久删除作品？', message: `将从 CD2 删除「${title}」的正片、同名 NFO 和封面。不会删除 Season 目录或其他作品。`, confirmText: '删除作品', danger: true })) return
          message(`正在从 CD2 删除「${title}」…`)
          try {
            const result = await post('delete_work', { author_id: a.id, work_id: work.work_id || '', file_id: work.file_id, parent_id: work.parent_id, name: work.name, folder_ids: branch.map(item => item.id), confirm: true })
            replace(result.author)
            state.listing.works = state.listing.works.filter(item => String(item.file_id) !== String(work.file_id))
            images.delete(key); imageValues.delete(key)
            try { sessionStorage.setItem(listCacheKey(a), JSON.stringify(state.listing)) } catch (_) { /* Server cache remains authoritative. */ }
            drawWorks(grid); updateAuthorChrome(); message(result.message)
          } catch (error) { message(error.message || '删除失败', true) }
        })
        remove.classList.add('fp-work-menu__delete'); menu.append(remove)
        const rect = wrap.getBoundingClientRect()
        menu.style.left = `${Math.max(8, Math.min(event.clientX - rect.left, rect.width - 126))}px`
        menu.style.top = `${Math.max(8, event.clientY - rect.top)}px`
        wrap.classList.add('has-context-menu'); wrap.append(menu)
        activeWorkMenu = menu; activeWorkMenuOwner = wrap; remove.focus({ preventScroll: true })
      })
      wrap.append(card); applyText()
      if (work.image_names?.length || (work.nfo_exists && (!work.title_loaded || refreshArtwork))) {
        whenVisible(wrap, () => {
          if (work.nfo_exists && (!work.title_loaded || refreshArtwork)) {
            post('library_title', { author_id: a.id, folder_ids: branch.map(f => f.id), file_id: work.file_id, refresh: refreshArtwork }).then(result => {
              Object.assign(work, { title: result.title, nfo_title: result.nfo_title, nfo_error: result.nfo_error, title_loaded: true })
              if (wrap.isConnected) applyText()
            }).catch(error => { if (wrap.isConnected) { work.nfo_error = error.message; applyText() } })
          }
          if (!work.image_names?.length) return
          cachedImage(key, () => post('library_image', { author_id: a.id, folder_ids: branch.map(f => f.id), file_id: work.file_id, refresh: refreshArtwork }).then(result => result.image?.data_url || '')).then(url => { if (wrap.isConnected) applyCover(url) })
        })
      }
    }
    if (!next.childNodes.length) next.append(el('p', 'fp-empty', needle ? '没有匹配的作品' : `本地缓存中没有达到 ${state.minVideoMb} MB 的视频。`))
    for (const n of grid.children) visible.unobserve(n)
    grid.replaceChildren(next)
    state.refreshArtwork = false
  }
  function goBranch(branch) { state.branch = branch; state.filter = ''; render() }
  const listingSignature = listing => JSON.stringify((listing?.works || []).map(work => [work.file_id, work.name, work.title, work.nfo_version, work.image_names, work.branch]))
  async function loadFolder(force = false, refreshArtwork = false) {
    const a = author(); if (!a?.root_folder_id || loadingLibrary) return
    const request = ++state.request
    loadingLibrary = true
    message(force ? '正在从 CD2 刷新缓存…' : '')
    try {
      let result
      if (force) {
        let first = true
        do {
          result = await post('library_index', { author_id: a.id, force: first, cache_only: false }); first = false
          if (disposed || request !== state.request) return
          message(result.complete ? `缓存刷新完成 · ${result.works.length} 部作品` : `正在刷新缓存 · 已读取 ${result.scanned} 个目录 · ${result.pending} 个目录待处理`)
        } while (!disposed && request === state.request && !result.complete)
      } else {
        result = await post('library_index', { author_id: a.id, cache_only: true })
      }
      if (disposed || request !== state.request) return
      const changed = listingSignature(state.listing) !== listingSignature(result)
      state.listing = result; state.autoMatch = result.auto_match || state.autoMatch; state.minVideoMb = result.min_video_mb ?? state.minVideoMb
      if (refreshArtwork) { state.refreshArtwork = true; images.clear() }
      try { sessionStorage.setItem(listCacheKey(a), JSON.stringify(result)) } catch (_) { /* Server disk cache remains authoritative. */ }
      const grid = main.querySelector('.fp-works')
      if (grid && (changed || refreshArtwork || !grid.childNodes.length)) drawWorks(grid)
      updateAuthorChrome()
      if (!force) message(`使用本地缓存 · ${result.works.length} 部作品${result.pending ? ` · ${result.pending} 个目录等待事件刷新` : ''}`)
    } catch (error) {
      if (request === state.request) message(`${error.message || '缓存刷新失败'}；继续使用现有缓存。`, true)
    } finally { if (request === state.request) loadingLibrary = false }
  }
  async function checkCache() {
    const a = author()
    if (disposed || !state.cacheReady || !a?.root_folder_id || loadingLibrary || checkingCache) return
    checkingCache = true
    try {
      const result = await post('library_cache_status', { author_id: a.id })
      if (disposed || state.authorId !== a.id) return
      if (!cacheTokens.has(a.id)) {
        cacheTokens.set(a.id, result.token)
      } else if (cacheTokens.get(a.id) !== result.token) {
        if (dialogs.size) { message('目录有更新，将在关闭编辑后更新作品列表；当前输入保留。'); return }
        cacheTokens.set(a.id, result.token); await loadFolder(false, true)
      }
      if (result.pending) {
        cacheWarningShown = true
        message(`本地缓存可用 · ${result.pending} 个目录等待事件刷新`)
      } else if (cacheWarningShown) {
        cacheWarningShown = false
        message('')
      }
      if (state.autoMatchReady) {
        const progress = await post('auto_match_status', { author_id: a.id })
        const before = JSON.stringify(state.autoMatch); state.autoMatch = progress.auto_match
        if (before !== JSON.stringify(state.autoMatch) && !dialogs.size) updateAuthorChrome()
      }
    } catch (_) { /* Transient local status errors never erase a displayed library. */ }
    finally { checkingCache = false }
  }
  const cacheTimer = setInterval(checkCache, 30000)
  async function storageSettings() {
    const body = el('fieldset', 'fp-editor fp-editor-fields'), note = el('div', 'fp-status', '正在读取 CloudDrive2 设置…')
    const d = modal('精选女优插件设置', body, 'md'); let busy = false
    try {
      const current = await post('storage_settings')
      if (!d.el.isConnected) return
      const endpoint = input(current.endpoint || '192.168.31.10:19798'), rootPath = input(current.root || '/dbonline/国产精选'), token = input('', 'password')
      token.placeholder = current.token_set ? '已加密保存；留空保持不变' : 'CloudDrive2 API Token'
      token.autocomplete = 'new-password'
      body.append(ui.settingsCard({ title: 'CloudDrive2', description: '精选女优的目录、NFO、图片和文件操作均通过此连接完成。', children: [
        ui.settingRow({ label: 'gRPC 地址', description: '不含 http(s)://', control: endpoint }),
        ui.settingRow({ label: '规范路径前缀', description: '用于把现有绑定路径映射到 Token 根目录。', control: rootPath }),
        ui.settingRow({ label: 'API Token', description: current.token_set ? 'Token 已保存到 NOOR 加密 secret store。' : '建议限制根目录到精选作者目录。', control: token }),
      ] }))
      note.textContent = current.token_set ? 'CloudDrive2 Token 已配置' : '尚未配置 Token'
      const setBusy = value => { busy = value; body.disabled = value; test.disabled = save.disabled = close.disabled = value }
      const values = () => ({ endpoint: endpoint.value.trim(), root: rootPath.value.trim(), token: token.value.trim() })
      const test = button('测试连接', async () => {
        setBusy(true); note.textContent = '正在测试 CloudDrive2…'; note.dataset.error = 'false'
        try { const result = await post('test_storage', values()); note.textContent = `${result.message} · 根目录 ${result.root_items} 项` }
        catch (error) { note.textContent = error.message; note.dataset.error = 'true' }
        finally { setBusy(false) }
      })
      const save = ui.button({ label: '保存设置', tone: 'primary', onClick: async () => {
        setBusy(true); note.textContent = '正在验证并保存…'; note.dataset.error = 'false'
        try { const result = await post('save_storage_settings', values()); state.storage = { type: 'clouddrive2', enabled: true, endpoint: result.endpoint }; token.value = ''; note.textContent = `${result.message} · 根目录 ${result.root_items} 项`; message(result.message) }
        catch (error) { note.textContent = error.message; note.dataset.error = 'true' }
        finally { setBusy(false) }
      } })
      const close = button('关闭', () => d.close())
      d.beforeClose = () => !busy
      editorFooter(d, note, [test, close, save])
    } catch (error) { note.textContent = error.message; note.dataset.error = 'true' }
  }
  function librarySettings() {
    const body = el('div', 'fp-editor'), min = input(state.minVideoMb ?? 100, 'number'), interval = input(state.checkMinutes ?? 0, 'number'), note = el('div', 'fp-status')
    min.min = '1'; min.max = '100000'; min.step = '1'
    interval.min = '0'; interval.max = '10080'; interval.step = '15'
    body.append(ui.settingsCard({ title: '作品识别与缓存', children: [ui.settingRow({ label: '最小视频体积（MB）', description: '默认 100 MB；1 MB = 1024² 字节。只筛选列表，不删除文件或已有资料。', control: min }), ui.settingRow({ label: '兜底检查间隔（分钟）', description: '默认 0：完全使用本地缓存，由 CD2/Emby 通知触发精确目录刷新；也可设置低频兜底检查。', control: interval })] }), note)
    const d = modal('作品筛选', body, 'md'); let saving = false; d.beforeClose = () => !saving
    const save = button('保存', async () => {
      saving = true; save.disabled = true; note.textContent = '保存中…'
      try {
        const result = await post('save_library_settings', { min_video_mb: Number(min.value), check_minutes: Number(interval.value) }); state.minVideoMb = result.min_video_mb; state.checkMinutes = result.check_minutes ?? state.checkMinutes
        state.request++; saving = false; await d.close(); render(); if (author()) await loadFolder()
      } catch (e) { note.textContent = e.message }
      finally { saving = false; save.disabled = false }
    })
    footer(d, [button('取消', () => d.close()), save])
  }
  async function editWork(a, branch, candidate, refresh = false) {
    const body = el('fieldset', 'fp-editor fp-editor-fields'), note = el('div', 'fp-status', '正在读取作品资料…'); body.append(note)
    const d = modal('编辑作品 NFO', body)
    try {
      const bound = await post('library_bind', { author_id: a.id, folder_ids: branch.map(f => f.id), file_id: candidate.file_id }); replace(bound.author)
      const draft = await post('prepare_work', { author_id: a.id, work_id: bound.work.id, refresh })
      if (!d.el.isConnected) return
      const controls = {}, changes = { item_id: '', data_url: '', image_url: '' }
      let dirty = false, saving = false, matching = false, readingImage = false, showCandidate = () => {}
      d.beforeClose = async () => !saving && !matching && !readingImage && (!dirty || await ui.confirm({ title: '放弃修改？', message: '尚未保存到 CD2，关闭会丢失本次修改。' }))
      const mark = () => { dirty = true; note.textContent = '未保存'; note.dataset.error = 'false' }
      const panels = editorTabs(body, d, [['details', '作品资料'], ['artwork', '封面与匹配']], key => { if (key === 'artwork') showCandidate() })
      const summary = el('div', 'fp-actions')
      summary.append(ui.tag({ label: a.name }), ui.tag({ label: `季 ${draft.fields.season || '—'} · 集 ${draft.fields.episode || bound.work.episode_number || '—'}` }), ui.tag({ label: candidate.nfo_exists ? '已有 NFO' : '待创建 NFO' }))
      panels.details.append(summary)
      const basic = el('div', 'fp-nfo-fields'), classification = el('div', 'fp-nfo-fields fp-editor-section')
      panels.details.append(basic)
      const coverColumn = el('div', 'fp-editor'), matchColumn = el('div', 'fp-editor'), coverLayout = el('div', 'fp-cover-columns')
      coverLayout.append(coverColumn, matchColumn); panels.artwork.append(coverLayout)
      const preview = el('div', 'fp-cover-preview'); coverColumn.append(el('strong', '', '封面预览'), preview)
      const showCover = url => preview.replaceChildren(url ? photo(url, '作品封面') : el('span', 'fp-muted', '暂无封面，可上传或匹配来源'))
      showCover(draft.image?.data_url)
      const coverActions = el('div', 'fp-actions'), file = input('', 'file'); file.accept = 'image/*'; file.hidden = true
      const imageUrl = input(); imageUrl.placeholder = '封面图片外链（保存时导入 CD2）'
      file.onchange = () => {
        const picked = file.files?.[0]; if (!picked) return
        if (picked.size > 25 * 1024 * 1024) { note.textContent = '图片不能超过 25 MiB'; return }
        readingImage = true; body.disabled = true; save.disabled = true; note.textContent = '正在读取上传图片…'
        const reader = new FileReader()
        reader.onload = () => { Object.assign(changes, { item_id: '', image_url: '', data_url: reader.result }); imageUrl.value = ''; showCover(reader.result); mark() }
        reader.onerror = () => { note.textContent = '图片读取失败，请重新选择'; note.dataset.error = 'true' }
        reader.onloadend = () => { readingImage = false; body.disabled = false; save.disabled = false; file.value = '' }
        reader.readAsDataURL(picked)
      }
      imageUrl.oninput = () => { Object.assign(changes, { item_id: '', data_url: '', image_url: imageUrl.value.trim() }); showCover(imageUrl.value.trim() ? '' : draft.image?.data_url); mark(); if (imageUrl.value.trim()) preview.replaceChildren(el('span', 'fp-muted', '外链图片将在保存时导入')) }
      coverActions.append(button('上传封面', () => file.click()), button('保留现有封面', () => { Object.assign(changes, { item_id: '', data_url: '', image_url: '' }); imageUrl.value = ''; showCover(draft.image?.data_url); mark() }), file)
      coverColumn.append(coverActions, field('导入图片外链', imageUrl), el('small', 'fp-muted', '封面随底部“保存到 CD2”一同写入。'))
      for (const [key, label] of [['title', 'NFO 标题'], ['tags', '标签'], ['actors', '演员'], ['studio', '片商（不确定可留空）'], ['genres', '分类'], ['premiered', '发布日期（未知留空）'], ['plot', '简介']]) {
        const value = draft.fields[key], control = key === 'plot' ? ui.textarea({ rows: 4 }) : input()
        control.value = Array.isArray(value) ? value.join('，') : value || ''; controls[key] = control
        control.oninput = mark
        const item = field(label, control)
        if (key === 'title' || key === 'plot') item.classList.add('fp-nfo-wide')
        ;(['title', 'plot', 'premiered'].includes(key) ? basic : classification).append(item)
      }
      controls.premiered.parentElement.after(field('标题工具', button('从文件名提取标题', () => { controls.title.value = draft.clean_title; mark() })))
      panels.details.append(ui.settingsCard({ title: '分类信息', description: '多项用逗号分隔；不确定的内容可留空。', children: [classification] }),
        ui.settingsCard({ title: '原始文件', collapsible: true, open: false, children: [el('p', 'fp-path', draft.work.remote_path || draft.work.name), el('small', 'fp-muted', '本次保存修改 NFO 与封面，保留文件名和目录。')] }))
      const sources = el('div', 'fp-editor'), sourceBody = el('div', 'fp-editor'), sourceSummary = el('strong'); sources.append(sourceSummary, sourceBody)
      let matches = draft.matches || [], matchTitle = draft.match_title || controls.title.value, matchRequest = 0
      const sourceImages = new Map()
      const sourceImage = id => {
        if (!sourceImages.has(id)) sourceImages.set(id, post('source_image', { author_id: a.id, item_id: id }).then(r => r.image?.data_url || ''))
        return sourceImages.get(id)
      }
      const adoptCandidate = async (id, automatic = false) => {
        const m = matches.find(row => row.id === id)
        if (!m) return
        controls.title.value = m.title
        Object.assign(changes, { item_id: id, data_url: '', image_url: '' }); imageUrl.value = ''; mark()
        note.textContent = `${automatic ? '已自动匹配' : '已采用手动匹配'} · ${Math.round(m.score * 100)}% · 尚未保存到 CD2`
        try { const url = await sourceImage(id); if (changes.item_id === id) showCover(url) }
        catch { sourceImages.delete(id); note.textContent += '；封面预览失败，保存时会重试，原封面不会被破坏。' }
      }
      const drawMatches = () => {
        sourceBody.replaceChildren(); sourceSummary.textContent = `手动匹配 · ${matches.length} 个缓存候选`
        sourceBody.append(el('p', 'fp-muted', `匹配依据：${matchTitle}（仅查询缓存）`))
        if (!matches.length) {
          sourceBody.append(el('p', 'fp-muted', a.external_source?.error ? `来源缓存为空，上次抓取失败：${a.external_source.error}。请在作者设置重新抓取；仍可以直接手动编辑。` : '未设置来源或没有缓存。可以直接手动编辑；来源在作者设置中配置。'))
          showCandidate = () => {}; return
        }
        let selected = matches[0].id
        const choose = ui.select({ value: selected, options: matches.map(m => ({ value: m.id, label: `${Math.round(m.score * 100)}% · ${m.title}` })), onChange: value => { selected = value; showCandidate() } })
        const sample = el('div', 'fp-cover-preview')
        showCandidate = async () => {
          if (panels.artwork.hidden) return
          const id = selected; sample.replaceChildren(el('p', 'fp-muted', '通过 NOOR 代理读取候选封面…'))
          try {
            const url = await sourceImage(id)
            if (id === selected) sample.replaceChildren(url ? photo(url, '来源候选，尚未保存') : el('p', '', '候选没有封面'))
          } catch (error) { sourceImages.delete(id); if (id === selected) sample.replaceChildren(el('p', 'fp-muted', error.message || '封面读取失败')) }
        }
        sourceBody.append(choose, sample, button('采用此标题和封面', () => adoptCandidate(selected)))
        showCandidate()
      }
      drawMatches()
      const automatic = button('自动匹配标题和封面', async () => {
        if (matching || saving || readingImage) return
        const request = ++matchRequest, title = controls.title.value.trim()
        matching = true; body.disabled = true; save.disabled = true; automatic.disabled = true; note.textContent = '正在从缓存自动匹配…'
        try {
          const r = await post('match_source_title', { author_id: a.id, title }); if (request !== matchRequest || !d.el.isConnected) return
          matches = r.matches; matchTitle = title; drawMatches()
          if (!matches.length) { note.textContent = '没有可用来源候选；仍可手动编辑标题和上传封面。'; return }
          const top = matches[0], second = matches[1], margin = top.score - (second?.score || 0)
          if (top.score < .42 || (top.score < .88 && margin < .06)) {
            const ok = await ui.confirm({ title: '自动匹配置信度较低', message: `最佳候选 ${Math.round(top.score * 100)}% · ${top.title}。是否仍采用？也可以取消后在“手动匹配”中选择。` })
            if (!ok) { note.textContent = '未采用低置信候选，可继续手动匹配。'; return }
          }
          await adoptCandidate(top.id, true)
        }
        catch (e) { note.textContent = e.message }
        finally { matching = false; body.disabled = false; save.disabled = false; automatic.disabled = false }
      })
      matchColumn.append(automatic, sources); note.textContent = '已读取 · 未修改'
      const save = ui.button({ label: '保存到 CD2', tone: 'primary', onClick: async () => {
        if (saving || matching || readingImage) return
        saving = true; body.disabled = true; save.disabled = true; note.dataset.error = 'false'; note.textContent = '正在写入 CD2 并确认文件…'
        const fields = Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value]))
        try {
          const result = await post('save_work', { author_id: a.id, work_id: bound.work.id, hash: draft.hash, fields, ...changes })
          dirty = false; saving = false; replace(result.author); images.clear(); folders.clear(); await d.close()
          if (state.authorId === a.id) await loadFolder()
          message(result.message)
        } catch (error) { note.textContent = error.message || '保存失败，输入已保留，请重试'; note.dataset.error = 'true' }
        finally { saving = false; body.disabled = false; save.disabled = false }
      } })
      editorFooter(d, note, [button('重新读取磁盘', async () => {
        if (saving || matching || readingImage || (dirty && !await ui.confirm({ title: '重新读取磁盘？', message: '将放弃本次未保存修改。' }))) return
        dirty = false; await d.close(); await editWork(a, branch, candidate, true)
      }), button('取消', () => d.close()), save])
    } catch (error) { note.textContent = error.message || '无法读取作品资料'; note.dataset.error = 'true' }
  }
  function cleanupDialog(a, branch) {
    const body = el('div', 'fp-editor'), max = input(2, 'number'), recursive = sdk.ui.settingsSwitch({ checked: true }), texts = sdk.ui.settingsSwitch({ checked: false })
    max.min = '0.01'; max.max = '100'; max.step = '0.1'
    const scope = a.remote_path + branch.map(f => '/' + f.name).join('')
    body.append(el('p', 'fp-path', `范围：${scope}`), ui.settingsCard({ title: '候选筛选', children: [
      sdk.ui.settingRow({ label: '大小上限（MB）', control: max }),
      ui.settingRow({ label: '包含子目录', control: recursive }),
      ui.settingRow({ label: '包含其他小文本／网页', description: '默认只筛选广告文本、快捷方式和系统杂项。', control: texts }),
    ] }), el('p', 'fp-muted', '保护所有视频、字幕、NFO、图片和备份。清理前保留远端备份，可从记录恢复；不会释放备份占用的容量。'))
    const note = el('div', 'fp-status', '先扫描候选，再确认清理。'), list = el('div', 'fp-cleanup-list'); body.append(note, list)
    const d = modal('清理垃圾文件', body)
    let plan = null, busy = false, stop = false
    const selected = new Set()
    const uncertain = () => Object.assign(new Error('结果待确认，未重复提交。请稍后打开清理记录核对；剩余文件未处理。'), { uncertain: true })
    const runOperation = async (entry, operation, existingJob = null) => {
      const payload = { author_id: a.id, plan_id: plan.id, file_id: entry.file_id, operation, confirm: true }
      const expected = operation === 'cleanup_restore' ? 'restored' : 'cleaned'
      let job = existingJob, errors = 0
      const reconcile = async () => {
        try {
          const record = await post('cleanup_history', { author_id: a.id })
          const item = record.plans.find(p => p.id === payload.plan_id)?.items.find(r => r.file_id === entry.file_id)
          const active = record.jobs?.find(j => j.plan_id === payload.plan_id && j.file_id === entry.file_id && j.operation === operation)
          if (active) return { job: active }
          if (item?.status === expected) return { result: { item } }
        } catch (_) { /* A lost response is not proof of failure. */ }
        throw uncertain()
      }
      if (!job) {
        try { job = (await post('cleanup_start', payload)).job }
        catch (_) {
          const recovered = await reconcile()
          if (recovered.result) return recovered.result
          job = recovered.job
        }
      }
      while (!disposed) {
        if (job.status === 'completed') return job.result
        if (job.status === 'failed') throw new Error(job.error || '处理失败，请检查清理记录')
        if (job.status === 'unknown') {
          const recovered = await reconcile()
          if (recovered.result) return recovered.result
          job = recovered.job
        }
        note.textContent = `${entry.name} · ${job.phase || '正在处理'}${stop ? ' · 完成当前项后停止' : ''}`
        await new Promise(resolve => setTimeout(resolve, 1500))
        if (disposed) break
        try { job = (await post('cleanup_job_status', { author_id: a.id, job_id: job.id })).job; errors = 0 }
        catch (_) {
          note.textContent = `${entry.name} · 查询暂时失败，正在重新查询（不会重复清理）`
          if (++errors >= 3) {
            const recovered = await reconcile()
            if (recovered.result) return recovered.result
            // Leave an interrupted connection safely reconcilable from history.
            throw uncertain()
          }
        }
      }
      throw uncertain()
    }
    d.beforeClose = () => !busy
    const bytes = size => size < 1024 ? `${size} B` : size < 1048576 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1048576).toFixed(2)} MB`
    const updateButtons = () => {
      scan.disabled = busy; clean.disabled = busy || !plan?.complete || !selected.size; history.disabled = busy
      max.disabled = recursive.input.disabled = texts.input.disabled = busy
      cancel.disabled = !busy
    }
    const draw = () => {
      list.replaceChildren()
      const statuses = { cleaned: '已清理 · 可恢复', restored: '已恢复', backed_up: '已备份 · 待确认清理结果', pending: '待清理' }
      if (plan?.items.length) list.append(ui.checkbox({ label: '全选候选', checked: selected.size > 0 && selected.size === plan.items.filter(r => !['cleaned', 'restored'].includes(r.status)).length, disabled: busy, onChange: checked => {
        selected.clear(); if (checked) plan.items.filter(r => !['cleaned', 'restored'].includes(r.status)).forEach(r => selected.add(r.file_id)); draw()
      } }))
      for (const entry of plan?.items || []) {
        const row = el('div', 'fp-cleanup-row'), info = el('div', 'fp-cleanup-info')
        const checkbox = ui.checkbox({ checked: selected.has(entry.file_id), disabled: busy || ['cleaned', 'restored'].includes(entry.status), label: entry.name, onChange: checked => { checked ? selected.add(entry.file_id) : selected.delete(entry.file_id); updateButtons() } })
        info.append(checkbox, el('small', 'fp-path', entry.path), el('small', 'fp-muted', `${bytes(entry.size)} · ${entry.reason} · ${statuses[entry.status] || entry.status}`))
        if (entry.error) info.append(el('small', 'fp-cleanup-error', entry.error))
        row.append(info)
        if (entry.backup && entry.status !== 'restored') {
          const restore = button('恢复', async () => {
            if (!await ui.confirm({ title: '恢复文件？', message: `将 ${entry.name} 恢复到原目录；遇到同名文件不会覆盖。` })) return
            busy = true; draw(); note.textContent = '正在恢复…'
            try { const r = await runOperation(entry, 'cleanup_restore'); Object.assign(entry, r.item); note.textContent = '文件已恢复，备份保留。'; folders.clear() }
            catch (e) { entry.error = e.message; note.textContent = e.message }
            finally { busy = false; draw() }
          }); restore.disabled = busy; row.append(restore)
        }
        list.append(row)
      }
      if (plan?.complete && !plan.items.length) list.append(el('p', 'fp-muted', '没有符合条件的垃圾文件。'))
      updateButtons()
    }
    const invalidate = () => { plan = null; selected.clear(); list.replaceChildren(); note.textContent = '筛选条件已变更，请重新扫描。'; updateButtons() }
    max.oninput = recursive.input.onchange = texts.input.onchange = invalidate
    const scan = button('扫描候选', async () => {
      busy = true; stop = false; plan = null; selected.clear(); list.replaceChildren(); updateButtons()
      try {
        do {
          const result = await post('cleanup_scan', { author_id: a.id, folder_ids: branch.map(f => f.id), plan_id: plan?.id || '', max_mb: Number(max.value), recursive: recursive.input.checked, include_text: texts.input.checked })
          plan = result.plan; note.textContent = `已扫描 ${plan.seen.length} 个目录、${plan.scanned} 个条目，发现 ${plan.items.length} 个候选。`
        } while (!plan.complete && !stop && !disposed)
        if (plan.complete) { plan.items.forEach(r => selected.add(r.file_id)); note.textContent += ' 请核对清单后清理。' }
        else note.textContent += ' 扫描已停止，未删除任何文件。'
        if (plan.errors?.length) note.textContent += ' ' + plan.errors.join('；')
      } catch (e) { note.textContent = e.message || '扫描失败，未删除任何文件。' }
      finally { busy = false; draw() }
    })
    const clean = button('清理所选', async () => {
      const targets = plan.items.filter(r => selected.has(r.file_id))
      if (!targets.length || !await ui.confirm({ title: `清理 ${targets.length} 个文件？`, message: `范围：${plan.scope}。共 ${bytes(targets.reduce((sum, r) => sum + r.size, 0))}；先备份再从原目录删除，可恢复。`, danger: true })) return
      busy = true; stop = false; draw(); let done = 0, failed = 0, unknown = 0
      for (const entry of targets) {
        if (stop || disposed) break
        note.textContent = `正在备份并清理 ${entry.name} · ${done + failed + 1}/${targets.length}`
        try {
          const result = await runOperation(entry, 'cleanup_execute')
          Object.assign(entry, result.item); selected.delete(entry.file_id); done++
        } catch (e) { entry.error = e.message; if (e.uncertain) unknown++; else failed++; stop = true }
        draw()
      }
      busy = false; folders.clear(); images.clear(); note.textContent = `本次已确认清理 ${done} 个，失败 ${failed} 个${unknown ? `，待确认 ${unknown} 个` : ''}。${stop ? '后续已停止，可在清理记录中核对或恢复。' : '可从记录恢复，备份仍占用网盘容量。'}`; draw()
    })
    const history = button('清理记录', async () => {
      busy = true; updateButtons()
      try {
        const result = await post('cleanup_history', { author_id: a.id }); list.replaceChildren(); plan = null; selected.clear()
        for (const job of result.jobs || []) list.append(button(`查看进行中任务 · ${job.phase}`, async () => {
          plan = result.plans.find(p => p.id === job.plan_id)
          const entry = plan?.items.find(r => r.file_id === job.file_id)
          if (!entry) return
          busy = true; stop = false; draw()
          try { const r = await runOperation(entry, job.operation, job); Object.assign(entry, r.item); note.textContent = '已确认远端操作完成。'; folders.clear(); images.clear() }
          catch (e) { entry.error = e.message; note.textContent = e.message }
          finally { busy = false; draw() }
        }))
        for (const item of [...result.plans].reverse()) list.append(button(`${item.created_at} · ${item.items.filter(r => r.status === 'cleaned').length} 个已清理 · ${item.scope}`, () => { plan = item; note.textContent = '清理记录（支持恢复）；重新清理请先扫描。'; draw() }))
        note.textContent = result.plans.length ? '选择记录查看明细和恢复文件。' : '暂无清理记录。'
      } catch (e) { note.textContent = e.message }
      finally { busy = false; updateButtons() }
    })
    const cancel = button('停止后续处理', () => { stop = true; note.textContent = '当前请求完成后停止；已完成的清理仍可恢复。' })
    footer(d, [history, cancel, scan, clean]); updateButtons()
  }
  async function embyLayoutDialog(a) {
    const body = el('div', 'fp-editor'), note = el('div', 'fp-status', '正在生成整理预览…'), summary = el('div', 'fp-layout-summary'), list = el('div', 'fp-cleanup-list')
    body.append(note, summary, list)
    const d = modal('整理为 Emby 剧集', body)
    let plan = null, busy = true
    const close = button('关闭', () => d.close()); close.disabled = true
    const execute = button('执行整理', async () => {
      if (!plan || plan.conflicts?.length) return
      const confirmed = await ui.confirm({
        title: '执行 Emby 剧集整理？',
        message: `将移动 ${plan.works.length} 部正片及旁车，永久删除 ${plan.deletes.length} 个已列出的小广告／广告杂项，并删除确认已清空的原作品目录。删除后不可从精选女优恢复。`,
        danger: plan.deletes.length > 0,
      })
      if (!confirmed) return
      busy = true; execute.disabled = close.disabled = true
      try {
        let job = (await post('emby_layout_start', { author_id: a.id, plan_id: plan.id, confirm: true })).job
        while (!disposed && !['completed', 'failed', 'unknown'].includes(job.status)) {
          note.textContent = job.phase || '正在整理…'
          await new Promise(resolve => setTimeout(resolve, 1500))
          job = (await post('emby_layout_status', { author_id: a.id, plan_id: plan.id, job_id: job.id })).job
        }
        if (job.status !== 'completed') throw new Error(job.error || job.phase || '整理结果待确认')
        plan = job.result.plan; replace(job.result.author); folders.clear(); images.clear()
        try { sessionStorage.removeItem(listCacheKey(a)) } catch (_) {}
        state.request++; state.listing = null
        note.textContent = job.result.message
        drawPlan(); render(); loadFolder()
      } catch (error) {
        note.textContent = error.message || '整理失败，可重新执行同一预览继续未完成项目。'; note.dataset.error = 'true'
      } finally { busy = false; close.disabled = false; execute.disabled = Boolean(plan?.conflicts?.length) }
    })
    footer(d, [close, execute])
    const bytes = size => size < 1048576 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1048576).toFixed(1)} MB`
    const drawPlan = () => {
      summary.replaceChildren(); list.replaceChildren()
      if (!plan) return
      summary.append(
        el('strong', '', `${plan.works.length} 部作品 → ${plan.seasons.length} 个年份季`),
        el('span', 'fp-muted', `永久删除 ${plan.deletes.length} 个小广告／广告杂项 · 清理 ${plan.source_folders?.length || 0} 个原作品目录`),
      )
      for (const conflict of plan.conflicts || []) list.append(el('div', 'fp-cleanup-error', conflict))
      for (const season of plan.seasons || []) list.append(el('div', 'fp-layout-season', `${season.folder_name} · ${season.title}`))
      for (const item of plan.deletes || []) {
        const row = el('div', 'fp-cleanup-row'), info = el('div', 'fp-cleanup-info')
        info.append(el('strong', '', item.name), el('small', 'fp-path', item.path), el('small', 'fp-muted', `${bytes(item.size)} · ${item.reason} · ${item.status === 'deleted' ? '已删除' : '将永久删除'}`)); row.append(info); list.append(row)
      }
      execute.disabled = busy || Boolean(plan.conflicts?.length) || plan.status === 'completed'
      close.disabled = busy
    }
    try {
      const result = await post('emby_layout_preview', { author_id: a.id }); plan = result.plan
      note.textContent = plan.conflicts?.length ? `发现 ${plan.conflicts.length} 个冲突，未执行任何操作。` : '预览已生成，请核对将永久删除的小广告。'
      note.dataset.error = String(Boolean(plan.conflicts?.length)); busy = false; drawPlan()
    } catch (error) {
      busy = false; note.textContent = error.message || '无法生成整理预览'; note.dataset.error = 'true'; close.disabled = false; execute.disabled = true
    }
  }
  async function authorSettings(a) {
    const body = el('fieldset', 'fp-editor fp-editor-fields'), note = el('div', 'fp-status fp-editor-feedback', '正在读取作者资料…')
    const d = modal('作者设置', body)
    let active = 'profile', busy = false, profileDirty = false, sourceDirty = false, info = null, artsLoaded = false
    const name = input(a.name), aliases = input(a.aliases), bio = ui.textarea({ value: a.bio || '', rows: 5 })
    const source = a.external_source || {}, url = input(source.url || ''), cookie = input('', 'password')
    url.placeholder = 'https://madouqu.com/video/tag/…/'; cookie.placeholder = '留空保留已保存的 Cookie'; cookie.autocomplete = 'new-password'
    const sourceStatus = el('small', 'fp-muted', source.error || (source.url ? `已缓存 ${source.items?.length || 0} 部作品` : '尚未绑定来源'))
    const controls = { title: name, aliases, plot: bio }
    const feedback = (text, error = false) => { note.textContent = text; note.dataset.error = String(error) }
    const setBusy = value => { busy = value; body.disabled = value; updateFooter() }
    d.beforeClose = async () => !busy && (!(profileDirty || sourceDirty) || await ui.confirm({ title: '放弃修改？', message: '作者资料或来源还有未保存的修改。' }))
    const saveProfile = ui.button({ label: a.root_folder_id ? '保存资料到 CD2' : '保存资料', tone: 'primary', onClick: async () => {
      if (busy || !info) return
      if (!name.value.trim()) { feedback('作者名称不能为空', true); name.focus(); return }
      setBusy(true); feedback('正在保存作者资料…')
      try {
        const fields = Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value]))
        const result = a.root_folder_id
          ? await post('save_entity', { author_id: a.id, entity: 'author', entity_id: a.id, hash: info.nfo.hash || '', fields })
          : await post('update_author', { author_id: a.id, name: fields.title, aliases: fields.aliases, bio: fields.plot })
        if (result.info) info = result.info
        a = { ...a, ...(result.author || {}), name: fields.title.trim(), aliases: fields.aliases, bio: fields.plot }
        replace(a); profileDirty = false; render()
        feedback(a.root_folder_id ? '作者资料已保存到 CD2；Emby 尚未刷新。' : '作者资料已保存。')
      } catch (error) { feedback(error.message, true) }
      finally { setBusy(false) }
    } })
    const saveSource = ui.button({ label: '保存来源并抓取', tone: 'primary', onClick: async () => {
      if (busy) return
      setBusy(true); feedback('正在通过设置中的代理抓取作品来源…')
      try {
        const result = await post('bind_external_source', { author_id: a.id, url: url.value, cookie: cookie.value, user_agent: navigator.userAgent })
        a = result.author; replace(a); sourceDirty = false; cookie.value = ''; render()
        sourceStatus.textContent = `已缓存 ${result.source.items.length} 部作品`
        feedback('来源已保存，作品目录已缓存。')
      } catch (error) { feedback(error.message, true) }
      finally { setBusy(false) }
    } })
    const reread = button('重新读取资料', async () => {
      if (busy || (profileDirty && !await ui.confirm({ title: '重新读取作者资料？', message: '放弃尚未保存的作者名称、别名和简介。' }))) return
      await readProfile()
    })
    const close = button('关闭', () => d.close())
    function updateFooter() {
      saveProfile.disabled = busy || !info; saveSource.disabled = reread.disabled = close.disabled = busy
      editorFooter(d, note, active === 'profile' ? [reread, close, saveProfile] : active === 'storage' ? [close, saveSource] : [close])
    }
    const panels = editorTabs(body, d, [['profile', '作者资料'], ['images', '图片'], ['storage', '来源与存储']], key => {
      active = key; updateFooter()
      if (key === 'profile' && profileDirty) feedback('作者资料未保存')
      if (key === 'storage' && sourceDirty) feedback('来源设置未保存')
      if (key === 'images' && !artsLoaded) {
        artsLoaded = true
        for (const [role, label, description] of [['poster', '作者海报', '建议竖版 2:3'], ['fanart', '背景图', '建议横版 16:9，如 1920 × 1080'], ['clearlogo', '透明 LOGO', 'PNG，保留透明通道；设置后替代作者名显示']]) {
          imageEditor(panels.images, a, role, label, {}, { setBusy, isBusy: () => busy, description })
        }
      }
    })
    for (const control of Object.values(controls)) control.oninput = () => { profileDirty = true; feedback('作者资料未保存') }
    for (const control of [url, cookie]) control.oninput = () => { sourceDirty = true; feedback('来源设置未保存') }
    panels.profile.append(ui.settingsCard({ title: '作者资料', description: '作为 Emby 剧集资料保存。', children: [
      ui.settingRow({ label: '作者名称', control: name }), ui.settingRow({ label: '别名', control: aliases }),
      ui.settingRow({ label: '简介', description: '填写后直接显示在作者主页。', control: bio }),
    ] }))
    panels.images.append(el('p', 'fp-muted', '各张图片单独保存到 CD2，上传或导入后立即生效。'))
    panels.storage.append(ui.settingsCard({ title: '标题与封面来源', children: [
      ui.settingRow({ label: '麻豆区页面', description: '使用 NOOR 网络代理；匹配使用本地缓存。', control: url }),
      ui.settingRow({ label: 'Cookie', description: '可选', control: cookie }),
      ui.settingRow({ label: '缓存状态', control: sourceStatus }),
    ] }), sdk.ui.settingsCard({ title: 'CD2 存储', children: [
      ui.settingRow({ label: '作者目录', control: sdk.ui.settingPath({ value: a.remote_path || '', onBrowse: async () => {
        await d.close(); if (!d.el.isConnected) browseAuthor(a)
      } }) }),
      ui.settingRow({ label: '解除绑定', description: '保留 CD2 中的视频、NFO 和图片。', control: ui.button({ label: '解除作者绑定', tone: 'danger', onClick: async () => {
        if (busy || !await ui.confirm({ title: '解除作者绑定？', message: '将移除作者的管理关系，未保存编辑也将放弃。CD2 文件全部保留。', danger: true, confirmText: '解除绑定' })) return
        setBusy(true)
        try {
          await post('unbind', { author_id: a.id, entity: 'author', entity_id: a.id })
          profileDirty = sourceDirty = false; setBusy(false); await d.close(); state.authorId = ''; await overview(); render()
        } catch (error) { feedback(error.message, true); setBusy(false) }
      } }) }),
    ] }))
    async function readProfile() {
      setBusy(true); feedback('正在读取作者资料…')
      try {
        const result = await post('read_entity', { author_id: a.id, entity: 'author', entity_id: a.id })
        if (!d.el.isConnected) return
        if (result.info.nfo.status === 'error') throw new Error(result.info.nfo.error)
        info = result.info
        const values = info.nfo.fields || {}
        for (const [key, control] of Object.entries(controls)) control.value = values[key] ?? ({ title: a.name, aliases: a.aliases, plot: a.bio }[key] || '')
        profileDirty = false; feedback('作者资料已读取')
      } catch (error) { info = null; feedback(error.message, true) }
      finally { setBusy(false) }
    }
    await readProfile()
  }
  function yearSettings(a, year) {
    const body = el('div', 'fp-editor'); body.append(el('p', 'fp-muted', `固定季号 S${String(year.season_number).padStart(2, '0')}，新增年份不会重排。`), button('编辑年份资料', () => metadata(a, 'year', year.id)))
    imageEditor(body, a, 'poster', '年份海报', { year_id: year.id }); modal(year.display_title || year.title, body)
  }
  async function metadata(a, entity, entityId) {
    const body = el('div', 'fp-editor'), note = el('p', 'fp-status', '正在读取…'); body.append(note); const d = modal('编辑资料', body, 'md')
    try {
      const { info } = await post('read_entity', { author_id: a.id, entity, entity_id: entityId })
      if (info.nfo.status === 'error') throw new Error(info.nfo.error)
      const fields = info.nfo.fields || {}, controls = {}
      for (const [key, label, fallback] of [['title', '名称', info.model?.title || a.name], ...(entity === 'author' ? [['aliases', '别名', a.aliases]] : []), ['plot', '简介', entity === 'author' ? a.bio : '']]) {
        const n = key === 'plot' ? ui.textarea({ rows: 4 }) : input(); n.value = fields[key] ?? fallback ?? ''; n.oninput = () => { note.textContent = '未保存' }; controls[key] = n; body.append(field(label, n))
      }
      note.textContent = '已读取'
      const save = button(a.root_folder_id ? '保存到 CD2' : '保存资料', async () => {
        save.disabled = true; note.textContent = '保存中…'
        try {
          const fields = Object.fromEntries(Object.entries(controls).map(([k, n]) => [k, n.value]))
          if (a.root_folder_id) await post('save_entity', { author_id: a.id, entity, entity_id: entityId, hash: info.nfo.hash || '', fields })
          else await post('update_author', { author_id: a.id, name: fields.title, aliases: fields.aliases, bio: fields.plot })
          await overview(); render(); d.close(); message(a.root_folder_id ? '资料已保存到 CD2；Emby 尚未刷新。' : '作者资料已保存，尚未绑定目录。')
        } catch (e) { note.textContent = e.message; save.disabled = false }
      }); footer(d, [button('取消', () => d.close()), save])
    } catch (e) { note.textContent = e.message }
  }
  async function imageEditor(parent, a, role, label, extra = {}, lifecycle = {}) {
    const box = el('div', 'fp-editor'), preview = el('div', `fp-image-preview fp-image-preview--${role}`), note = el('small', 'fp-muted')
    box.append(preview, note)
    parent.append(ui.settingsCard({ children: [ui.settingRow({ label, description: lifecycle.description || '', control: box })] }))
    if (!a.root_folder_id) { note.textContent = '绑定目录后可设置'; return }
    const url = input(); url.placeholder = '图片外链'; const file = input('', 'file'); file.accept = 'image/*'; file.hidden = true
    const actions = el('div', 'fp-actions')
    const save = async source => {
      if (lifecycle.isBusy?.()) return
      lifecycle.setBusy?.(true)
      actions.querySelectorAll('button').forEach(b => { b.disabled = true }); note.textContent = '正在转换并保存到 CD2…'
      try { const result = await post('upload_image', { author_id: a.id, role, ...extra, ...source }); images.clear(); preview.replaceChildren(photo(result.image.data_url, label)); note.textContent = 'CD2 保存成功；Emby 尚未刷新。'; render() }
      catch (e) { note.textContent = e.message }
      finally { actions.querySelectorAll('button').forEach(b => { b.disabled = false }); lifecycle.setBusy?.(false) }
    }
    file.onchange = () => { const picked = file.files?.[0]; if (!picked) return; if (picked.size > 25 * 1024 * 1024) { note.textContent = '图片不能超过 25 MiB'; return } const reader = new FileReader(); reader.onload = () => save({ data_url: reader.result }); reader.readAsDataURL(picked) }
    actions.append(button('上传图片', () => file.click()), button('导入外链', () => save({ image_url: url.value })), file); box.append(field('图片外链', url), actions)
    try { const image = await artwork(a, role, extra); if (!note.textContent) { if (image) preview.replaceChildren(photo(image, label)); else preview.textContent = '未设置' } }
    catch (error) { note.textContent = error.message || '图片读取失败，可重新打开设置重试。' }
  }
  function createAuthor() {
    const body = el('div', 'fp-editor'), name = input(), aliases = input(), note = el('p', 'fp-muted', '创建后选择 CD2 文件夹。'); body.append(field('作者名称', name), field('别名', aliases), note)
    const d = modal('添加作者', body, 'md'), save = button('下一步：选择目录', async () => {
      save.disabled = true
      try { const result = await post('create_author', { name: name.value, aliases: aliases.value }); state.authors.push(result.author); enter(result.author); d.close(); browseAuthor(result.author) } catch (e) { note.textContent = e.message; save.disabled = false }
    }); footer(d, [button('取消', () => d.close()), save])
  }
  function browseAuthor(a) {
    const body = el('div', 'fp-editor'), stack = [{ id: '0', path: '/', name: '' }], d = modal('选择 CD2 作者目录', body)
    const load = async () => {
      const here = stack.at(-1); body.replaceChildren(el('p', '', '读取目录…'))
      try {
        const r = await post('browse', { folder_id: here.id, path: here.path }); body.replaceChildren(el('code', 'fp-path', here.path))
        if (stack.length > 1) body.append(button('‹ 上一级', () => { stack.pop(); load() }))
        for (const f of r.items.filter(f => f.is_directory && f.name !== '.noor-backups')) body.append(button(f.name, () => { stack.push({ id: f.file_id, path: f.path, name: f.name }); load() }))
        const select = button('绑定当前目录', async () => {
          select.disabled = true
          try { const r = await post('bind_author_folder', { author_id: a.id, folder_id: here.id, path: here.path, name: here.name }); replace(r.author); images.clear(); folders.clear(); d.close(); enter(r.author) } catch (e) { body.append(el('p', '', e.message)); select.disabled = false }
        }); select.disabled = here.id === '0'; footer(d, [button('取消', () => d.close()), select])
      } catch (e) { body.replaceChildren(el('p', '', e.message), button('重试', load)) }
    }; load()
  }
  try { await overview(); render() } catch (error) { message(error.message, true); main.append(button('重试', async () => { await overview(); render() })) }
  return () => { disposed = true; clearInterval(cacheTimer); state.request++; visible.disconnect(); dialogs.forEach(d => { d.beforeClose = null; d.close() }); images.clear(); imageValues.clear(); folders.clear() }
}
