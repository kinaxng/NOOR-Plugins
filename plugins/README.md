# NOOR Plugins

NOOR 插件用于把 PT/RSS、下载器、字幕搜索、概览卡片和独立页面接入主程序。插件必须自包含业务能力，主程序只提供通用运行时、配置、侧边栏挂载和 SDK。

## 当前内置插件

- `mteam-plugin`：M-Team RSS / 片单 / 字幕搜索 / 下载推送。
- `qbittorrent`：qBittorrent 下载器和任务管理页面。
- `transmission`：Transmission 下载器接入。
- `widget-system`：系统概览卡片。
- `local-subtitle-library`：本地字幕库搜索源，默认启用。
- `xunlei-subtitle`：迅雷在线字幕搜索源，默认启用。

## 插件目录

每个插件一个目录，至少包含：

```text
plugins/<plugin-id>/
  plugin.json          # manifest，必须存在
  backend.py           # 可选，后端 handler
  frontend/page.js     # 可选，sidebar_page 前端入口
  frontend/style.css   # 可选，插件页面样式
```

详细开发规范见：

- `plugins/PLUGIN_DEVELOPMENT.md`
- `plugins/PLUGIN_DESIGN.md`
- `plugins/PLUGIN_SDK.md`
- `plugins/PLUGIN_CLI.md`
