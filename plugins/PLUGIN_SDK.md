# NOOR Plugin SDK / Component Roadmap

目标：插件不是另一套小网站。插件应能复用 NOOR 主程序提供的 UI、API、配置、弹窗、toast、分页、表单和任务能力，让用户或 AI 按文档/CLI/Skill 快速生成一致风格的插件。

## 1. 当前现实

当前插件前端入口是：

```js
export async function mount(el, sdk) {
  el.innerHTML = '...'
  return () => { el.innerHTML = '' }
}
```

这能跑，但插件只能写原生 JS/HTML/CSS，因此 M-Team 这类插件暂时不能直接 `import VuiPagination.vue`。这不是最终目标，只是第一阶段为了快速打通插件页面、sidebar、配置、RSS、下载器和字幕搜索。

## 2. 目标架构

后续插件前端应支持两种模式。

### 2.1 Lite 模式：无构建插件

适合简单插件、AI 快速生成、用户手写。

主程序通过 `sdk.ui` 暴露可生成 DOM 的组件函数：

```js
export async function mount(el, sdk) {
  const page = sdk.ui.page({ className: 'demo-page' })
  page.append(
    sdk.ui.tabs({ value: 'rss', tabs: [{ key: 'rss', label: 'RSS' }], onChange: key => {} }),
    sdk.ui.pagination({ page: 1, total: 120, onPage: page => {} })
  )
  el.replaceChildren(page)
  return () => page.remove()
}
```

优点：

- 不需要插件构建工具。
- 用户复制一个 `page.js` 就能跑。
- AI 生成成本最低。

### 2.2 Vue 模式：组件插件

适合复杂插件。

插件可以导出 Vue component 或 setup 函数，由主程序挂载，并注入主程序组件库：

```js
import Page from './Page.vue'

export default {
  component: Page,
}
```

主程序提供：

```ts
sdk.vue.app
sdk.vue.components.VuiButton
sdk.vue.components.VuiPagination
sdk.vue.components.VuiTabs
sdk.vue.components.VuiSubmitButton
sdk.vue.components.BaseModal
```

优点：

- 插件可以真正复用主程序 Vue 组件。
- 页面复杂度高时比原生 JS 更可维护。

## 3. SDK 必须暴露的能力

### 3.1 API

```ts
sdk.api.fetch(path, init?)
sdk.api.plugin(path, init?)
sdk.api.download(url, filename?)
```

约束：

- 插件禁止硬编码后端 host/port。
- 插件调用自身接口必须优先用 `sdk.api.plugin()`。

### 3.2 UI

第一批应提供：

```ts
sdk.ui.button(options)
sdk.ui.submitButton(options)
sdk.ui.tabs(options)
sdk.ui.pagination(options)
sdk.ui.badge(options)
sdk.ui.card(options)
sdk.ui.modal(options)
sdk.ui.confirm(options)
sdk.ui.skeletonGrid(options)
sdk.ui.emptyState(options)
sdk.ui.notice(options)
```

其中分页能力对齐主程序 `VuiPagination`：

- 上一页 / 下一页
- 页码窗口
- 移动端压缩
- Home / End / PageUp / PageDown
- 默认不展示总数和每页数量



### 3.5 交互式提交按钮

`submitButton` 是 NOOR 标准的“按钮 + 进度条 + 最终反馈”组件，用于所有提交任务、推送下载、索引重建等动作。

```js
const btn = sdk.ui.submitButton({
  idleLabel: '推送下载',
  successLabel: '推送成功',
  errorLabel: '推送失败',
  status: 'idle', // idle | running | success | error
  progress: 0,
  onClick: submit,
})

btn.__setState('running', 45, '45%')
btn.__setState('success', 100, '推送成功')
btn.__setState('error', 100, '推送失败')
```

规则：

- 点击后按钮自身显示进度，不额外插入大进度条。
- 成功/失败状态保留到页面刷新或业务显式重置。
- 进行中不要反复重绘整张列表；只更新按钮自身状态，避免闪烁。
- 提交字幕、LADA、下载推送、图谱重建都应优先使用这个组件。

### 3.4 反向沉淀规则

插件里出现稳定、复用价值高且符合 NOOR 视觉 token 的模式时，不应长期停留在插件私有 CSS 中。处理顺序：

