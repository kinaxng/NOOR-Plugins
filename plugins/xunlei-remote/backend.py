from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from httpx import RemoteProtocolError

from app.plugins.contracts import PluginTestResult

PLUGIN_ID = "xunlei-remote"
DEFAULT_ENTRY_PATH = "/webman/3rdparty/pan-xunlei-com/index.cgi/"
DEFAULT_SAVE_PATH = "/downloads/xunlei/"
DEFAULT_RESTORE_SCAN_ROOTS: list[str] = []
DEFAULT_RESTORE_PATH_MAPPINGS: list[tuple[str, str]] = []
_speed_scheduler_task: asyncio.Task[None] | None = None
_speed_scheduler_stop: asyncio.Event | None = None
_speed_scheduler_status: dict[str, Any] = {
    "status": "idle",
    "last_checked_at": None,
    "last_applied_at": None,
    "last_message": "等待检测下载任务",
    "last_error": "",
}
def _base(config: dict[str, Any]) -> str:
    raw = str(config.get("base_url") or "").strip()
    if not raw:
        raw = "https://127.0.0.1:5001"
    raw = raw.rstrip("/")
    if raw.endswith("/webman/3rdparty/pan-xunlei-com/index.cgi"):
        raw = raw[: -len("/webman/3rdparty/pan-xunlei-com/index.cgi")]
    return raw


def _entry_path(config: dict[str, Any]) -> str:
    value = str(config.get("entry_path") or DEFAULT_ENTRY_PATH).strip() or DEFAULT_ENTRY_PATH
    if not value.startswith("/"):
        value = "/" + value
    if not value.endswith("/"):
        value += "/"
    return value


def _entry_url(config: dict[str, Any]) -> str:
    return f"{_base(config)}{_entry_path(config)}"


def _api_url(config: dict[str, Any], path: str) -> str:
    return urljoin(_entry_url(config), path.lstrip("/"))


def _verify(config: dict[str, Any]) -> bool:
    return not bool(config.get("insecure_skip_verify", True))


