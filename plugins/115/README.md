# NOOR 115 Plugin

NOOR 的 115 Open Platform 插件。它把 115 当作离线下载和媒体数据源，由 NOOR 持久保存文件身份、生成本地 STRM、按需解析播放地址，并只对新增媒体执行一次 MediaInfo 探测。Cookie API、CloudDrive2 和 FUSE 挂载都不是依赖。

## 功能与边界

- 官方设备授权、Access Token 刷新、账号状态和精确目录访问。
- 作为 NOOR 正式 downloader 接收 magnet、ed2k 和官方接口支持的 HTTP(S) 任务。
- 只轮询官方离线任务列表；完成后只遍历任务返回的结果目录。
- 以 `provider=115 + file_id` 保存媒体身份，路径只作展示。
- 在 NAS 本地原子生成 `.strm`，内容是稳定的 NOOR URL，不是 115 CDN 临时链接。
- 播放时验证独立的 scoped service token，短缓存解析结果并返回 HTTP 302。
- 对新增且身份发生变化的媒体排队执行 ffprobe；默认并发 1，结果同时进入 SQLite 和版本化 JSON cache。
- 提供持久、幂等的媒体事件 outbox，供需要它的本地消费者消费；115 不直接调用整理器。

115 插件不包含 Emby 数据库逻辑。NOOR 核心提供媒体库 adapter；MediaStreams 注入若后续需要，将通过受支持的 Emby Server Plugin 完成，不直接修改 Emby SQLite。

## 安装与授权

1. 将本仓库添加到 NOOR 插件仓库并安装、启用 `115`。
2. 在 115 开放平台注册应用，把 Client ID 填入插件设置。
3. 设置默认离线目录 ID、STRM incoming 本地目录和 NOOR 外部访问地址。
4. 打开 115 页面，点击“连接 115”，完成官方设备授权。
5. 在 NOOR 的资源绑定中把需要的非 PT 来源绑定到 115 downloader。

OAuth token 只在后端使用，通过 NOOR 插件 secret store 加密保存，不会返回浏览器或写入普通插件配置。生产部署建议设置 `NOOR_PLUGIN_SECRET_KEY`，使密钥与运行数据分离。

## 关键配置

- `offline_directory_id`：115 默认离线目录 ID；不是 FUSE 路径。
- `strm_directory`：NAS 上的本地 AV incoming 目录，例如 `/data/strm/av/incoming`。
- `public_base_url`：Emby/ffprobe 能访问的 NOOR 地址，例如 `https://noor.example`。
- `media_extensions`：完成目录内允许生成 STRM 的媒体扩展名。
- `discovery_max_depth` / `discovery_max_items`：单个完成任务的有界发现范围。
- `stream_url_cache_seconds`：115 临时播放地址的内存短缓存，最多 300 秒，不持久化。
- `mediainfo_enabled` / `mediainfo_concurrency`：只对新媒体 probe；默认并发 1。
- `mediainfo_timeout` / `mediainfo_retry_limit`：超时和指数退避重试上限。
- 115 插件不会提交 MDC-NG 任务，也不会替 MDC-NG 判断 AV、UC 或 porn 分类。
- 请在 MDC-NG 中把容器内的 `/data/strm/av/incoming` 加为监控目录；MDC-NG 负责读取 STRM、分类、刮削和硬链接。

## STRM 与播放

生成文件内容类似：

```text
https://noor.example/api/plugins/115/stream/123456789?token=<NOOR scoped token>
```

这个 token 不是 115 OAuth credential，只允许调用 stream resolver，可随插件数据一起撤销。Resolver 按 `file_id` 找到当前 pick code，向 115 获取临时 URL，然后返回 302。NOOR 默认不代理视频字节；客户端会在跳转后的 115 URL 上重新发起 Range 请求。

重命名或移动 115 文件不会改变已有 STRM，只要 `file_id` 仍有效。删除文件后 resolver 返回 404。同一 `file_id` 不会因为改名重复建媒体记录；同内容不同 file ID 暂按不同云文件保守保存。

## MediaInfo

worker 使用 `ffprobe -v error -print_format json -show_format -show_streams <NOOR stream URL>`。cache identity 为 `file_id + sha1 + size + schema_version`。命中 `ready` 记录时不会再次运行 ffprobe；内容身份变化才会失效。JSON 位于插件私有数据目录的 `mediainfo/{file_id}.json`，卸载时是否删除取决于用户选择的 purge-data 行为。

## 本地整理与事件契约

插件 action `pipeline_events` 返回待消费事件，`ack_pipeline_event` 接收 `event_id` 确认完成：

- `115.media.discovered`
- `115.strm.created`
- `115.mediainfo.ready`

事件持久、幂等，不包含 OAuth token、stream token 或 115 临时 URL。`115.strm.created` 是本地文件已就绪的通知；MDC-NG 通过监控目录自行发现它，不需要 NOOR 创建 MDC 任务。`pipeline_events` / `ack_pipeline_event` 保留给未来的本地消费者或审计使用。

MDC-NG 只接收本地 `.strm`，不接触 115 文件或 FUSE 路径。其分类、刮削、硬链接和后续媒体库通知都由 MDC-NG 自己完成；115 插件不直接调用 MDC-NG，也不硬编码 Emby 分类。

NOOR 的 Emby adapter 仍可被其他受支持流程使用；115 插件不直接触发它。官方 REST API 没有稳定的外部 MediaStreams 写入契约，因此当前不做数据库 hack；MediaInfo cache 已为未来独立 Emby Server Plugin 预留结构化输入。

## 风控建议

- 保持 API 最小间隔至少 1 秒，MediaInfo 并发保持 1。
- 不把离线目录设为云盘根目录；插件也会拒绝以根目录作为任务结果扫描。
- 不提高发现上限来模拟全库索引。
- 发生限流/风控错误时，任务轮询至少冷却 300 秒；ffprobe 失败使用指数退避。
- 不让 MDC-NG、Emby 和 NOOR 对同一远程视频重复 probe。

## Smoke test

1. 提交一个测试 magnet，确认任务状态 queued → downloading → completed。
2. 检查只访问该任务结果目录，并生成本地 STRM。
3. `cat` STRM，确认其中只有 NOOR URL。
4. `curl -I` 确认返回 302；用 `curl -H 'Range: bytes=0-1023' -L` 验证 Range。
5. 运行 ffprobe 和实际播放器拖动测试。
6. 检查 MediaInfo JSON；再次排队同一文件，确认不再 probe。
7. 在 115 改名/移动文件后确认原 STRM 仍能解析。
8. 在 MDC-NG 监控目录中验证自动分类、刮削、硬链接、Emby 入库、Direct Play 和 seek。

## 故障排查

- `Token 异常`：重新执行设备授权；不要把 token 粘贴到前端或日志。
- `请先配置 NOOR 外部访问地址`：填写 Emby 和 NOOR 后端都能访问的绝对 HTTP(S) 地址。
- resolver 403：STRM 中 scoped token 已撤销或插件数据被重建，重新生成 STRM。
- resolver 404：云文件已删除，或旧记录缺失 pick code。
- ffprobe retry/failed：先用同一 STRM URL 做 Range smoke test，再检查 115 风控和网络可达性。
- `MDC-NG 未发现 STRM`：确认 NOOR 的 `strm_directory` 与 MDC-NG 的路径映射/监控目录对应（例如 NOOR `/home/kinax/Videos/strm/av/incoming` → MDC `/data/strm/av/incoming`）。
- `MDC-NG 分类不正确`：分类属于 MDC-NG 的监控目录配置和命名规则，不由 115 插件决定。