1. 先抽象为 `sdk.ui.*` 通用接口。
2. 主程序页面和其他插件都通过同一接口复用。
3. 插件私有 CSS 只保留业务布局、特殊网格和数据可视化。

当前已从插件实践沉淀到 SDK 的组件：

- `topBar`：左 tabs / 右 actions 的页面顶部操作行。
- `actionRow`：统一 30px 操作按钮/状态排列容器。
- `statCard` / `statGrid`：qB、迅雷、JavDB、推荐中心都需要的统计卡。
- `mediaCard`：M-Team / JavDB / AVDB 这类横向图卡。
- `loadingState`：统一 spinner + 文案，避免插件各自画加载框。

### 3.3 Toast / Dialog

```ts
sdk.toast.success(message)
sdk.toast.error(message)
sdk.toast.warning(message)
sdk.toast.info(message)
sdk.dialog.confirm(options)
sdk.dialog.open(componentOrHtml, options)
```

插件禁止使用浏览器原生：

```js
alert()
confirm()
prompt()
```

### 3.4 配置

```ts
sdk.config.get()
sdk.config.set(partial)
sdk.config.open()
```

插件不直接读写 `.env`。

### 3.5 主程序集成

```ts
sdk.router.push(path)
sdk.jobs.open(jobId)
sdk.downloaders.submit(payload)
sdk.subtitles.search(videoCode)
```

这类能力必须是抽象接口，不能让插件知道主程序内部 store 结构。

## 4. 插件 CLI 目标

目标命令：

```bash
noor-plugin create mteam-like --type rss_source --with sidebar_page,download_submit
noor-plugin dev ./plugins/my-plugin
noor-plugin validate ./plugins/my-plugin
noor-plugin pack ./plugins/my-plugin
```

生成结构：

```text
plugins/my-plugin/
  plugin.json
  backend.py
  frontend/page.js
  frontend/style.css
  README.md
  CAPABILITIES.md
```

`validate` 至少检查：

- manifest 合法性。
- capability 与 handler 是否匹配。
- sidebar 页面是否导出 `mount()`。
- CSS 是否带插件前缀。
- 是否使用了 `alert/confirm/prompt`。
- 是否硬编码主程序 host/port。

## 5. AI / Skill 目标

后续可以提供 Codex/Claude Skill：

```text
用 NOOR 插件 SDK 创建一个 RSS + 下载推送插件，插件名 xxx，sidebar 显示 xxx。
```

Skill 应读取：

- `plugins/PLUGIN_DEVELOPMENT.md`
- `plugins/PLUGIN_DESIGN.md`
- `plugins/PLUGIN_SDK.md`
- 插件模板目录

然后自动生成：

- manifest
- backend handler
- frontend page
- config schema
- tests

## 6. 当前过渡约定

在 SDK UI 真正落地前：

- 插件仍使用原生 `mount(el, sdk)`。
- 插件 CSS 必须尽量复刻 NOOR token 和组件样式。
- 不允许为单个插件继续在主程序写专用页面。
- 新增复杂插件时，应优先把通用能力沉淀到 `sdk.ui`，而不是复制 CSS。

## 6.1 已落地的 Lite UI 初版

主程序现在已经在插件宿主中注入 `sdk.ui`。原生 `mount(el, sdk)` 插件可以直接使用，不需要额外构建。

已可用：

```js
sdk.ui.page({ children })
sdk.ui.button({ label, tone, active, disabled, onClick })
sdk.ui.submitButton({ idleLabel, status, progress, onClick })
sdk.ui.tabs({ value, tabs, onChange })
sdk.ui.pagination({ page, totalPages, onPage })
sdk.ui.badge({ label, tone, active, onClick })
sdk.ui.chip({ label, active, onClick })
sdk.ui.card({ children, href, target, onClick })
sdk.ui.notice({ text, tone })
sdk.ui.emptyState({ text })
sdk.ui.skeletonCard()
sdk.ui.skeletonGrid({ count })
sdk.ui.field({ label, hint, control })
sdk.ui.input({ value, placeholder, readonly, onInput })
sdk.ui.textarea({ value, placeholder, rows, readonly, onInput })
sdk.ui.select({ value, options, onChange })
sdk.ui.modal({ title, content, width, footer, onClose })
sdk.ui.confirm({ title, message, confirmText, danger })
sdk.ui.previewImage(src, images)
sdk.ui.dialog(options) // modal alias
```

