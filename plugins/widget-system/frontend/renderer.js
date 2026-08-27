function number(value) {
  const parsed = Number(value || 0)
  return Number.isFinite(parsed) ? parsed : 0
}

function percent(value) {
  return Math.max(0, Math.min(100, Math.round(number(value))))
}

function memoryPercent(used, total) {
  return number(total) > 0 ? percent((number(used) / number(total)) * 100) : 0
}

function metric(label, value, tone) {
  return `<div class="widget-system-metric widget-system-metric--${tone}">
    <div><span>${label}</span><strong>${value}%</strong></div>
    <i><b style="width:${value}%"></b></i>
  </div>`
}

function render(container, data, collapsed) {
  const gpu = data?.gpu || {}
  const cpu = data?.cpu_mem || {}
  const values = [
    ['CPU', percent(cpu.cpu_util), 'cpu'],
    ['GPU', percent(gpu.gpu_util), 'gpu'],
    ['RAM', memoryPercent(cpu.mem_used, cpu.mem_total), 'ram'],
    ['VRAM', memoryPercent(gpu.mem_used, gpu.mem_total), 'vram'],
  ]
  if (collapsed) {
    container.innerHTML = `<div class="widget-system-collapsed">${values.map(([label, value, tone]) =>
      `<div class="widget-system-dot widget-system-dot--${tone}" style="--value:${value * 3.6}deg"><span>${value}</span><em>${label}</em></div>`
    ).join('')}</div>`
    return
  }
  container.innerHTML = `<div class="widget-system-sidebar">
    ${values.map(([label, value, tone]) => metric(label, value, tone)).join('')}
    <div class="widget-system-footer"><span>CPU ${number(cpu.cpu_temp).toFixed(0)}°C</span><span>GPU ${number(gpu.temp).toFixed(0)}°C · ${number(gpu.power).toFixed(0)}W</span></div>
  </div>`
}

export async function renderSidebarWidget(container, ctx) {
  let stopped = false
  let timer = null
  const update = async () => {
    try {
      const response = await ctx.sdk.api.post(`/plugins/${ctx.pluginId}/actions/metrics`, { payload: {} })
      const body = response?.data || response || {}
      if (!stopped) render(container, body.data || body, !!ctx.collapsed)
      const delay = Math.max(1000, number(body.poll_interval_ms) || 5000)
      if (!stopped) timer = window.setTimeout(update, delay)
    } catch {
      if (!stopped) timer = window.setTimeout(update, 5000)
    }
  }
  await update()
  return () => {
    stopped = true
    if (timer) window.clearTimeout(timer)
  }
}

export function renderDashboardWidget(container, ctx) {
  render(container, ctx.payload || ctx.widget?.payload || {}, false)
}
