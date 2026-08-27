function el(tag, cls = '', text = '') {
  const node = document.createElement(tag)
  if (cls) node.className = cls
  if (text) node.textContent = text
  return node
}

export async function mount(root, sdk) {
  const state = {
    stats: null,
    query: '',
    aliases: '',
    resolving: false,
    items: [],
    error: '',
    disabled: false,
  }

  const api = (action, payload = {}) => sdk.api.post(`/plugins/gfriends/actions/${action}`, { payload }).then(r => r.data)

  root.innerHTML = ''
  const page = el('div', 'gfriends-page')
  const header = el('div', 'gfriends-header')
  header.append(
    el('div', 'gfriends-title', 'Gfriends'),
    el('div', 'gfriends-subtitle', '演员头像库辅助工具。用于在演员资料编辑时查询候选头像，再由用户手动选择。'),
  )
  const actions = el('div', 'gfriends-actions')
  const syncBtn = sdk.ui.button({ label: '刷新索引', onClick: () => syncIndex(true) })
  actions.appendChild(syncBtn)
  header.appendChild(actions)

  const statsBox = el('div', 'gfriends-stats')
  const form = el('div', 'gfriends-form')
  const nameInput = sdk.ui.input({
    value: state.query,
    placeholder: '输入演员名，例如 波多野結衣 / 波多野结衣',
    onInput: value => { state.query = String(value || '') },
  })
  const aliasInput = sdk.ui.input({
    value: state.aliases,
    placeholder: '可选别名，逗号分隔',
    onInput: value => { state.aliases = String(value || '') },
  })
  const testBtn = sdk.ui.button({ label: '查询候选头像', tone: 'primary', onClick: () => loadCandidates() })
  form.append(
    sdk.ui.field({ label: '姓名', control: nameInput }),
    sdk.ui.field({ label: '别名', control: aliasInput }),
    testBtn,
  )

  const resultBox = el('div', 'gfriends-result')
  page.append(header, statsBox, form, resultBox)
  root.appendChild(page)

  function renderStats() {
    statsBox.innerHTML = ''
    if (state.disabled) {
      statsBox.appendChild(sdk.ui.notice({ tone: 'info', text: '插件未启用，请在插件管理或设置中启用后再使用。' }))
      return
    }
    const stats = state.stats || {}
    const created = Number(stats.created_at || 0) ? new Date(Number(stats.created_at) * 1000).toLocaleString() : '未同步'
    statsBox.append(
      sdk.ui.badge({ label: `${Number(stats.total_images || 0)} 张头像`, tone: 'info' }),
      sdk.ui.badge({ label: `${Number(stats.alias_count || 0)} 个名称索引`, tone: 'info' }),
      sdk.ui.badge({ label: `索引时间 ${created}`, tone: 'info' }),
    )
  }

  function renderResult() {
    resultBox.innerHTML = ''
    if (state.error) {
      resultBox.appendChild(sdk.ui.notice({ tone: 'error', text: state.error }))
      return
    }
    if (!state.items.length) {
      resultBox.appendChild(sdk.ui.emptyState({ text: '未匹配到头像' }))
      return
    }
    const grid = el('div', 'gfriends-grid')
    for (const item of state.items) {
      const card = el('div', 'gfriends-card')
      const avatar = el('div', 'gfriends-avatar')
      const img = el('img')
      img.src = item.url
      img.alt = item.name || state.query
      avatar.appendChild(img)
      const info = el('div', 'gfriends-card__info')
      info.append(
        el('strong', '', item.name || state.query),
        el('span', '', (item.aliases || []).join(' · ')),
        el('code', '', item.folder || ''),
      )
      card.append(avatar, info)
      grid.appendChild(card)
    }
    resultBox.appendChild(grid)
  }

  async function loadStats() {
    try {
      const res = await api('stats', { ensure: false })
      state.stats = res.index
      if (res?.ok === false) {
        state.disabled = true
        state.stats = {}
      }
      renderStats()
    } catch {}
  }

  async function syncIndex(force = false) {
    state.error = ''
    syncBtn.disabled = true
    try {
      const res = await api('sync', { force })
      state.stats = res.index
      if (res?.ok === false) {
        state.disabled = true
        state.error = res.message || '插件未启用'
        renderResult()
        return
      }
      sdk.toast.success('Gfriends 索引已更新')
      renderStats()
    } catch (error) {
      state.error = error?.response?.data?.detail || error?.message || '同步失败'
      renderResult()
    } finally {
      syncBtn.disabled = false
    }
  }

  async function loadCandidates() {
    state.error = ''
    state.items = []
    state.resolving = true
    testBtn.disabled = true
    renderResult()
    try {
      const aliases = state.aliases.split(/[,，、]/).map(x => x.trim()).filter(Boolean)
      const result = await api('candidates', { name: state.query, aliases, limit: 36 })
      if (result?.ok === false) {
        state.error = result.message || '插件未启用'
        return
      }
      state.items = result.items || []
      if (result?.index) state.stats = result.index
      renderStats()
    } catch (error) {
      state.error = error?.response?.data?.detail || error?.message || '查询失败'
    } finally {
      state.resolving = false
      testBtn.disabled = false
      renderResult()
    }
  }

  await loadStats()
  if (!state.disabled && !state.stats?.alias_count) void syncIndex(false)

  return () => {
    root.innerHTML = ''
  }
}
