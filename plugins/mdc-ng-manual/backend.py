from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

import httpx

from app.plugins.contracts import PluginTestResult
from app.core.models import Job
from app.plugins.external_tasks import (
    create_external_task_job,
    list_provider_external_jobs,
    set_external_task_state,
)
from app.tasks.manager import job_manager

PLUGIN_ID = "mdc-ng-manual"
NOOR_JOB_TYPE = "external_task"
STATUS_LABELS = {
    -2: "已终止",
    -1: "失败",
    0: "等待中",
    1: "执行中",
    2: "已完成",
    3: "重整等待",
    4: "重整中",
}
LINK_MODE_LABELS = {
    0: "硬链接",
    1: "复制",
    2: "移动",
    3: "原地整理",
    4: "软链接",
}
EMPTY_STATE = {"status": "UNSET", "message": "", "fieldErrors": {}, "timestamp": 0}


def _base_url(config: dict[str, Any]) -> str:
    return str(config.get("base_url") or "http://127.0.0.1:9208").strip().rstrip("/")


def _headers(config: dict[str, Any], *, accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "NOOR/1.0 (+mdc-ng-manual)",
        "Referer": f"{_base_url(config)}/manual-jobs",
    }
    cookie = str(config.get("cookie") or "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _timeout(config: dict[str, Any]) -> float:
    try:
        return max(3.0, min(float(config.get("timeout") or 20), 120.0))
    except Exception:
        return 20.0


class _MdcClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    async def get(self, path: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_timeout(self.config), follow_redirects=True, trust_env=False) as client:
            return await client.get(f"{_base_url(self.config)}{path}", headers=_headers(self.config))

    async def post_text_action(self, path: str, action_id: str, payload: Any) -> httpx.Response:
        headers = _headers(self.config, accept="text/x-component")
        headers["Next-Action"] = action_id
        headers["Content-Type"] = "text/plain;charset=UTF-8"
        async with httpx.AsyncClient(timeout=_timeout(self.config), follow_redirects=True, trust_env=False) as client:
            return await client.post(f"{_base_url(self.config)}{path}", headers=headers, content=json.dumps(payload, ensure_ascii=False))

    async def post_form_action(self, path: str, action_id: str, data: list[tuple[str, str]]) -> httpx.Response:
        headers = _headers(self.config, accept="text/x-component")
        headers["Next-Action"] = action_id
        async with httpx.AsyncClient(timeout=_timeout(self.config), follow_redirects=True, trust_env=False) as client:
            files = [(key, (None, value)) for key, value in data]
            return await client.post(f"{_base_url(self.config)}{path}", headers=headers, files=files)


def _extract_first_json_line(text: str) -> dict[str, Any]:
    candidate: dict[str, Any] | None = None
    for line in text.splitlines():
        if ":" not in line:
            continue
        prefix, rest = line.split(":", 1)
        if prefix.isdigit() and rest.startswith("{"):
            try:
                candidate = json.loads(rest)
            except Exception:
                continue
    if candidate is not None:
        return candidate
    raise ValueError("未能解析 MDC-NG Server Action 返回数据")


def _find_json_objects(text: str, marker: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    pos = 0
    while True:
        idx = text.find(marker, pos)
        if idx < 0:
            break
        start = idx + len(marker)
        if start >= len(text) or text[start] != "{":
            pos = idx + len(marker)
            continue
        try:
            obj, end = decoder.raw_decode(text[start:])
        except Exception:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        pos = start + end
    return out


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_duration_seconds(value: float | None) -> str:
    if value is None:
        return ""
    seconds = max(0, int(round(value)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _safe_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return [value]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [value]


def _normalize_job(job: dict[str, Any], base_url: str) -> dict[str, Any]:
    created = _parse_dt(job.get("created_at"))
    started = _parse_dt(job.get("started_at"))
    ended = _parse_dt(job.get("end_at"))
    duration: float | None = None
    if started and ended:
        duration = (ended - started).total_seconds()
    elif created and ended:
        duration = (ended - created).total_seconds()
    return {
        "id": job.get("id"),
        "source_paths": _safe_json_list(job.get("source_pathes")),
        "target_dir": job.get("target_dir") or "",
        "link_mode": int(job.get("link_mode") or 0),
        "link_mode_label": LINK_MODE_LABELS.get(int(job.get("link_mode") or 0), str(job.get("link_mode") or "")),
        "status": int(job.get("status") or 0),
        "status_label": STATUS_LABELS.get(int(job.get("status") or 0), str(job.get("status") or "")),
        "stage": int(job.get("stage") or 0),
        "created_at": job.get("created_at") or "",
        "started_at": job.get("started_at") or "",
        "end_at": job.get("end_at") or "",
        "duration": _format_duration_seconds(duration),
        "finish_count": int(job.get("finish_count") or 0),
        "skip_count": int(job.get("skip_count") or 0),
        "error_count": int(job.get("error_count") or 0),
        "abort_count": int(job.get("abort_count") or 0),
        "total_count": int(job.get("total_count") or 0),
        "error_message": job.get("error_message") or "",
        "tasks_url": f"{base_url}/tasks?manual_job_id={job.get('id')}",
    }


def _display_name_for_paths(source_paths: list[str]) -> str:
    if not source_paths:
        return "MDC-NG 手动任务"
    first = os.path.basename(source_paths[0].rstrip("/")) or source_paths[0]
    if len(source_paths) == 1:
        return first
    return f"{first} 等 {len(source_paths)} 项"


def _noor_status_from_external(job: dict[str, Any] | None) -> str:
    if not job:
        return "queued"
    status = int(job.get("status") or 0)
    if status in {0, 3}:
        return "queued"
    if status in {1, 4}:
        return "running"
    if status == 2:
        return "completed"
    if status == -2:
        return "cancelled"
    return "failed"


def _noor_progress_from_external(job: dict[str, Any] | None, *, status: str | None = None) -> int:
    if not job:
        return 0
    resolved_status = status or _noor_status_from_external(job)
    if resolved_status == "completed":
        return 100
    if resolved_status in {"failed", "cancelled", "skipped"}:
        return 0
    total = int(job.get("total_count") or 0)
    done = int(job.get("finish_count") or 0) + int(job.get("skip_count") or 0) + int(job.get("error_count") or 0) + int(job.get("abort_count") or 0)
    if total > 0:
        progress = int(round(done / total * 100))
        if resolved_status == "running":
            return max(5, min(progress, 99))
        return max(1, min(progress, 95))
    return 15 if resolved_status == "running" else 3


def _remote_id(remote_job: dict[str, Any] | None) -> str | None:
    value = remote_job.get("id") if isinstance(remote_job, dict) else None
    return str(value) if value is not None else None


def _build_external_task_data(
    source_paths: list[str],
    target_folder: str,
    remote_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_paths": source_paths,
        "target_folder": target_folder,
    }
    if remote_job:
        payload["remote_job"] = remote_job
    return payload


def _match_remote_job_for_local(local_job: Job, remote_jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    metadata = local_job.result_metadata or {}
    ext = metadata.get("external_task") if isinstance(metadata, dict) else {}
    if isinstance(ext, dict):
        remote_id = ext.get("external_id")
        data = ext.get("data") if isinstance(ext.get("data"), dict) else {}
        remote_job = data.get("remote_job") if isinstance(data, dict) else None
        remote_id = remote_id or (remote_job.get("id") if isinstance(remote_job, dict) else None)
        if remote_id is not None:
            for candidate in remote_jobs:
                if str(candidate.get("id")) == str(remote_id):
                    return candidate
    source_path = local_job.input_path
    for candidate in remote_jobs:
        sources = candidate.get("source_paths") or []
        if source_path and source_path in sources:
            return candidate
    return None


async def _create_noor_job_for_remote_submission(
    config: dict[str, Any],
    payload: dict[str, Any],
    submit_result: dict[str, Any],
) -> dict[str, Any]:
    source_paths = _parse_source_paths(payload.get("source_paths") or payload.get("paths") or payload.get("sources"))
    original_source_paths = _parse_source_paths(payload.get("original_source_paths")) or source_paths
    defaults = await _fetch_defaults(config)
    target_folder = str(payload.get("target_folder") or defaults.get("target_folder") or "").strip()
    display_name = _display_name_for_paths(source_paths)
    remote_job = submit_result.get("remote_job") if isinstance(submit_result, dict) else None
    noor_status = _noor_status_from_external(remote_job) if submit_result.get("ok") else "failed"
    progress = _noor_progress_from_external(remote_job, status=noor_status) if submit_result.get("ok") else 0
    detail = submit_result.get("message") or (remote_job.get("status_label") if isinstance(remote_job, dict) else None)
    error_message = None if submit_result.get("ok") else (submit_result.get("message") or "提交失败")
    job = await create_external_task_job(
        provider_id=PLUGIN_ID,
        provider_label="MDC-NG",
        name=display_name,
        input_path=original_source_paths[0] if original_source_paths else "",
        external_id=_remote_id(remote_job),
        status=noor_status,
        progress=progress,
        detail=detail,
        error_message=error_message,
        can_cancel=False,
        data={
            **_build_external_task_data(source_paths, target_folder, remote_job if isinstance(remote_job, dict) else None),
            "original_source_paths": original_source_paths,
        },
        phase_label="MDC-NG 重新整理",
    )
    await job_manager.add_log(job.id, f"MDC-NG 提交结果: {submit_result.get('message') or ('成功' if submit_result.get('ok') else '失败')}")
    if isinstance(remote_job, dict):
        await job_manager.add_log(job.id, f"MDC-NG 任务 ID: {remote_job.get('id')}")
    submit_result["noor_job_id"] = job.id
    submit_result["noor_job"] = job.model_dump()
    return submit_result


async def _fetch_defaults(config: dict[str, Any]) -> dict[str, Any]:
    client = _MdcClient(config)
    ts = int(time.time() * 1000)
    resp = await client.post_text_action("/manual-jobs", "607cd0d4b9dd96a2410d82956a9b3427020d2353c1", ["common", ts])
    resp.raise_for_status()
    payload = _extract_first_json_line(resp.text)
    global_cfg = payload.get("global") or {}
    watch_dirs = []
    for idx, item in enumerate(global_cfg.get("watch_dirs") or []):
        if not isinstance(item, dict):
            continue
        watch_dirs.append({
            "index": idx,
            "path": str(item.get("path") or "").strip(),
            "has_override": bool(item.get("config_override")),
        })
    return {
        "target_folder": str(global_cfg.get("target_folder") or "").strip(),
        "link_mode": int(global_cfg.get("link_mode") or 0),
        "delete_empty_parent_after_move": bool(global_cfg.get("delete_empty_parent_after_move")),
        "watch_dirs": watch_dirs,
    }


async def _fetch_jobs(config: dict[str, Any]) -> list[dict[str, Any]]:
    client = _MdcClient(config)
    resp = await client.get("/manual-jobs")
    resp.raise_for_status()
    html = resp.text
    jobs = _find_json_objects(html, '"job":')
    dedup: dict[int, dict[str, Any]] = {}
    for job in jobs:
        try:
            job_id = int(job.get("id"))
        except Exception:
            continue
        dedup[job_id] = job
    items = [_normalize_job(job, _base_url(config)) for _, job in sorted(dedup.items(), key=lambda item: item[0], reverse=True)]
    return items


def _parse_source_paths(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "")
    parts = [line.strip() for line in text.replace("\r", "\n").split("\n")]
    return [part for part in parts if part]


def _source_path_mappings(config: dict[str, Any]) -> list[tuple[str, str]]:
    mappings: list[tuple[str, str]] = []
    raw = str(config.get("source_path_mappings") or "")
    for line in raw.splitlines():
        if "=>" not in line:
            continue
        source, target = (part.strip().rstrip("/") for part in line.split("=>", 1))
        if source and target:
            mappings.append((source, target))
    local_prefix = str(config.get("local_source_prefix") or "").strip().rstrip("/")
    mdc_prefix = str(config.get("mdc_source_prefix") or "").strip().rstrip("/")
    if local_prefix and mdc_prefix:
        mappings.append((local_prefix, mdc_prefix))
    return sorted(mappings, key=lambda item: len(item[0]), reverse=True)


def _map_source_path(config: dict[str, Any], path: str) -> str:
    for source, target in _source_path_mappings(config):
        if path == source:
            return target
        if path.startswith(f"{source}/"):
            return f"{target}{path[len(source):]}"
    return path


async def _create_manual_job(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    source_paths = _parse_source_paths(payload.get("source_paths") or payload.get("paths") or payload.get("sources"))
    if not source_paths:
        raise ValueError("至少填写一个刮削路径")
    defaults: dict[str, Any] | None = None
    target_folder = str(payload.get("target_folder") or "").strip()
    if not target_folder:
        defaults = await _fetch_defaults(config)
        target_folder = str(defaults.get("target_folder") or "").strip()
    if not target_folder:
        raise ValueError("缺少整理目录")
    try:
        link_mode = int(payload.get("link_mode"))
    except Exception:
        if defaults is None:
            defaults = await _fetch_defaults(config)
        link_mode = int(defaults.get("link_mode") or 0)
    reuse_index_raw = payload.get("reuse_watch_index")
    reuse_index: int | None = None
    if reuse_index_raw not in (None, "", "none", "-1"):
        try:
            reuse_index = int(reuse_index_raw)
        except Exception:
            reuse_index = None
    if "delete_empty_parent_after_move" in payload:
        delete_empty_parent_after_move = bool(payload.get("delete_empty_parent_after_move"))
    else:
        if defaults is None:
            defaults = await _fetch_defaults(config)
        delete_empty_parent_after_move = bool(defaults.get("delete_empty_parent_after_move"))
    ts = int(time.time() * 1000)
    bound = [
        ts,
        None if reuse_index is None else str(reuse_index),
        source_paths,
        {**EMPTY_STATE, "timestamp": ts},
        "$K1",
    ]
    form_data: list[tuple[str, str]] = [("1__pathes", path) for path in source_paths]
    form_data.extend([
        ("1_target_folder", target_folder),
        ("1_link_mode", str(link_mode)),
        ("0", json.dumps(bound, ensure_ascii=False)),
    ])
    if delete_empty_parent_after_move:
        form_data.append(("1_delete_empty_parent_after_move", "on"))
    client = _MdcClient(config)
    before_jobs = await _fetch_jobs(config)
    before_ids = {str(job.get("id")) for job in before_jobs}
    resp = await client.post_form_action("/manual-jobs", "7cae04395aa159ff837a4b1ead83c053a8dac5204f", form_data)
    resp.raise_for_status()
    result = _extract_first_json_line(resp.text)
    ok = result.get("status") == "SUCCESS"
    remote_job = None
    jobs: list[dict[str, Any]] = []
    if ok:
        jobs = await _fetch_jobs(config)
        remote_job = next((job for job in jobs if str(job.get("id")) not in before_ids), None)
    return {
        "ok": ok,
        "status": result.get("status") or "",
        "message": result.get("message") or ("创建成功" if ok else "创建失败"),
        "fieldErrors": result.get("fieldErrors") or {},
        "remote_job": remote_job,
        "jobs": jobs[:20] if jobs else [],
    }


async def sync_external_tasks(config: dict[str, Any], *, job_id: str | None = None) -> dict[str, Any]:
    local_jobs = await list_provider_external_jobs(PLUGIN_ID, job_id=job_id, active_only=not bool(job_id))
    if not local_jobs:
        return {"updated": 0}

    remote_jobs = await _fetch_jobs(config)
    updated = 0
    for local_job in local_jobs:
        remote_job = _match_remote_job_for_local(local_job, remote_jobs)
        next_status = _noor_status_from_external(remote_job) if remote_job else local_job.status
        next_progress = _noor_progress_from_external(remote_job, status=next_status) if remote_job else local_job.progress
        next_detail = (
            remote_job.get("status_label")
            if isinstance(remote_job, dict)
            else local_job.detail
        )
        next_error = remote_job.get("error_message") if isinstance(remote_job, dict) and next_status in {"failed", "cancelled"} else None
        metadata = local_job.result_metadata or {}
        ext = metadata.get("external_task") if isinstance(metadata, dict) else None
        if isinstance(ext, dict):
            data = ext.get("data") if isinstance(ext.get("data"), dict) else {}
            data = {**data, "remote_job": remote_job or data.get("remote_job")}
            ext = {**ext, "external_id": _remote_id(remote_job) or ext.get("external_id"), "data": data}
            metadata = {**metadata, "external_task": ext}
        updated_job = await set_external_task_state(
            local_job.id,
            status=next_status,
            progress=next_progress,
            detail=next_detail,
            error_message=next_error,
            result_metadata=metadata if isinstance(metadata, dict) else None,
            phase_label="MDC-NG 重新整理",
        )
        if updated_job:
            updated += 1
    return {"updated": updated}


async def test(config: dict[str, Any]) -> PluginTestResult:
    try:
        defaults = await _fetch_defaults(config)
        jobs = await _fetch_jobs(config)
        return PluginTestResult(
            ok=True,
            message="MDC-NG 手动任务接口可用",
            details={
                "target_folder": defaults.get("target_folder"),
                "watch_dirs": len(defaults.get("watch_dirs") or []),
                "jobs": len(jobs),
            },
        )
    except Exception as exc:
        return PluginTestResult(ok=False, message=f"MDC-NG 连接失败: {exc}", details={})


async def handle_action(action: str, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if action == "overview":
        defaults = await _fetch_defaults(config)
        jobs = await _fetch_jobs(config)
        return {
            "ok": True,
            "base_url": _base_url(config),
            "defaults": defaults,
            "jobs": jobs[:50],
            "stats": {
                "total": len(jobs),
                "running": sum(1 for job in jobs if job["status"] in (0, 1, 3, 4)),
                "finished": sum(1 for job in jobs if job["status"] == 2),
                "failed": sum(1 for job in jobs if job["status"] in (-2, -1)),
            },
        }
    if action == "create":
        original_source_paths = _parse_source_paths(payload.get("source_paths") or payload.get("paths") or payload.get("sources"))
        mapped_source_paths = [_map_source_path(config, path) for path in original_source_paths]
        mapped_payload = {**payload, "source_paths": mapped_source_paths, "original_source_paths": original_source_paths}
        result = await _create_manual_job(config, mapped_payload)
        return await _create_noor_job_for_remote_submission(config, mapped_payload, result)
    raise ValueError(f"unsupported action: {action}")
