# NOOR Plugin CLI 草案

NOOR 插件 CLI 的目标是：让用户、开发者或 AI 不需要理解主程序内部结构，也能快速创建风格一致、能力完整、可验证的插件。

## 1. 命令设计

```bash
noor-plugin create <plugin-id>
noor-plugin dev <plugin-path>
noor-plugin validate <plugin-path>
noor-plugin pack <plugin-path>
noor-plugin install <repo-or-zip>
```

## 2. create

```bash
noor-plugin create mteam-plugin \
  --type rss_source \
  --cap sidebar_page,rss_fetch,download_submit,subtitle_search \
  --frontend lite
```

输出：

```text
plugins/mteam-plugin/
  plugin.json
  backend.py
  frontend/page.js
  frontend/style.css
  README.md
  tests/test_mteam_plugin.py
```

## 2.1 当前已落地的 create 初版

仓库内当前可直接执行：

```bash
scripts/noor-plugin create demo-plugin --type rss_source
scripts/noor-plugin create my-tool --type tool --name "My Tool"
```

默认会生成：

```text
plugins/<plugin-id>/
  plugin.json
  backend.py
  frontend/page.js      # 声明 sidebar_page 时生成
  frontend/style.css    # 声明 sidebar_page 时生成
  README.md
```

生成的前端模板默认使用：

- `sdk.api.plugin()`
- `sdk.ui.page()`
- `sdk.ui.button()`
- `sdk.ui.skeletonGrid()`
- `sdk.ui.notice()`
- `sdk.ui.emptyState()`
- `sdk.ui.card()`

因此新插件不会从第一天就复制一套 UI。


## 3. validate

检查项：

- `plugin.json` schema。
- `id` 与目录名一致。
- capability 与后端 handler 对应。
- frontend entry/style 文件存在。
- `mount(el, sdk)` 是否存在。
- 禁止 `alert/confirm/prompt`。
- 禁止硬编码 `localhost:9898`、`127.0.0.1:9898`、`192.168.*` 这类主程序地址。
- CSS class 是否使用插件前缀。
- 是否引用 NOOR 设计 token。

## 3.1 当前已落地的 validate 初版

仓库内当前可直接执行：

```bash
scripts/noor-plugin validate plugins
scripts/noor-plugin validate plugins/mteam-plugin --strict
```

当前检查项：

- `plugin.json` 必填字段。
- `id` 与插件目录名一致。
- `id` 只能使用小写字母、数字、连字符。
- `type` / `capabilities` 是否在已知集合内。
- `sidebar_page` 插件必须提供 frontend entry。
- frontend entry / style 文件必须存在。
- frontend entry 应导出 `mount(el, sdk)`。
- 禁止 `alert()`、`window.confirm()`、`prompt()`。
- 禁止硬编码 NOOR 主程序 host/port。
- 插件自身 API 建议使用 `sdk.api.plugin()`。
- CSS 应使用插件前缀或 `noor-plugin-*`。
- CSS 应优先使用 NOOR design token。
- 声明后端能力时应提供 `backend.py`。

输出格式保持 AI 可读：

```text
NOOR_PLUGIN_OK /path/to/plugin
NOOR_PLUGIN_ERROR CODE path message
NOOR_PLUGIN_WARN CODE path message
```


## 4. pack

打包成：

```text
<plugin-id>-<version>.zip
```

包内不包含：

- `__pycache__`
- `.pytest_cache`
- `node_modules`
- 临时下载文件
- 用户私密配置

## 4.1 当前已落地的 pack 初版

仓库内当前可直接执行：

```bash
scripts/noor-plugin pack plugins/mteam-plugin
scripts/noor-plugin pack plugins/mteam-plugin --output-dir /tmp/noor-plugin-pack --force
```

行为：

- 打包前默认先执行 validate；存在 ERROR 时拒绝打包。
- 输出 `<plugin-id>-<version>.zip`。
- zip 内以 `<plugin-id>/` 为根目录。
- 默认排除：
  - `__pycache__`
  - `.pytest_cache`
  - `node_modules`
  - `.git`
  - `dist`
  - `cache`
  - `tmp`
  - `*.pyc` / `*.log` / `*.tmp` / `*.db` / `*.sqlite`

可选：

```bash
--force          覆盖已有 zip
--skip-validate  跳过打包前验证，不推荐常规使用
```


## 5. dev

开发模式应提供：

- manifest 热重载。
- frontend entry 热重载。
- backend handler reload。
- 插件 API 请求日志。

## 6. 与 AI Skill 配合

CLI 输出的模板和 `validate` 错误信息必须足够结构化，让 AI 可以根据报错自动修复插件。

示例：

```text
NOOR_PLUGIN_ERROR CSS_PREFIX_MISSING plugins/demo/frontend/style.css .card
建议：改为 .demo-card
```
