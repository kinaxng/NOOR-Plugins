export async function mount(el, sdk = {}) {
  const pluginId = sdk.pluginId || 'mdc-ng-manual'
  const apiPlugin = (path, init) => sdk.api?.plugin ? sdk.api.plugin(path, init) : fetch(`/api/plugins/${pluginId}${path}`, init)
  const state = {
    loading: true,
    error: '',
    notice: '',
    noticeTone: 'info',
    jobs: [],
    stats: { total: 0, running: 0, finished: 0, failed: 0 },
    defaults: { target_folder: '', link_mode: 0, watch_dirs: [] },
    sourcePaths: '',
    targetFolder: '',
    linkMode: '0',
    reuseWatchIndex: '-1',
    deleteEmptyParentAfterMove: false,
    page: 1,
    pageSize: 10,
    submitStatus: 'idle',
    submitProgress: 0,
    refreshing: false,
    timer: null,
    baseUrl: '',
  }

  el.innerHTML = `
    <div class="mdc-page">
      <div data-role="topbar"></div>
      <div data-role="notice"></div>
      <div class="mdc-layout">
        <section data-role="form" class="mdc-form"></section>
        <section data-role="jobs" class="mdc-jobs"></section>
      </div>
    </div>
  `

  const $ = role => el.querySelector(`[data-role="${role}"]`)
  const formHost = $('form')
  const jobsHost = $('jobs')
  const topbarHost = $('topbar')
  const noticeHost = $('notice')

  const notify = (tone, message) => {
    state.notice = message || ''
    state.noticeTone = tone || 'info'
    if (message) sdk.toast?.[tone === 'error' ? 'error' : tone === 'warning' ? 'warning' : tone === 'success' ? 'success' : 'info']?.(message)
    renderNotice()
  }

  const escapeHtml = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
  const modeOptions = [
    { label: '硬链接', value: '0' },
    { label: '复制', value: '1' },
    { label: '移动', value: '2' },
    { label: '原地整理', value: '3' },
    { label: '软链接', value: '4' },
  ]

  function jobsForPage() {
    const start = (state.page - 1) * state.pageSize
    return state.jobs.slice(start, start + state.pageSize)
  }

  function renderNotice() {
    noticeHost.innerHTML = ''
    if (!state.notice) return
    noticeHost.appendChild(sdk.ui?.notice
      ? sdk.ui.notice({ text: state.notice, tone: state.noticeTone })
      : (() => {
          const box = document.createElement('div')
          box.className = `mdc-notice mdc-notice--${state.noticeTone}`
          box.textContent = state.notice
          return box
        })())
  }

  function renderTopbar() {
    topbarHost.innerHTML = ''
    const stats = [
      sdk.ui?.badge ? sdk.ui.badge({ label: `总任务 ${state.stats.total}`, tone: 'info' }) : null,
      sdk.ui?.badge ? sdk.ui.badge({ label: `执行中 ${state.stats.running}`, tone: state.stats.running ? 'primary' : 'info' }) : null,
      sdk.ui?.badge ? sdk.ui.badge({ label: `已完成 ${state.stats.finished}`, tone: 'success' }) : null,
      sdk.ui?.badge ? sdk.ui.badge({ label: `失败/终止 ${state.stats.failed}`, tone: state.stats.failed ? 'danger' : 'info' }) : null,
    ].filter(Boolean)
    const refreshBtn = sdk.ui?.button
      ? sdk.ui.button({ label: state.refreshing ? '刷新中…' : '刷新', tone: 'primary', disabled: state.refreshing, onClick: () => loadOverview(true) })
      : (() => {
          const btn = document.createElement('button')
          btn.textContent = state.refreshing ? '刷新中…' : '刷新'
          btn.onclick = () => loadOverview(true)
          return btn
        })()
    const openBtn = sdk.ui?.button
      ? sdk.ui.button({ label: '打开 MDC-NG', onClick: () => window.open(`${state.baseUrl || ''}/manual-jobs`, '_blank', 'noopener') })
      : (() => {
          const btn = document.createElement('button')
          btn.textContent = '打开 MDC-NG'
          btn.onclick = () => window.open(`${state.baseUrl || ''}/manual-jobs`, '_blank', 'noopener')
          return btn
        })()
    if (sdk.ui?.topBar) {
      const topbar = sdk.ui.topBar({ tabs: stats, actions: [refreshBtn, openBtn] })
      topbarHost.appendChild(topbar?.el || topbar)
      return
    }
    const bar = document.createElement('div')
    bar.className = 'noor-plugin-topbar'
    const left = document.createElement('div')
    left.className = 'noor-plugin-topbar__tabs'
    stats.forEach(node => left.appendChild(node))
    const right = document.createElement('div')
    right.className = 'noor-plugin-topbar__actions'
    right.append(refreshBtn, openBtn)
    bar.append(left, right)
    topbarHost.appendChild(bar)
  }

  function renderForm() {
    formHost.innerHTML = ''
    const card = document.createElement('div')
    card.className = 'mdc-panel'

    const title = document.createElement('div')
    title.className = 'mdc-panel__title'
    title.textContent = '创建手动任务'

    const hint = document.createElement('div')
    hint.className = 'mdc-panel__hint'
    hint.textContent = '每行一个源路径；提交后任务仍由 MDC-NG 后台顺序执行。'

    const fields = document.createElement('div')
    fields.className = 'mdc-fields'

    const sourceControl = sdk.ui?.textarea
      ? sdk.ui.textarea({ value: state.sourcePaths, rows: 7, placeholder: '/data/downloads/av\n/data/downloads/av/ABC-123', onInput: value => { state.sourcePaths = value } })
      : (() => {
          const input = document.createElement('textarea')
          input.rows = 7
          input.value = state.sourcePaths
          input.placeholder = '/data/downloads/av'
          input.oninput = e => { state.sourcePaths = e.target.value }
          return input
        })()
    const targetControl = sdk.ui?.input
      ? sdk.ui.input({ value: state.targetFolder, placeholder: '/data/media/av', onInput: value => { state.targetFolder = value } })
      : (() => {
          const input = document.createElement('input')
          input.value = state.targetFolder
          input.placeholder = '/data/media/av'
          input.oninput = e => { state.targetFolder = e.target.value }
          return input
        })()
    const modeControl = sdk.ui?.select
      ? sdk.ui.select({ value: state.linkMode, options: modeOptions, onChange: value => { state.linkMode = String(value); renderForm() } })
      : (() => {
          const sel = document.createElement('select')
          modeOptions.forEach(opt => {
            const o = document.createElement('option')
            o.value = opt.value
            o.textContent = opt.label
            if (opt.value === state.linkMode) o.selected = true
            sel.appendChild(o)
          })
          sel.onchange = e => { state.linkMode = e.target.value; renderForm() }
          return sel
        })()

    const watchOptions = [{ label: '不复用', value: '-1' }, ...(state.defaults.watch_dirs || []).map(item => ({ label: item.path || `监控目录 ${item.index}`, value: String(item.index) }))]
    const reuseControl = sdk.ui?.select
      ? sdk.ui.select({ value: state.reuseWatchIndex, options: watchOptions, onChange: value => { state.reuseWatchIndex = String(value) } })
      : (() => {
          const sel = document.createElement('select')
          watchOptions.forEach(opt => {
            const o = document.createElement('option')
            o.value = opt.value
            o.textContent = opt.label
            if (opt.value === state.reuseWatchIndex) o.selected = true
            sel.appendChild(o)
          })
          sel.onchange = e => { state.reuseWatchIndex = e.target.value }
          return sel
        })()

    fields.append(
      sdk.ui?.field ? sdk.ui.field({ label: '刮削路径', hint: '支持多行批量提交。', control: sourceControl }) : sourceControl,
      sdk.ui?.field ? sdk.ui.field({ label: '整理目录', hint: `默认值来自 MDC-NG：${state.defaults.target_folder || '未读取到'}`, control: targetControl }) : targetControl,
      sdk.ui?.field ? sdk.ui.field({ label: '整理模式', hint: '与 MDC-NG 手动任务页一致。', control: modeControl }) : modeControl,
      sdk.ui?.field ? sdk.ui.field({ label: '配置复用', hint: '仅复用监控目录的高级规则；不改写当前表单值。', control: reuseControl }) : reuseControl,
    )

    if (state.linkMode === '2') {
      const moveRow = document.createElement('label')
      moveRow.className = 'mdc-checkbox'
      const box = document.createElement('input')
      box.type = 'checkbox'
      box.checked = !!state.deleteEmptyParentAfterMove
      box.onchange = e => { state.deleteEmptyParentAfterMove = !!e.target.checked }
      const text = document.createElement('span')
      text.textContent = '移动后自动删除空目录'
      moveRow.append(box, text)
      fields.appendChild(moveRow)
    }

    const footer = document.createElement('div')
    footer.className = 'mdc-form__footer'
    const defaultsBtn = sdk.ui?.button
      ? sdk.ui.button({ label: '恢复默认值', onClick: () => { applyDefaults(); renderForm() } })
      : (() => {
          const btn = document.createElement('button')
          btn.textContent = '恢复默认值'
          btn.onclick = () => { applyDefaults(); renderForm() }
          return btn
        })()
    const submitBtn = sdk.ui?.submitButton
      ? sdk.ui.submitButton({
          idleLabel: '提交任务',
          status: state.submitStatus,
          progress: state.submitProgress,
          disabled: state.submitStatus === 'running',
          onClick: submit,
        })
      : (() => {
          const btn = document.createElement('button')
          btn.textContent = state.submitStatus === 'running' ? '提交中…' : state.submitStatus === 'success' ? '提交成功' : '提交任务'
          btn.disabled = state.submitStatus === 'running'
          btn.onclick = submit
          return btn
        })()
    footer.append(defaultsBtn, submitBtn)

    card.append(title, hint, fields, footer)
    formHost.appendChild(card)
  }

  function renderJobs() {
    jobsHost.innerHTML = ''
    const card = document.createElement('div')
    card.className = 'mdc-panel'

    const head = document.createElement('div')
    head.className = 'mdc-panel__header'
    head.innerHTML = `<div><div class="mdc-panel__title">最近任务</div><div class="mdc-panel__hint">只展示最近 50 条，从 MDC-NG 页面解析。</div></div>`

    const body = document.createElement('div')
    body.className = 'mdc-jobs__body'

    if (state.loading) {
      body.appendChild(sdk.ui?.loadingState ? sdk.ui.loadingState({ text: '加载中…' }) : document.createTextNode('加载中…'))
    } else if (state.error) {
      body.appendChild(sdk.ui?.errorState ? sdk.ui.errorState({ text: state.error }) : document.createTextNode(state.error))
    } else if (!state.jobs.length) {
      body.appendChild(sdk.ui?.emptyState ? sdk.ui.emptyState({ text: '暂无任务' }) : document.createTextNode('暂无任务'))
    } else {
      for (const job of jobsForPage()) {
        const row = document.createElement('article')
        row.className = 'mdc-job'
        const sources = Array.isArray(job.source_paths) ? job.source_paths : []
        const sourceHtml = sources.length ? sources.map(item => `<div class="mdc-job__path">${escapeHtml(item)}</div>`).join('') : '<div class="mdc-job__path">-</div>'
        row.innerHTML = `
          <div class="mdc-job__main">
            <div class="mdc-job__meta">
              <strong>#${escapeHtml(job.id)}</strong>
              <span>${escapeHtml(job.link_mode_label || '')}</span>
              <span>${escapeHtml(job.created_at || '')}</span>
              <span>${escapeHtml(job.duration || '')}</span>
            </div>
            <div class="mdc-job__paths">${sourceHtml}</div>
            <div class="mdc-job__target">→ ${escapeHtml(job.target_dir || '')}</div>
            <div class="mdc-job__progress">
              <span>成功 ${job.finish_count || 0}</span>
              <span>跳过 ${job.skip_count || 0}</span>
              <span>错误 ${job.error_count || 0}</span>
              <span>总数 ${job.total_count || 0}</span>
            </div>
            ${job.error_message ? `<div class="mdc-job__error">${escapeHtml(job.error_message)}</div>` : ''}
          </div>
          <div class="mdc-job__side">
            <div class="mdc-job__status mdc-job__status--${job.status === 2 ? 'success' : job.status === 1 || job.status === 0 || job.status === 3 || job.status === 4 ? 'running' : 'danger'}">${escapeHtml(job.status_label || '')}</div>
            <div class="mdc-job__actions"></div>
          </div>
        `
        const actions = row.querySelector('.mdc-job__actions')
        const openTask = sdk.ui?.button
          ? sdk.ui.button({ label: '查看任务', onClick: () => window.open(job.tasks_url, '_blank', 'noopener') })
          : document.createElement('button')
        if (!sdk.ui?.button) {
          openTask.textContent = '查看任务'
          openTask.onclick = () => window.open(job.tasks_url, '_blank', 'noopener')
        }
        actions.appendChild(openTask)
        body.appendChild(row)
      }
    }

    const totalPages = Math.max(1, Math.ceil(state.jobs.length / state.pageSize))
    const pager = document.createElement('div')
    pager.className = 'mdc-jobs__pager'
    if (totalPages > 1 && sdk.ui?.pagination) {
      pager.appendChild(sdk.ui.pagination({ page: state.page, totalPages, onPage: page => { state.page = page; renderJobs() } }))
    }
    card.append(head, body, pager)
    jobsHost.appendChild(card)
  }

  function renderAll() {
    renderTopbar()
    renderNotice()
    renderForm()
    renderJobs()
  }

  function applyDefaults() {
    state.targetFolder = state.defaults.target_folder || ''
    state.linkMode = String(state.defaults.link_mode ?? 0)
    state.deleteEmptyParentAfterMove = !!state.defaults.delete_empty_parent_after_move
    state.reuseWatchIndex = '-1'
  }

  async function loadOverview(showToast = false) {
    state.refreshing = true
    if (!state.jobs.length) state.loading = true
    state.error = ''
    renderAll()
    try {
      const resp = await apiPlugin(`/actions/overview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload: {} }),
      })
      const data = await resp.json()
      if (!resp.ok || data?.ok === false) throw new Error(data?.detail || data?.message || '插件未启用')
      state.defaults = data.defaults || { target_folder: '', link_mode: 0, watch_dirs: [] }
      state.baseUrl = data.base_url || state.baseUrl
      state.jobs = Array.isArray(data.jobs) ? data.jobs : []
      state.stats = data.stats || { total: state.jobs.length, running: 0, finished: 0, failed: 0 }
      if (!state.targetFolder) applyDefaults()
      if (state.page > Math.max(1, Math.ceil(state.jobs.length / state.pageSize))) state.page = 1
      if (showToast) notify('success', 'MDC-NG 任务列表已刷新')
    } catch (e) {
      state.error = e?.message || '加载失败'
      if (showToast) notify('error', state.error)
    } finally {
      state.loading = false
      state.refreshing = false
      renderAll()
    }
  }

  async function submit() {
    state.submitStatus = 'running'
    state.submitProgress = 30
    renderForm()
    try {
      const resp = await apiPlugin(`/actions/create`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          payload: {
            source_paths: state.sourcePaths,
            target_folder: state.targetFolder,
            link_mode: Number(state.linkMode),
            reuse_watch_index: state.reuseWatchIndex,
            delete_empty_parent_after_move: !!state.deleteEmptyParentAfterMove,
          },
        }),
      })
      const data = await resp.json()
      if (!resp.ok || !data?.ok) {
        const msg = data?.message || data?.detail || '提交失败'
        throw new Error(msg)
      }
      state.submitProgress = 100
      state.submitStatus = 'success'
      notify('success', data.message || '任务已提交到 MDC-NG')
      state.jobs = Array.isArray(data.jobs) ? data.jobs : state.jobs
      state.page = 1
      renderAll()
      window.setTimeout(() => loadOverview(false), 800)
    } catch (e) {
      state.submitStatus = 'error'
      state.submitProgress = 100
      notify('error', e?.message || '提交失败')
      renderForm()
    }
  }

  await loadOverview(false)
  state.timer = window.setInterval(() => loadOverview(false), 10000)
  return () => {
    if (state.timer) window.clearInterval(state.timer)
  }
}
