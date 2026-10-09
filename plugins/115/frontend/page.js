function fmtBytes(value) {
  const size = Number(value || 0)
  if (!size) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const index = Math.min(units.length - 1, Math.floor(Math.log(size) / Math.log(1024)))
  return `${(size / 1024 ** index).toFixed(index > 2 ? 2 : 1)} ${units[index]}`
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char])
}

function labelStatus(value) {
  return ({ completed: '完成', downloading: '下载中', queued: '排队', failed: '失败', created: '就绪', ready: '就绪', waiting: '等待', partial: '处理中', waiting_mediainfo: '等待 MediaInfo', notified: '已通知', pending: '等待' })[value] || value || '等待'
}

export async function mount(root, sdk) {
  const state = {
    account: null, pipeline: null, config: {}, projects: [], assistant: null, assistantSettings: null, assistantTasks: [],
    authUid: '', authQr: '', authMessage: '', polling: false, pollErrors: 0,
    selectedStage: {},
    importFolder: { id: '', trail: [], items: [], selected: new Map() },
  }
  let pollTimer = null
  root.innerHTML = ''
  const page = sdk.ui?.page ? sdk.ui.page({ className: 'noor-plugin-115-page' }) : document.createElement('div')
  page.classList.add('noor-plugin-115-page')
  const panels = {}
  const panel = document.createElement('section')
  panel.dataset.panel = 'overview'
  panel.className = 'noor-plugin-115-panel'
  panels.overview = panel
  page.append(panel)
  root.append(page)

  function action(name, payload = {}) { return sdk.api.post(`/plugins/115/actions/${name}`, { payload }).then(response => response?.data || response) }
  function toastError(error, fallback) { sdk.toast?.error?.(error?.response?.data?.detail || error?.message || fallback) }
  function stopPolling() { state.polling = false; if (pollTimer) window.clearTimeout(pollTimer); pollTimer = null }

  async function loadAll() {
    await loadOverview()
    await Promise.all([
      loadSettingsData(),
      state.account?.connected ? loadAssistantData() : Promise.resolve(),
    ])
    renderOverview()
  }

  async function connect() {
    try {
      const data = await action('auth_start')
      Object.assign(state, { authUid: data.uid || '', authQr: data.qrcode_image || '', authMessage: '请使用 115 App 扫描并确认授权', polling: true, pollErrors: 0 })
      renderOverview(); pollAuth()
    } catch (error) { toastError(error, '无法开始 115 授权') }
  }

  async function pollAuth() {
    if (!state.polling || !state.authUid) return
    try {
      const data = await action('auth_poll', { uid: state.authUid })
      state.pollErrors = 0
      if (data.connected) { stopPolling(); sdk.toast?.success?.('115 已连接'); await loadOverview(); return }
      state.authMessage = data.message || '等待扫码确认'
      if (data.terminal) { stopPolling(); renderOverview(); return }
    } catch (error) {
      state.pollErrors += 1
      state.authMessage = error?.response?.data?.detail || error?.message || '授权状态读取失败'
      if (state.pollErrors >= 3) { stopPolling(); renderOverview(); return }
    }
    pollTimer = window.setTimeout(pollAuth, 2000)
  }

  async function disconnect() {
    const confirmed = await sdk.ui?.confirm?.({ title: '断开 115', message: '将删除 NOOR 保存的 115 OAuth Token。', confirmText: '断开', danger: true })
    if (confirmed === false) return
    await action('disconnect'); stopPolling(); state.account = { connected: false }; renderOverview()
  }

  async function loadOverview() {
    panels.overview.innerHTML = '<div class="noor-plugin-115-empty"><strong>正在汇总 115 工作流</strong></div>'
    try {
      state.account = await action('status')
      if (state.account.connected) {
        const [pipeline, projects] = await Promise.all([
          action('pipeline_status'),
          action('projects', { limit: 100 }),
        ])
        state.pipeline = pipeline
        state.projects = projects.items || []
      } else {
        state.pipeline = null
        state.projects = []
      }
    } catch (error) { state.account = { connected: false, status: 'service_error', message: error?.response?.data?.detail || error?.message || '插件服务暂时不可用' } }
    renderOverview()
  }

  function renderOverview() {
    const panel = panels.overview
    const account = state.account || {}
    if (state.polling) {
      panel.innerHTML = `<section class="noor-plugin-115-connect-card"><header><div class="noor-plugin-115-connect-brand"><i>115</i><div><span>115 CLOUD</span><h2>扫码连接</h2></div></div><em class="noor-plugin-115-status is-running">等待确认</em></header><div class="noor-plugin-115-connect-body"><div><strong>使用 115 App 扫一扫</strong><p>${esc(state.authMessage)}</p><button data-action="connect">重新获取二维码</button></div>${state.authQr ? `<div class="noor-plugin-115-qr"><img src="${esc(state.authQr)}" alt="115 授权二维码"><small>115 App · 扫一扫</small></div>` : ''}</div></section>`
      bindOverview(); return
    }
    if (account.status === 'service_error') {
      panel.innerHTML = `<section class="noor-plugin-115-connect-card is-error"><header><div class="noor-plugin-115-connect-brand"><i>115</i><div><span>PLUGIN SERVICE</span><h2>115 服务暂时不可用</h2></div></div><em class="noor-plugin-115-status is-failed">服务异常</em></header><div class="noor-plugin-115-connect-error"><p>${esc(account.message)}</p><button data-action="refresh">重新载入</button></div></section>`
      bindOverview(); return
    }
    if (!account.connected) {
      const accessLimited = account.status === 'access_limit'
      const tokenError = ['token_error', 'risk_control'].includes(account.status)
      const title = accessLimited ? '115 访问暂时受限' : tokenError ? '重新连接账号' : '连接 115'
      const badge = accessLimited ? '访问受限' : tokenError ? '授权失效' : '未连接'
      const detail = accessLimited ? esc(account.message || '115 Open API 已达到当前访问上限，NOOR 已降低后台检查频率，请稍后重试。') : tokenError ? esc(account.message || '当前授权已失效，请重新扫码。') : '授权后即可使用云下载、已有媒体导入和 STRM 播放。'
      const primary = accessLimited ? '<button class="is-primary" data-action="refresh">稍后重试</button>' : '<button class="is-primary" data-action="connect">扫码连接</button>'
      panel.innerHTML = `<section class="noor-plugin-115-connect-card"><header><div class="noor-plugin-115-connect-brand"><i>115</i><div><span>115 CLOUD</span><h2>${title}</h2></div></div><em class="noor-plugin-115-status ${accessLimited || tokenError ? 'is-failed' : 'is-waiting'}">${badge}</em></header><div class="noor-plugin-115-connect-body"><div><strong>${accessLimited ? '无需重新扫码' : '使用 115 App 扫码授权'}</strong><p>${detail}</p><small>${state.config.client_id ? 'Client ID 已配置' : '连接前请先在设置中填写 Client ID'}</small><div>${primary}<button data-action="open-settings">设置</button></div></div><div class="noor-plugin-115-connect-mark"><b>115</b><span>OPEN PLATFORM</span></div></div></section>`
      bindOverview(); return
    }
    const pipeline = state.pipeline || {}
    const assistant = pipeline.strm_assistant || {}
    const percent = Math.min(100, Number(account.space?.total) ? Number(account.space?.used || 0) / Number(account.space.total) * 100 : 0)
    panel.innerHTML = `<div class="noor-plugin-115-primary-grid"><section class="noor-plugin-115-hero"><header><div><span>115 CLOUD</span><h2>115 网盘</h2></div><em class="noor-plugin-115-card-state is-ready">已连接</em></header><div class="noor-plugin-115-profile"><img src="${esc(account.avatar)}" alt=""><div><strong>${esc(account.user_name || account.user_id || '115 用户')}</strong><p>${esc(account.vip || '115')} · 已用 ${fmtBytes(account.space?.used)} / ${fmtBytes(account.space?.total)}</p></div></div><div class="noor-plugin-115-space"><i><b style="width:${percent}%"></b></i><span>剩余 ${fmtBytes(account.space?.remaining)}</span></div><div class="noor-plugin-115-account-actions"><button class="is-primary" data-action="cloud-downloads">云下载</button><button class="is-quiet" data-action="disconnect">断开账号</button><button class="is-quiet" data-action="open-settings">设置</button><button class="is-quiet" data-action="refresh">刷新</button></div></section><section class="noor-plugin-115-assistant-card ${assistant.available ? 'is-ready' : 'is-warning'}"><header><div><span>STRM ASSISTANT</span><h2>神医助手 <small>${assistant.available ? `社区版 ${esc(assistant.version)}` : '未检测到插件'}</small></h2></div><em class="noor-plugin-115-card-state ${assistant.available ? 'is-ready' : 'is-warning'}">${assistant.available ? '已安装' : '未安装'}</em></header><dl><div><dt>运行状态</dt><dd>${assistant.available ? '可用' : '不可用'}</dd></div><div><dt>配置管理</dt><dd>${assistant.settings_writable ? 'NOOR 可管理' : '只读'}</dd></div><div><dt>MediaInfo</dt><dd>${assistant.available ? 'Sidecar 恢复' : '未接入'}</dd></div></dl><button data-action="open-assistant">管理神医助手</button></section></div>${renderOverviewProjects()}`
    bindOverview()
  }

  function bindOverview() {
    panels.overview.querySelector('[data-action="connect"]')?.addEventListener('click', connect)
    panels.overview.querySelector('[data-action="disconnect"]')?.addEventListener('click', disconnect)
    panels.overview.querySelector('[data-action="refresh"]')?.addEventListener('click', loadAll)
    panels.overview.querySelector('[data-action="cloud-downloads"]')?.addEventListener('click', () => openCloudDownloadsModal())
    panels.overview.querySelector('[data-action="open-assistant"]')?.addEventListener('click', openAssistantModal)
    panels.overview.querySelector('[data-action="open-settings"]')?.addEventListener('click', openSettingsModal)
    panels.overview.querySelectorAll('[data-project-stage]').forEach(button => button.addEventListener('click', () => {
      const projectId = button.dataset.projectId
      const stage = button.dataset.projectStage
      state.selectedStage[projectId] = state.selectedStage[projectId] === stage ? '' : stage
      renderOverview()
    }))
    panels.overview.querySelectorAll('[data-retry-project]').forEach(button => button.addEventListener('click', async () => {
      const confirmed = await sdk.ui?.confirm?.({ title: '重试工程', message: '将使用已经发现的 115 文件重新执行未完成的处理，不会再次提交离线下载。', confirmText: '重试' })
      if (confirmed === false) return
      try { await action('retry_pipeline', { task_id: button.dataset.retryProject }); sdk.toast?.success?.('工程已重新进入处理队列'); await loadAll() } catch (error) { toastError(error, '工程重试失败') }
    }))
    panels.overview.querySelector('[data-action="sync-projects"]')?.addEventListener('click', async () => {
      await action('sync_tasks')
      await action('sync_organized')
      await loadOverview()
    })
  }

  function renderOverviewProjects() {
    return `<section class="noor-plugin-115-project-section"><div class="noor-plugin-115-section-head"><div><h2>离线工程</h2></div><button data-action="sync-projects">同步状态</button></div>${state.projects.length ? `<div class="noor-plugin-115-projects">${state.projects.map(renderProject).join('')}</div>` : '<div class="noor-plugin-115-empty is-compact"><strong>暂无离线工程</strong></div>'}</section>`
  }

  function renderProject(item) {
    const stages = [['offline', '离线'], ['discover', '发现'], ['strm', 'STRM'], ['mediainfo', 'MediaInfo'], ['organize', '整理'], ['assistant', '神医'], ['emby', 'Emby']]
    const media = item.media?.[0] || {}
    const blocked = projectBlocker(item, media)
    const selected = state.selectedStage[item.id] || ''
    const cover = item.cover_url ? `<img src="${esc(item.cover_url)}" alt="${esc(item.name)}">` : `<div class="noor-plugin-115-cover-fallback">${esc(item.name || '115')}</div>`
    return `<article class="noor-plugin-115-project"><div class="noor-plugin-115-project-poster"><div class="noor-plugin-115-project-cover">${cover}</div></div><div class="noor-plugin-115-project-body"><header><div><span>${esc(String(item.source_kind || '').toUpperCase())}</span><strong>${esc(item.name || item.source_hint || item.id)}</strong><small>${esc(item.created_at)} · ${fmtBytes(media.size || 0)}</small></div></header><div class="noor-plugin-115-stage-line">${stages.map(([key, label]) => `<button data-project-stage="${key}" data-project-id="${esc(item.id)}" class="is-${esc(item.stages?.[key])} ${selected === key ? 'is-selected' : ''}"><span>${label}</span><small>${labelStatus(item.stages?.[key])}</small></button>`).join('')}</div>${selected ? renderStageDetail(selected, item, media, blocked) : ''}</div></article>`
  }

  function renderStageDetail(stage, item, media, blocked) {
    const details = {
      offline: ['115 离线下载', item.error || `创建于 ${item.created_at || '—'}；${item.completed_at ? `完成于 ${item.completed_at}` : `当前进度 ${Number(item.progress || 0)}%`}`],
      discover: ['结果文件识别', media.name ? `${media.name} · file_id ${media.file_id}` : (item.error || '尚未发现与该工程匹配的视频文件。')],
      strm: ['稳定 STRM', media.strm_path || '尚未生成 STRM。'],
      mediainfo: ['MediaInfo', media.mediainfo_error || (media.mediainfo_probed_at ? `已于 ${media.mediainfo_probed_at} 完成；累计尝试 ${media.mediainfo_attempts || 1} 次。` : '等待媒体流信息提取。')],
      organize: ['本地整理', media.organized_path || '等待 MDC-NG 完成分类和刮削。'],
      assistant: ['神医助手', media.strm_assistant_error || (media.strm_assistant_status === 'ready' ? 'Sidecar 已准备，可以复用 NOOR MediaInfo。' : '等待生成或恢复 Sidecar。')],
      emby: ['Emby 入库', media.emby_status === 'notified' ? '已完成定向刷新通知。' : '等待向 Emby 发送定向刷新。'],
    }
    const [title, detail] = details[stage] || [blocked.title, blocked.detail]
    const canRetry = item.status === 'completed' && !['ready', 'completed', 'notified', 'created', 'reused'].includes(String(item.stages?.[stage] || ''))
    return `<div class="noor-plugin-115-stage-detail"><div><strong>${esc(title)}</strong><p>${esc(detail)}</p></div>${canRetry ? `<button data-retry-project="${esc(item.id)}">重试工程</button>` : ''}</div>`
  }

  function projectBlocker(item, media) {
    if (item.error) return { tone: 'failed', label: '发生错误', title: '工程执行失败', detail: item.error }
    if (item.status === 'queued') return { tone: 'waiting', label: '等待离线', title: '等待 115 接收任务', detail: '任务已提交，尚未开始下载。' }
    if (item.status === 'downloading') return { tone: 'running', label: `${Number(item.progress || 0)}%`, title: '115 正在离线下载', detail: `当前进度 ${Number(item.progress || 0)}%，完成后会自动发现视频。` }
    if (!item.media?.length) return { tone: 'waiting', label: '发现文件', title: '正在识别任务结果', detail: '离线已完成，正在定位与该番号匹配的视频文件。' }
    if (media.mediainfo_status !== 'ready') return { tone: media.mediainfo_status === 'failed' ? 'failed' : 'running', label: 'MediaInfo', title: '正在读取媒体流信息', detail: media.mediainfo_status === 'failed' ? '远程媒体探测失败，可同步状态后重试。' : 'NOOR 正在通过 Range 提取编码、分辨率、音轨和时长。' }
    if (media.organization_status !== 'completed') return { tone: 'waiting', label: '等待整理', title: '等待 MDC-NG 分类', detail: 'STRM 与 MediaInfo 已就绪，等待 MDC-NG 完成刮削和分类。' }
    if (media.strm_assistant_status !== 'ready') return { tone: 'running', label: '神医恢复', title: '正在准备神医 Sidecar', detail: media.strm_assistant_error || '将 NOOR MediaInfo 转换成神医助手可恢复的格式。' }
    if (media.emby_status !== 'notified') return { tone: 'waiting', label: '等待入库', title: '等待 Emby 入库', detail: '本地媒体已经整理完成，正在向 Emby 发送定向刷新。' }
    return { tone: 'ready', label: '已完成', title: '工程已完成', detail: '离线、STRM、MediaInfo、整理和 Emby 入库均已完成。' }
  }

  async function loadAssistantData() {
    try {
      const [status, settings, tasks] = await Promise.all([action('strm_assistant_status'), action('strm_assistant_settings'), action('strm_assistant_tasks')])
      state.assistant = status; state.assistantSettings = settings; state.assistantTasks = tasks.items || []
    } catch (error) { state.assistant = { available: false, error: error?.message }; state.assistantSettings = null; state.assistantTasks = [] }
  }

  function renderAssistantSettings(settings) {
    const groups = [...new Set((settings.definitions || []).map(item => item.group))]
    const wrap = document.createElement('div'); wrap.className = 'noor-plugin-115-assistant-config'
    const pluginSwitch = (key, checked, disabled = false) => { const control = sdk.ui.settingsSwitch({ checked, disabled }); control.input.dataset.pluginConfig = key; return control }
    wrap.append(sdk.ui.settingsCard({ children: [
      sdk.ui.settingRow({ label: '启用神医助手', description: '检测到社区版插件后默认启用；关闭后 NOOR 不再生成或恢复神医 Sidecar。', control: pluginSwitch('strm_assistant_enabled', state.config.strm_assistant_enabled !== false && state.assistant?.available, !state.assistant?.available) }),
      sdk.ui.settingRow({ label: '生成 MediaInfo', description: '新增媒体只探测一次并缓存，供神医助手恢复媒体流信息。', control: pluginSwitch('mediainfo_enabled', state.config.mediainfo_enabled !== false) }),
    ] }))
    groups.forEach((group, index) => wrap.append(sdk.ui.settingsCard({ title: group, description: assistantGroupHelp(group), collapsible: true, open: index < 2, children: settings.definitions.filter(item => item.group === group).map(item => sdk.ui.settingRow({ label: item.label, description: item.help, control: assistantControl(item, settings.values?.[item.id]) })) })))
    const footer = document.createElement('footer'); footer.className = 'noor-plugin-115-settings-actions'; const save = document.createElement('button'); save.dataset.action = 'save-assistant'; save.disabled = !settings.writable; save.textContent = '保存设置'; footer.append(save); wrap.append(footer)
    return wrap
  }

  function assistantGroupHelp(group) {
    return ({
      入库: '决定新媒体加入 Emby 时是否自动处理',
      性能: '控制读取速度；115 推荐远程并发保持 1',
      MediaInfo: '决定是否复用 NOOR 已准备的视频流信息',
      媒体库: '多版本等 Emby 展示增强',
      片头片尾: '主要面向连续剧，对 AV 电影没有必要',
      元数据: '剧集元数据维护，对 AV 电影通常无需调整',
    })[group] || '高级功能，不确定时保持默认'
  }

  function assistantControl(definition, value) {
    let control
    if (definition.type === 'boolean') { const toggle = sdk.ui.settingsSwitch({ checked: Boolean(value) }); control = toggle; toggle.input.dataset.setting = definition.id }
    else if (definition.type === 'select') { control = document.createElement('select'); control.dataset.setting = definition.id; for (const item of definition.options || []) { const option = document.createElement('option'); option.value = item.value; option.textContent = item.label; option.selected = item.value === value; control.append(option) } }
    else { control = document.createElement('input'); control.dataset.setting = definition.id; control.type = definition.type === 'number' ? 'number' : 'text'; control.value = value ?? ''; if (definition.min != null) control.min = definition.min; if (definition.max != null) control.max = definition.max }
    if (!String(definition.id).includes('root')) return control
    const path = sdk.ui.settingPath({ value, onBrowse: target => openLocalDirectoryPicker(target) }); path.input.dataset.setting = definition.id; return path
  }

  function renderAssistantTasks(tasks) {
    return `<section class="noor-plugin-115-assistant-maintenance"><header><div><h3>维护任务</h3></div></header><div class="noor-plugin-115-assistant-tasks">${tasks.map(task => `<article><div><strong>${esc(task.label)}</strong><span>${esc(task.description)}</span><small>最近结果：${esc(task.last_status || '尚未执行')} ${esc(task.last_ended_at || '')}</small></div><em class="is-${esc(task.state)}">${task.state === 'running' ? '运行中' : '空闲'}</em><button data-run-task="${esc(task.id)}" data-risk="${esc(task.risk)}" data-label="${esc(task.label)}" ${task.state === 'running' ? 'disabled' : ''}>${task.state === 'running' ? `${task.progress.toFixed(0)}%` : '手动运行'}</button></article>`).join('')}</div></section>`
  }

  function bindAssistant(panel, modal) {
    panel.querySelector('[data-action="recommended"]')?.addEventListener('click', () => {
      for (const [key, value] of Object.entries(state.assistantSettings.recommended || {})) {
        const input = panel.querySelector(`[data-setting="${key}"]`); if (!input) continue
        if (input.type === 'checkbox') input.checked = Boolean(value); else input.value = value
      }
    })
    panel.querySelector('[data-action="save-assistant"]')?.addEventListener('click', async () => {
      const values = {}; panel.querySelectorAll('[data-setting]').forEach(input => { values[input.dataset.setting] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value })
      try {
        const pluginConfig = { ...state.config }; panel.querySelectorAll('[data-plugin-config]').forEach(input => { pluginConfig[input.dataset.pluginConfig] = input.checked })
        await savePluginConfig(pluginConfig)
        const result = await action('strm_assistant_update_settings', { values })
        sdk.toast?.success?.(result.restart_required ? '设置已保存；部分选项需重启 Emby 生效' : '神医助手设置已保存')
        await loadAssistantData(); modal?.close?.(); openAssistantModal()
      } catch (error) { toastError(error, '保存失败') }
    })
    panel.querySelectorAll('[data-pick-path]').forEach(button => button.addEventListener('click', () => openLocalDirectoryPicker(panel.querySelector(`[data-setting="${button.dataset.pickPath}"]`))))
    panel.querySelectorAll('[data-run-task]').forEach(button => button.addEventListener('click', async () => {
      const warning = button.dataset.risk === 'high' ? '该任务可能读取大量远程媒体。确定现在执行？' : '确定立即执行该神医助手任务？'
      const confirmed = await sdk.ui?.confirm?.({ title: button.dataset.label, message: warning, confirmText: '执行' })
      if (confirmed === false) return
      try { await action('strm_assistant_run_task', { task_id: button.dataset.runTask }); sdk.toast?.success?.('任务已提交'); await loadAssistantData(); modal?.close?.(); openAssistantModal() } catch (error) { toastError(error, '任务启动失败') }
    }))
  }

  async function loadSettingsData() {
    try {
      const info = await sdk.api.get('/plugins/115/config').then(response => response?.data || response)
      state.config = info.config || {}
    } catch (error) { state.config = {}; toastError(error, '设置读取失败') }
  }

  function modalButton(label, onClick, tone = 'default') {
    if (sdk.ui?.button) return sdk.ui.button({ label, tone, onClick })
    const button = document.createElement('button'); button.textContent = label; button.addEventListener('click', onClick); return button
  }

  function openCloudDownloadsModal(filter = 'all') {
    const content = document.createElement('div')
    content.className = 'noor-plugin-115-modal noor-plugin-115-cloud-downloads'
    const filters = [['all', '全部'], ['active', '下载中'], ['completed', '已完成'], ['failed', '失败']]
    const items = state.projects.filter(item => filter === 'all' || (filter === 'active' ? ['queued', 'downloading'].includes(item.status) : item.status === filter))
    content.innerHTML = `<div class="noor-plugin-115-cloud-toolbar"><div>${filters.map(([key, label]) => `<button class="${filter === key ? 'is-active' : ''}" data-cloud-filter="${key}">${label}</button>`).join('')}</div><div class="noor-plugin-115-cloud-actions"><button data-action="import-existing">导入已有媒体</button><button class="is-primary" data-action="add-cloud-download">＋ 添加云下载</button></div></div><div class="noor-plugin-115-cloud-list">${items.length ? items.map(item => `<article><div class="noor-plugin-115-cloud-icon">${esc(String(item.source_kind || 'URL').toUpperCase())}</div><div><strong>${esc(item.name || item.source_hint || item.id)}</strong><span>${item.status === 'downloading' ? `${Number(item.progress || 0)}%` : labelStatus(item.status)} · ${esc(item.created_at || '')}</span></div><em class="noor-plugin-115-status is-${item.status === 'completed' ? 'ready' : item.status === 'failed' ? 'failed' : 'running'}">${labelStatus(item.status)}</em></article>`).join('') : '<div class="noor-plugin-115-empty is-compact"><strong>暂无任务</strong></div>'}</div>`
    let modal = sdk.ui.modal({ title: '云下载', content, width: 'lg', closeOnMask: false })
    content.querySelectorAll('[data-cloud-filter]').forEach(button => button.addEventListener('click', () => { modal.close(); openCloudDownloadsModal(button.dataset.cloudFilter) }))
    content.querySelector('[data-action="add-cloud-download"]')?.addEventListener('click', () => { modal.close(); openAddDownloadModal() })
    content.querySelector('[data-action="import-existing"]')?.addEventListener('click', () => { modal.close(); openExistingImportModal() })
  }

  async function openExistingImportModal() {
    const rootId = String(state.config.offline_directory_id || '0')
    const rootName = state.config.offline_directory_name || '默认离线目录'
    state.importFolder = { id: rootId, trail: [{ id: rootId, name: rootName }], items: [], selected: new Map() }
    const content = document.createElement('div')
    content.className = 'noor-plugin-115-modal noor-plugin-115-import-browser'
    state.importModal = sdk.ui.modal({ title: '导入已有媒体', content, width: 'lg', closeOnMask: false })
    await loadImportFolder(rootId, rootName, content)
  }

  async function loadImportFolder(id, name, content, trailIndex = null) {
    if (trailIndex !== null) state.importFolder.trail = state.importFolder.trail.slice(0, trailIndex + 1)
    else if (state.importFolder.trail.at(-1)?.id !== id) state.importFolder.trail.push({ id, name })
    state.importFolder.id = id
    content.innerHTML = '<div class="noor-plugin-115-empty is-compact"><strong>正在读取 115 目录</strong></div>'
    try {
      const data = await action('list_folder', { folder_id: id, limit: 500 })
      state.importFolder.items = data.items || []
      renderImportFolder(content)
    } catch (error) { content.innerHTML = `<div class="noor-plugin-115-empty is-compact"><strong>目录读取失败</strong><span>${esc(error?.message || '')}</span></div>` }
  }

  function renderImportFolder(content) {
    const selected = state.importFolder.selected
    content.innerHTML = `<div class="noor-plugin-115-import-head"><div class="noor-plugin-115-breadcrumbs">${state.importFolder.trail.map((item, index) => `<button data-import-crumb="${index}">${esc(item.name)}</button>`).join('<i>›</i>')}</div><div class="noor-plugin-115-import-selection"><span>已选 ${selected.size} 项</span><button data-import-select-all>全选</button><button data-import-invert>反选</button></div></div><div class="noor-plugin-115-import-list">${state.importFolder.items.length ? state.importFolder.items.map(item => `<article class="${item.imported ? 'is-imported' : ''}"><input type="checkbox" data-import-check="${esc(item.file_id)}" ${selected.has(item.file_id) ? 'checked' : ''} ${item.imported ? 'disabled' : ''}><button data-import-open="${item.is_directory ? esc(item.file_id) : ''}" data-import-name="${esc(item.name)}"><i>${item.is_directory ? '文件夹' : esc(String(item.extension || 'FILE').toUpperCase())}</i><span><strong>${esc(item.name)}</strong><small>${item.imported ? '已导入' : item.is_directory ? '点击进入目录' : fmtBytes(item.size)}</small></span></button></article>`).join('') : '<div class="noor-plugin-115-empty is-compact"><strong>当前目录为空</strong></div>'}</div><div class="noor-plugin-115-import-actions"><button data-import-back>返回云下载</button><button class="is-primary" data-import-submit ${selected.size ? '' : 'disabled'}>加入导入队列 ${selected.size || ''}</button></div>`
    content.querySelectorAll('[data-import-check]').forEach(input => input.addEventListener('change', () => {
      const item = state.importFolder.items.find(row => row.file_id === input.dataset.importCheck)
      if (input.checked && item) selected.set(item.file_id, item)
      else selected.delete(input.dataset.importCheck)
      renderImportFolder(content)
    }))
    content.querySelector('[data-import-select-all]')?.addEventListener('click', () => {
      state.importFolder.items.filter(item => !item.imported).forEach(item => selected.set(item.file_id, item))
      renderImportFolder(content)
    })
    content.querySelector('[data-import-invert]')?.addEventListener('click', () => {
      state.importFolder.items.filter(item => !item.imported).forEach(item => selected.has(item.file_id) ? selected.delete(item.file_id) : selected.set(item.file_id, item))
      renderImportFolder(content)
    })
    content.querySelectorAll('[data-import-open]').forEach(button => button.addEventListener('click', () => { if (button.dataset.importOpen) loadImportFolder(button.dataset.importOpen, button.dataset.importName, content) }))
    content.querySelectorAll('[data-import-crumb]').forEach(button => button.addEventListener('click', () => { const index = Number(button.dataset.importCrumb); const target = state.importFolder.trail[index]; loadImportFolder(target.id, target.name, content, index) }))
    content.querySelector('[data-import-back]')?.addEventListener('click', () => { state.importModal?.close?.(); openCloudDownloadsModal() })
    content.querySelector('[data-import-submit]')?.addEventListener('click', async button => {
      button.currentTarget.disabled = true
      try {
        const result = await action('import_existing', { items: [...selected.values()].map(item => ({ file_id: item.file_id, parent_id: item.parent_id, name: item.name, is_directory: item.is_directory })) })
        sdk.toast?.success?.(`已加入队列 ${result.queued || 0} 项，跳过 ${result.duplicates || 0} 项${result.failed ? `，失败 ${result.failed} 项` : ''}`)
        await loadAll(); state.importModal?.close?.()
      } catch (error) { button.currentTarget.disabled = false; toastError(error, '导入失败') }
    })
  }

  function openAddDownloadModal() {
    const content = document.createElement('div')
    content.className = 'noor-plugin-115-modal noor-plugin-115-download-form'
    content.innerHTML = `<label><span>离线链接</span><textarea data-download-url rows="5" placeholder="magnet、ed2k 或 http/https"></textarea></label><label><span>工程名称</span><input data-download-name placeholder="可选，例如 ABC-123"></label>`
    let modal
    const submit = modalButton('开始离线', async () => {
      const url = content.querySelector('[data-download-url]').value.trim()
      const name = content.querySelector('[data-download-name]').value.trim()
      if (!url) { sdk.toast?.error?.('请输入离线链接'); return }
      try {
        await sdk.api.post('/plugins/115/downloads', { payload: { url, name } })
        sdk.toast?.success?.('离线任务已提交'); modal?.close?.(); await loadAll()
      } catch (error) { toastError(error, '离线任务提交失败') }
    }, 'primary')
    modal = sdk.ui.modal({ title: '添加云下载', content, footer: [modalButton('取消', () => modal.close()), submit], width: 'lg', closeOnMask: false })
    window.setTimeout(() => content.querySelector('[data-download-url]')?.focus(), 0)
  }

  async function openAssistantModal() {
    if (!state.assistantSettings) await loadAssistantData()
    const content = document.createElement('div')
    content.className = 'noor-plugin-115-modal noor-plugin-115-assistant-modal'
    if (!state.assistantSettings) content.innerHTML = `<div class="noor-plugin-115-empty is-compact"><strong>未能读取神医助手</strong></div>`
    else { content.append(renderAssistantSettings(state.assistantSettings)); const tasks = document.createElement('div'); tasks.innerHTML = renderAssistantTasks(state.assistantTasks); content.append(...tasks.childNodes) }
    const recommended = state.assistantSettings?.writable ? modalButton('推荐配置', () => content.querySelector('[data-action="recommended"]')?.click()) : null
    let modal = sdk.ui.modal({ title: '管理神医助手', content, width: 'lg', closeOnMask: false, headerActions: recommended ? [recommended] : [] })
    const hiddenRecommended = document.createElement('button'); hiddenRecommended.hidden = true; hiddenRecommended.dataset.action = 'recommended'; content.prepend(hiddenRecommended)
    bindAssistant(content, modal)
  }

  function detectedNoorUrl() {
    const { protocol, hostname, port, origin } = window.location
    return port === '5173' ? `${protocol}//${hostname}:9898` : origin
  }

  function settingsContent() {
    const offlineName = state.config.offline_directory_name || (state.config.offline_directory_id === '0' ? '根目录' : '已选目录')
    const content = document.createElement('div'); content.className = 'noor-plugin-115-modal noor-plugin-115-sdk-settings'
    const input = (key, value, options = {}) => { const el = document.createElement('input'); el.dataset.config = key; el.value = value || ''; Object.assign(el, options); return el }
    const select = (key, value, items) => { const el = document.createElement('select'); el.dataset.config = key; for (const item of items) { const option = document.createElement('option'); option.value = item.value; option.textContent = item.label; option.selected = item.value === value; el.append(option) } return el }
    const folderControl = sdk.ui.settingPath({ value: offlineName, onBrowse: target => openCloudDirectoryPicker(target) }); folderControl.input.dataset.role = 'offline-directory-name'
    const strmControl = sdk.ui.settingPath({ value: state.config.strm_directory, onBrowse: target => openLocalDirectoryPicker(target) }); strmControl.input.dataset.config = 'strm_directory'
    const offlineCard = sdk.ui.settingsCard({ title: '离线下载', children: [
      sdk.ui.settingRow({ label: '115 Client ID', description: '用于 115 Open Platform 授权，不会发送给其他服务。', control: input('client_id', state.config.client_id) }),
      sdk.ui.settingRow({ label: '默认离线目录', description: '云下载任务和目录监控所使用的 115 文件夹。', control: folderControl }),
      sdk.ui.settingRow({ label: '任务发现模式', description: '决定只跟踪 NOOR 任务，还是同时发现其他软件添加的内容。', control: select('task_discovery_mode', state.config.task_discovery_mode || 'noor_only', [{ value: 'noor_only', label: '仅 NOOR 任务' }, { value: 'watch_folder', label: '监控绑定目录' }]) }),
    ] })
    const strmCard = sdk.ui.settingsCard({ title: 'STRM 与播放', children: [
      sdk.ui.settingRow({ label: '本地 STRM incoming', description: 'STRM 生成后交给 MDC-NG 监控和整理的本地目录。', control: strmControl }),
      sdk.ui.settingRow({ label: 'NOOR 播放入口', description: '默认根据当前访问地址自动检测，也可以手动调整。', control: input('public_base_url', state.config.public_base_url || detectedNoorUrl()) }),
      sdk.ui.settingRow({ label: '交付方式', description: '兼容代理适合 Emby；确认客户端能正确携带 Range 后可使用 302。', control: select('stream_delivery_mode', state.config.stream_delivery_mode || 'proxy', [{ value: 'proxy', label: '兼容代理' }, { value: 'redirect', label: '302 直连' }]) }),
    ] })
    content.append(offlineCard, strmCard)
    return content
  }

  async function openSettingsModal() {
    await loadSettingsData()
    const content = settingsContent()
    let modal
    modal = sdk.ui.modal({ title: '115 设置', content, footer: [modalButton('取消', () => modal.close()), modalButton('保存', () => saveSettings(content, modal), 'primary')], width: 'lg', closeOnMask: false })
    bindSettings(content)
  }

  function bindSettings(panel) {
    panel.querySelector('[data-action="pick-strm-directory"]')?.addEventListener('click', () => openLocalDirectoryPicker(panel.querySelector('[data-config="strm_directory"]')))
  }

  async function openCloudDirectoryPicker(target) {
    const content = document.createElement('div'); content.className = 'noor-plugin-115-modal noor-plugin-115-cloud-folder-picker'
    const trail = [{ id: '0', name: '根目录' }]
    let current = trail[0]
    let modal
    const load = async (folder, trimIndex = -1) => {
      if (trimIndex >= 0) trail.splice(trimIndex + 1)
      else if (trail.at(-1)?.id !== folder.id) trail.push(folder)
      current = folder
      content.innerHTML = '<div class="noor-plugin-115-empty is-compact"><strong>正在读取 115 目录</strong></div>'
      try {
        const data = await action('list_folder', { folder_id: folder.id, limit: 500 })
        const directories = (data.items || []).filter(item => item.is_directory)
        content.innerHTML = `<div class="noor-plugin-115-picker-head"><div class="noor-plugin-115-breadcrumbs">${trail.map((item, index) => `<button data-cloud-crumb="${index}">${esc(item.name)}</button>`).join('<i>›</i>')}</div></div><div class="noor-plugin-115-picker-list">${directories.map(item => `<button data-cloud-folder="${esc(item.file_id)}" data-cloud-name="${esc(item.name)}"><span>文件夹</span><strong>${esc(item.name)}</strong></button>`).join('') || '<div class="noor-plugin-115-empty is-compact"><strong>当前没有子目录</strong></div>'}</div>`
        content.querySelectorAll('[data-cloud-folder]').forEach(button => button.addEventListener('click', () => load({ id: button.dataset.cloudFolder, name: button.dataset.cloudName })))
        content.querySelectorAll('[data-cloud-crumb]').forEach(button => button.addEventListener('click', () => { const index = Number(button.dataset.cloudCrumb); load(trail[index], index) }))
      } catch (error) { content.innerHTML = `<div class="noor-plugin-115-error">${esc(error?.response?.data?.detail || error?.message || '115 目录读取失败')}</div>` }
    }
    modal = sdk.ui.modal({ title: '选择 115 目录', content, width: 'lg', closeOnMask: false, footer: [modalButton('取消', () => modal.close()), modalButton('选择当前目录', () => { state.config.offline_directory_id = current.id; state.config.offline_directory_name = trail.map(item => item.name).join(' / '); target.value = state.config.offline_directory_name; modal.close() }, 'primary')] })
    await load(current)
  }

  async function openLocalDirectoryPicker(target) {
    if (!target) return
    const content = document.createElement('div')
    content.className = 'noor-plugin-115-modal noor-plugin-115-local-picker'
    let currentPath = target.value || ''
    let modal
    const load = async path => {
      content.innerHTML = '<div class="noor-plugin-115-empty is-compact"><strong>正在读取目录</strong></div>'
      try {
        const data = await sdk.api.get(`/settings/directories?path=${encodeURIComponent(path || '')}`).then(response => response?.data || response)
        currentPath = data.path
        content.innerHTML = `<div class="noor-plugin-115-picker-head"><button data-picker-up ${data.parent ? '' : 'disabled'}>上一级</button><code>${esc(data.path)}</code></div><div class="noor-plugin-115-picker-list">${(data.entries || []).filter(item => item.is_dir).map(item => `<button data-picker-path="${esc(item.path)}"><span>文件夹</span><strong>${esc(item.name)}</strong></button>`).join('') || '<div class="noor-plugin-115-empty is-compact"><strong>当前没有子目录</strong></div>'}</div>`
        content.querySelector('[data-picker-up]')?.addEventListener('click', () => load(data.parent))
        content.querySelectorAll('[data-picker-path]').forEach(button => button.addEventListener('click', () => load(button.dataset.pickerPath)))
      } catch (error) { content.innerHTML = `<div class="noor-plugin-115-error">${esc(error?.response?.data?.detail || error?.message || '目录读取失败')}</div>` }
    }
    modal = sdk.ui.modal({ title: '选择本地目录', content, width: 'lg', closeOnMask: false, footer: [modalButton('取消', () => modal.close()), modalButton('选择当前目录', () => { target.value = currentPath; modal.close() }, 'primary')] })
    await load(currentPath)
  }

  async function savePluginConfig(config) {
    const response = await fetch('/api/plugins/115/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config }) })
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`)
    state.config = (await response.json()).config || config
    return state.config
  }

  async function saveSettings(panel, modal) {
    const config = { ...state.config }
    panel.querySelectorAll('[data-config]').forEach(input => { config[input.dataset.config] = input.type === 'checkbox' ? input.checked : input.value })
    try {
      await savePluginConfig(config); sdk.toast?.success?.('115 设置已保存'); modal?.close?.(); await loadAll()
    } catch (error) { toastError(error, '设置保存失败') }
  }

  await loadAll()
  return () => { stopPolling(); root.innerHTML = '' }
}
