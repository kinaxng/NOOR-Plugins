# NOOR Plugins

[Plugin SDK 文档与组件展示](https://kinaxng.github.io/NOOR-Plugins/)

NOOR 官方插件仓库、插件市场索引与 Plugin SDK。

将仓库地址 `https://github.com/kinaxng/NOOR-Plugins` 添加到 NOOR 插件市场后，即可浏览和安装这里的插件。官方 NOOR 发行版会在首次启动时自动加入该源。

## 开发插件

```bash
./scripts/noor-plugin create my-plugin --type tool
./scripts/noor-plugin validate ./plugins/my-plugin
./scripts/noor-plugin pack ./plugins/my-plugin
```

开发规范：

- [插件开发指南](./plugins/PLUGIN_DEVELOPMENT.md)
- [设计规范](./plugins/PLUGIN_DESIGN.md)
- [SDK 路线与接口](./plugins/PLUGIN_SDK.md)
- [CLI 文档](./plugins/PLUGIN_CLI.md)

每个插件必须拥有独立的 `plugin.json`，业务代码和前端资源应当自包含。运行数据由 NOOR 放入插件私有数据目录，卸载时可由用户选择保留或删除。

## 市场索引

根目录的 [`plugins.json`](./plugins.json) 是 NOOR 插件商店读取的官方索引。新增插件或发布新版本时，需要同步更新索引中的版本及元数据。

## 许可证

除插件目录另有注明外，本仓库使用 GNU Affero General Public License v3.0。
