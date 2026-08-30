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
from .services.mediainfo import enqueue as enqueue_mediainfo, process_next as process_next_mediainfo
from .services.pipeline import process_pipeline_once
from .services.storage import acknowledge_event, create_task, emit_event, find_duplicate, init_storage, list_events, list_media, list_tasks, media_dict, recover_interrupted_mediainfo, source_identity, task_dict, update_task, upsert_media, utcnow
from .services.strm import create_strm, is_media_file, resolve_stream as resolve_stream_service

_auth_sessions: dict[str, dict[str, Any]] = {}
_poll_task: asyncio.Task[None] | None = None
_poll_stop: asyncio.Event | None = None
_mediainfo_task: asyncio.Task[None] | None = None
_mediainfo_stop: asyncio.Event | None = None
_pipeline_task: asyncio.Task[None] | None = None
_pipeline_stop: asyncio.Event | None = None


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
    all_local = await asyncio.to_thread(list_tasks, active_only=False, limit=500)
    pipeline_completed: list[str] = []
    for task in all_local:
        retry_due = task.pipeline_status != "failed" or not task.updated_at or (utcnow() - task.updated_at).total_seconds() >= 300
        if task.status == "completed" and task.pipeline_status != "completed" and retry_due:
            try:
                await discover_completed_task(config, task)
                pipeline_completed.append(task.info_hash)
            except Exception as exc:
                await asyncio.to_thread(update_task, task.info_hash, pipeline_status="failed", error_message=str(exc)[:1000])
    local = [task for task in all_local if task.status in {"queued", "downloading"}]
    if not local:
        return {"checked": 0, "updated": 0, "completed": pipeline_completed}
    remote = await list_remote_tasks(Client115(config), max_pages=int(config.get("offline_poll_max_pages") or 5))
    by_hash = {item["info_hash"].lower(): item for item in remote}
    updated = 0
    completed: list[str] = []
    for task in local:
        current = by_hash.get(task.info_hash.lower())
        if not current:
            continue
        values: dict[str, Any] = {"status": current["status"], "progress": current["progress"], "error_message": current["error"], "result_file_id": current["file_id"]}
        if current["status"] == "completed":
            values["completed_at"] = task.completed_at or utcnow()
            completed.append(task.info_hash)
        changed = task.status != current["status"] or task.progress != current["progress"] or task.result_file_id != current["file_id"]
        if changed:
            saved = await asyncio.to_thread(update_task, task.info_hash, **values)
            updated += 1
        else:
            saved = task
        if current["status"] == "completed" and saved.pipeline_status != "completed":
            try:
                await discover_completed_task(config, saved)
                pipeline_completed.append(task.info_hash)
            except Exception as exc:
                await asyncio.to_thread(update_task, task.info_hash, pipeline_status="failed", error_message=str(exc)[:1000])
                raise
    return {"checked": len(local), "updated": updated, "completed": list(dict.fromkeys(completed + pipeline_completed))}


async def _walk_task_root(config: dict[str, Any], root_id: str) -> list[dict[str, Any]]:
    """Walk only the completed task root, never the user's cloud root."""
    if not root_id or root_id == "0":
        raise ValueError("115 离线任务未返回安全的结果目录，已拒绝扫描根目录")
    client = Client115(config)
    max_depth = max(0, min(int(config.get("discovery_max_depth") or 4), 8))
    max_items = max(20, min(int(config.get("discovery_max_items") or 2000), 10000))
    queue: list[tuple[str, int, str]] = [(root_id, 0, "")]
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    while queue and len(files) < max_items:
        folder_id, depth, path = queue.pop(0)
        if folder_id in seen:
            continue
        seen.add(folder_id)
        offset = 0
        while len(files) < max_items:
            page = await client.list_folder(folder_id, offset=offset, limit=min(500, max_items - len(files)))
            rows = page["items"]
            for item in rows:
                item["display_path"] = f"{path}/{item['name']}".lstrip("/")
                if item.get("is_directory"):
                    if depth < max_depth and item.get("file_id"):
                        queue.append((item["file_id"], depth + 1, item["display_path"]))
                else:
                    files.append(item)
            offset += len(rows)
            if not rows or offset >= int(page.get("count") or 0):
                break
    return files


