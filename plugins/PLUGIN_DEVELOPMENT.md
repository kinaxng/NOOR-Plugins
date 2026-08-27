# NOOR Plugin Development

本文档固定 NOOR 插件开发约定。目标是让插件贡献能力，而不是让主程序为某个具体插件硬编码业务。

## 1. Manifest

每个插件必须提供 `plugin.json`。

```json
{
  "id": "qbittorrent",
  "name": "qBittorrent",
  "version": "0.3.0",
  "type": "downloader",
  "description": "qBittorrent 下载器接入与 WebUI 管理页面",
  "tags": ["qb", "downloader"],
  "capabilities": ["network_outbound", "download_submit", "sidebar_page"],
  "config_schema": {},
  "default_config": {},
  "contributions": {},
  "frontend": {}
}
```

### `type`

当前支持：

- `rss_source`：RSS/PT 源。
- `downloader`：下载器。
- `dashboard_widget`：概览卡片。
- `subtitle_provider`：字幕搜索源，例如本地字幕库、迅雷字幕、M-Team 字幕。

### `capabilities`

常用能力：

- `network_outbound`：插件会访问外部网络。
- `rss_fetch`：提供 RSS/Feed 列表。
- `download_submit`：可接收下载推送。
- `sidebar_page`：贡献一个侧边栏页面。
- `subtitle_search`：可参与字幕搜索。
- `subtitle_search_local`：本地字幕源；用户选择“仅本地”时只调用这类插件。
- `dashboard_widget`：贡献概览卡片。
- `local_metrics`：读取本机状态。

能力是运行时和 UI 的声明，不等于自动实现。真正逻辑应在 `backend.py` 或前端模块中实现。

## 1.1 Manifest 健康约束

内置插件必须满足以下约束，已有回归测试覆盖：

- 插件目录名必须等于 `plugin.json` 里的 `id`。
- `id` 只能使用小写字母、数字和 `-`。
- 声明 `sidebar_page` capability 时，必须同时声明：
  - `contributions.sidebar.label`
  - `contributions.sidebar.route = /plugins/<plugin-id>`
  - `contributions.sidebar.icon`
  - `frontend.entry`
- 声明 `frontend.entry` / `frontend.style` 时，文件必须真实存在，路径必须位于插件目录的 `frontend/` 下。
- sidebar 插件的 `frontend.entry` 必须导出 `mount(el, sdk)`，并返回 cleanup 函数。
- sidebar 插件禁止使用浏览器原生 `alert()` / `confirm()`。
- 插件 CSS 类名必须使用插件前缀，例如 `.mteam-*`、`.qb-*`，只有 `.is-active` 这类状态类例外。
- `config_schema` 中每个公开字段必须有 `label`。
- `config_schema.<key>.default` 出现时，`default_config` 中也必须有同名 key。

这些检查位于：

```text
backend/tests/test_builtin_plugin_manifests.py
```

## 2. 配置

`config_schema` 用于设置页生成配置表单。支持字段类型：

- `string`
- `password`
- `number`
- `boolean`
- `select`：通过 `options` 提供选项。

`default_config` 是默认配置。运行时读取配置时会执行：

```text
default_config + 用户保存配置
```

插件代码不要直接读取 `.env` 作为业务配置入口。Docker 和开发环境都应通过设置页保存插件配置。

## 3. 后端 handler

可选文件：

```text
plugins/<plugin-id>/backend.py
```

运行时会按需加载。根据能力实现以下异步函数。

### 通用测试

```python
async def test(config: dict) -> PluginTestResult:
    ...
```

### RSS / Feed

```python
async def fetch_rss_items(manifest, config: dict, limit: int = 30, force_refresh: bool = False) -> dict:
    return {"items": [], "total": 0}
```

Feed item 建议字段：

```json
{
  "title": "原始标题",
  "display_title": "展示标题",
  "link": "详情页",
  "pubDate": "发布时间",
  "category": "分类",
  "image_url": "封面缓存或外链",
  "download_url": "下载链接",
  "enclosure_url": "下载链接",
  "size_bytes": 0
}
```

