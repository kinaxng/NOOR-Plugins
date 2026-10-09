from __future__ import annotations

import time
import asyncio
import contextlib
import hashlib
import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any

from app.plugins.contracts import PluginTestResult
from app.plugins.secrets import plugin_secret_store
from app.integrations.media_library import notify_server_media_created

from .services.auth import (
    code_challenge,
    poll_device_authorization,
    start_device_authorization,
)
from .services.client import ACCESS_LIMIT_CODES, Client115, Error115, PLUGIN_ID, normalize_file
from .services import directory_changes
from .services.offline import add_urls, list_remote_tasks
from .services.mediainfo import (
    enqueue as enqueue_mediainfo,
    process_next as process_next_mediainfo,
)
from .services.storage import (
    acknowledge_event,
    create_task,
    emit_event,
    find_duplicate,
    get_folder_watch_state,
    get_mediainfo,
    get_media,
    get_strm_by_local_path,
    get_task,
    init_storage,
    list_events,
    list_media,
    list_pending_pipeline_tasks,
    list_tasks,
    mark_media_organized,
    mark_strm_assistant,
    media_dict,
    mediainfo_dict,
    reconcile_strm_references,
    recover_interrupted_mediainfo,
    save_folder_watch_state,
    source_identity,
    task_dict,
    update_task,
    upsert_media,
    utcnow,
)
from .services.strm import (
    create_strm,
    is_media_file,
    resolve_stream as resolve_stream_service,
    rotate_stream_token,
)
from .services.strm_assistant import run_task as run_strm_assistant_task
from .services.strm_assistant import scheduled_tasks as strm_assistant_tasks
from .services.strm_assistant import settings as strm_assistant_settings
from .services.strm_assistant import status as strm_assistant_status
from .services.strm_assistant import update_settings as update_strm_assistant_settings
from .services.strm_assistant import write_sidecar as write_strm_assistant_sidecar
from .services.subtitles import is_subtitle_file, match_subtitle, sync_subtitle

_auth_sessions: dict[str, dict[str, Any]] = {}
_poll_task: asyncio.Task[None] | None = None
_directory_cache_task: asyncio.Task | None = None
_poll_stop: asyncio.Event | None = None
_mediainfo_task: asyncio.Task[None] | None = None
_mediainfo_stop: asyncio.Event | None = None
logger = logging.getLogger("noor.plugin.115")


def media_path_mappings(config: dict[str, Any]) -> list[dict[str, str]]:
    """Publish the configured STRM path mapping to NOOR Core."""
    local_incoming = Path(str(config.get("strm_directory") or "")).expanduser()
    server_incoming = Path(str(config.get("organizer_incoming_path") or "")).expanduser()
    if not local_incoming.is_absolute() or not server_incoming.is_absolute():
        return []
    return [{"server_prefix": str(server_incoming.parent), "local_prefix": str(local_incoming.parent)}]


def resolve_artwork(config: dict[str, Any], file_id: str) -> str:
    """Return a scraped vertical poster belonging to a known 115 media row."""
    media = get_media(str(file_id))
    if not media or not media.organized_path:
        raise FileNotFoundError("115 media artwork is not ready")
    remote_parts = PurePosixPath(media.organized_path).parts
    local_incoming = Path(str(config.get("strm_directory") or "")).resolve()
    local_parts = local_incoming.parts
    marker = next((part for part in reversed(local_parts) if part.lower() == "av"), "")
    if not marker or marker not in remote_parts:
        raise FileNotFoundError("115 media artwork path cannot be mapped")
    remote_index = len(remote_parts) - 1 - list(reversed(remote_parts)).index(marker)
    local_index = len(local_parts) - 1 - list(reversed(local_parts)).index(marker)
    local_root = Path(*local_parts[: local_index + 1])
    media_path = local_root.joinpath(*remote_parts[remote_index + 1 :]).resolve()
    directory = media_path.parent
    if local_root not in directory.parents and directory != local_root:
        raise PermissionError("115 media artwork path escaped STRM root")
    for name in ("poster.jpg", "poster.jpeg", "poster.png", "folder.jpg"):
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("115 media poster not found")


def _public_account(data: dict[str, Any]) -> dict[str, Any]:
    space = (
        data.get("rt_space_info") if isinstance(data.get("rt_space_info"), dict) else {}
    )
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
        access_limited = exc.code in ACCESS_LIMIT_CODES
        return {
            "connected": False,
            "status": "access_limit" if access_limited else "risk_control" if exc.risk_control else "token_error",
            "code": exc.code,
            "message": str(exc),
        }


async def test(config: dict[str, Any]) -> PluginTestResult:
    status = await account_status(config)
    return PluginTestResult(
        ok=bool(status.get("connected")),
        message="115 Open Platform connected"
        if status.get("connected")
        else str(status.get("message") or "115 未连接"),
        details={k: v for k, v in status.items() if k not in {"avatar"}},
    )


