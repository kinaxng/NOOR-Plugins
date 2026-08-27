# Security Policy

## Reporting a vulnerability

请优先使用 GitHub 的 **Private vulnerability reporting** 私下报告安全问题。不要在公开 Issue 中提交密钥、Cookie、访问令牌、真实媒体路径或可识别个人环境的日志。

报告中请包含受影响版本、复现步骤、潜在影响与可行的缓解建议。维护者会尽快确认并协调修复与披露时间。

## Local data

NOOR 会连接本地媒体、下载器和外部服务。部署者应自行保护 `.env`、数据库、插件运行数据和日志，并为服务使用最小权限账户。仓库的忽略规则不能替代提交前的密钥扫描。
