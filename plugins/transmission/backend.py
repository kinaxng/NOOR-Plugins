from __future__ import annotations

from typing import Any

import httpx

from app.plugins.contracts import PluginTestResult


PLUGIN_ID = "transmission"
FIELDS = ["id", "name", "status", "percentDone", "totalSize", "leftUntilDone", "rateDownload", "rateUpload", "eta", "downloadDir", "error", "errorString", "addedDate", "doneDate", "hashString"]


def _url(config: dict[str, Any]) -> str:
    return str(config.get("rpc_url") or "http://127.0.0.1:9091/transmission/rpc").strip()


def _timeout(config: dict[str, Any]) -> float:
    try:
        return max(5.0, min(float(config.get("timeout") or 20), 120.0))
    except (TypeError, ValueError):
        return 20.0


def _auth(config: dict[str, Any]) -> tuple[str, str] | None:
    username, password = str(config.get("username") or ""), str(config.get("password") or "")
    return (username, password) if username or password else None


async def _rpc(config: dict[str, Any], method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {"method": method, "arguments": arguments or {}}
    async with httpx.AsyncClient(timeout=_timeout(config), follow_redirects=True, trust_env=False, auth=_auth(config)) as client:
        response = await client.post(_url(config), json=body)
        if response.status_code == 409:
            session_id = response.headers.get("X-Transmission-Session-Id")
            if not session_id:
                raise ValueError("Transmission RPC 要求会话 ID，但服务器未返回 X-Transmission-Session-Id")
            response = await client.post(_url(config), json=body, headers={"X-Transmission-Session-Id": session_id})
    if response.status_code >= 400:
        raise ValueError(f"Transmission RPC 请求失败 ({response.status_code})：{response.text[:240]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("Transmission RPC 返回了无效响应") from exc
    if not isinstance(data, dict):
        raise ValueError("Transmission RPC 返回了无效响应")
    if data.get("result") != "success":
        raise ValueError(f"Transmission RPC 操作失败：{data.get('result') or 'unknown'}")
    return data.get("arguments") if isinstance(data.get("arguments"), dict) else {}


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    status = int(item.get("status") or 0)
    status_name = {0: "stopped", 1: "check_wait", 2: "checking", 3: "download_wait", 4: "downloading", 5: "seed_wait", 6: "seeding"}.get(status, "unknown")
    return {
        "id": str(item.get("id") or ""), "hash": str(item.get("hashString") or ""), "name": str(item.get("name") or ""),
        "status": status_name, "progress": max(0.0, min(float(item.get("percentDone") or 0), 1.0)),
        "size": int(item.get("totalSize") or 0), "amount_left": int(item.get("leftUntilDone") or 0),
        "dlspeed": int(item.get("rateDownload") or 0), "upspeed": int(item.get("rateUpload") or 0),
        "eta": int(item.get("eta") or -1), "save_path": str(item.get("downloadDir") or ""),
        "error": int(item.get("error") or 0), "message": str(item.get("errorString") or ""),
        "added_on": int(item.get("addedDate") or 0), "completion_on": int(item.get("doneDate") or 0), "raw": item,
    }


async def test(config: dict[str, Any]) -> PluginTestResult:
    try:
        session = await _rpc(config, "session-get")
        return PluginTestResult(ok=True, message="transmission connected", details={"version": session.get("version"), "rpc_version": session.get("rpc-version")})
    except Exception as exc:
        return PluginTestResult(ok=False, message=f"transmission failed: {exc}")


async def submit_download(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    url = str(payload.get("url") or payload.get("urls") or payload.get("magnet") or "").strip()
    if not url:
        raise ValueError("缺少下载链接")
    args: dict[str, Any] = {"filename": url}
    download_dir = str(payload.get("savepath") or config.get("download_dir") or "").strip()
    if download_dir:
        args["download-dir"] = download_dir
    result = await _rpc(config, "torrent-add", args)
    torrent = result.get("torrent-added") or result.get("torrent-duplicate")
    if not isinstance(torrent, dict):
        raise ValueError("Transmission 未确认创建下载任务")
    return {"ok": True, "message": "submitted to transmission", "torrent": _normalize(torrent), "duplicate": "torrent-duplicate" in result, "savepath": download_dir}


async def _tasks(config: dict[str, Any]) -> dict[str, Any]:
    result = await _rpc(config, "torrent-get", {"fields": FIELDS})
    raw = result.get("torrents") if isinstance(result.get("torrents"), list) else []
    items = [_normalize(item) for item in raw if isinstance(item, dict)]
    return {"ok": True, "torrents": items, "total": len(items)}


async def _operate(config: dict[str, Any], method: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw_ids = payload.get("ids") or payload.get("id") or payload.get("torrent_id")
    ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
    ids = [int(value) for value in ids if str(value or "").strip()]
    if not ids:
        raise ValueError("缺少 Transmission 任务 ID")
    result = await _rpc(config, method, {"ids": ids, "delete-local-data": bool(payload.get("delete_files"))} if method == "torrent-remove" else {"ids": ids})
    return {"ok": True, "result": result, "ids": ids}


async def handle_action(action: str, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if action == "test":
        result = await test(config)
        return {"ok": result.ok, "message": result.message, "details": result.details or {}}
    if action in {"overview", "tasks"}:
        return await _tasks(config)
    if action == "submit_download":
        return await submit_download(payload, config)
    if action == "pause_task":
        return await _operate(config, "torrent-stop", payload)
    if action in {"resume_task", "retry_task"}:
        return await _operate(config, "torrent-start", payload)
    if action in {"delete_task", "delete_tasks"}:
        return await _operate(config, "torrent-remove", payload)
    if action == "download_options":
        return {"ok": True, "downloader": PLUGIN_ID, "default_savepath": str(config.get("download_dir") or ""), "supports_categories": False, "supports_savepath": True, "supports_small_file_filter": False}
    raise ValueError(f"unsupported action: {action}")