async def submit_download(
    config: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    raw_urls = payload.get("urls") or payload.get("url") or ""
    urls = [
        str(value).strip()
        for value in (
            raw_urls if isinstance(raw_urls, list) else str(raw_urls).splitlines()
        )
        if str(value).strip()
    ]
    if not urls:
        raise ValueError("missing url/magnet")
    if len(urls) != 1:
        raise ValueError("115 downloader currently accepts one task per submission")
    digest, kind, hint = source_identity(urls[0])
    duplicate = await asyncio.to_thread(find_duplicate, digest)
    if duplicate:
        return {
            "ok": True,
            "duplicate": True,
            "task_id": duplicate.id,
            "status": duplicate.status,
            "message": "115 离线任务已存在",
        }
    directory_id = str(
        payload.get("save_directory")
        or payload.get("savepath")
        or config.get("offline_directory_id")
        or "0"
    )
    hashes = await add_urls(Client115(config), urls, directory_id)
    info_hash = hashes[0]
    context = {
        key: payload.get(key)
        for key in (
            "source_plugin_id",
            "subscription_id",
            "subscription_code",
            "code",
            "cover_url",
            "fanart_url",
        )
        if payload.get(key)
    }
    task = await asyncio.to_thread(
        create_task,
        info_hash=info_hash,
        source_digest=digest,
        source_kind=kind,
        source_hint=hint,
        name=str(payload.get("name") or payload.get("title") or hint),
        target_directory_id=directory_id,
        context=context,
    )
    return {
        "ok": True,
        "task_id": task.id,
        "info_hash": info_hash,
        "status": task.status,
        "message": "submitted to 115 offline",
    }


async def sync_offline_tasks(config: dict[str, Any]) -> dict[str, Any]:
    all_local = await asyncio.to_thread(list_tasks, active_only=False, limit=500)
    # Always synchronize active remote downloads before doing potentially slow
    # completed-file discovery. A stale pipeline retry may traverse a shared
    # cloud folder and must never leave newly submitted tasks stuck as queued.
    local = [task for task in all_local if task.status in {"queued", "downloading"}]
    updated = 0
    completed: list[str] = []
    if local:
        remote = await list_remote_tasks(
            Client115(config), max_pages=int(config.get("offline_poll_max_pages") or 5)
        )
        by_hash = {item["info_hash"].lower(): item for item in remote}
        for task in local:
            current = by_hash.get(task.info_hash.lower())
            if not current:
                continue
            values: dict[str, Any] = {
                "status": current["status"],
                "progress": current["progress"],
                "error_message": current["error"],
                "result_file_id": current["file_id"],
            }
            if current["status"] == "completed":
                values["completed_at"] = task.completed_at or utcnow()
                completed.append(task.info_hash)
                directory_changes.notify_write("upload_completed", {"folder_id": task.target_directory_id})
            if (
                task.status != current["status"]
                or task.progress != current["progress"]
                or task.result_file_id != current["file_id"]
            ):
                await asyncio.to_thread(update_task, task.info_hash, **values)
                updated += 1

    pipeline_tasks = await asyncio.to_thread(list_pending_pipeline_tasks, limit=500)
    pipeline_completed: list[str] = []
    import_budget = max(1, min(int(config.get("existing_import_batch_size") or 1), 10))
    for task in pipeline_tasks:
        context = task.context if isinstance(task.context, dict) else {}
        if context.get("imported_existing") and task.pipeline_status != "completed":
            if import_budget <= 0:
                continue
            import_budget -= 1
        retry_due = task.pipeline_status != "failed" or not task.updated_at or (utcnow() - task.updated_at).total_seconds() >= 300
        if not retry_due:
            continue
        try:
            await discover_completed_task(config, task)
            pipeline_completed.append(task.info_hash)
        except Exception as exc:
            await asyncio.to_thread(update_task, task.info_hash, pipeline_status="failed", error_message=str(exc)[:1000])
    return {
        "checked": len(local),
        "updated": updated,
        "completed": list(dict.fromkeys(completed + pipeline_completed)),
    }


async def sync_watched_folder(config: dict[str, Any]) -> dict[str, Any]:
    """Incrementally turn direct changes in the bound folder into pipeline jobs."""
    if str(config.get("task_discovery_mode") or "noor_only") != "watch_folder":
        return {"enabled": False, "queued": 0}
    folder_id = str(config.get("offline_directory_id") or "0")
    if folder_id == "0":
        return {"enabled": True, "queued": 0, "reason": "root_folder_refused"}

    state = await asyncio.to_thread(get_folder_watch_state, folder_id)
    client = Client115(config)
    first = await client.list_folder(folder_id, offset=0, limit=500)
    first_rows = first.get("items") or []
    newest = max((int(row.get("updated_at") or 0) for row in first_rows), default=0)
    newest_ids = [
        str(row.get("file_id") or "")
        for row in first_rows
        if int(row.get("updated_at") or 0) == newest
    ]
    if not state or not state.initialized:
        await asyncio.to_thread(
            save_folder_watch_state,
            folder_id,
            cursor_updated_at=newest,
            cursor_file_ids=newest_ids,
        )
        logger.info("[115] folder watch baseline folder=%s", folder_id)
        return {"enabled": True, "initialized": True, "queued": 0}

    cursor = int(state.cursor_updated_at or 0)
    boundary_ids = set(state.cursor_file_ids or [])
    rows = list(first_rows)
    offset = len(first_rows)
    count = int(first.get("count") or 0)
    max_pages = max(1, min(int(config.get("folder_watch_max_pages") or 5), 20))
    pages = 1
    while rows and offset < count and pages < max_pages:
        if any(int(row.get("updated_at") or 0) < cursor for row in rows[-500:]):
            break
        page = await client.list_folder(folder_id, offset=offset, limit=500)
        page_rows = page.get("items") or []
        if not page_rows:
            break
        rows.extend(page_rows)
        offset += len(page_rows)
        pages += 1

    candidates = [
        row
        for row in rows
        if int(row.get("updated_at") or 0) > cursor
        or (
            int(row.get("updated_at") or 0) == cursor
            and str(row.get("file_id") or "") not in boundary_ids
        )
    ]
    queued = 0
    for item in reversed(candidates):
        file_id = str(item.get("file_id") or "").strip()
        remote_updated = int(item.get("updated_at") or 0)
        if not file_id or (
            not item.get("is_directory")
            and await asyncio.to_thread(get_media, file_id)
        ):
            continue
        digest = hashlib.sha256(
            f"115-watch:{folder_id}:{file_id}:{remote_updated}".encode()
        ).hexdigest()
        if await asyncio.to_thread(find_duplicate, digest):
            continue
        info_hash = f"watch-{digest[:32]}"
        task = await asyncio.to_thread(
            create_task,
            info_hash=info_hash,
            source_digest=digest,
            source_kind="115-watch",
            source_hint=file_id,
            name=str(item.get("name") or file_id),
            target_directory_id=folder_id,
            context={
                "code": str(item.get("name") or file_id),
                "imported_existing": True,
                "watched_folder": True,
                "watch_updated_at": remote_updated,
            },
        )
        await asyncio.to_thread(
            update_task,
            task.info_hash,
            status="completed",
            progress=100,
            result_file_id=file_id,
            completed_at=utcnow(),
        )
        queued += 1
        logger.info("[115] watched item queued file_id=%s task=%s", file_id, task.id)

    if newest >= cursor:
        ids = newest_ids if newest > cursor else list(boundary_ids | set(newest_ids))
        await asyncio.to_thread(
            save_folder_watch_state,
            folder_id,
            cursor_updated_at=newest,
            cursor_file_ids=ids,
        )
    return {"enabled": True, "queued": queued, "checked": len(rows)}


async def _walk_task_root(
    config: dict[str, Any], root_id: str
) -> tuple[list[dict[str, Any]], bool]:
    """Walk only the completed task root, never the user's cloud root."""
    if not root_id or root_id == "0":
        raise ValueError("115 离线任务未返回安全的结果目录，已拒绝扫描根目录")
    client = Client115(config)
    max_depth = max(0, min(int(config.get("discovery_max_depth") or 4), 8))
    max_items = max(20, min(int(config.get("discovery_max_items") or 2000), 10000))
    queue: list[tuple[str, int, str]] = [(root_id, 0, "")]
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    complete = True
    while queue and len(files) < max_items:
        folder_id, depth, path = queue.pop(0)
        if folder_id in seen:
            continue
        seen.add(folder_id)
        offset = 0
        while len(files) < max_items:
            page = await client.list_folder(
                folder_id, offset=offset, limit=min(500, max_items - len(files))
            )
            rows = page["items"]
            for item in rows:
                item["display_path"] = f"{path}/{item['name']}".lstrip("/")
                if item.get("is_directory"):
                    if depth < max_depth and item.get("file_id"):
                        queue.append((item["file_id"], depth + 1, item["display_path"]))
                    elif item.get("file_id"):
                        complete = False
                else:
                    files.append(item)
            offset += len(rows)
            if not rows or offset >= int(page.get("count") or 0):
                break
            if len(files) >= max_items:
                complete = False
    if queue:
        complete = False
    return files, complete


_MEDIA_CODE_RE = re.compile(r"(?i)(?:FC2(?:-PPV)?[-_ ]?\d{5,8}|[A-Z]{2,10}[-_ ]?\d{2,6})")


def _task_expected_codes(task: Any) -> set[str]:
    context = task.context if isinstance(task.context, dict) else {}
    candidates = [
        context.get("subscription_code"),
        context.get("code"),
        # Manual links receive a digest as their display name, not a media code.
        "" if re.fullmatch(r"[0-9a-fA-F]{16,64}", str(task.name or "")) else task.name,
    ]
    return {
        re.sub(r"[^A-Z0-9]", "", match.group(0).upper()).replace("FC2PPV", "FC2")
        for value in candidates
        for match in _MEDIA_CODE_RE.finditer(str(value or ""))
    }


def _matches_task_identity(item: dict[str, Any], expected_codes: set[str]) -> bool:
    if not expected_codes:
        return True
    normalized = re.sub(
        r"[^A-Z0-9]", "", str(item.get("name") or "").upper()
    ).replace("FC2PPV", "FC2")
    return any(code in normalized for code in expected_codes)


async def discover_completed_task(config: dict[str, Any], task: Any) -> list[str]:
    root_id = str(task.result_file_id or "")
    await asyncio.to_thread(update_task, task.info_hash, pipeline_status="running")
    info = await Client115(config).file_info(root_id)
    if info.get("pick_code") and is_media_file({**info, "is_directory": False}, config):
        files = [
            {**info, "is_directory": False, "display_path": info.get("name") or root_id}
        ]
        discovery_complete = True
    else:
        expected_codes = _task_expected_codes(task)
        shared_roots = {"", "0", str(getattr(task, "target_directory_id", "") or ""),
                        str(config.get("offline_directory_id") or "")}
        if root_id in shared_roots and not expected_codes:
            raise ValueError("115 仅返回共享下载目录，无法确定此手动任务的结果；请从远端文件浏览中选择对应文件导入，避免误处理其他任务")
        files, discovery_complete = await _walk_task_root(config, root_id)
        if expected_codes:
            matched_media = [
                item
                for item in files
                if is_media_file(item, config)
                and _matches_task_identity(item, expected_codes)
            ]
            if not matched_media:
                raise ValueError(
                    "115 任务结果目录未发现与任务番号匹配的视频，已拒绝处理共享目录"
                )
            files = [
                item
                for item in files
                if item in matched_media
                or (
                    is_subtitle_file(item, config)
                    and any(match_subtitle(media, item) for media in matched_media)
                )
            ]
    detected: list[str] = []
    subtitle_files = [item for item in files if is_subtitle_file(item, config)]
    client = Client115(config)
    for item in files:
        if not is_media_file(item, config):
            continue
        media, _created = await asyncio.to_thread(
            upsert_media,
            item,
            task_id=task.info_hash,
            display_path=item.get("display_path") or item["name"],
        )
        strm = await asyncio.to_thread(
            create_strm, config, item, owner_id=task.info_hash
        )
        synced_subtitles = []
        for subtitle in subtitle_files:
            if match_subtitle(item, subtitle):
                synced_subtitles.append(
                    await sync_subtitle(
                        config,
                        client=client,
                        media=item,
                        subtitle=subtitle,
                        strm_path=strm["path"],
                    )
                )
        await asyncio.to_thread(
            emit_event,
            "115.media.discovered",
            file_id=media.file_id,
            payload={
                "provider": "115",
                "file_id": media.file_id,
                "sha1": media.sha1,
                "size": media.size,
                "name": media.name,
            },
            dedupe_key=f"115.media.discovered:{media.file_id}:{media.sha1}:{media.size}",
        )
        await asyncio.to_thread(
            emit_event,
            "115.strm.created",
            file_id=media.file_id,
            payload={
                "provider": "115",
                "file_id": media.file_id,
                "local_path": strm["path"],
                "name": media.name,
                "subtitles": [
                    {"file_id": row["file_id"], "local_path": row["path"]}
                    for row in synced_subtitles
                ],
            },
            dedupe_key=f"115.strm.created:{media.file_id}:{strm['path']}",
        )
        if config.get("mediainfo_enabled", True):
            await asyncio.to_thread(enqueue_mediainfo, media)
        detected.append(media.file_id)
    cleanup = {"skipped": True, "reason": "discovery_incomplete"}
    if discovery_complete:
        cleanup = await asyncio.to_thread(
            reconcile_strm_references,
            task.info_hash,
            set(detected),
            output_root=str(config.get("strm_directory") or ""),
        )
        cleanup["skipped"] = False
    await asyncio.to_thread(
        update_task,
        task.info_hash,
        detected_file_ids=detected,
        pipeline_status="completed",
        error_message="",
    )
    logger.info(
        "[115] task reconciliation task=%s complete=%s result=%s",
        task.info_hash,
        discovery_complete,
        cleanup,
    )
    return detected


async def resolve_stream(
    config: dict[str, Any], file_id: str, request: Any, token: str
) -> dict[str, Any]:
    user_agent = (
        str(request.headers.get("user-agent") or "NOOR-stream-client")
        if request is not None
        else "NOOR-stream-client"
    )
    return await resolve_stream_service(
        config,
        file_id,
        token=token,
        user_agent=user_agent,
    )


async def _organizer_context(config: dict[str, Any]) -> dict[str, Any]:
    legacy_path = str(config.get("organizer_incoming_path") or "").rstrip("/")
    result: dict[str, Any] = {
        "available": False,
        "incoming_path": legacy_path,
        "settings_url": "",
        "watch_dirs": [],
    }
    try:
        from app.plugins.runtime import runtime

        overview = await runtime.handle_action("mdc-ng-manual", "overview", {})
        watch_dirs = [
            str(item.get("path") or "").rstrip("/")
            for item in ((overview.get("defaults") or {}).get("watch_dirs") or [])
            if str(item.get("path") or "").strip()
        ]
        preferred = next((path for path in watch_dirs if path == legacy_path), "")
        if not preferred:
            preferred = next(
                (path for path in watch_dirs if path.lower().endswith("/incoming") and "/strm/" in path.lower()),
                "",
            )
        result.update(
            available=True,
            incoming_path=preferred or legacy_path,
            settings_url=(
                f"{str(overview.get('base_url') or '').rstrip('/')}/settings/watch-dir"
                if str(overview.get("base_url") or "").startswith(("http://", "https://"))
                else ""
            ),
            watch_dirs=watch_dirs,
        )
    except Exception as exc:
        result["message"] = str(exc)
    return result
    return await resolve_stream_service(
        config, str(file_id), token=str(token or ""), user_agent=user_agent
    )


async def sync_organized_results(config: dict[str, Any]) -> dict[str, int]:
    """Consume completed MDC-NG rows and notify Emby exactly once per target."""
    organizer = await _organizer_context(config)
    organizer_root = str(organizer.get("incoming_path") or "").rstrip("/")
    local_root = str(config.get("strm_directory") or "").rstrip("/")
    if not organizer_root or not local_root:
        return {"checked": 0, "matched": 0, "notified": 0, "failed": 0}
    from app.plugins.runtime import runtime

    response = await runtime.handle_action(
        "mdc-ng-manual",
        "organized_results",
        {"source_prefix": organizer_root, "limit": 200},
    )
    items = response.get("items") if isinstance(response, dict) else []
    result = {"checked": len(items or []), "matched": 0, "notified": 0, "failed": 0}
    root_path = PurePosixPath(organizer_root)
    for item in items or []:
        source_path = str(item.get("source_path") or "")
        target_path = str(item.get("target_path") or "")
        try:
            relative = PurePosixPath(source_path).relative_to(root_path)
        except (TypeError, ValueError):
            continue
        strm = await asyncio.to_thread(
            get_strm_by_local_path, str(Path(local_root).joinpath(*relative.parts))
        )
        if not strm:
            continue
        result["matched"] += 1
        media = await asyncio.to_thread(get_media, strm.file_id)
        integration_enabled = bool(config.get("strm_assistant_enabled", True))
        already_notified = bool(
            media
            and media.organized_path == target_path
            and media.emby_status == "notified"
        )
        if not media or (
            media.organized_path == target_path
            and media.emby_status == "notified"
            and (not integration_enabled or media.strm_assistant_status == "ready")
        ):
            continue
        if integration_enabled and media.strm_assistant_status != "ready":
            record = await asyncio.to_thread(get_mediainfo, strm.file_id)
            if not record or record.status != "ready":
                await asyncio.to_thread(
                    mark_strm_assistant,
                    strm.file_id,
                    status="waiting_mediainfo",
                    error="",
                )
            else:
                try:
                    sidecar = await asyncio.to_thread(
                        write_strm_assistant_sidecar,
                        {**config, "organizer_incoming_path": organizer_root},
                        target_path,
                        media,
                        record,
                    )
                    await asyncio.to_thread(
                        mark_strm_assistant,
                        strm.file_id,
                        status="ready",
                        error="",
                    )
                    logger.info(
                        "[115] StrmAssistant sidecar ready file_id=%s path=%s",
                        strm.file_id,
                        sidecar,
                    )
                except Exception as exc:
                    await asyncio.to_thread(
                        mark_strm_assistant,
                        strm.file_id,
                        status="failed",
                        error=str(exc),
                    )
                    logger.warning(
                        "[115] StrmAssistant sidecar failed file_id=%s error=%s",
                        strm.file_id,
                        exc,
                    )
        if already_notified:
            continue
        try:
            emby = await notify_server_media_created(target_path)
            ok = bool(emby.get("ok"))
        except Exception as exc:
            logger.warning(
                "[115] Emby targeted update failed file_id=%s target=%s error=%s",
                strm.file_id,
                target_path,
                exc,
            )
            ok = False
        media = await asyncio.to_thread(
            mark_media_organized,
            strm.file_id,
            server_path=target_path,
            emby_ok=ok,
        )
        result["notified" if ok else "failed"] += 1
        await asyncio.to_thread(
            emit_event,
            "115.emby.notified" if ok else "115.emby.failed",
            file_id=strm.file_id,
            payload={
                "provider": "115",
                "file_id": strm.file_id,
                "source_path": source_path,
                "local_path": target_path,
                "status": media.emby_status if media else "failed",
            },
            dedupe_key=f"115.emby:{'notified' if ok else 'failed'}:{strm.file_id}:{target_path}",
        )
    return result


async def _poll_loop(config: dict[str, Any]) -> None:
    global _poll_stop
    _poll_stop = asyncio.Event()
    while not _poll_stop.is_set():
        try:
            if config.get("access_token") or config.get("refresh_token"):
                await sync_watched_folder(config)
                await sync_offline_tasks(config)
            if config.get("organizer_sync_enabled", True):
                await sync_organized_results(config)
        except Error115 as exc:
            if exc.risk_control:
                await asyncio.sleep(
                    max(300, int(config.get("offline_poll_interval") or 60) * 5)
                )
        except Exception as exc:
            logger.exception("[115] offline polling failed: %s", exc)
        try:
            await asyncio.wait_for(
                _poll_stop.wait(),
                timeout=max(30, int(config.get("offline_poll_interval") or 60)),
            )
        except asyncio.TimeoutError:
            pass


async def _mediainfo_loop(config: dict[str, Any]) -> None:
    global _mediainfo_stop
    _mediainfo_stop = asyncio.Event()
    while not _mediainfo_stop.is_set():
        worked = False
        if config.get("mediainfo_enabled", True):
            concurrency = max(1, min(int(config.get("mediainfo_concurrency") or 1), 4))
            results = await asyncio.gather(
                *(process_next_mediainfo(config) for _ in range(concurrency)),
                return_exceptions=True,
            )
            worked = any(
                result is not None and not isinstance(result, Exception)
                for result in results
            )
        try:
            await asyncio.wait_for(_mediainfo_stop.wait(), timeout=2 if worked else 10)
        except asyncio.TimeoutError:
            pass


async def start_background(config: dict[str, Any]) -> None:
    global _poll_task, _mediainfo_task, _directory_cache_task
    init_storage()
    await asyncio.to_thread(recover_interrupted_mediainfo)
    if not _poll_task or _poll_task.done():
        _poll_task = asyncio.create_task(_poll_loop(dict(config)))
    if not _mediainfo_task or _mediainfo_task.done():
        _mediainfo_task = asyncio.create_task(_mediainfo_loop(dict(config)))
    if not _directory_cache_task or _directory_cache_task.done():
        _directory_cache_task = asyncio.create_task(directory_changes.run(dict(config), Client115))


async def stop_background() -> None:
    global _poll_task, _poll_stop, _mediainfo_task, _mediainfo_stop, _directory_cache_task
    if _directory_cache_task:
        _directory_cache_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _directory_cache_task
        _directory_cache_task = None
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
    _poll_task = None
    _poll_stop = None
    _mediainfo_task = None
    _mediainfo_stop = None


async def on_config_updated(config: dict[str, Any]) -> None:
    """Restart workers so they never retain a stale configuration snapshot."""
    await stop_background()
    await start_background(dict(config))


async def handle_action(
    action: str, config: dict[str, Any], payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = payload or {}
    # 115 receives one magnet/ED2K/HTTP source per offline task. Its
    # destination is a cloud folder ID, not a local downloader path, so the
    # shared push dialog only needs to show the downloader name.
    if action == "download_options":
        return {
            "ok": True,
            "downloader": "115 离线",
            "default_savepath": "",
            "default_category": "",
            "categories": [],
            "paths": [],
            "supports_categories": False,
            "supports_savepath": False,
            "supports_rename": False,
            "supports_resource_preview": False,
            "supports_file_indices": False,
            "supports_small_file_filter": False,
        }
    if action in {"status", "account"}:
        return await account_status(config)
    if action == "directory_event":
        folder_ids = payload.get("folder_ids") if isinstance(payload.get("folder_ids"), list) else []
        file_ids = payload.get("file_ids") if isinstance(payload.get("file_ids"), list) else []
        paths = payload.get("paths") if isinstance(payload.get("paths"), list) else []
        folder_ids = [str(value).strip() for value in folder_ids[:200] if str(value).strip() not in {"", "0"}]
        file_ids = [str(value).strip() for value in file_ids[:200] if str(value).strip()]
        paths = [str(value).strip() for value in paths[:200] if str(value).strip()]
        if not folder_ids and not file_ids and not paths:
            raise ValueError("目录变更事件缺少 folder_ids、file_ids 或 paths")
        directory_changes.invalidate(folder_ids, file_ids)
        matched = directory_changes.invalidate_paths(paths)
        return {
            "ok": True,
            "folders": len(set(folder_ids)),
            "files": len(set(file_ids)),
            "paths": len(set(paths)),
            "matched_folders": matched,
            "message": "115 目录变更事件已接收",
        }
    if action == "pipeline_status":
        incoming = str(config.get("strm_directory") or "").rstrip("/")
        organizer = await _organizer_context(config)
        organizer_path = str(organizer.get("incoming_path") or "").rstrip("/")
        result = {
            "strm_directory": incoming,
            "organizer_incoming_path": organizer_path,
            "strm_directory_ready": bool(
                incoming and await asyncio.to_thread(Path(incoming).is_dir)
            ),
            "organizer_available": bool(organizer.get("available")),
            "organizer_watching": organizer_path in (organizer.get("watch_dirs") or []),
            "organizer_settings_url": str(organizer.get("settings_url") or ""),
            "watch_dirs": organizer.get("watch_dirs") or [],
        }
        if organizer.get("message"):
            result["message"] = organizer["message"]
        result["ready"] = bool(
            result["strm_directory_ready"]
            and result["organizer_available"]
            and result["organizer_watching"]
        )
        result["strm_assistant"] = await strm_assistant_status()
        result["strm_assistant_enabled"] = bool(
            config.get("strm_assistant_enabled", True)
        )
        return result
    if action == "strm_assistant_status":
        return await strm_assistant_status()
    if action == "strm_assistant_settings":
        return await asyncio.to_thread(strm_assistant_settings)
    if action == "strm_assistant_update_settings":
        values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
        return await asyncio.to_thread(update_strm_assistant_settings, values)
    if action == "strm_assistant_tasks":
        return await strm_assistant_tasks()
    if action == "strm_assistant_run_task":
        return await run_strm_assistant_task(str(payload.get("task_id") or ""))
    if action == "sync_organized":
        return await sync_organized_results(config)
    if action == "rotate_stream_token":
        return await asyncio.to_thread(rotate_stream_token, config)
    if action == "auth_start":
        session = await start_device_authorization(config)
        uid = session["uid"]
        if not uid:
            raise ValueError("115 未返回授权会话")
        _auth_sessions[uid] = {**session, "created_at": time.time()}
        return {key: session[key] for key in ("uid", "qrcode_image")}
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
        page = await Client115(config).list_folder(
            str(payload.get("folder_id") or config.get("offline_directory_id") or "0"),
            offset=int(payload.get("offset") or 0),
            limit=int(payload.get("limit") or 200),
        )
        imported_ids = {
            row.file_id for row in await asyncio.to_thread(list_media, 1000)
        }
        for item in page.get("items") or []:
            item["imported"] = bool(
                not item.get("is_directory") and item.get("file_id") in imported_ids
            )
        return page
    if action == "import_existing":
        raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
        items = [item for item in raw_items if isinstance(item, dict)]
        if not items:
            raise ValueError("请选择要导入的 115 文件或文件夹")
        results = []
        for selected in items:
            file_id = str(selected.get("file_id") or "").strip()
            name = str(selected.get("name") or file_id).strip()
            if not file_id or file_id == "0":
                results.append({"file_id": file_id, "name": name, "status": "failed", "error": "不允许导入 115 根目录"})
                continue
            existing_media = await asyncio.to_thread(get_media, file_id)
            if existing_media:
                results.append({"file_id": file_id, "name": name, "status": "duplicate", "task_id": existing_media.source_task_id})
                continue
            digest = hashlib.sha256(f"115-import:{file_id}".encode()).hexdigest()
            duplicate = await asyncio.to_thread(find_duplicate, digest)
            if duplicate:
                results.append({"file_id": file_id, "name": name, "status": "duplicate", "task_id": duplicate.id})
                continue
            task_key = f"import-{file_id}"
            task = await asyncio.to_thread(
                create_task,
                info_hash=task_key,
                source_digest=digest,
                source_kind="115",
                source_hint=file_id,
                name=name,
                target_directory_id=str(selected.get("parent_id") or ""),
                context={"code": name, "imported_existing": True},
            )
            task = await asyncio.to_thread(
                update_task,
                task.info_hash,
                status="completed",
                progress=100,
                result_file_id=file_id,
                completed_at=utcnow(),
            )
            results.append({"file_id": file_id, "name": name, "status": "queued", "task_id": task.id})
        return {
            "ok": not any(item["status"] == "failed" for item in results),
            "items": results,
            "queued": sum(item["status"] == "queued" for item in results),
            "duplicates": sum(item["status"] == "duplicate" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
        }
    if action == "file_info":
        return await Client115(config).file_info(str(payload.get("file_id") or ""))
    if action == "create_folder":
        return await Client115(config).create_folder(
            str(payload.get("parent_id") or "0"), str(payload.get("name") or "")
        )
    if action == "tasks":
        return {
            "items": [
                task_dict(task)
                for task in await asyncio.to_thread(
                    list_tasks,
                    active_only=False,
                    limit=int(payload.get("limit") or 100),
                )
            ]
        }
    if action == "projects":
        tasks = await asyncio.to_thread(
            list_tasks, active_only=False, limit=int(payload.get("limit") or 100)
        )
        media_rows = await asyncio.to_thread(list_media, 1000)
        media_by_id = {item.file_id: media_dict(item) for item in media_rows}
        projects = []
        for task in tasks:
            item = task_dict(task)
            context = task.context if isinstance(task.context, dict) else {}
            item["cover_url"] = str(
                context.get("cover_url") or context.get("fanart_url") or ""
            )
            files = [
                media_by_id[file_id]
                for file_id in item.get("detected_file_ids") or []
                if file_id in media_by_id
            ]
            item["media"] = files
            if files:
                try:
                    resolve_artwork(config, files[0]["file_id"])
                    item["cover_url"] = f"/api/plugins/115/artwork/{files[0]['file_id']}"
                except (FileNotFoundError, PermissionError):
                    # A horizontal provider jacket is not a vertical poster.
                    item["cover_url"] = ""
            item["stages"] = {
                "offline": task.status,
                "discover": task.pipeline_status,
                "strm": "ready" if files and all(row["strm_status"] in {"created", "ready", "reused"} for row in files) else ("waiting" if not files else "partial"),
                "mediainfo": "ready" if files and all(row["mediainfo_status"] == "ready" for row in files) else ("waiting" if not files else "partial"),
                "organize": "ready" if files and all(row["organization_status"] == "completed" for row in files) else "waiting",
                "assistant": "ready" if files and all(row["strm_assistant_status"] == "ready" for row in files) else "waiting",
                "emby": "ready" if files and all(row["emby_status"] == "notified" for row in files) else "waiting",
            }
            projects.append(item)
        return {"items": projects}
    if action == "sync_tasks":
        return await sync_offline_tasks(config)
    if action == "retry_pipeline":
        task_id = str(payload.get("task_id") or payload.get("info_hash") or "")
        task = await asyncio.to_thread(get_task, task_id)
        if not task:
            raise LookupError("115 offline task not found")
        if task.status != "completed":
            raise ValueError("only completed tasks can retry the media pipeline")
        task = await asyncio.to_thread(
            update_task, task.info_hash, pipeline_status="pending", error_message=""
        )
        try:
            detected = await discover_completed_task(config, task)
        except Exception as exc:
            await asyncio.to_thread(
                update_task, task.info_hash, pipeline_status="failed", error_message=str(exc)[:1000]
            )
            raise
        return {"ok": True, "task_id": task.id, "detected_file_ids": detected}
    if action == "media":
        return {
            "items": [
                media_dict(item)
                for item in await asyncio.to_thread(
                    list_media, int(payload.get("limit") or 200)
                )
            ]
        }
    if action == "mediainfo":
        file_id = str(payload.get("file_id") or "")
        if not file_id:
            raise ValueError("missing file_id")
        record = await asyncio.to_thread(get_mediainfo, file_id)
        if not record:
            raise LookupError("115 MediaInfo not found")
        return mediainfo_dict(record)
    if action == "mark_organized":
        file_id = str(payload.get("file_id") or "")
        if not file_id:
            raise ValueError("missing file_id")
        emby = payload.get("emby") if isinstance(payload.get("emby"), dict) else {}
        media = await asyncio.to_thread(
            mark_media_organized,
            file_id,
            local_path=str(payload.get("local_path") or ""),
            server_path=str(payload.get("server_path") or ""),
            emby_ok=bool(emby.get("ok")),
        )
        if not media:
            raise LookupError("115 media file not found")
        await asyncio.to_thread(
            emit_event,
            "115.emby.notified",
            file_id=file_id,
            payload={
                "provider": "115",
                "file_id": file_id,
                "local_path": media.organized_path,
                "status": media.emby_status,
            },
            dedupe_key=f"115.emby.notified:{file_id}:{media.organized_path}",
        )
        return {"ok": True, "file_id": file_id, "emby_status": media.emby_status}
    if action == "pipeline_events":
        return {
            "items": await asyncio.to_thread(
                list_events,
                pending_only=bool(payload.get("pending_only", True)),
                limit=int(payload.get("limit") or 100),
            )
        }
    if action == "ack_pipeline_event":
        event_id = str(payload.get("event_id") or "")
        if not event_id:
            raise ValueError("missing event_id")
        return {"ok": await asyncio.to_thread(acknowledge_event, event_id)}
    raise LookupError(f"unsupported 115 action: {action}")
