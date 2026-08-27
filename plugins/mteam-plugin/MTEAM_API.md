# M-Team 插件 API / RSS 合规说明

本插件只接入 M-Team 提供的 **官方 API** 与 **官方 RSS**。当前实现明确不使用 Cookie、不读取浏览器会话、不抓取网页 HTML、不依赖私自抓包接口。

## 配置项

- `BASE URL`：M-Team 官方 API 所在站点，插件会拼接 `/api/...` 调用接口。测试站可填 `https://test2.m-team.cc`，正式站切换时按 M-Team 官方 API 地址填写。
- `RSS 地址`：M-Team 生成的 RSS fetch 地址，通常形如 `/api/rss/fetch?...&dl=1...`。RSS 中的 `enclosure` 用于下载器推送。
- `API KEY`：所有受保护 API 均通过 `x-api-key` 请求头传入。
- `DMM 图片缓存天数`：仅控制图片本地缓存有效期，避免页面直接热链远端封面。

## 请求约定

```http
x-api-key: <M-Team API KEY>
Accept: application/json
Content-Type: application/json
```

图片缓存只做匿名 HTTP 图片下载；不会附带 Cookie、API KEY、Authorization 或浏览器会话。受保护数据必须走上面的 `x-api-key` API。

## NOOR 当前已接入能力

| 能力 | 接口 / 来源 | 用途 |
|---|---|---|
| RSS 浏览 | `GET /api/rss/fetch` | M-Team 插件 RSS tab |
| RSS 下载链接 | RSS `enclosure` / `/api/rss/dlv2` | 一键推送下载器 |
| 种子详情 | `POST /api/torrent/detail`，参数 `id` | 补封面、修正 RSS 封面 |
| 片单详情 | `POST /api/album/albumDetail` | 片单名称、数量、简介 |
| 片单种子 | `POST /api/album/albumTorrentSearch` | 只填 albumId 时展示内容 |
| 字幕搜索 | `POST /api/subtitle/search` | 字幕 Panel 的 M-Team 搜索源 |
| 字幕下载 | `GET /api/subtitle/dl?id=...` | 下载字幕内容并本地缓存 |
| 下载器推送 | RSS download URL + 下载器插件 | 推送 qBittorrent / Transmission |

## 当前刻意不做的事

- 不使用 `Authorization: Bearer` 这类泛化 token 逻辑；M-Team 统一使用 `x-api-key`。
- 不暴露“搜索接口路径 / 请求方法”给用户配置，避免把插件变成不透明抓接口工具。
- 不在没有用户片单配置时自动请求 `albumSearch` 拉推荐列表；正式站切换后保持空状态，由用户添加 RSS / albumId。
- 不通过详情页 HTML 抓封面；封面补全只走 `POST /api/torrent/detail`。

## 后续可评估能力

以下均应继续保持官方 API 接入方式：

- 种子搜索：`POST /api/torrent/search`
- 种子文件列表：`POST /api/torrent/files`
- MediaInfo：`POST /api/torrent/mediaInfo`
- 分类 / 编码 / 来源列表：`POST /api/torrent/categoryList` 等
- 字幕列表 / 语言 / genlink / dlV2：`/api/subtitle/*`
- DMM 元数据：`/api/dmm/*`