def _headers(config: dict[str, Any], *, json_api: bool = False, pan_auth: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*" if json_api else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
        "User-Agent": str(config.get("user_agent") or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    }
    token = str(config.get("authorization") or config.get("token") or "").strip()
    if token:
        headers["Authorization"] = token
    cookie = str(config.get("cookie") or "").strip()
    if cookie:
        headers["Cookie"] = cookie
    if json_api:
        headers["Content-Type"] = "application/json"
    if pan_auth:
        headers["pan-auth"] = pan_auth
        headers["device-space"] = ""
        headers["Referer"] = _entry_url(config)
    return headers


def _synology_auth_error_message(config: dict[str, Any]) -> str:
    has_authorization = bool(str(config.get("authorization") or config.get("token") or "").strip())
    has_cookie = bool(str(config.get("cookie") or "").strip())
    if has_authorization:
        return "群晖登录态校验失败：当前 Authorization 认证令牌无效或已过期，请从迅雷 NAS 请求头重新复制完整 Authorization 值"
    if has_cookie:
        return "群晖登录态校验失败：当前 Cookie/DSM 登录态已失效，请重新复制迅雷 NAS 请求头 Cookie，或填写更稳定的 Authorization: Basic ... 认证令牌"
    return "群晖登录态校验失败：请在插件配置里填写从迅雷 NAS 请求头复制的 Cookie，或填写 Authorization: Basic ... 认证令牌"


def _extract_pan_auth(text: str) -> str:
    patterns = [
        r'function\s+uiauth\(value\)\s*\{\s*return\s+"([^"]+)"\s*\}',
        r'uiauth\s*[:=]\s*"([^"]+)"',
        r'pan_auth\s*[:=]\s*"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    # 兜底：页面里通常会出现一个 JWT 形态的 UIAuth。
    for match in re.finditer(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', text):
        token = match.group(0)
        if len(token) > 80:
            return token
    raise ValueError("未能从迅雷 NAS 页面提取 pan_auth/uiauth")


def _parse_device_id(data: dict[str, Any]) -> str:
    target = str(data.get("target") or data.get("space") or "")
    if "#" in target:
        return target.split("#", 1)[1]
    for key in ("device_id", "deviceId", "id"):
        value = str(data.get(key) or "")
        if value:
            return value
    raise ValueError(f"未能从设备信息提取 device_id: {data}")


def _format_size(size: Any) -> str:
    try:
        value = float(size or 0)
    except Exception:
        value = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.2f} {units[idx]}"


def _split_config_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").replace("|", "\n").splitlines() if item.strip()]


def _restore_scan_roots(config: dict[str, Any]) -> list[Path]:
    roots = _split_config_lines(config.get("restore_scan_roots")) or DEFAULT_RESTORE_SCAN_ROOTS
    out: list[Path] = []
    for root in roots:
        try:
            path = Path(root).expanduser()
        except Exception:
            continue
        if path.exists() and path.is_dir():
            out.append(path)
    return out


def _restore_path_mappings(config: dict[str, Any]) -> list[tuple[str, str]]:
    raw = _split_config_lines(config.get("restore_path_mappings"))
    mappings: list[tuple[str, str]] = []
    for line in raw:
        if "=" in line:
            left, right = line.split("=", 1)
        elif "=>" in line:
            left, right = line.split("=>", 1)
        else:
            continue
        left = left.strip().rstrip("/")
        right = right.strip().rstrip("/")
        if left and right:
            mappings.append((left, right))
    return mappings or DEFAULT_RESTORE_PATH_MAPPINGS


def _remote_path_for_local(config: dict[str, Any], local_path: str) -> str:
    value = str(local_path or "")
    for local_prefix, remote_prefix in _restore_path_mappings(config):
        local_prefix = local_prefix.rstrip("/")
        remote_prefix = remote_prefix.rstrip("/")
        if value == local_prefix or value.startswith(f"{local_prefix}/"):
            suffix = value[len(local_prefix):].lstrip("/")
            return f"{remote_prefix}/{suffix}".rstrip("/")
    return value


def _extract_restore_code(path: Path) -> str:
    candidates = [path.stem, path.parent.name, path.name]
    for value in candidates:
        cleaned = re.sub(r"\.(?:mp4|mkv|avi|wmv|mov|ts|m2ts|xltd|xtld|td)$", "", value, flags=re.I)
        match = re.search(r"([A-Z]{2,8}[-_ ]?\d{2,5}(?:[-_ ]?[A-Z])?|FC2[-_ ]?(?:PPV[-_ ]?)?\d{5,8}|\d{3,6}JAC[-_ ]?\d{2,5})", cleaned, re.I)
        if match:
            return re.sub(r"[_ ]+", "-", match.group(1)).upper()
    return ""


def _scan_restore_files(config: dict[str, Any], limit: int = 80) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    limit = max(1, min(int(limit or 80), 500))
    for root in _restore_scan_roots(config):
        for path in root.rglob("*"):
            if len(items) >= limit:
                return items
            if not path.is_file() or path.suffix.lower() not in {".xltd", ".xtld"}:
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                stat = path.stat()
            except Exception:
                continue
            remote_file = _remote_path_for_local(config, str(path))
            remote_dir = str(Path(remote_file).parent)
            items.append({
                "path": str(path),
                "name": path.name,
                "code": _extract_restore_code(path),
                "size": stat.st_size,
                "size_formatted": _format_size(stat.st_size),
                "mtime": int(stat.st_mtime),
                "remote_file": remote_file,
                "remote_dir": remote_dir,
                "recyclable": "#recycle" in str(path),
            })
    items.sort(key=lambda item: int(item.get("mtime") or 0), reverse=True)
    return items


async def _task_history_index(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, device_id: str, *, pages: int = 8, limit: int = 500) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    page_token = ""
    for _ in range(max(1, min(int(pages or 8), 20))):
        data = await _tasks(config, client, pan_auth, device_id, phase="all", limit=limit, page_token=page_token)
        tasks.extend([task for task in data.get("tasks", []) if isinstance(task, dict)])
        page_token = str(data.get("next_page_token") or "")
        if not page_token:
            break
    return tasks


def _match_restore_task(item: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    code = str(item.get("code") or "").lower()
    name = str(item.get("name") or "").lower()
    parent = Path(str(item.get("path") or "")).parent.name.lower()
    best: tuple[int, dict[str, Any]] | None = None
    for task in tasks:
        url = str(task.get("url") or "")
        if not url:
            continue
        hay = f"{task.get('name','')} {task.get('savepath','')} {url}".lower()
        score = 0
        if code and code in hay:
            score += 100
        if parent and parent in hay:
            score += 40
        if name.replace(".xltd", "").replace(".xtld", "") and name.replace(".xltd", "").replace(".xtld", "") in hay:
            score += 30
        if not score:
            continue
        if best is None or score > best[0]:
            best = (score, task)
    if not best:
        return None
    task = best[1]
    return {
        "id": task.get("id") or "",
        "name": task.get("name") or "",
        "phase": task.get("phase") or "",
        "url": task.get("url") or "",
        "savepath": task.get("savepath") or "",
        "score": best[0],
    }





def _extract_xunlei_message(data: Any = None, text: str = "", fallback: str = "迅雷请求失败") -> str:
    messages: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("title", "message", "error", "error_description", "detail", "msg"):
                raw = value.get(key)
                if isinstance(raw, str) and raw.strip():
                    messages.append(raw.strip())
            raw_messages = value.get("messages")
            if isinstance(raw_messages, list):
                for item in raw_messages:
                    if isinstance(item, str) and item.strip():
                        messages.append(item.strip())
                    else:
                        walk(item)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    if not messages and text:
        stripped = re.sub(r"<[^>]+>", "", text).strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
                walk(parsed)
            except Exception:
                messages.append(stripped[:240])
    for message in messages:
        normalized = re.sub(r"<[^>]+>", "", message).strip()
        if normalized:
            return normalized
    return fallback


def _extract_task_daily_limit(info: Any) -> dict[str, Any] | None:
    if not isinstance(info, dict):
        return None
    stack = [info]
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if not isinstance(item, dict) or id(item) in seen:
            continue
        seen.add(id(item))
        limit = item.get("task_daily_limit")
        if isinstance(limit, dict) and (limit.get("title") or limit.get("messages")):
            title = _extract_xunlei_message(limit, fallback="迅雷 NAS 免费任务额度受限")
            return {
                "limited": True,
                "title": title,
                "messages": limit.get("messages") if isinstance(limit.get("messages"), list) else [],
                "url": limit.get("url") or "",
                "url_title": limit.get("url_title") or "",
                "raw": limit,
            }
        for value in item.values():
            if isinstance(value, dict):
                stack.append(value)
    return None


def _is_task_daily_limit_message(message: str, daily_limit: dict[str, Any] | None = None) -> bool:
    value = str(message or "").lower()
    if "task_create_count_limit" in value:
        return True
    if "今日3个免费下载任务数已用完" in str(message or ""):
        return True
    if "免费下载任务数已用完" in str(message or ""):
        return True
    return bool(daily_limit and daily_limit.get("title"))


def _task_daily_limit_message(daily_limit: dict[str, Any] | None, fallback: str = "迅雷 NAS 今日免费下载任务额度已用完") -> str:
    if daily_limit and daily_limit.get("title"):
        return str(daily_limit.get("title") or "").strip() or fallback
    return fallback


def _is_task_daily_limit_active(daily_limit: dict[str, Any] | None) -> bool:
    if not isinstance(daily_limit, dict):
        return False
    if daily_limit.get("limited") is True:
        return True
    return _is_task_daily_limit_message(str(daily_limit.get("title") or ""), daily_limit)


def _is_subtitle_name(name: str) -> bool:
    return name.lower().rsplit(".", 1)[-1] in {"srt", "ass", "ssa", "vtt", "sub", "idx", "sup", "smi", "lrc"} if "." in name else False


def _min_file_size_mb_value(config: dict[str, Any], payload: dict[str, Any] | None = None) -> float:
    raw = (payload or {}).get("min_file_size_mb")
    if raw in (None, ""):
        raw = config.get("min_file_size_mb") or 10
    try:
        mb = float(raw or 0)
    except Exception:
        mb = 10
    return max(0.0, mb)


def _is_obvious_ad_file(file: dict[str, Any], min_keep_bytes: int = 10 * 1024 * 1024) -> bool:
    name = str(file.get("name") or "")
    size = int(file.get("size_bytes") or 0)
    if _is_subtitle_name(name):
        return False
    if size and size < min_keep_bytes:
        return True
    lowered = name.lower()
    return lowered.endswith(".url") or "最新地址" in name or "最新アダルト" in name or "ai 助手" in name.lower()


def _auto_file_indices(files: list[dict[str, Any]], min_keep_bytes: int = 10 * 1024 * 1024) -> str:
    kept = [int(f.get("file_index") or 0) for f in files if not _is_obvious_ad_file(f, min_keep_bytes=min_keep_bytes)]
    if not kept or len(kept) == len(files):
        return "--1"
    return ",".join(str(x) for x in sorted(kept))

def _extract_files(resources: list[dict[str, Any]], parent: str = "", counter: list[int] | None = None) -> list[dict[str, Any]]:
    if counter is None:
        counter = [0]
    files: list[dict[str, Any]] = []
    for resource in resources:
        name = str(resource.get("name") or "")
        full_path = f"{parent}/{name}" if parent else name
        is_dir = bool(resource.get("is_dir"))
        children = (((resource.get("dir") or {}) if isinstance(resource.get("dir"), dict) else {}).get("resources") or [])
        if is_dir and isinstance(children, list) and children:
            files.extend(_extract_files(children, full_path, counter))
            continue
        counter[0] += 1
        resource_id = str(resource.get("id") or "")
        file_index = counter[0]
        if resource_id:
            try:
                file_index = int(resource_id.split(".")[-1])
            except Exception:
                pass
        size = int(float(resource.get("file_size") or resource.get("size") or 0))
        meta = resource.get("meta") if isinstance(resource.get("meta"), dict) else {}
        files.append({
            "id": resource_id,
            "name": name,
            "full_path": full_path,
            "size_bytes": size,
            "size_formatted": _format_size(size),
            "file_index": file_index,
            "mime_type": str(meta.get("mime_type") or ""),
            "hash": str(meta.get("hash") or ""),
        })
    return files


def _pick_task_name(payload: dict[str, Any], files: list[dict[str, Any]], url: str) -> str:
    for key in ("rename", "name", "title"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    if files:
        return str(files[0].get("name") or "迅雷下载任务")
    dn = re.search(r"[?&]dn=([^&]+)", url)
    return dn.group(1) if dn else "迅雷下载任务"


def _normalize_download_path_item(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        path = item.strip()
        return {"name": path, "path": path, "source": "download_paths"} if path else None
    if not isinstance(item, dict):
        return None
    path = str(
        item.get("path")
        or item.get("real_path")
        or item.get("RealPath")
        or item.get("download_path")
        or item.get("parent_folder_path")
        or ""
    ).strip()
    if not path:
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        path = str(params.get("RealPath") or params.get("AliasPath") or "").strip()
    if not path:
        return None
    name = str(item.get("name") or item.get("title") or item.get("alias") or item.get("FileName") or path).strip() or path
    return {
        "name": name,
        "path": path,
        "usage": item.get("usage"),
        "limit": item.get("limit"),
        "is_root_path": bool(item.get("is_root_path", False)),
        "file_id": str(item.get("file_id") or item.get("id") or item.get("Id") or ""),
        "source": "download_paths",
    }


def _merge_paths(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            path = str(item.get("path") or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            out.append(item)
    return out


async def _client(config: dict[str, Any], timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=_verify(config), trust_env=False)


async def _pan_auth(config: dict[str, Any], client: httpx.AsyncClient) -> str:
    configured = str(config.get("pan_auth") or "").strip()
    if configured:
        return configured
    # 群晖套件入口页通常不需要 DSM 登录即可返回 HTML 和 UIAuth，
    # 但后续 API 会校验 DSM/Basic 权限；这里仍可先提取 pan_auth。
    resp = await client.get(_entry_url(config), headers=_headers(config))
    resp.raise_for_status()
    return _extract_pan_auth(resp.text)


def _is_synology_auth_error(exc: Exception) -> bool:
    text = str(exc)
    return "syno_unauthorised" in text or "群晖登录态校验失败" in text


async def _device_id(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str) -> tuple[str, dict[str, Any]]:
    configured = str(config.get("device_id") or "").strip()
    try:
        resp = await client.post(
            _api_url(config, "device/info/watch"),
            params={"pan_auth": pan_auth, "device_space": ""},
            headers=_headers(config, json_api=True, pan_auth=pan_auth),
            content="{}",
        )
        resp.raise_for_status()
        data = resp.json()
    except RemoteProtocolError as exc:
        # 群晖反代在 401 时可能提前结束 chunked body，httpx 会先抛协议错误；
        # 实际根因通常仍是缺少 DSM/Basic 登录态。
        raise ValueError(_synology_auth_error_message(config)) from exc
    except Exception as exc:
        body = getattr(resp, "text", "") if "resp" in locals() else ""
        if "syno_unauthorised" in body or "群晖登录态校验失败" in body:
            raise ValueError(_synology_auth_error_message(config)) from exc
        raise
    return configured or _parse_device_id(data), data


async def _context(config: dict[str, Any], client: httpx.AsyncClient) -> tuple[str, str, dict[str, Any]]:
    pan_auth = await _pan_auth(config, client)
    device_id, info = await _device_id(config, client, pan_auth)
    return pan_auth, device_id, info


def _space(device_id: str) -> str:
    return f"device_id#{device_id}"


def _normalize_task(task: dict[str, Any]) -> dict[str, Any]:
    params = task.get("params") if isinstance(task.get("params"), dict) else {}
    size = int(float(task.get("file_size") or 0))
    progress = float(task.get("progress") or params.get("progress") or 0)
    if progress > 1:
        progress = progress / 100
    requested_savepath = str(params.get("parent_folder_path") or "")
    real_path = str(
        params.get("real_path")
        or params.get("RealPath")
        or params.get("download_path")
        or params.get("file_path")
        or ""
    )
    display_savepath = real_path or requested_savepath
    return {
        "id": str(task.get("id") or ""),
        "name": str(task.get("name") or task.get("file_name") or ""),
        "type": str(task.get("type") or ""),
        "phase": str(task.get("phase") or ""),
        "message": str(task.get("message") or ""),
        "size": size,
        "size_formatted": _format_size(size),
        "progress": max(0, min(progress, 1)),
        "created_time": str(task.get("created_time") or ""),
        "updated_time": str(task.get("updated_time") or ""),
        "savepath": display_savepath,
        "requested_savepath": requested_savepath,
        "real_path": real_path,
        "url": str(params.get("url") or ""),
        "speed": int(float(params.get("speed") or 0)) if str(params.get("speed") or "").replace(".", "", 1).isdigit() else 0,
        "raw": task,
    }


async def _tasks(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, device_id: str, *, phase: str = "all", limit: int = 100, page_token: str = "") -> dict[str, Any]:
    phase_map = {
        "active": "PHASE_TYPE_PENDING,PHASE_TYPE_RUNNING,PHASE_TYPE_PAUSED,PHASE_TYPE_ERROR",
        "done": "PHASE_TYPE_COMPLETE",
        "error": "PHASE_TYPE_ERROR",
        "paused": "PHASE_TYPE_PAUSED",
        "running": "PHASE_TYPE_RUNNING,PHASE_TYPE_PENDING",
    }
    filters: dict[str, Any] = {"type": {"in": "user#download-url,user#download"}}
    if phase != "all":
        filters["phase"] = {"in": phase_map.get(phase, phase)}
    resp = await client.get(
        _api_url(config, "drive/v1/tasks"),
        params={
            "space": _space(device_id),
            "page_token": page_token,
            "filters": json.dumps(filters, separators=(",", ":")),
            "limit": str(max(1, min(int(limit or 100), 500))),
            "pan_auth": pan_auth,
            "device_space": "",
        },
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "ok": True,
        "tasks": [_normalize_task(t) for t in data.get("tasks", []) if isinstance(t, dict)],
        "next_page_token": data.get("next_page_token") or "",
        "expires_in": data.get("expires_in"),
        "raw_total": len(data.get("tasks", []) if isinstance(data.get("tasks"), list) else []),
    }


async def _operate_task(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, device_id: str, task_id: str, phase: str) -> dict[str, Any]:
    body = {
        "space": _space(device_id),
        "type": "user#download-url",
        "id": task_id,
        "set_params": {"spec": json.dumps({"phase": phase}, separators=(",", ":"))},
    }
    resp = await client.patch(
        _api_url(config, "drive/v1/task"),
        params={"pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
        content=json.dumps(body, separators=(",", ":")),
    )
    resp.raise_for_status()
    return {"ok": True, "result": resp.json()}


async def _delete_tasks(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, device_id: str, ids: list[str], *, delete_files: bool = False) -> dict[str, Any]:
    if delete_files:
        results: list[dict[str, Any]] = []
        for task_id in ids:
            results.append(await _operate_task(config, client, pan_auth, device_id, task_id, "delete"))
        return {"ok": True, "delete_files": True, "results": results}
    query = "&".join(f"task_ids={task_id}" for task_id in ids)
    resp = await client.delete(
        f"{_api_url(config, 'drive/v1/tasks')}?space={_space(device_id)}&{query}",
        params={"pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    resp.raise_for_status()
    return {"ok": True, "result": resp.json() if resp.text else {}}


async def _files(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, device_id: str, *, parent_id: str = "", limit: int = 100, page_token: str = "") -> dict[str, Any]:
    resp = await client.get(
        _api_url(config, "drive/v1/files"),
        params={
            "space": _space(device_id),
            "parent_id": parent_id,
            "limit": str(max(1, min(int(limit or 100), 500))),
            "page_token": page_token,
            "with": ["withCategoryDiskMountPath", "withCategoryDownloadPath"],
            "pan_auth": pan_auth,
            "device_space": "",
        },
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    resp.raise_for_status()
    data = resp.json()
    return {"ok": True, "files": data.get("files") or [], "next_page_token": data.get("next_page_token") or "", "raw": data}


async def _download_paths(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str) -> dict[str, Any]:
    resp = await client.get(
        _api_url(config, "device/download_paths"),
        params={"pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    resp.raise_for_status()
    data = resp.json()
    raw_items = data if isinstance(data, list) else (data.get("paths") or data.get("download_paths") or data.get("list") or [])
    paths = []
    for item in raw_items if isinstance(raw_items, list) else []:
        normalized = _normalize_download_path_item(item)
        if normalized:
            paths.append(normalized)
    return {"ok": True, "paths": paths, "raw": data}


async def _resolve_parent_folder_id(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, savepath: str) -> str:
    explicit = str(config.get("parent_folder_id") or "").strip()
    if explicit:
        return explicit
    target = str(savepath or "").strip().rstrip("/")
    if not target:
        return ""
    try:
        data = await _download_paths(config, client, pan_auth)
    except Exception:
        return ""
    for item in data.get("paths") or []:
        path = str(item.get("path") or "").strip().rstrip("/")
        if path and path == target:
            return str(item.get("file_id") or item.get("folder_id") or "").strip()
    return ""


async def _require_parent_folder_id_for_explicit_savepath(
    config: dict[str, Any],
    client: httpx.AsyncClient,
    pan_auth: str,
    savepath: str,
) -> str:
    """Resolve a user-specified save path and fail closed when it is unknown.

    Xunlei creates tasks by folder id, not by path. If NOOR accepts an explicit
    savepath but cannot map it to a folder id, it must fail instead of silently
    downloading a large file into the wrong directory.
    """
    folder_id = await _resolve_parent_folder_id(config, client, pan_auth, savepath)
    if folder_id:
        return folder_id
    available: list[str] = []
    try:
        data = await _download_paths(config, client, pan_auth)
        available = [str(item.get("path") or "").strip() for item in (data.get("paths") or []) if str(item.get("path") or "").strip()]
    except Exception:
        available = []
    hint = f"；可用路径：{', '.join(available[:6])}" if available else ""
    raise ValueError(f"迅雷保存路径无法解析：{savepath}{hint}")


async def _create_download_path(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, path: str) -> dict[str, Any]:
    path = str(path or "").strip()
    if not path:
        raise ValueError("missing path")
    body_variants = [
        {"path": path},
        {"real_path": path},
        {"RealPath": path},
        {"file_path": path},
    ]
    last_error: Exception | None = None
    for body in body_variants:
        try:
            resp = await client.post(
                _api_url(config, "device/download_path"),
                params={"pan_auth": pan_auth, "device_space": ""},
                headers=_headers(config, json_api=True, pan_auth=pan_auth),
                content=json.dumps(body, separators=(",", ":")),
            )
            resp.raise_for_status()
            return {"ok": True, "path": path, "result": resp.json() if resp.text else {}}
        except Exception as exc:
            last_error = exc
    raise ValueError(f"创建下载路径失败: {last_error}")


async def _browse_folders(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, device_id: str, *, parent_id: str = "", limit: int = 200) -> dict[str, Any]:
    resp = await client.get(
        _api_url(config, "drive/v1/files"),
        params={
            "space": _space(device_id),
            "parent_id": parent_id,
            "limit": str(max(1, min(int(limit or 200), 500))),
            "page_token": "",
            "filters": json.dumps({"kind": {"eq": "drive#folder"}}, separators=(",", ":")),
            "with": ["withCategoryDiskMountPath", "withCategoryDownloadPath"],
            "pan_auth": pan_auth,
            "device_space": "",
        },
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    resp.raise_for_status()
    data = resp.json()
    folders = []
    for item in data.get("files") or []:
        if not isinstance(item, dict):
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        real_path = str(params.get("RealPath") or item.get("RealPath") or "")
        folders.append({
            "id": str(item.get("id") or item.get("Id") or ""),
            "name": str(item.get("name") or item.get("FileName") or real_path or ""),
            "path": real_path,
            "parent_id": str(item.get("parent_id") or item.get("ParentID") or ""),
            "raw": item,
        })
    return {"ok": True, "folders": folders, "next_page_token": data.get("next_page_token") or ""}


async def _about(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str) -> dict[str, Any]:
    resp = await client.get(
        _api_url(config, "drive/v1/about"),
        params={"pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    resp.raise_for_status()
    return {"ok": True, "about": resp.json()}


async def _device_config(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str) -> dict[str, Any]:
    resp = await client.get(
        _api_url(config, "device/config"),
        params={"pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    resp.raise_for_status()
    return {"ok": True, "config": resp.json()}


async def _resource_info(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, url: str) -> dict[str, Any]:
    resp = await client.post(
        _api_url(config, "drive/v1/resource/list"),
        params={"pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
        content=json.dumps({"page_size": 2000, "urls": url}, separators=(",", ":")),
    )
    resp.raise_for_status()
    data = resp.json()
    resources = (((data.get("list") or {}) if isinstance(data.get("list"), dict) else {}).get("resources") or [])
    files = _extract_files(resources if isinstance(resources, list) else [])
    total_size = sum(int(f.get("size_bytes") or 0) for f in files)
    return {
        "raw": data,
        "files": sorted(files, key=lambda x: int(x.get("file_index") or 0)),
        "total_files": len(files),
        "total_size_bytes": total_size,
        "total_size_formatted": _format_size(total_size),
    }




def _try_speed_preview(info: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    data = info if isinstance(info, dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    countdown = data.get("count_down") if isinstance(data.get("count_down"), dict) else {}
    statistic = data.get("statistic") if isinstance(data.get("statistic"), dict) else {}
    status = data.get("status", "UNKNOWN")
    try:
        total = int(float(usage.get("total") or 0))
    except Exception:
        total = 0
    try:
        used = int(float(usage.get("used") or 0))
    except Exception:
        used = 0
    remaining = max(total - used, 0)
    try:
        count_total = int(float(countdown.get("total") or 0))
    except Exception:
        count_total = 0
    try:
        count_used = int(float(countdown.get("used") or 0))
    except Exception:
        count_used = 0
    can_prompt = status not in {-1, "-1", "DISABLED"} and remaining > 0
    return {
        "status": status,
        "status_text": {
            -1: "已禁用", "-1": "已禁用",
            0: "可试用", "0": "可试用",
            1: "试用中", "1": "试用中",
            2: "已暂停", "2": "已暂停",
        }.get(status, "未知"),
        "usage_total": total,
        "usage_used": used,
        "usage_remaining": remaining,
        "countdown_total": count_total,
        "countdown_used": count_used,
        "countdown_remaining": max(count_total - count_used, 0) if count_total else 0,
        "average_speed": statistic.get("average_speed"),
        "saved_sec": statistic.get("saved_sec"),
        "start_time": statistic.get("start_time"),
        "can_prompt": can_prompt,
        "note": "启用自动试用加速后，NOOR 会在存在等待中或下载中的任务且仍有可用次数时自动领取。",
        "raw": data if bool(config.get("debug_raw_try_speed")) else {},
    }


async def _try_speed_info(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str) -> dict[str, Any]:
    resp = await client.get(
        _api_url(config, "device/v1/try_speed/get_info"),
        params={"pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    if resp.status_code >= 400:
        raise ValueError(_extract_xunlei_message(resp.json() if resp.text else {}, resp.text, fallback="迅雷试用加速状态读取失败"))
    data = resp.json() if resp.text else {}
    return {"ok": True, "try_speed": _try_speed_preview(data, config)}


async def _try_speed_apply(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str) -> dict[str, Any]:
    before = await _try_speed_info(config, client, pan_auth)
    preview = before.get("try_speed") or {}
    if not preview.get("can_prompt"):
        return {"ok": True, "applied": False, "reason": "当前没有可领取的试用加速", "try_speed": preview}
    resp = await client.post(
        _api_url(config, "device/v1/try_speed/apply"),
        params={"pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
        content="{}",
    )
    data = resp.json() if resp.text else {}
    if resp.status_code >= 400:
        raise ValueError(_extract_xunlei_message(data, resp.text, fallback="迅雷试用加速领取失败"))
    message = str(data.get("message") or "")
    if message and message != "ok":
        return {"ok": True, "applied": False, "reason": message, "result": data, "try_speed": preview}
    after = await _try_speed_info(config, client, pan_auth)
    return {"ok": True, "applied": True, "result": data, "try_speed": after.get("try_speed") or preview}


async def _auto_try_speed_once(config: dict[str, Any]) -> dict[str, Any]:
    global _speed_scheduler_status
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    async with await _client(config, timeout=float(config.get("timeout") or 30)) as client:
        pan_auth, device_id, _device_info = await _context(config, client)
        tasks = await _tasks(config, client, pan_auth, device_id, phase="running", limit=100)
        active = tasks.get("tasks") if isinstance(tasks.get("tasks"), list) else []
        if not active:
            result = {"ok": True, "applied": False, "reason": "没有等待中或下载中的任务"}
        else:
            result = await _try_speed_apply(config, client, pan_auth)
    applied = bool(result.get("applied"))
    _speed_scheduler_status = {
        "status": "running" if applied else "idle",
        "last_checked_at": checked_at,
        "last_applied_at": checked_at if applied else _speed_scheduler_status.get("last_applied_at"),
        "last_message": "已自动使用试用加速" if applied else str(result.get("reason") or "当前无需领取"),
        "last_error": "",
    }
    return result


async def _speed_scheduler_loop() -> None:
    global _speed_scheduler_stop, _speed_scheduler_status
    _speed_scheduler_stop = asyncio.Event()
    while not _speed_scheduler_stop.is_set():
        interval = 15
        try:
            from app.plugins.runtime import runtime

            config = runtime.get_config(PLUGIN_ID)
            interval = max(10, min(int(config.get("auto_try_speed_interval") or 15), 300))
            if bool(config.get("auto_try_speed", True)):
                await _auto_try_speed_once(config)
            else:
                _speed_scheduler_status = {**_speed_scheduler_status, "status": "disabled", "last_message": "自动试用加速已关闭", "last_error": ""}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _speed_scheduler_status = {
                **_speed_scheduler_status,
                "status": "failed",
                "last_checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "last_message": "自动试用加速检测失败",
                "last_error": str(exc),
            }
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_speed_scheduler_stop.wait(), timeout=interval)


async def start_background(_config: dict[str, Any] | None = None) -> None:
    global _speed_scheduler_task
    if not _speed_scheduler_task or _speed_scheduler_task.done():
        _speed_scheduler_task = asyncio.create_task(_speed_scheduler_loop())


async def stop_background() -> None:
    global _speed_scheduler_task, _speed_scheduler_stop
    if _speed_scheduler_stop:
        _speed_scheduler_stop.set()
    if _speed_scheduler_task:
        _speed_scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _speed_scheduler_task
    _speed_scheduler_task = None
    _speed_scheduler_stop = None


def background_tasks(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or {}
    enabled = bool(config.get("auto_try_speed", True))
    interval = max(10, min(int(config.get("auto_try_speed_interval") or 15), 300))
    status = dict(_speed_scheduler_status)
    return [{
        "id": "xunlei-remote.auto-try-speed",
        "title": "迅雷试用加速",
        "status": status.get("status") if enabled else "disabled",
        "last_run_at": status.get("last_checked_at"),
        "last_finished_at": status.get("last_checked_at"),
        "summary": f"每 {interval} 秒检测 · {'自动领取' if enabled else '已关闭'}",
        "detail": status.get("last_error") or status.get("last_message"),
        "metrics": {"enabled": enabled, "interval_seconds": interval, "last_applied_at": status.get("last_applied_at")},
    }]


async def _try_speed_config(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str) -> dict[str, Any]:
    resp = await client.get(
        _api_url(config, "device/v1/try_speed/get_config"),
        params={"pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    if resp.status_code >= 400:
        raise ValueError(_extract_xunlei_message(resp.json() if resp.text else {}, resp.text, fallback="迅雷试用加速配置读取失败"))
    data = resp.json() if resp.text else {}
    return {"ok": True, "config": data}


async def _flow_info(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str) -> dict[str, Any]:
    resp = await client.get(
        _api_url(config, "flow/v1/about"),
        params={"scene": "DownloadUrl", "pan_auth": pan_auth, "device_space": ""},
        headers=_headers(config, json_api=True, pan_auth=pan_auth),
    )
    if resp.status_code >= 400:
        raise ValueError(_extract_xunlei_message(resp.json() if resp.text else {}, resp.text, fallback="迅雷会员流量状态读取失败"))
    data = resp.json() if resp.text else {}
    def as_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except Exception:
            return 0
    limit = as_int(data.get("limit"))
    usage = as_int(data.get("usage"))
    quota = data.get("quota") if isinstance(data.get("quota"), dict) else {}
    package_detail = data.get("package_detail") if isinstance(data.get("package_detail"), list) else []
    return {
        "ok": True,
        "flow": {
            "limit": limit,
            "usage": usage,
            "remain": max(limit - usage, 0),
            "limit_formatted": _format_size(limit),
            "usage_formatted": _format_size(usage),
            "remain_formatted": _format_size(max(limit - usage, 0)),
            "quota": quota,
            "package_detail": package_detail if bool(config.get("debug_raw_try_speed")) else [],
            "raw": data if bool(config.get("debug_raw_try_speed")) else {},
        },
    }


async def _restore_candidates(config: dict[str, Any], client: httpx.AsyncClient, pan_auth: str, device_id: str, *, limit: int = 80) -> dict[str, Any]:
    files = _scan_restore_files(config, limit=limit)
    tasks = await _task_history_index(config, client, pan_auth, device_id)
    matched = 0
    for item in files:
        match = _match_restore_task(item, tasks)
        item["matched_task"] = match
        item["restorable"] = bool(match and match.get("url") and item.get("remote_dir") and int(item.get("size") or 0) > 0)
        if item["restorable"]:
            matched += 1
    return {
        "ok": True,
        "items": files,
        "total": len(files),
        "matched": matched,
        "scan_roots": [str(root) for root in _restore_scan_roots(config)],
        "path_mappings": [{"local": left, "remote": right} for left, right in _restore_path_mappings(config)],
    }


async def _delete_residual(config: dict[str, Any], raw_path: Any) -> dict[str, Any]:
    return await _delete_restore_file(config, {"path": raw_path})


async def _delete_restore_file(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    target_path = str(payload.get("path") or "").strip()
    if not target_path:
        raise ValueError("missing xltd path")
    path = Path(target_path).expanduser().resolve()
    allowed = [root.resolve() for root in _restore_scan_roots(config)]
    if not any(path == root or root in path.parents for root in allowed):
        raise ValueError("残留文件不在允许扫描目录内")
    if path.suffix.lower() not in {".xltd", ".xtld"}:
        raise ValueError("只允许删除 .xltd / .xtld 残留文件")
    if not path.exists() or not path.is_file():
        raise ValueError("残留文件不存在")
    stat = path.stat()
    path.unlink()
    return {"ok": True, "deleted": True, "path": str(path), "size": stat.st_size, "size_formatted": _format_size(stat.st_size)}

async def test(config: dict[str, Any]) -> PluginTestResult:
    try:
        async with await _client(config, timeout=10.0) as client:
            pan_auth = await _pan_auth(config, client)
            device_id, info = await _device_id(config, client, pan_auth)
        return PluginTestResult(ok=True, message="xunlei remote connected", details={"base_url": _base(config), "device_id": device_id, "target": info.get("target")})
    except Exception as e:
        return PluginTestResult(ok=False, message=f"xunlei remote failed: {e}")


def _extract_download_urls(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("urls", "url", "magnet"):
        raw = payload.get(key)
        if raw is None:
            continue
        if isinstance(raw, (list, tuple, set)):
            values.extend(str(item or "").strip() for item in raw)
        else:
            values.extend(part.strip() for part in str(raw).splitlines())
    out: list[str] = []
    for item in values:
        if item and item not in out:
            out.append(item)
    return out


async def _submit_single_download(config: dict[str, Any], payload: dict[str, Any], url: str) -> dict[str, Any]:
    if not url:
        raise ValueError("missing url/magnet")
    explicit_savepath = "savepath" in payload and str(payload.get("savepath") or "").strip()
    savepath = str(payload.get("savepath") or config.get("savepath") or DEFAULT_SAVE_PATH).strip() or DEFAULT_SAVE_PATH
    raw_file_indices = str(payload.get("file_indices") or payload.get("file_range") or config.get("file_indices") or "auto").strip() or "auto"
    file_indices = raw_file_indices
    min_file_size_mb = _min_file_size_mb_value(config, payload)
    min_keep_bytes = int(min_file_size_mb * 1024 * 1024)

    async with await _client(config, timeout=float(config.get("timeout") or 30)) as client:
        pan_auth = await _pan_auth(config, client)
        device_id, device_info = await _device_id(config, client, pan_auth)
        daily_limit = _extract_task_daily_limit(device_info)
        resource = await _resource_info(config, client, pan_auth, url)
        files = resource["files"]
        if not files:
            raise ValueError("无法获取磁力资源文件信息，迅雷未返回可下载文件")
        if file_indices.lower() == "auto":
            file_indices = _auto_file_indices(files, min_keep_bytes=min_keep_bytes)
        parent_folder_id = str(payload.get("parent_folder_id") or payload.get("folder_id") or "").strip()
        if not parent_folder_id:
            if explicit_savepath:
                parent_folder_id = await _require_parent_folder_id_for_explicit_savepath(config, client, pan_auth, savepath)
            else:
                parent_folder_id = await _resolve_parent_folder_id(config, client, pan_auth, savepath)
        total_files = int(resource["total_files"] or len(files))
        total_size = int(resource["total_size_bytes"] or 0)
        task_name = _pick_task_name(payload, files, url)
        first_file = max(files, key=lambda f: int(f.get("size_bytes") or 0))
        if file_indices == "--1":
            download_count = total_files
            download_size = total_size
        else:
            wanted = set[int]()
            for part in file_indices.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    start, end = part.split("-", 1)
                    wanted.update(range(int(start), int(end) + 1))
                else:
                    wanted.add(int(part))
            selected = [f for f in files if int(f.get("file_index") or 0) in wanted]
            if selected:
                first_file = max(selected, key=lambda f: int(f.get("size_bytes") or 0))
            download_count = len(selected) or total_files
            download_size = sum(int(f.get("size_bytes") or 0) for f in selected) or total_size
        body = {
            "type": "user#download-url",
            "name": task_name,
            "file_name": task_name,
            "file_size": str(download_size),
            "space": f"device_id#{device_id}",
            "params": {
                "target": f"device_id#{device_id}",
                "url": url,
                "total_file_count": str(download_count),
                "parent_folder_path": savepath,
                "sub_file_index": file_indices,
                "mime_type": str(first_file.get("mime_type") or ""),
                "file_id": str(first_file.get("id") or ""),
            },
        }
        if parent_folder_id:
            body["params"]["parent_folder_id"] = parent_folder_id
        resp = await client.post(
            _api_url(config, "drive/v1/task"),
            headers=_headers(config, json_api=True, pan_auth=pan_auth),
            content=json.dumps(body, separators=(",", ":")),
        )
        try:
            result = resp.json() if resp.text else {}
        except Exception:
            result = {}
        try:
            resp.raise_for_status()
        except Exception as exc:
            message = _extract_xunlei_message(result, getattr(resp, "text", ""), fallback=str(exc) or "迅雷任务创建失败")
            if _is_task_daily_limit_message(message, daily_limit):
                raise ValueError(_task_daily_limit_message(daily_limit)) from exc
            if daily_limit and daily_limit.get("title") and daily_limit["title"] not in message:
                message = f"{daily_limit['title']}；{message}"
            raise ValueError(message) from exc
    return {
        "ok": True,
        "message": "submitted to xunlei remote",
        "mode": "nas",
        "task": result.get("task") if isinstance(result, dict) else result,
        "result": result,
        "savepath": savepath,
        "file_indices": file_indices,
        "min_file_size_mb": min_file_size_mb,
        "resource": {k: resource[k] for k in ("total_files", "total_size_bytes", "total_size_formatted")},
    }


async def submit_download(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    urls = _extract_download_urls(payload)
    if not urls:
        raise ValueError("missing url/magnet")
    if len(urls) == 1:
        return await _submit_single_download(config, payload, urls[0])
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for url in urls:
        try:
            result = await _submit_single_download(config, payload, url)
            results.append(result)
        except Exception as exc:
            failures.append({"url": url, "message": str(exc) or "推送失败"})
    success_count = len(results)
    failure_count = len(failures)
    if success_count == 0 and failures:
        message = failures[0].get("message") or "迅雷推送失败"
    elif failure_count:
        message = f"部分推送失败（{success_count} 成功 / {failure_count} 失败）：{failures[0].get('message') or '未知错误'}"
    else:
        message = "submitted to xunlei remote"
    return {
        "ok": success_count > 0,
        "message": message,
        "success_count": success_count,
        "failure_count": failure_count,
        "total_count": len(urls),
        "results": results,
        "failures": failures,
    }


async def handle_action(action: str, config: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    if action == "download_options":
        out = {
            "ok": True,
            "downloader": PLUGIN_ID,
            "default_savepath": str(config.get("savepath") or DEFAULT_SAVE_PATH),
            "default_category": "",
            "categories": [],
            "paths": [],
            "supports_categories": False,
            "supports_savepath": True,
            "supports_rename": True,
            "supports_resource_preview": True,
            "supports_file_indices": True,
            "supports_small_file_filter": True,
            "file_indices": str(config.get("file_indices") or "auto"),
            "file_indices_options": [
                {
                    "value": "auto",
                    "label": "智能选择：跳过广告小文件，保留字幕",
                    "hint": "结合小文件阈值自动跳过样片、广告等小文件，字幕始终保留。",
                },
                {
                    "value": "--1",
                    "label": "下载全部文件",
                    "hint": "不做文件过滤，整包下载。",
                },
            ],
            "small_file_filter": {
                "mode": "size_threshold_mb",
                "default_mb": _min_file_size_mb_value(config),
                "min_mb": 0,
                "max_mb": 4096,
                "keep_subtitles": True,
            },
        }
        try:
            async with await _client(config, timeout=float(config.get("timeout") or 30)) as client:
                pan_auth, device_id, device_info = await _context(config, client)
                downloads = device_info.get("downloads") if isinstance(device_info, dict) else []
                root_paths = []
                for item in downloads if isinstance(downloads, list) else []:
                    normalized = _normalize_download_path_item(item)
                    if normalized:
                        normalized["source"] = "device_info"
                        root_paths.append(normalized)
                history_paths = []
                try:
                    history = await _download_paths(config, client, pan_auth)
                    history_paths = history.get("paths") or []
                    out["download_paths_raw_count"] = len(history_paths)
                except Exception as path_exc:
                    out["download_paths_warning"] = str(path_exc)
                fallback_paths = root_paths if bool(payload.get("include_root_paths")) else []
                paths = _merge_paths(history_paths, fallback_paths)
                out["paths"] = paths
                out["root_paths"] = root_paths
                out["categories"] = [{"name": p["name"] or p["path"], "save_path": p["path"]} for p in paths]
                if paths and (not out["default_savepath"] or out["default_savepath"] == DEFAULT_SAVE_PATH):
                    preferred = next((p for p in paths if not p.get("is_root_path")), paths[0])
                    out["default_savepath"] = preferred["path"]
                out["device_id"] = device_id
                daily_limit = _extract_task_daily_limit(device_info)
                if daily_limit:
                    out["task_daily_limit"] = daily_limit
        except Exception as exc:
            out["warning"] = str(exc)
        return out
    if action in {"delete_residual", "delete_restore_file"}:
        return await _delete_residual(config, payload.get("path")) if action == "delete_residual" else await _delete_restore_file(config, payload)
    async with await _client(config, timeout=float(config.get("timeout") or 30)) as client:
        pan_auth, device_id, device_info = await _context(config, client)
        if action == "restore_candidates":
            return await _restore_candidates(config, client, pan_auth, device_id, limit=int(payload.get("limit") or 80))
        if action == "try_speed_info":
            return await _try_speed_info(config, client, pan_auth)
        if action == "try_speed_config":
            return await _try_speed_config(config, client, pan_auth)
        if action == "try_speed_apply":
            return await _try_speed_apply(config, client, pan_auth)
        if action == "flow_info":
            return await _flow_info(config, client, pan_auth)
        if action == "device_info":
            out = {"ok": True, "device_id": device_id, "info": device_info}
            daily_limit = _extract_task_daily_limit(device_info)
            if daily_limit:
                out["task_daily_limit"] = daily_limit
            try:
                out.update(await _try_speed_info(config, client, pan_auth))
            except Exception as try_exc:
                out["try_speed_warning"] = str(try_exc)
            return out
        if action == "tasks":
            return await _tasks(
                config,
                client,
                pan_auth,
                device_id,
                phase=str(payload.get("phase") or "all"),
                limit=int(payload.get("limit") or 100),
                page_token=str(payload.get("page_token") or ""),
            )
        if action in {"pause_task", "resume_task", "retry_task", "delete_task_files"}:
            task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
            if not task_id:
                raise ValueError("missing task id")
            daily_limit = _extract_task_daily_limit(device_info)
            if action == "retry_task" and _is_task_daily_limit_active(daily_limit):
                raise ValueError(f"{_task_daily_limit_message(daily_limit)}；已阻止重试以避免迅雷 NAS 将失败任务从列表中移除")
            phase = {"pause_task": "pause", "resume_task": "running", "retry_task": "running", "delete_task_files": "delete"}[action]
            return await _operate_task(config, client, pan_auth, device_id, task_id, phase)
        if action == "delete_tasks":
            ids_raw = payload.get("ids") or payload.get("task_ids") or payload.get("id") or payload.get("task_id")
            ids = [str(x).strip() for x in (ids_raw if isinstance(ids_raw, list) else [ids_raw]) if str(x or "").strip()]
            if not ids:
                raise ValueError("missing task id")
            return await _delete_tasks(config, client, pan_auth, device_id, ids, delete_files=bool(payload.get("delete_files")))
        if action == "files":
            return await _files(
                config,
                client,
                pan_auth,
                device_id,
                parent_id=str(payload.get("parent_id") or ""),
                limit=int(payload.get("limit") or 100),
                page_token=str(payload.get("page_token") or ""),
            )
        if action == "about":
            return await _about(config, client, pan_auth)
        if action == "device_config":
            return await _device_config(config, client, pan_auth)
        if action == "download_paths":
            return await _download_paths(config, client, pan_auth)
        if action == "create_download_path":
            return await _create_download_path(config, client, pan_auth, str(payload.get("path") or payload.get("real_path") or ""))
        if action == "browse_folders":
            return await _browse_folders(config, client, pan_auth, device_id, parent_id=str(payload.get("parent_id") or ""), limit=int(payload.get("limit") or 200))
        if action == "resource_info":
            url = str(payload.get("url") or payload.get("magnet") or "").strip()
            if not url:
                raise ValueError("missing url/magnet")
            info = await _resource_info(config, client, pan_auth, url)
            return {"ok": True, **{k: info[k] for k in ("files", "total_files", "total_size_bytes", "total_size_formatted")}}
    raise ValueError(f"unsupported action: {action}")