约束：

- 插件页面优先用 `sdk.ui.*`，不要复制一套按钮、分页、弹窗交互。
- `sdk.ui.pagination()` 已带 `Home / End / PageUp / PageDown` 键盘翻页，并自动跳过输入框。
- `sdk.ui.modal()` 用于插件自定义弹窗，宽度支持 `sm / md / lg`，默认使用 NOOR 弹窗外壳。
- `sdk.ui.confirm()` 基于 `sdk.ui.modal()`，替代浏览器原生 `confirm()`，视觉与主程序一致。
- 复杂插件仍可保留自己的布局 CSS，但基础控件应逐步迁移到 SDK。

当前试点：`plugins/mteam-plugin/frontend/page.js` 已使用 `sdk.ui.tabs()`、`sdk.ui.badge()`、`sdk.ui.chip()`、`sdk.ui.card()`、`sdk.ui.pagination()`、`sdk.ui.modal()`、`sdk.ui.field/input/select()`、`sdk.ui.submitButton()` 和 `sdk.ui.confirm()`；`plugins/qbittorrent/frontend/page.js` 已使用 `tabs / notice / emptyState / modal / confirm / field / input / button`。

## 7. 实施顺序

建议分三步：

1. **先做 `sdk.ui` Lite 组件**
   - button
   - tabs
   - pagination
   - badge
   - modal/confirm
   - empty/loading/error

2. **迁移内置插件使用 `sdk.ui`**
   - mteam-plugin
   - qbittorrent
   - local-subtitle-library 设置页片段

3. **再做 Vue 模式插件**
   - 支持插件导出 Vue component
   - 主程序注入 NOOR 组件库
   - CLI 支持 `--frontend vue`


## 8. AI 生成插件页面提示词

给 AI 或普通开发者生成 NOOR 插件页面时，直接使用下面约束：

```text
为 NOOR 生成一个插件前端页面。必须使用 mount(el, sdk) 模式。
所有通用 UI 必须使用 sdk.ui：page、topBar、tabs、button、submitButton、badge、chip、pagination、input、select、search、emptyState、loadingState、errorState、modal、confirm。
不要使用 alert/confirm，不要硬编码后端 host/port，不要自定义按钮高度、badge 色板、tab 风格。
插件 CSS 只允许写业务布局和卡片内容，class 必须带插件名前缀。
顶部操作区必须使用 sdk.ui.topBar 或 .noor-plugin-topbar；移动端 actions 必须排在 tabs 上方。
分页使用 sdk.ui.pagination，空/加载/错误态互斥。
```

## 9. 当前 SDK UI 清单

```js
sdk.ui.page({ className, children })
sdk.ui.topBar({ tabs, actions, align })
sdk.ui.button({ label, tone, active, disabled, onClick })
sdk.ui.submitButton({ idleLabel, status, progress, onClick })
sdk.ui.badge({ label, tone, active, onClick })
sdk.ui.chip({ label, active, onClick })
sdk.ui.tabs({ value, tabs, onChange })
sdk.ui.pagination({ page, total, pageSize, totalPages, siblingCount, onPage })
sdk.ui.search({ value, placeholder, onInput, onClear })
sdk.ui.input({ value, placeholder, readonly, disabled, onInput, onKeydown })
sdk.ui.textarea({ value, placeholder, rows, readonly, disabled, onInput, onKeydown })
sdk.ui.select({ value, options, disabled, onChange })
sdk.ui.field({ label, hint, control })
sdk.ui.card({ children, href, target, onClick })
sdk.ui.notice({ text, tone })
sdk.ui.emptyState({ text })
sdk.ui.loadingState({ text })
sdk.ui.errorState({ text })
sdk.ui.skeletonCard()
sdk.ui.skeletonGrid({ count })
sdk.ui.previewImage(src, images)
sdk.ui.modal({ title, content, width, footer, onClose })
sdk.ui.dialog(options) // modal alias
sdk.ui.confirm({ title, message, confirmText, danger })
```