### 下载器

```python
async def submit_download(config: dict, payload: dict) -> dict:
    return {"ok": True}
```

下载器 payload 约定：

```json
{
  "url": "单个下载链接",
  "urls": "一个或多个下载链接",
  "title": "来源标题",
  "name": "来源名称",
  "category": "可选分类",
  "savepath": "可选保存路径",
  "tag": "可选标签",
  "tags": "可选标签"
}
```

qBittorrent 这类下载器必须只在 `submit_download()` 中给 NOOR 推送任务追加 NOOR 标签；不能扫描或修改用户原本存在的任务。

### 插件 action

```python
async def handle_action(action: str, config: dict, payload: dict) -> dict:
    ...
```

用于插件页面的私有操作，例如 qB 的 `overview`、`properties`、`apply_noor_filter`。

### 字幕搜索

字幕源插件应声明：

```json
{
  "type": "subtitle_provider",
  "capabilities": ["subtitle_search"]
}
```

本地字幕库这类插件额外声明：

```json
{
  "capabilities": ["subtitle_search", "subtitle_search_local"]
}
```

handler：

```python
async def search_subtitles(config: dict, video_code: str) -> list[dict]:
    return []
```

结果字段约定：

```json
{
  "id": "stable-result-id",
  "filename": "TEST-031.zh.srt",
  "ext": ".srt",
  "language": "中文",
  "source": "M-Team",
  "source_key": "mteam-plugin",
  "source_type": "remote_search",
  "url": "mteam://subtitle/55254",
  "score": 0.92
}
```

`url` 可以是普通 HTTP(S) 地址、本地字幕文件路径，或插件私有字幕地址。插件私有地址格式：

```text
<plugin-id>://subtitle/<subtitle-id>
```

若使用私有地址，插件需实现：

```python
async def fetch_subtitle_content(config: dict, subtitle_id: str) -> dict:
    return {"filename": "subtitle.srt", "content": "..."}
    # 或 return {"filename": "subtitle.srt", "bytes": b"..."}
```

本地字幕库结果必须设置：

```json
{ "source_type": "local_library" }
```

这样主程序下载字幕时会执行本地复制，而不是网络下载。

### 概览卡片

```python
async def build_widget(config: dict) -> DashboardWidget | None:
    ...
```

## 4. 前端页面插件

插件需要侧边栏页面时，在 manifest 中声明：

```json
{
  "capabilities": ["sidebar_page"],
  "contributions": {
    "sidebar": {
      "label": "M-Team",
      "route": "/plugins/mteam-plugin",
      "icon": "plugin"
    }
  },
  "frontend": {
    "type": "module",
    "entry": "frontend/page.js",
    "style": "frontend/style.css"
  }
}
```

`frontend/page.js` 必须导出：

```js
export async function mount(el, sdk) {
  el.innerHTML = '...'
  return () => { el.innerHTML = '' }
}
```

SDK 当前提供：

```js
sdk.pluginId
sdk.toast.success(message)
sdk.toast.error(message)
sdk.api.fetch(path, init)
sdk.api.plugin(path, init) // 自动加 /api/plugins/<pluginId>
```
SDK 目标能力见 `plugins/PLUGIN_SDK.md`。后续插件应优先复用 `sdk.ui` / 主程序组件，而不是复制一套 UI。当前原生 JS 插件只是过渡实现。


插件页面禁止使用全局 `alert()` / `confirm()`；应使用主程序 toast 或自定义 NOOR 风格弹窗。

## 5. 设计约束

插件页面必须遵守：

- `frontend/DESIGN.md`
- `plugins/PLUGIN_DESIGN.md`

硬性要求：

- 不自造主色、渐变、阴影体系。
- 页面 loading、empty、error、toast 不能堆叠。
- 一级 tab 使用主程序风格。
- 二级对象选择使用 pill/button。
- 三级筛选使用轻量 chip。
- 插件样式作用域必须以插件前缀开头，例如 `.mteam-`、`.qb-`。

