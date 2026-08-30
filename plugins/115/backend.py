from __future__ import annotations

import time
from typing import Any

from app.plugins.contracts import PluginTestResult
from app.plugins.secrets import plugin_secret_store

from .services.auth import code_challenge, poll_device_authorization, start_device_authorization
from .services.client import Client115, Error115, PLUGIN_ID, normalize_file

_auth_sessions: dict[str, dict[str, Any]] = {}


def _public_account(data: dict[str, Any]) -> dict[str, Any]:
    space = data.get("rt_space_info") if isinstance(data.get("rt_space_info"), dict) else {}
    return {
        "connected": True,
        "status": "connected",
        "user_id": str(data.get("user_id") or ""),
        "user_name": str(data.get("user_name") or ""),
        "avatar": str(data.get("user_face_m") or data.get("user_face_s") or ""),
        "vip": str((data.get("vip_info") or {}).get("level_name") or ""),
        "space": {
            "total": int((space.get("all_total") or {}).get("size") or 0),
            "used": int((space.get("all_use") or {}).get("size") or 0),
            "remaining": int((space.get("all_remain") or {}).get("size") or 0),
        },
    }


async def account_status(config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("access_token") and not config.get("refresh_token"):
        return {"connected": False, "status": "disconnected"}
    try:
        return _public_account(await Client115(config).user_info())
    except Error115 as exc:
        return {"connected": False, "status": "risk_control" if exc.risk_control else "token_error", "message": str(exc)}


async def test(config: dict[str, Any]) -> PluginTestResult:
    status = await account_status(config)
    return PluginTestResult(ok=bool(status.get("connected")), message="115 Open Platform connected" if status.get("connected") else str(status.get("message") or "115 未连接"), details={k: v for k, v in status.items() if k not in {"avatar"}})


async def handle_action(action: str, config: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    if action in {"status", "account"}:
        return await account_status(config)
    if action == "auth_start":
        session = await start_device_authorization(config)
        uid = session["uid"]
        if not uid:
            raise ValueError("115 未返回授权会话")
        _auth_sessions[uid] = {**session, "created_at": time.time()}
        return {key: session[key] for key in ("uid", "qrcode")}
    if action == "auth_poll":
        uid = str(payload.get("uid") or "")
        session = _auth_sessions.get(uid)
        if not session or time.time() - float(session.get("created_at") or 0) > 600:
            _auth_sessions.pop(uid, None)
            raise ValueError("115 授权会话已过期，请重新连接")
        result = await poll_device_authorization(config, session)
        if result.get("connected"):
            _auth_sessions.pop(uid, None)
        return result
    if action == "disconnect":
        plugin_secret_store.set(PLUGIN_ID, "access_token", "")
        plugin_secret_store.set(PLUGIN_ID, "refresh_token", "")
        _auth_sessions.clear()
        return {"ok": True, "connected": False}
    if action == "list_folder":
        return await Client115(config).list_folder(str(payload.get("folder_id") or config.get("offline_directory_id") or "0"), offset=int(payload.get("offset") or 0), limit=int(payload.get("limit") or 200))
    if action == "file_info":
        return await Client115(config).file_info(str(payload.get("file_id") or ""))
    raise LookupError(f"unsupported 115 action: {action}")

