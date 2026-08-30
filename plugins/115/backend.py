from __future__ import annotations

import time
import asyncio
import contextlib
from typing import Any

from app.plugins.contracts import PluginTestResult
from app.plugins.secrets import plugin_secret_store

from .services.auth import code_challenge, poll_device_authorization, start_device_authorization
from .services.client import Client115, Error115, PLUGIN_ID, normalize_file
from .services.offline import add_urls, list_remote_tasks
from .services.storage import create_task, find_duplicate, init_storage, list_tasks, source_identity, task_dict, update_task, utcnow

_auth_sessions: dict[str, dict[str, Any]] = {}
_poll_task: asyncio.Task[None] | None = None
_poll_stop: asyncio.Event | None = None


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


async def submit_download(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    raw_urls = payload.get("urls") or payload.get("url") or ""
    urls = [str(value).strip() for value in (raw_urls if isinstance(raw_urls, list) else str(raw_urls).splitlines()) if str(value).strip()]
    if not urls:
        raise ValueError("missing url/magnet")
    if len(urls) != 1:
        raise ValueError("115 downloader currently accepts one task per submission")
    digest, kind, hint = source_identity(urls[0])
    duplicate = await asyncio.to_thread(find_duplicate, digest)
    if duplicate:
        return {"ok": True, "duplicate": True, "task_id": duplicate.id, "status": duplicate.status, "message": "115 离线任务已存在"}
    directory_id = str(payload.get("save_directory") or payload.get("savepath") or config.get("offline_directory_id") or "0")
    hashes = await add_urls(Client115(config), urls, directory_id)
    info_hash = hashes[0]
    context = {key: payload.get(key) for key in ("source_plugin_id", "subscription_id", "subscription_code", "code") if payload.get(key)}
    task = await asyncio.to_thread(
        create_task,
        info_hash=info_hash, source_digest=digest, source_kind=kind, source_hint=hint,
        name=str(payload.get("name") or payload.get("title") or hint), target_directory_id=directory_id, context=context,
    )
    return {"ok": True, "task_id": task.id, "info_hash": info_hash, "status": task.status, "message": "submitted to 115 offline"}


async def sync_offline_tasks(config: dict[str, Any]) -> dict[str, Any]:
    local = await asyncio.to_thread(list_tasks, active_only=True, limit=500)
    if not local:
        return {"checked": 0, "updated": 0, "completed": []}
    remote = await list_remote_tasks(Client115(config), max_pages=int(config.get("offline_poll_max_pages") or 5))
    by_hash = {item["info_hash"].lower(): item for item in remote}
    updated = 0
    completed: list[str] = []
    for task in local:
        current = by_hash.get(task.info_hash.lower())
        if not current:
            continue
        values: dict[str, Any] = {"status": current["status"], "progress": current["progress"], "error_message": current["error"]}
        if current["status"] == "completed":
            values["completed_at"] = task.completed_at or utcnow()
            completed.append(task.info_hash)
        if task.status != current["status"] or task.progress != current["progress"]:
            await asyncio.to_thread(update_task, task.info_hash, **values)
            updated += 1
    return {"checked": len(local), "updated": updated, "completed": completed}


async def _poll_loop(config: dict[str, Any]) -> None:
    global _poll_stop
    _poll_stop = asyncio.Event()
    while not _poll_stop.is_set():
        try:
            if config.get("access_token") or config.get("refresh_token"):
                await sync_offline_tasks(config)
        except Error115 as exc:
            if exc.risk_control:
                await asyncio.sleep(max(300, int(config.get("offline_poll_interval") or 60) * 5))
        except Exception:
            pass
        try:
            await asyncio.wait_for(_poll_stop.wait(), timeout=max(30, int(config.get("offline_poll_interval") or 60)))
        except asyncio.TimeoutError:
            pass


async def start_background(config: dict[str, Any]) -> None:
    global _poll_task
    init_storage()
    if not _poll_task or _poll_task.done():
        _poll_task = asyncio.create_task(_poll_loop(dict(config)))


async def stop_background() -> None:
    global _poll_task, _poll_stop
    if _poll_stop:
        _poll_stop.set()
    if _poll_task:
        _poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _poll_task
    _poll_task = None
    _poll_stop = None


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
    if action == "tasks":
        return {"items": [task_dict(task) for task in await asyncio.to_thread(list_tasks, active_only=False, limit=int(payload.get("limit") or 100))]}
    if action == "sync_tasks":
        return await sync_offline_tasks(config)
    raise LookupError(f"unsupported 115 action: {action}")
