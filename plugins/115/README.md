# NOOR 115 Plugin

Official 115 Open Platform integration for offline downloads, stable local STRM files, redirect playback, and deduplicated MediaInfo.

The implementation is being delivered in milestones. Version 0.1 provides encrypted OAuth credential storage, device authorization, account status, token refresh, and exact folder/file API access. Cookie APIs and FUSE mounts are intentionally not used.

## Authorization

1. Register an application at the 115 Open Platform.
2. Enable this plugin and enter its Client ID in NOOR plugin settings.
3. Open the 115 page and choose **连接 115**.
4. Complete the official device authorization.

OAuth tokens are backend-only and encrypted at rest. They are never returned to the browser or written to normal plugin configuration.

## Risk-control defaults

- One API request per second by default.
- Exact directory access only.
- No periodic whole-drive scan.
- MediaInfo concurrency defaults to one in later milestones.

See `IMPLEMENTATION_PLAN_115.md` in the NOOR repository for the complete architecture and delivery plan.