## 6. 测试要求

新增插件或新增核心能力时，至少补一层测试：

- 后端 helper 规则测试。
- runtime 能力边界测试。
- 对真实外部服务的调用必须可 mock。

下载器插件至少覆盖：

- 默认配置合并。
- 提交任务 payload 规范。
- 标签/分类/保存路径规则。
- 小文件过滤、字幕保留等安全规则。

## 7. 边界原则

- 插件贡献页面和能力，主程序不为具体插件写业务页面。
- 主程序只负责：发现插件、配置、启停、挂载页面、调用能力。
- 插件不能默认修改外部系统中的历史数据。
- 真实删除、真实下载、真实任务优先级修改必须有明确触发路径。


### 统一订阅 / 洗版协议（官方内置插件）

NOOR 官方内置插件 `subscription-core` 负责订阅与洗版能力。内容插件不要自行实现长期订阅、定时检测、洗版删除等闭环逻辑。

职责边界：

- 内容插件（JavDB / AVDB / M-Team 等）：提供作品入口、资源搜索结果、订阅按钮入口。
- `subscription-core`：保存订阅、判断订阅/洗版、执行资源匹配、计算资源评分、记录订阅事件。
- 下载器插件：继续只提供统一下载能力。
- 媒体库 / 硬链接模块：负责入库确认和后续旧版本删除链路。

内容插件创建订阅时调用：

```js
await sdk.api.post('/plugins/subscription-core/actions/create', {
  payload: {
    code: 'TEST-027',
    title: '作品标题',
    cover_url: 'https://...',
    type: 'auto',              // auto: 媒体库已有则洗版，否则订阅
    mode: 'loose',             // loose | strict
    require_cracked: true,
    require_subtitle: false
  }
})
```

订阅模式：

- `loose`：宽松订阅。按 破解+中字 > 破解 > 中字 > 普通 排序，普通资源也可作为候选。
- `strict`：严格订阅。用户勾选的条件必须满足；破解+中字表示必须同时满足。

`subscription-core` 当前动作：

- `overview`：订阅列表、统计、最近事件。
- `classify`：根据媒体库判断指定番号是 `subscribe` 还是 `upgrade`。
- `create`：创建订阅，`type=auto` 时自动分类。
- `update`：更新订阅条件或状态。
- `delete`：删除订阅。
- `check_once`：手动检测一个或全部订阅。
- `evaluate_resource`：按统一规则为资源计算特征与评分。

洗版删除旧版本必须由后续确认链路执行：下载完成 → 媒体库刷新 → 确认新版本入库且番号一致 → 标记洗版 → 删除旧版本。不要在下载提交后立即删除旧文件。

## Plugin-private storage and lifecycle

插件不得把运行数据写入源码目录、共享 `data/plugin_cache` 或顶层 `data/<name>`。统一使用：

```python
from app.core.runtime_paths import plugin_cache_path, plugin_data_path, plugin_logs_path

data_file = plugin_data_path(PLUGIN_ID, "state.json")
cache_file = plugin_cache_path(PLUGIN_ID, "covers", "123.jpg")
log_file = plugin_logs_path(PLUGIN_ID, "runtime.log")
```

物理布局固定为：

```text
data/plugins/<plugin-id>/
├── config.json
├── data/
├── cache/
└── logs/
```

- 插件配置由 runtime 写入 `config.json`；插件不应另建第二份配置。
- 禁用和升级保留整个私有目录。
- 卸载默认删除代码、配置、数据、缓存和日志；用户可选择保留私有目录，以便重新安装时恢复。
- 如需卸载前释放外部资源，可实现 `async on_uninstall(config, *, purge_data: bool)`。
- 路径 helper 会将旧 `data/<plugin_id>`、下划线别名和 `data/plugin_cache/<plugin_id>` 非破坏性复制到新目录；旧目录只作为迁移回滚证据，不再写入。
