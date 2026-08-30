function el(tag, className = '', text = '') {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text) node.textContent = text
  return node
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]))
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
    : fetch(`/api/plugins/av-recommend${path}`, init)
}

async function refreshStoredImageCandidates(sdk, code) {
  const text = String(code || '').trim()
  if (!text) return []
  try {
    const response = await pluginFetch(sdk, '/actions/refresh_cover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ payload: { code: text } }),
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

async function renderFallbackImage(host, candidates, placeholder, onExhausted) {
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
    host.textContent = placeholder || 'NO IMAGE'
    return
  }
  const img = document.createElement('img')
  img.loading = 'lazy'
  img.alt = ''
  img.src = loaded
  host.appendChild(img)
}

function fmtSizeMb(value) {
  const mb = Number(value || 0)
  if (!Number.isFinite(mb) || mb <= 0) return ''
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)}GB`
  return `${Math.round(mb)}MB`
}

function fmtBytes(value) {
  const bytes = Number(value || 0)
  if (!Number.isFinite(bytes) || bytes <= 0) return ''
  return fmtSizeMb(bytes / 1024 / 1024)
}

function formatReleaseDate(value) {
  if (!value) return ''
  const text = String(value)
  return text.includes('T') ? text.slice(0, 10) : text
}

function detailCode(item) {
  return String(item?.code || item?.number || '').trim()
}

function detailTitle(item) {
  const code = detailCode(item)
  const title = String(item?.title || item?.origin_title || item?.display_title || '').trim()
  return code && title ? `${code} ${title}` : code || title || '未知作品'
}

function textHasKeywords(value, keywords) {
  if (value == null) return false
  if (typeof value === 'string') {
    const text = value.toLowerCase()
    return keywords.some(keyword => text.includes(String(keyword).toLowerCase()))
  }
  if (Array.isArray(value)) return value.some(entry => textHasKeywords(entry, keywords))
  if (typeof value === 'object') return Object.values(value).some(entry => textHasKeywords(entry, keywords))
  return textHasKeywords(String(value), keywords)
}

function detectCnsub(detail) {
  const keywords = ['中字', '字幕', '中文', '中文字幕', 'chs', 'cht']
  return textHasKeywords(detail?.categories, keywords)
    || textHasKeywords(detail?.magnets, keywords)
    || textHasKeywords(detail?.title, keywords)
}

function detectCracked(detail) {
  const keywords = ['破解', '破解版', '无码破解', 'uncensored leak']
  if (detail?.is_cracked || detail?.cracked) return true
  return textHasKeywords(detail?.categories, keywords)
    || textHasKeywords(detail?.magnets, keywords)
    || textHasKeywords(detail?.tags, keywords)
}

function compactResourceSubtitle(resource) {
  const raw = String(resource?.subtitle || '').trim()
  if (!raw) return ''
  const parts = raw.split('·').map(part => part.trim()).filter(Boolean)
  const compact = []
  for (const part of parts) {
    if (!compact.includes(part)) compact.push(part)
  }
  return compact.join(' · ')
}

function resourceProviderOrder(resource) {
  const provider = String(resource?.provider || resource?.key || '').trim()
  if (provider === 'avdb') return 0
  if (provider === 'mteam-plugin') return 1
  if (provider === 'javdb') return 2
  return 9
}

function badge(label, tone = 'neutral') {
  const b = el('span', `av-rec-badge av-rec-badge--${tone}`, label)
  return b
}

function scoreTone(score) {
  if (score >= 80) return 'hot'
  if (score >= 60) return 'good'
  return 'neutral'
}

function fmtPool(total, today) {
  const base = Number(total || 0)
  const inc = Number(today || 0)
  if (!Number.isFinite(base) || base <= 0) return inc > 0 ? `0+${inc}` : '0'
  if (!Number.isFinite(inc) || inc <= 0) return String(base)
  return `${base}+${inc}`
}

export async function mount(root, sdk) {
  const state = {
    sourceMode: 'latest',
    loading: false,
    error: '',
    data: null,
    activePanel: null,
    resourceSync: { active: false, count: 0, updated: false },
  }
  let resourcePoll = null
  let snapshotPoll = null
  let resourceSyncing = false
  let exposureObserver = null
  let exposureFlushTimer = null
  let exposureBatchSequence = 0
  const exposedCodes = new Set()
  const exposureTimers = new Map()
  const pendingExposures = new Map()
  const exposureSession = typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`

  root.innerHTML = ''
  const page = el('div', 'av-rec-page')
  const topbar = el('div', 'av-rec-topbar')
  const title = el('div', 'av-rec-title')
  title.innerHTML = '<strong>AV 推荐中心</strong><span>根据 JavDB 最新动态、完整候选池、媒体库偏好和资源可用性生成推荐</span>'
  const actions = el('div', 'av-rec-actions')
  const modeWrap = el('div', 'av-rec-modes')
  const refreshBtn = sdk.ui?.button ? sdk.ui.button({ label: '刷新推荐', tone: 'primary', onClick: () => load(true) }) : el('button', 'av-rec-btn', '刷新推荐')
  if (!sdk.ui?.button) refreshBtn.onclick = () => load(true)
  actions.append(modeWrap, refreshBtn)
  topbar.append(title, actions)
  const profile = el('div', 'av-rec-profile')
  const notice = el('div', 'av-rec-notice')
  const grid = el('div', 'av-rec-grid')
  page.append(topbar, profile, notice, grid)
  root.appendChild(page)

  function renderModes() {
    modeWrap.innerHTML = ''
    const modes = [
      ['latest', '最新推荐'],
      ['full', '完整推荐'],
    ]
    for (const [value, label] of modes) {
      const btn = el('button', 'av-rec-mode' + (state.sourceMode === value ? ' is-active' : ''), label)
      btn.onclick = () => {
        if (state.sourceMode === value) return
        state.sourceMode = value
        load(false)
      }
      modeWrap.appendChild(btn)
    }
  }

  function renderProfile() {
    const data = state.data || {}
    const p = data.profile || {}
    const stats = data.stats || {}
    const actorDiversity = stats.diversity?.actors || {}
    const routes = stats.route_evaluation?.routes || {}
    const activeRoutes = Object.values(routes).filter(item => item?.adaptation_status === 'active').length
    const topicMetrics = stats.topic_evaluation?.topics || {}
    const activeTopics = Object.values(topicMetrics).filter(item => item?.adaptation_status === 'active').length
    const searchEvaluation = stats.search_evaluation || {}
    const contextMixture = stats.context_mixture || {}
    const exposureFatigue = stats.exposure_fatigue || {}
    const shadow = stats.shadow_evaluation || {}
    const resourceShadow = stats.resource_shadow_evaluation || {}
    const resourceOutcome = stats.resource_evaluation || {}
    const coreRecall = stats.neighbor_recall || {}
    const coreFeatureQuality = coreRecall.feature_quality || {}
    const coreEdgeQuality = coreRecall.edge_quality || {}
    const coreOffline = coreRecall.offline_evaluation || {}
    const relationPolicy = coreOffline.relation_counterfactual?.policy || {}
    const coverageRepair = coreRecall.coverage_repair_state || {}
    const relationWeights = coreRecall.relation_weights || {}
    const temporal = stats.temporal_preference || {}
    const temporalScales = temporal.scales || {}
    const temporalBacktest = temporal.backtest || {}
    profile.innerHTML = ''
    const cards = [
      ['媒体库', p.media_count || 0, '已分析作品'],
      ['番号', p.code_count || 0, '可识别番号'],
      ['候选', stats.candidates || 0, '本轮扫描'],
      ['推荐', data.total || 0, '综合排序'],
      ['候选池', fmtPool(stats.candidate_pool_total, stats.candidate_pool_today), state.sourceMode === 'full' ? '完整累计+今日' : '历史累计+今日'],
      ['模型', data.model?.version || '-', data.model?.mode === 'auto' ? '自动保护' : '手动选择'],
      ['影子评估', `${shadow.paired_qualified || 0}/${shadow.minimum_qualified_sample || 20}`, shadow.recommended_policy === 'collecting' ? '共享候选静默采样' : (shadow.reason || '共享候选对照')],
      ['资源学习', `${resourceShadow.paired_qualified || 0}/${resourceShadow.minimum_qualified_sample || 20}`, resourceShadow.recommended_policy === 'collecting' ? '固定权重与学习权重静默对照' : (resourceShadow.reason || '资源策略对照')],
      ['资源校准', resourceOutcome.status === 'active' ? `${resourceOutcome.eligible || 0} 项` : '采集中', resourceOutcome.status === 'active' ? `${Object.values(resourceOutcome.weights || {}).filter(value => Math.abs(Number(value || 1) - 1) >= 0.01).length} 个资源特征已调整` : '达到对照样本后自动启用'],
      ['Core 邻域', coreRecall.selected || 0, `${coreRecall.core_only_selected || 0} 个独立召回`],
      ['关系图覆盖', coreRecall.graph_coverage_percent != null ? `${Number(coreRecall.graph_coverage_percent).toFixed(1)}%` : '待构建', `${coreRecall.linked_works || 0}/${coreRecall.unique_works || 0} 个唯一作品 · ${coreRecall.isolated_works || 0} 孤立`],
      ['画像融合', coreRecall.unique_works || 0, `${coreRecall.source_profiles || 0} 条来源画像 · ${coreRecall.duplicate_profiles || 0} 条重复已融合`],
      ['离线找回', coreOffline.hit_rate?.['@20'] != null ? `${Math.round(coreOffline.hit_rate['@20'] * 100)}%` : '待评估', coreOffline.evaluated ? `${coreOffline.evaluated} 部留一 · 覆盖 ${Math.round((coreOffline.coverage || 0) * 100)}%` : '留一邻域审计'],
      ['覆盖修复', `${coverageRepair.enriched || 0}/${coverageRepair.queued || 0}`, `${coreRecall.coverage_repair_queued || 0} 个本轮加入 · ${coverageRepair.failed || 0} 失败`],
      ['关系校准', Object.keys(relationWeights).length || 0, Object.keys(relationWeights).length ? `${Object.entries(relationWeights).map(([key, value]) => `${key} ${Math.round(value * 100)}%`).join(' · ')} · ${relationPolicy.status === 'confirming' ? `候选确认 ${relationPolicy.confirmations || 0}/${relationPolicy.required_confirmations || 2}` : relationPolicy.status === 'promoted' ? '新策略已晋升' : '稳定策略'}` : '训练/验证暂无一致调整'],
      ['时间画像', temporalScales.current?.work_count || 0, `${temporalScales.medium?.work_count || 0} 中期 · ${temporalScales.durable?.work_count || 0} 长期`],
      ['时间回测', temporalBacktest.recommended_policy === 'temporal' ? '近期增益' : temporalBacktest.recommended_policy === 'durable' ? '长期等权' : '采集中', temporalBacktest.evaluated ? `${temporalBacktest.evaluated} 个历史切点 · 验证 ${Math.round((temporalBacktest.utility_delta?.validation || 0) * 10000) / 100}%` : '等待足够历史样本'],
      ['特征净化', (coreFeatureQuality.dropped_actor_alias_terms || 0) + (coreFeatureQuality.dropped_actor_variant_terms || 0) + (coreFeatureQuality.dropped_code_prefix_categories || 0) + (coreFeatureQuality.dropped_operational_semantic_terms || 0), `${(coreFeatureQuality.dropped_actor_alias_terms || 0) + (coreFeatureQuality.dropped_actor_variant_terms || 0)} 演员重复 · ${coreFeatureQuality.dropped_code_prefix_categories || 0} 番号前缀 · ${coreFeatureQuality.dropped_operational_semantic_terms || 0} 技术/来源词`],
      ['标题补演员', coreFeatureQuality.title_inferred_actor_features || 0, `MDC-NG 低权重补全 · ${coreRecall.featureless_works || 0} 个仍无关系特征`],
      ['关系降噪', coreEdgeQuality.pruned_semantic_only || 0, `弱语义单边已剔除 · ${coreEdgeQuality.retained_pairs || 0} 条可信关系`],
      ['覆盖', actorDiversity.unique || 0, `有效演员 ${actorDiversity.effective || 0}`],
      ['路线学习', `${activeRoutes}/${Object.keys(routes).length}`, `${stats.route_evaluation?.eligible || 0} 可评估 · ${stats.route_evaluation?.converted || 0} 合格 · ${stats.route_evaluation?.verified || 0} 入库`],
      ['主题学习', `${activeTopics}/${Object.keys(topicMetrics).length}`, `${stats.topic_evaluation?.total_exposed || 0} 次主题曝光`],
      ['当前意图', stats.session_intent?.event_count || 0, '12 小时渐进衰减'],
      ['搜索学习', `${searchEvaluation.adaptive_signals || 0}/${searchEvaluation.signal_count || 0}`, `${searchEvaluation.eligible_events || 0} 个成熟样本`],
      ['意图门控', contextMixture.active ? `${Math.round((contextMixture.gate || 0) * 100)}%` : '待信号', contextMixture.active ? `可靠 ${Math.round((contextMixture.reliability || 0) * 100)}%` : '长期画像为主'],
      ['曝光轮换', exposureFatigue.active || 0, `${exposureFatigue.short || 0} 短时 · ${exposureFatigue.long || 0} 长期`],
    ]
    for (const [name, value, desc] of cards) {
      const card = el('div', 'av-rec-stat')
      card.innerHTML = `<span>${escapeHtml(name)}</span><strong>${escapeHtml(value)}</strong><em>${escapeHtml(desc)}</em>`
      profile.appendChild(card)
    }
    const prefs = el('div', 'av-rec-prefs')
    const groups = [
      ['演员', p.top_actors || []],
      ['类型', p.top_genres || []],
      ['标签', p.top_tags || []],
      ['兴趣主题', (p.top_interest_topics || []).map(item => ({ name: item.label, count: `${item.support || 0}部 · 置信${Math.round((item.confidence || 0) * 100)}%` }))],
    ]
    for (const [label, items] of groups) {
      const group = el('div', 'av-rec-pref-group')
      group.appendChild(el('span', '', label))
      const chips = el('div')
      for (const item of items.slice(0, 8)) chips.appendChild(badge(`${item.name} ${item.count}`, 'soft'))
      if (!items.length) chips.appendChild(badge('暂无', 'muted'))
      group.appendChild(chips)
      prefs.appendChild(group)
    }
    profile.appendChild(prefs)
  }

  function renderNotice() {
    notice.innerHTML = ''
    if (state.loading) {
      notice.className = 'av-rec-notice is-loading'
      notice.textContent = '正在读取媒体库偏好与 JavDB 候选…'
      return
    }
    if (state.error) {
      notice.className = 'av-rec-notice is-error'
      notice.textContent = state.error
      return
    }
    if (state.resourceSync.active) {
      notice.className = 'av-rec-notice is-loading'
      notice.textContent = `正在后台补全 ${state.resourceSync.count} 部作品的资源情报，卡片会自动更新。`
      return
    }
    if (state.resourceSync.updated) {
      notice.className = 'av-rec-notice'
      notice.textContent = '资源情报已完成增量更新。'
      return
    }
    if (state.data?.cache_status?.status === 'stale') {
      notice.className = 'av-rec-notice is-loading'
      notice.textContent = '正在使用已有推荐快照，后台已开始更新。'
      return
    }
    const warnings = (state.data?.warnings || []).filter(value => !/^正在后台补全\s*\d+/.test(String(value || '')))
    if (warnings.length) {
      notice.className = 'av-rec-notice is-warning'
      notice.textContent = warnings[0]
      return
    }
    const bg = state.data?.candidate_meta?.pool?.background || {}
    if (state.sourceMode === 'full' && bg.running) {
      notice.className = 'av-rec-notice is-loading'
      notice.textContent = '后台正在增量维护完整候选池，本页先使用已有候选池推荐。'
      return
    }
    notice.className = 'av-rec-notice'
    notice.textContent = ''
  }

  function skeleton() {
    grid.innerHTML = ''
    for (let i = 0; i < 8; i++) {
      const card = el('div', 'av-rec-card av-rec-skeleton')
      card.innerHTML = '<div></div><section><b></b><p></p><p></p></section>'
      grid.appendChild(card)
    }
  }

  async function feedback(item, kind) {
    try {
      await sdk.api.post('/plugins/av-recommend/actions/feedback', {
        payload: {
          code: item.code,
          kind,
          actors: item.actors || [],
          categories: item.categories || [],
          recall_sources: item.recall_sources || [],
          recommendation_rank: item.recommendation_rank || 0,
          model_version: item.model_version || state.data?.model?.version || 'unknown',
          is_exploration: !!item.is_exploration,
          exploration_kind: item.exploration_kind || '',
          interest_topic: item.interest_topic || {},
          interest_topic_hypothesis: item.interest_topic_hypothesis || {},
        },
      })
      sdk.toast?.success(kind === 'ignore' ? '已忽略' : kind === 'like' ? '已标记喜欢' : '已标记不感兴趣')
      await load(true)
    } catch (e) {
      sdk.toast?.error(e?.response?.data?.detail || e?.message || '操作失败')
    }
  }

  async function recordBehavior(item, eventType, data = {}) {
    const code = detailCode(item)
    if (!code) return
    try {
      await sdk.api.post('/plugins/av-recommend/actions/behavior', {
        payload: {
          code,
          event_type: eventType,
          actors: item.actors || [],
          categories: item.categories || [],
          recall_sources: item.recall_sources || [],
          recommendation_rank: item.recommendation_rank || 0,
          model_version: item.model_version || state.data?.model?.version || 'unknown',
          is_exploration: !!item.is_exploration,
          strategy: item.is_exploration ? 'exploration' : 'ranking',
          interest_topic: item.interest_topic || {},
          interest_topic_hypothesis: item.interest_topic_hypothesis || {},
          data,
        },
      })
    } catch (_) {
      // Behavior evidence must never block the user's primary action.
    }
  }

  function flushExposures() {
    exposureFlushTimer = null
    const items = Array.from(pendingExposures.values())
    pendingExposures.clear()
    if (!items.length) return
    exposureBatchSequence += 1
    sdk.api.post('/plugins/av-recommend/actions/exposure', {
      payload: {
        batch_id: `${exposureSession}:${exposureBatchSequence}`,
        items: items.map(item => ({
          code: item.code,
          actors: item.actors || [],
          categories: item.categories || [],
          rank: item.recommendation_rank || 0,
          score: item.score || 0,
          model_version: item.model_version || state.data?.model?.version || 'unknown',
          shadow_ranks: item.shadow_ranks || {},
          resource_shadow_ranks: item.resource_shadow_ranks || {},
          recall_sources: item.recall_sources || [],
          is_exploration: !!item.is_exploration,
          exploration_kind: item.exploration_kind || '',
          interest_topic: item.interest_topic || {},
          interest_topic_hypothesis: item.interest_topic_hypothesis || {},
          resource_summary: item.resource_summary || {},
          has_cnsub: !!item.has_cnsub,
          is_cracked: !!item.is_cracked,
          is_uncensored: !!item.is_uncensored,
          best_resource_size_mb: item.best_resource_size_mb || 0,
        })),
      },
    }).catch(() => {})
  }

  function queueExposure(item) {
    const code = detailCode(item)
    if (!code || exposedCodes.has(code)) return
    exposedCodes.add(code)
    pendingExposures.set(code, item)
    if (exposureFlushTimer) return
    exposureFlushTimer = window.setTimeout(flushExposures, 600)
  }

  function resetExposureObserver() {
    exposureObserver?.disconnect?.()
    exposureTimers.forEach(timer => window.clearTimeout(timer))
    exposureTimers.clear()
    if (typeof IntersectionObserver === 'undefined') {
      exposureObserver = null
      return
    }
    exposureObserver = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        const card = entry.target
        const existing = exposureTimers.get(card)
        if (!entry.isIntersecting || entry.intersectionRatio < 0.6) {
          if (existing) window.clearTimeout(existing)
          exposureTimers.delete(card)
          return
        }
        if (existing || exposedCodes.has(detailCode(card.__recommendationItem))) return
        const timer = window.setTimeout(() => {
          exposureTimers.delete(card)
          queueExposure(card.__recommendationItem)
          exposureObserver?.unobserve?.(card)
        }, 1200)
        exposureTimers.set(card, timer)
      })
    }, { threshold: [0.6] })
  }

  function openDislikePicker(item) {
    const mask = el('div', 'av-rec-feedback-mask')
    const modal = el('div', 'av-rec-feedback-modal')
    mask.appendChild(modal)
    const tags = [
      ...(item.actors || []).map(name => ({ type: 'actor', label: name })),
      ...(item.categories || []).map(name => ({ type: 'category', label: name })),
    ].filter(x => x.label)
    const selected = new Set()
    modal.innerHTML = `
      <div class="av-rec-feedback-head">
        <strong>不感兴趣的点</strong>
        <button type="button" data-action="close">×</button>
      </div>
      <div class="av-rec-feedback-body">
        <p>不选标签则只隐藏这部作品；勾选标签后，后续相似推荐会降权。</p>
        <div class="av-rec-feedback-tags"></div>
      </div>
      <div class="av-rec-feedback-actions">
        <button type="button" data-action="cancel">取消</button>
        <button type="button" data-action="save" class="is-primary">确认</button>
      </div>
    `
    const close = () => mask.remove()
    mask.onclick = event => { if (event.target === mask) close() }
    modal.querySelector('[data-action="close"]').onclick = close
    modal.querySelector('[data-action="cancel"]').onclick = close
    const tagHost = modal.querySelector('.av-rec-feedback-tags')
    const seen = new Set()
    for (const tag of tags) {
      const key = `${tag.type}:${tag.label}`
      if (seen.has(key)) continue
      seen.add(key)
      const btn = el('button', `av-rec-feedback-tag av-rec-feedback-tag--${tag.type}`)
      btn.type = 'button'
      btn.textContent = tag.label
      btn.onclick = () => {
        if (selected.has(key)) selected.delete(key)
        else selected.add(key)
        btn.classList.toggle('is-active', selected.has(key))
      }
      tagHost.appendChild(btn)
    }
    if (!seen.size) tagHost.appendChild(el('span', 'av-rec-feedback-empty', '这部作品没有可用演员/类型标签'))
    modal.querySelector('[data-action="save"]').onclick = async () => {
      const actors = []
      const categories = []
      for (const key of selected) {
        const [type, ...rest] = String(key).split(':')
        const label = rest.join(':')
        if (type === 'actor') actors.push(label)
        else if (type === 'category') categories.push(label)
      }
      close()
      await feedback({ ...item, actors, categories }, 'dislike')
    }
    document.body.appendChild(mask)
  }

  async function openSubscription(item) {
    const code = detailCode(item)
    if (!code) {
      sdk.toast?.error?.('缺少作品番号')
      return
    }
    const payload = {
      code,
      title: detailTitle(item),
      cover_url: item.cover_url || item.thumb_url || '',
      fanart_url: item.fanart_url || item.cover_url || item.thumb_url || '',
      sourcePlugin: 'av-recommend',
      sourceLabel: '推荐中心',
      sourceRoute: window.location.pathname + window.location.search,
      sourceContext: state.sourceMode === 'full' ? 'av-recommend-full' : 'av-recommend-latest',
      defaultMode: 'loose',
      requireCracked: false,
      requireSubtitle: false,
    }
    try {
      if (sdk.subscription?.open) {
        return sdk.subscription.open({
          ...payload,
          onSuccess: result => {
            sdk.toast?.success?.(result?.created ? '订阅已创建' : '订阅已存在')
            recordBehavior(item, 'subscription', { created: !!result?.created })
          },
        })
      }
      const resp = await sdk.api.post('/plugins/subscription-core/actions/create', {
        payload: {
          ...payload,
          source_plugin: payload.sourcePlugin,
          source_label: payload.sourceLabel,
          source_route: payload.sourceRoute,
          source_context: payload.sourceContext,
        },
      })
      sdk.toast?.success?.(resp?.data?.created ? '订阅已创建' : '订阅已存在')
      await recordBehavior(item, 'subscription', { created: !!resp?.data?.created })
    } catch (e) {
      sdk.toast?.error?.(e?.response?.data?.detail || e?.message || '订阅失败')
    }
  }

  function closeActivePanel() {
    if (state.activePanel?.close) state.activePanel.close()
    else if (state.activePanel?.remove) state.activePanel.remove()
    state.activePanel = null
  }

  function createPanel() {
    if (sdk.ui?.panel) return sdk.ui.panel({ title: '影片详情', eyebrow: 'JavDB', scroll: true })
    const mask = el('div', 'av-rec-detail-mask')
    const modal = el('div', 'av-rec-detail-modal')
    const head = el('div', 'av-rec-detail-modal__head')
    head.innerHTML = '<div><span>JavDB</span><strong>影片详情</strong></div>'
    const close = el('button', '', '×')
    close.type = 'button'
    close.onclick = () => {
      mask.remove()
      if (state.activePanel === panel) state.activePanel = null
    }
    head.appendChild(close)
    const body = el('div', 'av-rec-detail-modal__body')
    modal.append(head, body)
    mask.appendChild(modal)
    mask.onclick = event => {
      if (event.target === mask) close.click()
    }
    document.body.appendChild(mask)
    const panel = { body, close: () => mask.remove() }
    return panel
  }

  function renderLoading(host, text) {
    host.innerHTML = ''
    if (sdk.ui?.loadingState) host.appendChild(sdk.ui.loadingState({ text }))
    else host.appendChild(el('div', 'av-rec-detail-loading', text))
  }

  async function openDetail(item) {
    closeActivePanel()
    const code = detailCode(item) || String(item?.id || '').trim()
    if (!code) {
      sdk.toast?.error?.('缺少作品番号')
      return
    }
    const panel = createPanel()
    state.activePanel = panel
    renderLoading(panel.body, '正在调取详情数据...')
    try {
      const expectedMagnetsCount = Number(item?.magnets_count || item?.magnet_count || 0)
      const res = await sdk.api.post('/plugins/javdb/actions/video', { payload: { code, expected_magnets_count: expectedMagnetsCount } })
      const video = res.data?.data || {}
      recordBehavior({ ...item, ...video }, 'detail_view')
      panel.body.innerHTML = ''

      const content = el('div', 'av-rec-detail')
      const previewList = Array.isArray(video.previews) ? video.previews : []
      const images = [video.cover_url || item.cover_url, ...previewList].filter(Boolean)
      const gallery = el('div', 'av-rec-detail-gallery')
      const galleryViewport = el('div', 'av-rec-detail-gallery__viewport')
      const rail = el('div', 'av-rec-detail-gallery__rail')
      images.forEach(src => {
        const frame = el('button', 'av-rec-gallery-frame')
        frame.type = 'button'
        const img = el('img', 'av-rec-gallery-img')
        img.src = src
        img.alt = detailTitle(video)
        frame.onclick = () => sdk.ui?.previewImage?.(src, images)
        frame.appendChild(img)
        rail.appendChild(frame)
      })
      if (!images.length) rail.appendChild(el('div', 'av-rec-no-data', '暂无封面与剧照'))
      galleryViewport.appendChild(rail)
      gallery.appendChild(galleryViewport)
      if (images.length > 1) {
        const scrollGallery = direction => {
          const amount = Math.max(320, galleryViewport.clientWidth)
          galleryViewport.scrollBy({ left: direction * amount, behavior: 'smooth' })
        }
        const prevBtn = el('button', 'av-rec-gallery-nav av-rec-gallery-nav--prev', '‹')
        const nextBtn = el('button', 'av-rec-gallery-nav av-rec-gallery-nav--next', '›')
        prevBtn.type = 'button'
        nextBtn.type = 'button'
        prevBtn.onclick = () => scrollGallery(-1)
        nextBtn.onclick = () => scrollGallery(1)
        const syncGalleryNav = () => {
          const maxLeft = Math.max(0, galleryViewport.scrollWidth - galleryViewport.clientWidth)
          const left = galleryViewport.scrollLeft
          prevBtn.classList.toggle('is-hidden', left <= 4)
          nextBtn.classList.toggle('is-hidden', left >= maxLeft - 4)
        }
        galleryViewport.addEventListener('scroll', syncGalleryNav, { passive: true })
        requestAnimationFrame(syncGalleryNav)
        gallery.append(prevBtn, nextBtn)
      }
      content.appendChild(gallery)

      const hero = el('section', 'av-rec-detail-section av-rec-detail-hero')
      const heroHead = el('div', 'av-rec-detail-hero__head')
      const heroMeta = el('div', 'av-rec-detail-hero__meta')
      const codeText = detailCode(video) || code
      if (codeText) heroMeta.appendChild(el('span', 'av-rec-detail-hero__code', codeText))
      heroMeta.appendChild(el('h2', 'av-rec-detail-hero__title', detailTitle(video) || detailTitle(item)))
      const subtitle = String(video?.origin_title || '').trim()
      if (subtitle && subtitle !== String(video?.title || '').trim()) {
        heroMeta.appendChild(el('p', 'av-rec-detail-hero__subtitle', subtitle))
      }
      const heroBadges = el('div', 'av-rec-detail-hero__badges')
      if (detectCnsub(video)) heroBadges.appendChild(badge('中字', 'good'))
      if (detectCracked(video)) heroBadges.appendChild(badge('破解', 'hot'))
      if (Number(video?.magnets?.length || 0) > 0) heroBadges.appendChild(badge(`${video.magnets.length} 磁链`, 'info'))
      if (item.in_library || video.library?.in_library) heroBadges.appendChild(badge('已入库', 'good'))
      heroMeta.appendChild(heroBadges)
      const heroActions = el('div', 'av-rec-detail-hero__actions')
      const subBtn = el('button', 'av-rec-primary-action', '订阅')
      subBtn.type = 'button'
      subBtn.onclick = () => openSubscription({ ...item, ...video })
      heroActions.appendChild(subBtn)
      heroHead.append(heroMeta, heroActions)
      hero.appendChild(heroHead)
      const overview = el('div', 'av-rec-detail-overview')
      ;[
        ['上映日期', formatReleaseDate(video?.date || video?.release_date || item.release_date)],
        ['时长', video?.duration ? `${video.duration} 分钟` : ''],
        ['评分', video?.score ? String(video.score) : ''],
        ['来源', String(video?.source || video?.site || 'JavDB').trim()],
      ].filter(([, value]) => value).forEach(([label, value]) => {
        const card = el('div', 'av-rec-overview-card')
        card.appendChild(el('span', 'av-rec-overview-card__label', label))
        card.appendChild(el('strong', 'av-rec-overview-card__value', value))
        overview.appendChild(card)
      })
      if (overview.childNodes.length) hero.appendChild(overview)
      content.appendChild(hero)

      const info = el('section', 'av-rec-detail-section')
      const infoHead = el('div', 'av-rec-detail-section__head')
      infoHead.appendChild(el('span', 'av-rec-detail-section__title', '作品信息'))
      info.appendChild(infoHead)
      const meta = el('div', 'av-rec-detail-meta')
      const appendMetaRow = (label, source, tone = 'soft') => {
        const list = (Array.isArray(source) ? source : [source]).filter(Boolean)
        if (!list.length) return
        const row = el('div', 'av-rec-meta-row')
        row.appendChild(el('span', 'av-rec-meta-label', label))
        const badges = el('div', 'av-rec-meta-badges')
        list.forEach(entry => badges.appendChild(badge(entry?.name || entry?.label || String(entry || ''), tone)))
        row.appendChild(badges)
        meta.appendChild(row)
      }
      appendMetaRow('演员', video.actors || item.actors, 'info')
      appendMetaRow('系列', video.series || item.series, 'soft')
      appendMetaRow('导演', video.director || item.director, 'muted')
      appendMetaRow('制作商', video.maker || item.maker, 'muted')
      appendMetaRow('发行商', video.publisher || item.publisher, 'muted')
      appendMetaRow('类型', video.categories || item.categories, 'soft')
      if (meta.childNodes.length) {
        info.appendChild(meta)
        content.appendChild(info)
      }

      const resourceSection = el('section', 'av-rec-detail-section')
      const resourceHead = el('div', 'av-rec-detail-section__head')
      resourceHead.appendChild(el('span', 'av-rec-detail-section__title', '下载资源'))
      const resourceCount = el('span', 'av-rec-detail-section__meta', '0')
      resourceHead.appendChild(resourceCount)
      resourceSection.appendChild(resourceHead)
      const resourceList = el('div', 'av-rec-magnets')
      const providerBar = el('div', 'av-rec-resource-providers')
      resourceSection.appendChild(resourceList)
      content.appendChild(resourceSection)
      panel.body.appendChild(content)

      const fallbackResources = (Array.isArray(video.magnets) ? video.magnets : []).map((magnet, index) => ({
        id: `javdb:fallback:${index}`,
        provider: 'javdb',
        provider_label: magnet.site || 'JavDB',
        title: magnet.name || detailTitle(video),
        subtitle: [magnet.size || '', magnet.date || '', magnet.site || 'JavDB'].filter(Boolean).join(' · '),
        url: magnet.magnet || '',
        tags: Array.isArray(magnet.tags) ? magnet.tags : [],
        features: {
          has_subtitle: textHasKeywords(magnet.tags || magnet.name || '', ['中字', '字幕', '中文', '中文字幕', 'chs', 'cht']),
          is_cracked: textHasKeywords(magnet.tags || magnet.name || '', ['破解', '破解版', '无码破解', 'uncensored leak']),
          is_private_tracker: false,
        },
        compatible_downloaders: [],
        preferred_downloader: null,
      }))

      let selectedProvider = ''
      const renderResources = (resources, resetSelection = false) => {
        if (resetSelection) selectedProvider = ''
        resourceCount.textContent = String(resources.length)
        providerBar.innerHTML = ''
        resourceList.innerHTML = ''
        if (!resources.length) {
          if (providerBar.parentNode) providerBar.remove()
          resourceList.appendChild(el('div', 'av-rec-no-data', '暂无磁链资源'))
          return
        }
        const providerGroups = []
        const providerMap = new Map()
        resources.forEach(resource => {
          const key = String(resource.provider || resource.provider_label || 'other')
          const current = providerMap.get(key) || {
            key,
            label: String(resource.provider_label || resource.provider || '未知来源'),
            count: 0,
            isPrivateTracker: false,
          }
          current.count += 1
          current.isPrivateTracker = current.isPrivateTracker || !!resource?.features?.is_private_tracker
          providerMap.set(key, current)
        })
        providerGroups.push(...Array.from(providerMap.values()).sort((a, b) => resourceProviderOrder(a) - resourceProviderOrder(b)))
        if (!providerGroups.some(group => group.key === selectedProvider)) selectedProvider = providerGroups[0]?.key || ''
        providerGroups.forEach(group => {
          const pill = el('button', `av-rec-resource-pill${selectedProvider === group.key ? ' is-active' : ''}`, `${group.label} ${group.count}`)
          pill.type = 'button'
          if (group.isPrivateTracker) pill.dataset.tone = 'warning'
          pill.onclick = () => {
            selectedProvider = group.key
            renderResources(resources)
          }
          providerBar.appendChild(pill)
        })
        if (providerGroups.length && !providerBar.parentNode) resourceSection.insertBefore(providerBar, resourceList)
        resources.filter(resource => String(resource.provider || resource.provider_label || 'other') === selectedProvider).forEach(resource => {
          const row = el('div', 'av-rec-magnet-row')
          const info = el('div', 'av-rec-magnet-info')
          info.appendChild(el('div', 'av-rec-magnet-name', resource.title || '未知资源'))
          const line = compactResourceSubtitle(resource)
          if (line) info.appendChild(el('div', 'av-rec-magnet-meta', line))
          const tagRow = el('div', 'av-rec-magnet-tags')
          if (resource?.features?.has_subtitle) tagRow.appendChild(badge('中字', 'good'))
          if (resource?.features?.is_cracked) tagRow.appendChild(badge('破解', 'hot'))
          if (resource?.features?.is_private_tracker) tagRow.appendChild(badge('PT', 'warn'))
          if (tagRow.childNodes.length) info.appendChild(tagRow)
          row.appendChild(info)
          if (sdk.downloads?.open) {
            const pushBtn = el('button', 'av-rec-resource-btn', '推送下载')
            pushBtn.type = 'button'
            pushBtn.onclick = async () => {
              try {
                const resolved = (await sdk.api.post('/plugins/resources/resolve-download', {
                  provider_id: resource.provider,
                  item: resource,
                })).data
                const resolvedItem = resolved?.item || resource
                const resolvedUrl = resolved?.url || resolvedItem?.url
                const downloaderIds = Array.isArray(resolvedItem?.compatible_downloaders) ? resolvedItem.compatible_downloaders.filter(Boolean) : []
                const downloaderId = resolvedItem?.preferred_downloader || downloaderIds[0]
                if (!downloaderId || !resolvedUrl) throw new Error('资源链接解析失败')
                await sdk.downloads.open({
                  downloaderId,
                  downloaderIds,
                  url: resolvedUrl,
                  title: detailTitle(video),
                  rename: detailTitle(video),
                })
                await recordBehavior({ ...item, ...video }, 'download_intent', {
                  provider: resource.provider || '',
                  downloader_id: downloaderId,
                })
              } catch (e) {
                sdk.toast?.error?.(e?.response?.data?.detail || e?.message || '推送失败')
              }
            }
            row.appendChild(pushBtn)
          }
          resourceList.appendChild(row)
        })
      }
      renderResources(fallbackResources)
      sdk.api.post('/plugins/resources/search', {
        query: { code, title: detailTitle(item), expected_magnets_count: expectedMagnetsCount },
        providers: ['javdb', 'avdb', 'mteam-plugin'],
        limit_per_plugin: 6,
      }).then(resourceRes => {
        if (state.activePanel !== panel) return
        const brokerResources = Array.isArray(resourceRes?.data?.items) ? resourceRes.data.items : []
        const seen = new Set()
        const merged = [...brokerResources, ...fallbackResources].filter(resource => {
          const key = `${resource.provider || resource.provider_label}:${resource.id || resource.url || resource.title}`
          if (seen.has(key)) return false
          seen.add(key)
          return true
        })
        renderResources(merged, true)
      }).catch(() => {
        if (state.activePanel === panel && !fallbackResources.length) renderResources([])
      })
    } catch (e) {
      sdk.toast?.error?.(e?.response?.data?.detail || e?.message || '加载详情失败')
      closeActivePanel()
    }
  }

  function renderItems() {
    if (state.loading && !state.data) return skeleton()
    resetExposureObserver()
    grid.innerHTML = ''
    const items = state.data?.items || []
    if (!items.length) {
      const empty = el('div', 'av-rec-empty')
      empty.innerHTML = '<strong>暂无推荐</strong><span>可以先重建 Knowledge Core，或确认 JavDB 插件已启用。</span>'
      grid.appendChild(empty)
      return
    }
    for (const item of items) {
      const card = el('article', 'av-rec-card')
      card.__recommendationItem = item
      const image = el('button', 'av-rec-cover')
      image.type = 'button'
      image.onclick = () => openDetail(item)
      renderFallbackImage(
        image,
        imageCandidates(item.image_candidates, item.fanart_url, item.cover_url, item.thumb_url, item.image, item.poster_url, item.jacket_url),
        item.code || 'NO IMAGE',
        () => refreshStoredImageCandidates(sdk, item.code || item.number),
      )
      const body = el('section', 'av-rec-body')
      const head = el('div', 'av-rec-card-head')
      const title = el('button', 'av-rec-card-title')
      title.type = 'button'
      title.onclick = () => openDetail(item)
      title.innerHTML = `<strong>${escapeHtml(item.code)}</strong><span>${escapeHtml(item.title || '')}</span>`
      const score = el('div', `av-rec-score av-rec-score--${scoreTone(item.score)}`)
      score.innerHTML = `<strong>${escapeHtml(item.score)}</strong><span>推荐分</span>`
      head.append(title, score)
      const meta = el('div', 'av-rec-meta')
      if (item.release_date) meta.appendChild(badge(item.release_date, 'muted'))
      if (item.in_library) meta.appendChild(badge('已入库', 'good'))
      if (item.is_today_increment) meta.appendChild(badge('今日新增', 'hot'))
      if (item.magnets_count) meta.appendChild(badge(`${item.magnets_count} 磁链`, 'good'))
      for (const source of (item.source_tags || []).slice(0, 3)) meta.appendChild(badge(source.label || source.id, 'info'))
      const resourceSummary = item.resource_summary || {}
      if (resourceSummary.total) meta.appendChild(badge(`${resourceSummary.total} 资源`, 'info'))
      if (item.has_cnsub) meta.appendChild(badge('中字', 'good'))
      if (item.is_cracked) meta.appendChild(badge('破解', 'hot'))
      const size = fmtSizeMb(item.best_resource_size_mb) || fmtBytes(resourceSummary.best_size_bytes)
      if (size) meta.appendChild(badge(size, 'soft'))
      const people = el('div', 'av-rec-tags')
      if (item.personalized_score) people.appendChild(badge(`偏好 ${Math.round(item.personalized_score)}`, 'hot'))
      if (item.confidence) people.appendChild(badge(`置信 ${Math.round(item.confidence)}`, 'good'))
      if (item.match_level === 'strong') people.appendChild(badge('强匹配', 'hot'))
      else if (item.match_level === 'medium') people.appendChild(badge('中匹配', 'good'))
      else if (item.match_level === 'weak') people.appendChild(badge('弱匹配', 'soft'))
      for (const source of (resourceSummary.providers || []).slice(0, 3)) people.appendChild(badge(`${source.name}×${source.count}`, 'info'))
      for (const name of (item.actors || []).slice(0, 4)) people.appendChild(badge(name, 'soft'))
      if (item.series) people.appendChild(badge(`系列 ${item.series}`, 'soft'))
      if (item.director) people.appendChild(badge(`导演 ${item.director}`, 'muted'))
      for (const name of (item.title_traits || []).slice(0, 3)) people.appendChild(badge(`标题 ${name}`, 'info'))
      for (const name of (item.categories || []).slice(0, 5)) people.appendChild(badge(name, 'muted'))
      const breakdown = item.score_breakdown || {}
      const scoreParts = el('div', 'av-rec-score-parts')
      const scorePartValues = [
        ['演员', breakdown.actor_preference ?? breakdown.preference],
        ['类型', breakdown.category_preference],
        ['关系', breakdown.relationship_preference],
        ['趋势', breakdown.trend],
        ['主题', breakdown.interest_topic],
        ['资源', breakdown.resources],
        ['质量', breakdown.quality],
        ['降权', breakdown.penalty ? `-${Math.round(breakdown.penalty)}` : '0'],
      ]
      for (const [label, value] of scorePartValues) {
        const part = el('span')
        part.innerHTML = `<em>${escapeHtml(label)}</em><strong>${escapeHtml(value == null || value === '' ? 0 : Math.round(Number(value) || 0))}</strong>`
        scoreParts.appendChild(part)
      }
      const reasons = el('ul', 'av-rec-reasons')
      for (const reason of item.reasons || []) {
        const li = document.createElement('li')
        li.textContent = reason
        reasons.appendChild(li)
      }
      const explanation = item.recommendation_explanation || {}
      const explanationBox = el('details', 'av-rec-explanation')
      const interval = explanation.confidence || item.confidence_interval || {}
      const explanationTitle = document.createElement('summary')
      explanationTitle.textContent = `为什么推荐${interval.lower != null && interval.upper != null ? ` · 置信 ${Math.round(interval.lower)}–${Math.round(interval.upper)}` : ''}`
      const explanationFactors = el('div', 'av-rec-explanation-factors')
      const allFactors = explanation.factors || []
      const visibleFactors = allFactors.slice(0, 6)
      for (const factor of visibleFactors) {
        const node = document.createElement('span')
        node.innerHTML = `<em>${escapeHtml(factor.label || factor.type || '证据')}</em><strong>${escapeHtml(Math.round(Number(factor.score) || 0))}</strong>`
        explanationFactors.appendChild(node)
      }
      for (const factor of (explanation.counterfactors || []).slice(0, 2)) {
        const node = document.createElement('span')
        node.className = 'is-negative'
        node.innerHTML = `<em>${escapeHtml(factor.label || '降权')}</em><strong>${escapeHtml(Math.round(Number(factor.score) || 0))}</strong>`
        explanationFactors.appendChild(node)
      }
      const evidence = el('div', 'av-rec-explanation-evidence')
      const actorFactor = allFactors.find(factor => factor?.type === 'actor')
      const actorEvidence = Array.isArray(actorFactor?.evidence) ? actorFactor.evidence : []
      if (actorEvidence.length) {
        const mapped = actorEvidence.filter(row => row?.identity_source === 'mdc-ng').length
        const names = actorEvidence.map(row => `${row.name}（媒体库 ${row.library_count || 0} 部）`).join('、')
        evidence.appendChild(el('p', '', `演员依据：${names}${mapped ? ` · ${mapped} 位由 MDC-NG 统一身份` : ''}`))
      }
      const semanticFactor = allFactors.find(factor => factor?.type === 'semantic')
      const semanticEvidence = Array.isArray(semanticFactor?.evidence) ? semanticFactor.evidence : []
      if (semanticEvidence.length) {
        evidence.appendChild(el('p', '', `标题画像：${semanticEvidence.slice(0, 4).map(row => `${row.name}（媒体库 ${Math.round(Number(row.count) || 0)} 次）`).join('、')}`))
      }
      const resourceFactor = allFactors.find(factor => factor?.type === 'resource_calibration')
      if (resourceFactor?.evidence) {
        const labels = {
          cracked: '破解', subtitle: '中字', uncensored: '无码', private: '私有源', public: '公开源',
          multi_provider: '多来源', availability_4plus: '资源充足', provider_avdb: 'AVDB', provider_mteam: 'M-TEAM', provider_javdb: 'JavDB',
        }
        const features = (resourceFactor.evidence.features || []).map(value => labels[value] || value).slice(0, 5)
        const factor = Number(resourceFactor.evidence.factor || 1)
        if (features.length || Math.abs(factor - 1) >= 0.001) evidence.appendChild(el('p', '', `资源学习：${features.join('、') || '基础可用性'} · 权重 ×${factor.toFixed(2)}`))
      }
      explanationBox.append(explanationTitle, explanationFactors)
      if (evidence.childElementCount) explanationBox.appendChild(evidence)
      const buttons = el('div', 'av-rec-card-actions')
      const viewBtn = el('button', '', '详情')
      viewBtn.onclick = () => openDetail(item)
      const subBtn = el('button', 'av-rec-primary', '订阅')
      subBtn.onclick = () => openSubscription(item)
      const likeBtn = el('button', '', '喜欢')
      likeBtn.onclick = () => feedback(item, 'like')
      const dislikeBtn = el('button', '', '不感兴趣')
      dislikeBtn.onclick = () => openDislikePicker(item)
      const ignoreBtn = el('button', '', '忽略')
      ignoreBtn.onclick = () => feedback(item, 'ignore')
      buttons.append(viewBtn, subBtn, likeBtn, dislikeBtn, ignoreBtn)
      body.append(head, meta, people, scoreParts, reasons, explanationBox, buttons)
      card.append(image, body)
      grid.appendChild(card)
      exposureObserver?.observe?.(card)
    }
  }

  function render() {
    renderModes()
    renderProfile()
    renderNotice()
    renderItems()
  }

  function clearResourcePoll() {
    if (resourcePoll?.clear) resourcePoll.clear()
    resourcePoll = null
  }

  function scheduleSnapshotPoll() {
    if (snapshotPoll?.clear) snapshotPoll.clear()
    snapshotPoll = sdk.timers?.setTimeout
      ? sdk.timers.setTimeout(() => load(false, true), 2500)
      : { id: window.setTimeout(() => load(false, true), 2500), clear() { window.clearTimeout(this.id) } }
  }

  function scheduleResourcePoll(delay = 3500) {
    clearResourcePoll()
    resourcePoll = sdk.timers?.setTimeout
      ? sdk.timers.setTimeout(() => syncBackgroundResources(), delay)
      : { id: window.setTimeout(() => syncBackgroundResources(), delay), clear() { window.clearTimeout(this.id) } }
  }

  async function syncBackgroundResources() {
    if (resourceSyncing) return
    resourceSyncing = true
    try {
      const response = await sdk.api.get('/knowledge/resources/refresh/status', { timeout: 10000 })
      const counts = response.data?.counts || {}
      const count = Number(counts.queued || 0) + Number(counts.running || 0)
      const wasActive = state.resourceSync.active
      state.resourceSync.active = count > 0
      state.resourceSync.count = count
      renderNotice()
      if (count > 0) {
        scheduleResourcePoll()
      } else if (wasActive) {
        await load(false, true)
        state.resourceSync.updated = true
        renderNotice()
        scheduleResourcePoll(12000)
      }
    } catch {
      if (state.resourceSync.active) scheduleResourcePoll(6000)
    } finally {
      resourceSyncing = false
    }
  }

  async function load(refresh = false, silent = false) {
    if (!silent) state.loading = true
    state.error = ''
    if (!silent) render()
    try {
      const resp = await sdk.api.post('/plugins/av-recommend/actions/recommendations', {
        payload: { source_mode: state.sourceMode, limit: 60, refresh },
      }, { timeout: 90000 })
      state.data = resp.data
      if (state.data?.cache_status?.status === 'stale' && state.data?.cache_status?.refreshing) scheduleSnapshotPoll()
      else if (snapshotPoll?.clear) {
        snapshotPoll.clear()
        snapshotPoll = null
      }
      const queuedNotice = (state.data?.warnings || []).find(value => /^正在后台补全\s*\d+/.test(String(value || '')))
      if (refresh && queuedNotice) {
        state.resourceSync.active = true
        state.resourceSync.updated = false
        state.resourceSync.count = Number(String(queuedNotice).match(/\d+/)?.[0] || 0)
      }
    } catch (e) {
      state.error = e?.response?.data?.detail || e?.message || '推荐加载失败'
    } finally {
      if (!silent) state.loading = false
      render()
      await syncBackgroundResources()
    }
  }

  await load(false)
}
