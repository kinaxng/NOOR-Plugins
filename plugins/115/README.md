# NOOR 115 Plugin

NOOR 的 115 Open Platform 插件。它把 115 当作离线下载和媒体数据源，由 NOOR 持久保存文件身份、生成本地 STRM、按需解析播放地址，并只对新增媒体执行一次 MediaInfo 探测。Cookie API、CloudDrive2 和 FUSE 挂载都不是依赖。

## 功能与边界

- 官方设备授权、Access Token 刷新、账号状态和精确目录访问。
- 作为 NOOR 正式 downloader 接收 magnet、ed2k 和官方接口支持的 HTTP(S) 任务。
- 只轮询官方离线任务列表；完成后只遍历任务返回的结果目录。
- 以 `provider=115 + file_id` 保存媒体身份，路径只作展示。
- 在 NAS 本地原子生成 `.strm`，内容是稳定的 NOOR URL，不是 115 CDN 临时链接。
- 播放时验证独立的 scoped service token，短缓存解析结果，并以保留 Range 的兼容代理交付；确认客户端兼容时可切换为 HTTP 302。
- 对新增且身份发生变化的媒体排队执行 ffprobe；默认并发 1，结果同时进入 SQLite 和版本化 JSON cache。
- 提供持久、幂等的媒体事件 outbox，供需要它的本地消费者消费；115 不直接调用整理器。
- 可选适配 OneManJS 社区版 StrmAssistant：把 NOOR cache 转换为其原生持久化 JSON，在定向通知 Emby 前完成写入。

115 插件不包含 Emby 数据库逻辑，也不修改 Emby SQLite。它只使用社区版神医助手公开的持久化文件格式；Emby 连接与定向刷新仍复用 NOOR Core adapter。

## 安装与授权

1. 将本仓库添加到 NOOR 插件仓库并安装、启用 `115`。
2. 在 115 开放平台注册应用，把 Client ID 填入插件设置。
3. 在插件页面浏览并选择默认离线目录，再设置 STRM incoming 本地目录和 NOOR 外部访问地址。
4. 打开 115 页面，点击“连接 115”，完成官方设备授权。
5. 在 NOOR 的资源绑定中把需要的非 PT 来源绑定到 115 downloader。

OAuth token 只在后端使用，通过 NOOR 插件 secret store 加密保存，不会返回浏览器或写入普通插件配置。生产部署建议设置 `NOOR_PLUGIN_SECRET_KEY`，使密钥与运行数据分离。

## 关键配置

- 默认离线目录通过插件页面读取 115 目录树后选择，内部 ID 不需要手工填写。
- `strm_directory`：NAS 上的本地 AV incoming 目录，例如 `/data/strm/av/incoming`。
- `public_base_url`：Emby/ffprobe 能访问的 NOOR 地址，例如 `https://noor.example`。
- MDC-NG incoming 路径从 MDC-NG 自己的监控目录自动识别，不需要在 115 中重复配置；插件只核对和跟踪整理结果，不会提交 MDC 任务。
- `organizer_sync_enabled`：跟踪 MDC-NG 已完成的 incoming 整理结果，保存最终分类路径并幂等通知 Emby。
- `media_extensions`：完成目录内允许生成 STRM 的媒体扩展名。
- `discovery_max_depth` / `discovery_max_items`：单个完成任务的有界发现范围。
- `stream_url_cache_seconds`：115 临时播放地址的内存短缓存，最多 300 秒，不持久化。
- `stream_delivery_mode`：默认 `proxy`，解决 Emby 在 302 后改变 User-Agent 导致 115 CDN 拒绝的问题；`redirect` 可让兼容客户端直连 CDN。
- `mediainfo_enabled` / `mediainfo_concurrency`：只对新媒体 probe；默认并发 1。
- `mediainfo_timeout` / `mediainfo_retry_limit`：超时和指数退避重试上限。
- `strm_assistant_enabled`：为新整理的 STRM 写入社区版神医助手可恢复的 `*-mediainfo.json`；默认开启。
- 115 插件不会提交 MDC-NG 任务，也不会替 MDC-NG 判断 AV、UC 或 porn 分类。
- 请在 MDC-NG 中把容器内的 `/data/strm/av/incoming` 加为监控目录；MDC-NG 负责读取 STRM、分类、刮削和硬链接。
- 115 后台只读匹配 MDC-NG 的 `source_path → target_path`；已通知的同一目标不会重复刷新 Emby。
- STRM/字幕以不可执行的 `0666` 生成，便于不同容器 UID/GID 在 `fs.protected_hardlinks=1` 的 NAS 上硬链接；目录 ACL 仍负责访问边界。

## STRM 与播放

生成文件内容类似：

```text
https://noor.example/api/plugins/115/stream/123456789?token=<NOOR scoped token>
```

这个 token 不是 115 OAuth credential，只允许调用 stream resolver，可撤销、可轮换。Resolver 按 `file_id` 找到当前 pick code，再向 115 获取临时 URL。默认兼容代理会把客户端的 Range 精确转发给 115，因此不会为了小段读取而下载整个文件；它解决了 Emby 跳转后请求头变化造成的 403。选择 `302 直连` 时，视频字节不经过 NOOR，但必须先验证所用客户端的播放与拖动。

