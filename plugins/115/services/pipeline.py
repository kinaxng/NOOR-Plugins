from __future__ import annotations

import asyncio
from typing import Any

from .storage import acknowledge_event, emit_event, list_events


def _external_data(job: Any) -> dict[str, Any]:
    metadata = getattr(job, "result_metadata", None) or {}
    external = metadata.get("external_task") if isinstance(metadata, dict) else {}
    return external.get("data") if isinstance(external, dict) and isinstance(external.get("data"), dict) else {}


async def _submit_organizer(config: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    plugin_id = str(config.get("organizer_plugin_id") or "mdc-ng-manual").strip()
    if not plugin_id or plugin_id == "115":
        return None
    from app.plugins.runtime import runtime
    try:
        if not runtime.is_enabled(plugin_id):
            return None
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        local_path = str(payload.get("local_path") or "")
        if not local_path:
            return None
        result = await runtime.handle_action(plugin_id, str(config.get("organizer_action") or "create"), {
            "source_paths": [local_path],
            "provider": "115", "file_id": event.get("file_id"), "pipeline_event_id": event.get("id"),
        })
        if not isinstance(result, dict) or not result.get("ok") or not result.get("noor_job_id"):
            return None
        job_id = str(result["noor_job_id"])
        await asyncio.to_thread(acknowledge_event, event["id"])
        await asyncio.to_thread(emit_event, "115.organizer.submitted", file_id=event.get("file_id") or "",
            payload={"provider": "115", "file_id": event.get("file_id") or "", "organizer_plugin_id": plugin_id,
                "noor_job_id": job_id, "source_path": local_path},
            dedupe_key=f"115.organizer.submitted:{event.get('id')}")
        return {"status": "submitted", "job_id": job_id}
    except Exception:
        # Keep the source event pending. A disabled or temporarily unavailable
        # organizer must not lose the media handoff.
        return None


async def _sync_submission(config: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    plugin_id = str(payload.get("organizer_plugin_id") or "")
    job_id = str(payload.get("noor_job_id") or "")
    if not plugin_id or not job_id:
        return None
    from app.plugins.runtime import runtime
    from app.tasks.manager import job_manager
    if runtime.is_enabled(plugin_id):
        await runtime.sync_external_tasks(job_id=job_id)
    job = await job_manager.get_job(job_id)
    if not job or job.status not in {"completed", "failed", "cancelled", "skipped"}:
        return None
    if job.status != "completed":
        await asyncio.to_thread(acknowledge_event, event["id"])
        await asyncio.to_thread(emit_event, "115.organizer.failed", file_id=event.get("file_id") or "",
            payload={"provider": "115", "file_id": event.get("file_id") or "", "noor_job_id": job_id,
                "status": job.status, "error": str(job.error_message or "")[:500]},
            dedupe_key=f"115.organizer.failed:{job_id}")
        return {"status": job.status, "job_id": job_id}
    data = _external_data(job)
    target = str(data.get("target_folder") or "")
    notification: dict[str, Any] = {"ok": False, "status": "missing_target"}
    if target and config.get("media_library_notify_enabled", True):
        from app.integrations.media_library import notify_server_media_created
        notification = await notify_server_media_created(target)
    await asyncio.to_thread(acknowledge_event, event["id"])
    await asyncio.to_thread(emit_event, "115.organizer.completed", file_id=event.get("file_id") or "",
        payload={"provider": "115", "file_id": event.get("file_id") or "", "noor_job_id": job_id,
            "target_folder": target, "media_library": notification},
        dedupe_key=f"115.organizer.completed:{job_id}")
    return {"status": "completed", "job_id": job_id, "target_folder": target, "media_library": notification}


async def process_pipeline_once(config: dict[str, Any]) -> list[dict[str, Any]]:
    if not config.get("auto_organize_enabled", True):
        return []
    pending = await asyncio.to_thread(list_events, pending_only=True, limit=100)
    results: list[dict[str, Any]] = []
    for event in pending:
        result = None
        if event.get("type") == "115.strm.created":
            result = await _submit_organizer(config, event)
        elif event.get("type") == "115.organizer.submitted":
            result = await _sync_submission(config, event)
        if result:
            results.append(result)
    return results
