function fmtBytes(value) {
  const size = Number(value || 0)
  if (!size) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const index = Math.min(units.length - 1, Math.floor(Math.log(size) / Math.log(1024)))
  return `${(size / 1024 ** index).toFixed(index > 2 ? 2 : 1)} ${units[index]}`
}

export async function mount(root, sdk) {
  const state = { account: null, authUid: '', authUrl: '', polling: false }
  let pollTimer = null
  root.innerHTML = ''
  const page = sdk.ui?.page ? sdk.ui.page({ className: 'noor-plugin-115-page' }) : document.createElement('div')
  page.classList.add('noor-plugin-115-page')
  const tabs = sdk.ui?.tabs ? sdk.ui.tabs({
    value: 'account',
    tabs: [{ key: 'account', label: '账号' }, { key: 'tasks', label: '离线任务' }, { key: 'media', label: '媒体' }],
    onChange: key => {
      page.querySelectorAll('[data-panel]').forEach(node => { node.hidden = node.dataset.panel !== key })
      if (key === 'tasks') loadTasks()
    },
  }) : document.createElement('div')
  const actions = document.createElement('div')
  actions.className = 'noor-plugin-topbar__actions'
  const statusBadge = sdk.ui?.badge ? sdk.ui.badge('检查中', 'muted') : document.createElement('span')
  const settingsButton = sdk.ui?.button ? sdk.ui.button({ label: '配置', onClick: () => sdk.config?.open?.() }) : document.createElement('button')
  actions.append(statusBadge, settingsButton)
  const topbar = sdk.ui?.topBar ? sdk.ui.topBar({ tabs, actions }) : document.createElement('div')
  if (!sdk.ui?.topBar) topbar.append(tabs, actions)

  const accountPanel = document.createElement('section')
  accountPanel.dataset.panel = 'account'
  accountPanel.className = 'noor-plugin-115-account'
  const taskPanel = document.createElement('section')
  taskPanel.dataset.panel = 'tasks'
  taskPanel.hidden = true
  taskPanel.innerHTML = '<div class="noor-plugin-115-empty"><strong>正在读取离线任务</strong></div>'
  const mediaPanel = document.createElement('section')
  mediaPanel.dataset.panel = 'media'
  mediaPanel.hidden = true
  mediaPanel.innerHTML = '<div class="noor-plugin-115-empty"><strong>媒体流水线尚未产生记录</strong><span>只处理新完成任务中的媒体，不扫描整个网盘。</span></div>'
  page.append(topbar, accountPanel, taskPanel, mediaPanel)
  root.append(page)

  function stopPolling() {
    state.polling = false
    if (pollTimer) window.clearTimeout(pollTimer)
    pollTimer = null
  }

  async function pollAuth() {
    if (!state.polling || !state.authUid) return
    try {
      const response = await sdk.api.post('/plugins/115/actions/auth_poll', { payload: { uid: state.authUid } })
      const data = response?.data || response
      if (data.connected) {
        stopPolling()
        sdk.toast?.success?.('115 已连接')
        await loadAccount()
        return
      }
    } catch (error) {
      stopPolling()
      sdk.toast?.error?.(error?.response?.data?.detail || error?.message || '115 授权失败')
      renderAccount()
      return
    }
    pollTimer = window.setTimeout(pollAuth, 2000)
  }

  async function connect() {
    try {
      const response = await sdk.api.post('/plugins/115/actions/auth_start', { payload: {} })
      const data = response?.data || response
      state.authUid = data.uid || ''
      state.authUrl = data.qrcode || ''
      state.polling = true
      renderAccount()
      pollAuth()
    } catch (error) {
      sdk.toast?.error?.(error?.response?.data?.detail || error?.message || '无法开始 115 授权')
    }
  }

  async function disconnect() {
    const confirmed = await sdk.ui?.confirm?.({ title: '断开 115', message: '将删除 NOOR 保存的 115 OAuth Token。', confirmLabel: '断开' })
    if (confirmed === false) return
    await sdk.api.post('/plugins/115/actions/disconnect', { payload: {} })
    stopPolling()
    state.account = { connected: false, status: 'disconnected' }
    renderAccount()
  }

  function renderAccount() {
    const account = state.account || {}
    statusBadge.textContent = account.connected ? '已连接' : account.status === 'token_error' ? 'Token 异常' : '未连接'
    statusBadge.className = `noor-plugin-badge noor-plugin-badge--${account.connected ? 'success' : account.status === 'token_error' ? 'error' : 'warning'}`
    if (state.polling) {
      accountPanel.innerHTML = `<div class="noor-plugin-115-auth"><span>115 OPEN PLATFORM</span><strong>等待账号授权</strong><p>请打开授权地址并完成确认。Token 只会由后端接收并加密保存。</p>${state.authUrl ? `<a href="${state.authUrl}" target="_blank" rel="noopener">打开 115 授权页面</a><code>${state.authUrl}</code>` : ''}</div>`
      return
    }
    if (!account.connected) {
      accountPanel.innerHTML = '<div class="noor-plugin-115-auth"><span>115 OPEN PLATFORM</span><strong>连接你的 115 账号</strong><p>使用官方开放平台设备授权，不读取浏览器 Cookie。</p></div>'
      const button = sdk.ui?.button ? sdk.ui.button({ label: '连接 115', tone: 'primary', onClick: connect }) : document.createElement('button')
      if (!sdk.ui?.button) { button.textContent = '连接 115'; button.onclick = connect }
      accountPanel.firstElementChild.append(button)
      return
    }
    accountPanel.innerHTML = `<div class="noor-plugin-115-profile"><img src="${account.avatar || ''}" alt=""><div><span>CONNECTED ACCOUNT</span><strong>${account.user_name || account.user_id || '115 用户'}</strong><p>${account.vip || '115'} · 已使用 ${fmtBytes(account.space?.used)} / ${fmtBytes(account.space?.total)}</p></div></div><div class="noor-plugin-115-space"><i><b style="width:${Math.min(100, Number(account.space?.total) ? Number(account.space.used || 0) / Number(account.space.total) * 100 : 0)}%"></b></i><span>剩余 ${fmtBytes(account.space?.remaining)}</span></div>`
    const button = sdk.ui?.button ? sdk.ui.button({ label: '断开连接', onClick: disconnect }) : document.createElement('button')
    if (!sdk.ui?.button) { button.textContent = '断开连接'; button.onclick = disconnect }
    accountPanel.append(button)
  }

  async function loadAccount() {
    try {
      const response = await sdk.api.post('/plugins/115/actions/status', { payload: {} })
      state.account = response?.data || response
    } catch (error) {
      state.account = { connected: false, status: 'error', message: error?.message || '' }
    }
    renderAccount()
  }

  async function loadTasks() {
    try {
      const response = await sdk.api.post('/plugins/115/actions/tasks', { payload: { limit: 100 } })
      const items = (response?.data || response)?.items || []
      if (!items.length) {
        taskPanel.innerHTML = '<div class="noor-plugin-115-empty"><strong>暂无离线任务</strong><span>可以从订阅中心或资源卡片推送到 115。</span></div>'
        return
      }
      taskPanel.innerHTML = `<div class="noor-plugin-115-task-list">${items.map(item => `<article><div><strong>${item.name || item.source_hint || '115 离线任务'}</strong><span>${item.source_kind} · 目录 ${item.target_directory_id}</span></div><em class="is-${item.status}">${item.status}</em><b>${item.progress || 0}%</b><small>${item.created_at || ''}</small></article>`).join('')}</div>`
    } catch (error) {
      taskPanel.innerHTML = `<div class="noor-plugin-115-empty"><strong>离线任务读取失败</strong><span>${error?.response?.data?.detail || error?.message || ''}</span></div>`
    }
  }

  await loadAccount()
  return () => { stopPolling(); root.innerHTML = '' }
}