重命名或移动 115 文件不会改变已有 STRM，只要 `file_id` 仍有效。删除文件后 resolver 返回 404。同一 `file_id` 不会因为改名重复建媒体记录；同内容不同 file ID 暂按不同云文件保守保存。

STRM 作为长期资产记录在插件数据库中。索引包含 115 文件身份、内容摘要和生成配置指纹；输出存在且内容正确时直接复用，文件丢失或内容损坏时自动重建。离线任务与 STRM 的引用分开记录，为后续安全清理提供共享引用保护。

完成目录中的同名外挂字幕会随新增媒体同步到 STRM 旁边，支持 `srt`、`ass`、`ssa`、`sub`、`vtt`，并保留 `.zh`、`.chs`、`.forced`、`.sdh` 等限定后缀。字幕按 115 文件身份和内容摘要复用，默认拒绝读取超过 20 MiB 的字幕文件。

任务完成目录会进行保守对账。只有目录发现完整成功，且没有达到深度或条目上限时，才会解除本任务不再存在的 STRM 引用；其他任务仍引用时保留输出。只有无共享引用、路径严格位于 STRM 输出根目录且扩展名受控时才删除文件。读取、生成或删除失败时保留记录供下次重试。

## MediaInfo

worker 使用 `ffprobe -v error -print_format json -show_format -show_streams <NOOR stream URL>`。cache identity 为 `file_id + sha1 + size + schema_version`。命中 `ready` 记录时不会再次运行 ffprobe；内容身份变化才会失效。JSON 位于插件私有数据目录的 `mediainfo/{file_id}.json`，卸载时是否删除取决于用户选择的 purge-data 行为。

## 本地整理与事件契约

插件 action `pipeline_events` 返回待消费事件，`ack_pipeline_event` 接收 `event_id` 确认完成：

- `115.media.discovered`
- `115.strm.created`
- `115.mediainfo.ready`

事件持久、幂等，不包含 OAuth token、stream token 或 115 临时 URL。`115.strm.created` 是本地文件已就绪的通知；MDC-NG 通过监控目录自行发现它，不需要 NOOR 创建 MDC 任务。`pipeline_events` / `ack_pipeline_event` 保留给未来的本地消费者或审计使用。

MDC-NG 只接收本地 `.strm`，不接触 115 文件或 FUSE 路径。其分类、刮削和硬链接由 MDC-NG 完成；115 插件不直接调用 MDC-NG，也不硬编码 Emby 分类。插件只读取整理结果，并在神医助手 sidecar 就绪后通知 Emby。

### OneManJS 神医助手社区版

当前兼容基线是 `OneManJS/StrmAssistant`，不是收费原版。社区版没有收费版文档中的 `POST /Items/SyncMediaInfo` 路由，因此 NOOR 不伪造 API 调用。其原生恢复约定是：最终 STRM 为 `ABC-123.strm` 时，相邻文件命名为 `ABC-123-mediainfo.json`，内容包含 `MediaSourceInfo` 和 `Chapters`。插件把自身一次性 ffprobe 结果转换成这一格式，原子写入后才调用 Emby 定向更新。

神医助手必须启用 MediaInfo 持久化的 `Restore` 模式。NOOR 的账号页显示检测到的插件版本，媒体列表显示 `waiting_mediainfo / ready / failed`。`ready` 表示 sidecar 已生成并可供新增 Item 恢复，不代表播放器网络质量本身已经验证。

115 页面可读取社区版神医助手的实际版本、关键设置和相关 Emby 计划任务。计划任务可以由 NOOR 手动启动；高远程读取量任务会二次确认。社区版没有独立设置 API，因此 NOOR 只更新 `Strm Assistant*.json` 这些精确配置文件，部分选项需要重启 Emby 后生效。不要给 NOOR 整个 Emby 配置目录的写权限。

整理器完成最终本地目录后，应调用 NOOR 核心的通用回调，而不是让 115 插件保存 Emby 凭据：

```http
POST /api/media-library/organized?token=<MEDIA_LIBRARY_WEBHOOK_TOKEN>
Content-Type: application/json

{"provider":"115","file_id":"...","local_path":"/volume/media/av/日本有码/ABC-123/ABC-123.strm"}
```

NOOR 会把本地路径映射为 Emby 可见路径，调用受支持的 `/emby/Library/Media/Updated` 定向刷新，并把通知状态回写到 115 媒体记录。若整理器已经持有 Emby 命名空间路径，可改传 `server_path`。

其他可信 NOOR 消费者仍可调用插件 action `mediainfo` 并传入 `file_id`，读取 `provider/file_id/sha1/size/schema_version/status/probed_at/media`。该接口只读取缓存，不会触发新的 ffprobe。

跨进程消费者使用 NOOR Core 的 machine-to-machine 接口，不需要接触插件 OAuth 凭据：

```http
GET /api/media-library/providers/115/media/{file_id}/mediainfo
X-NOOR-Service-Token: <MEDIA_LIBRARY_WEBHOOK_TOKEN>
```

返回同一份版本化 MediaInfo cache。社区版神医助手通过 sidecar 在 Item 首次加入时恢复媒体流；NOOR 不直接写 Emby MediaStreams，也不修改数据库。

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
4. 用 `curl -H 'Range: bytes=0-1023'` 确认返回 206；若选择 302 模式，加 `-L` 跟随跳转验证 Range。
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