async def discover_completed_task(config: dict[str, Any], task: Any) -> list[str]:
    root_id = str(task.result_file_id or "")
    await asyncio.to_thread(update_task, task.info_hash, pipeline_status="running")
    info = await Client115(config).file_info(root_id)
    if info.get("pick_code") and is_media_file({**info, "is_directory": False}, config):
        files = [{**info, "is_directory": False, "display_path": info.get("name") or root_id}]
    else:
        files = await _walk_task_root(config, root_id)
    detected: list[str] = []
    for item in files:
        if not is_media_file(item, config):
            continue
        media, _created = await asyncio.to_thread(upsert_media, item, task_id=task.info_hash, display_path=item.get("display_path") or item["name"])
        strm = await asyncio.to_thread(create_strm, config, item)
        await asyncio.to_thread(emit_event, "115.media.discovered", file_id=media.file_id,
            payload={"provider": "115", "file_id": media.file_id, "sha1": media.sha1, "size": media.size, "name": media.name},
            dedupe_key=f"115.media.discovered:{media.file_id}:{media.sha1}:{media.size}")
        await asyncio.to_thread(emit_event, "115.strm.created", file_id=media.file_id,
            payload={"provider": "115", "file_id": media.file_id, "local_path": strm["path"], "name": media.name},
            dedupe_key=f"115.strm.created:{media.file_id}:{strm['path']}")
        if config.get("mediainfo_enabled", True):
            await asyncio.to_thread(enqueue_mediainfo, media)
        detected.append(media.file_id)
    await asyncio.to_thread(update_task, task.info_hash, detected_file_ids=detected, pipeline_status="completed", error_message="")
    return detected


async def resolve_stream(config: dict[str, Any], file_id: str, request: Any, token: str) -> dict[str, Any]:
    user_agent = str(request.headers.get("user-agent") or "NOOR-stream-client") if request is not None else "NOOR-stream-client"
    return await resolve_stream_service(config, str(file_id), token=str(token or ""), user_agent=user_agent)


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


async def _mediainfo_loop(config: dict[str, Any]) -> None:
    global _mediainfo_stop
    _mediainfo_stop = asyncio.Event()
    while not _mediainfo_stop.is_set():
        worked = False
        if config.get("mediainfo_enabled", True):
            concurrency = max(1, min(int(config.get("mediainfo_concurrency") or 1), 4))
            results = await asyncio.gather(*(process_next_mediainfo(config) for _ in range(concurrency)), return_exceptions=True)
            worked = any(result is not None and not isinstance(result, Exception) for result in results)
        try:
            await asyncio.wait_for(_mediainfo_stop.wait(), timeout=2 if worked else 10)
        except asyncio.TimeoutError:
            pass


async def _pipeline_loop(config: dict[str, Any]) -> None:
    global _pipeline_stop
    _pipeline_stop = asyncio.Event()
    while not _pipeline_stop.is_set():
        try:
            await process_pipeline_once(config)
        except Exception:
            pass
        try:
            await asyncio.wait_for(_pipeline_stop.wait(), timeout=max(30, int(config.get("pipeline_poll_interval") or 60)))
        except asyncio.TimeoutError:
            pass


async def start_background(config: dict[str, Any]) -> None:
    global _poll_task, _mediainfo_task, _pipeline_task
    init_storage()
    await asyncio.to_thread(recover_interrupted_mediainfo)
    if not _poll_task or _poll_task.done():
        _poll_task = asyncio.create_task(_poll_loop(dict(config)))
    if not _mediainfo_task or _mediainfo_task.done():
        _mediainfo_task = asyncio.create_task(_mediainfo_loop(dict(config)))
    if not _pipeline_task or _pipeline_task.done():
        _pipeline_task = asyncio.create_task(_pipeline_loop(dict(config)))


async def stop_background() -> None:
    global _poll_task, _poll_stop, _mediainfo_task, _mediainfo_stop, _pipeline_task, _pipeline_stop
    if _poll_stop:
        _poll_stop.set()
    if _poll_task:
        _poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _poll_task
    if _mediainfo_stop:
        _mediainfo_stop.set()
    if _mediainfo_task:
        _mediainfo_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _mediainfo_task
    if _pipeline_stop:
        _pipeline_stop.set()
    if _pipeline_task:
        _pipeline_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _pipeline_task
    _poll_task = None
    _poll_stop = None
    _mediainfo_task = None
    _mediainfo_stop = None
    _pipeline_task = None
    _pipeline_stop = None


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
    if action == "media":
        return {"items": [media_dict(item) for item in await asyncio.to_thread(list_media, int(payload.get("limit") or 200))]}
    if action == "pipeline_events":
        return {"items": await asyncio.to_thread(list_events, pending_only=bool(payload.get("pending_only", True)), limit=int(payload.get("limit") or 100))}
    if action == "ack_pipeline_event":
        event_id = str(payload.get("event_id") or "")
        if not event_id:
            raise ValueError("missing event_id")
        return {"ok": await asyncio.to_thread(acknowledge_event, event_id)}
    raise LookupError(f"unsupported 115 action: {action}")
