from __future__ import annotations

from typing import Any
from pathlib import Path
import asyncio
import re
import time

import httpx

from app.plugins.contracts import PluginTestResult

PLUGIN_ID = "qbittorrent"

_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup", ".smi", ".lrc", ".ttml"}


def _noor_tag(config: dict[str, Any]) -> str:
    return str(config.get("noor_tag") or "noor").strip() or "noor"


def _show_noor_only(config: dict[str, Any]) -> bool:
    return bool(config.get("show_noor_only", False))


def _min_file_size_mb_value(config: dict[str, Any], payload: dict[str, Any] | None = None) -> float:
    raw = (payload or {}).get("min_file_size_mb")
    if raw in (None, ""):
        raw = config.get("min_file_size_mb") or 0
    try:
        mb = float(raw or 0)
    except Exception:
        mb = 0
    return max(0.0, mb)


def _min_file_size_bytes(config: dict[str, Any], payload: dict[str, Any] | None = None) -> int:
    return max(0, int(_min_file_size_mb_value(config, payload) * 1024 * 1024))


def _is_subtitle_file(name: str) -> bool:
    return Path(name or "").suffix.lower() in _SUBTITLE_EXTS


def _torrent_params(config: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    params = {k: v for k, v in overrides.items() if v not in (None, "")}
    if _show_noor_only(config):
        params["tag"] = _noor_tag(config)
    return params


def _merge_tags(config: dict[str, Any], *values: Any) -> str:
    tags: list[str] = []
    for raw in (*values, _noor_tag(config)):
        for tag in str(raw or "").replace("|", ",").split(","):
            tag = tag.strip()
            if tag and tag not in tags:
                tags.append(tag)
    return ",".join(tags)


def _base(config: dict[str, Any]) -> str:
    return str(config.get("base_url") or "http://127.0.0.1:8080").rstrip("/")


def _auth(config: dict[str, Any]) -> dict[str, str]:
    return {
        "username": str(config.get("username") or ""),
        "password": str(config.get("password") or ""),
    }


def _api_key(config: dict[str, Any]) -> str:
    return str(config.get("api_key") or "").strip()


async def _client(config: dict[str, Any], timeout: float = 15.0) -> httpx.AsyncClient:
    api_key = _api_key(config)
    if api_key:
        return httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False, headers={"Authorization": f"Bearer {api_key}"})
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False)
    login = await client.post(f"{_base(config)}/api/v2/auth/login", data=_auth(config))
    login_text = (login.text or "").strip()
    login_ok = login.status_code < 400 and (login_text.lower() in {"ok.", "ok"} or bool(client.cookies))
    if not login_ok:
        detail = login.text.strip() or login.reason_phrase or str(login.status_code)
        await client.aclose()
        raise ValueError(f"qb login failed: {detail}")
    return client


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value or "")
    return tuple(int(part or 0) for part in match.groups()) if match else (0, 0, 0)


def _prefers_start_stop(version: str) -> bool:
    return _version_tuple(version) >= (5, 2, 0)


def _auth_mode(config: dict[str, Any]) -> str:
    return "api_key" if _api_key(config) else "cookie"


def _torrent_control_endpoints(action: str, version: str) -> list[str]:
    modern = {"pause": "/api/v2/torrents/stop", "resume": "/api/v2/torrents/start"}
    legacy = {"pause": "/api/v2/torrents/pause", "resume": "/api/v2/torrents/resume"}
    if action not in modern:
        raise ValueError(f"unsupported torrent control action: {action}")
    preferred, fallback = (modern, legacy) if _prefers_start_stop(version) else (legacy, modern)
    return [preferred[action], fallback[action]]


async def _qb_version(client: httpx.AsyncClient, config: dict[str, Any]) -> str:
    response = await client.get(f"{_base(config)}/api/v2/app/version")
    response.raise_for_status()
    return response.text.strip()


def _num(value: Any, default: float = 0) -> float:
    try:
        return float(value or 0)
    except Exception:
        return default


def _normalize_torrent(t: dict[str, Any]) -> dict[str, Any]:
    amount_left = _num(t.get("amount_left"))
    size = _num(t.get("size"))
    downloaded = _num(t.get("downloaded"))
    uploaded = _num(t.get("uploaded"))
    dlspeed = _num(t.get("dlspeed"))
    upspeed = _num(t.get("upspeed"))
    progress = _num(t.get("progress"))
    ratio = _num(t.get("ratio"))
    eta = int(_num(t.get("eta"), -1))
    state = str(t.get("state") or "")
    return {
        "hash": str(t.get("hash") or ""),
        "name": str(t.get("name") or ""),
        "state": state,
        "category": str(t.get("category") or ""),
        "tags": str(t.get("tags") or ""),
        "save_path": str(t.get("save_path") or ""),
        "content_path": str(t.get("content_path") or ""),
        "progress": max(0, min(progress, 1)),
        "size": int(size),
        "amount_left": int(amount_left),
        "downloaded": int(downloaded),
        "uploaded": int(uploaded),
        "dlspeed": int(dlspeed),
        "upspeed": int(upspeed),
        "ratio": ratio,
        "eta": eta,
        "num_seeds": int(_num(t.get("num_seeds"))),
        "num_leechs": int(_num(t.get("num_leechs"))),
        "added_on": int(_num(t.get("added_on"))),
        "completion_on": int(_num(t.get("completion_on"))),
        "tracker": str(t.get("tracker") or ""),
    }


def _parse_add_response(resp: httpx.Response) -> dict[str, Any]:
    text = (resp.text or "").strip()
    data: Any = {}
    if text:
        try:
            data = resp.json()
        except Exception:
            data = {"raw": text}
    if not isinstance(data, dict):
        data = {"raw": data}
    success = int(_num(data.get("success_count"), 0))
    pending = int(_num(data.get("pending_count"), 0))
    failure = int(_num(data.get("failure_count"), 0))
    raw_text = str(data.get("raw") or text or "")
    if "fail" in raw_text.lower() and success <= 0 and pending <= 0:
        raise ValueError(raw_text or "qb add failed")
    if failure > 0 and success <= 0 and pending <= 0:
        reason = data.get("error") or data.get("message") or data.get("raw") or "qB 添加失败"
        raise ValueError(str(reason))
    return {
        "raw": data,
        "success_count": success,
        "pending_count": pending,
        "failure_count": failure,
        "added_torrent_ids": data.get("added_torrent_ids") if isinstance(data.get("added_torrent_ids"), list) else [],
    }


async def test(config: dict[str, Any]) -> PluginTestResult:
    try:
        client = await _client(config, timeout=10.0)
        try:
            version = await _qb_version(client, config)
        finally:
            await client.aclose()
        return PluginTestResult(ok=True, message="qb connected", details={"version": version, "api_mode": "start_stop" if _prefers_start_stop(version) else "pause_resume", "auth_mode": _auth_mode(config)})
    except Exception as e:
        return PluginTestResult(ok=False, message=f"qb failed: {e}")


async def submit_download(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    urls = payload.get("urls") or payload.get("url")
    category = payload.get("category") or config.get("category") or ""
    savepath = payload.get("savepath") or config.get("savepath") or ""
    rename = payload.get("rename") or ""
    title = str(rename or payload.get("name") or payload.get("title") or "").strip()
    tag = _merge_tags(config, payload.get("tag"), payload.get("tags"))
    if not urls:
        raise ValueError("missing url(s)")
    client = await _client(config, timeout=20.0)
    try:
        before = int(time.time())
        data = {"urls": urls, "category": category, "savepath": savepath, "tags": tag}
        if rename:
            data["rename"] = str(rename)
        add = await client.post(f"{_base(config)}/api/v2/torrents/add", data=data)
        add.raise_for_status()
        add_result = _parse_add_response(add)
        added = await _wait_for_submitted_torrent(client, config, title=title or rename, before=before)
        if not added:
            if int(add_result.get("pending_count") or 0) > 0:
                raise ValueError("qB 已接受请求，但 8 秒内未确认新增任务；可能仍在解析元数据、种子已存在，或 qB 无法访问该下载地址。")
            raise ValueError("qB 已接受请求，但 8 秒内未发现新增任务；通常是种子已存在、下载地址失效，或 qB 无法访问该下载地址。")
        filter_result = await _apply_small_file_filter_to_recent_noor(client, config, title=title or rename, payload=payload)
    finally:
        await client.aclose()
    return {"ok": True, "message": "submitted to qbittorrent", "pending": False, "tags": tag, "savepath": savepath, "category": category, "rename": rename, "torrent": added, "add_result": add_result, "small_file_filter": filter_result}


async def _wait_for_submitted_torrent(client: httpx.AsyncClient, config: dict[str, Any], *, title: str, before: int) -> dict[str, Any] | None:
    title_l = (title or "").strip().lower()
    tag = _noor_tag(config)
    for attempt in range(8):
        resp = await client.get(
            f"{_base(config)}/api/v2/torrents/info",
            params={"sort": "added_on", "reverse": "true", "limit": 12, "tag": tag},
        )
        resp.raise_for_status()
        raw_items = resp.json()
        for raw in raw_items if isinstance(raw_items, list) else []:
            item = _normalize_torrent(raw)
            name_l = item.get("name", "").lower()
            added_on = int(item.get("added_on") or 0)
            if added_on >= before - 2 and (not title_l or title_l in name_l or name_l in title_l):
                return item
        if attempt < 7:
            await asyncio.sleep(1)
    return None


async def _overview(config: dict[str, Any]) -> dict[str, Any]:
    client = await _client(config)
    try:
        base = _base(config)
        version_resp = await client.get(f"{base}/api/v2/app/version")
        transfer = await client.get(f"{base}/api/v2/transfer/info")
        categories = await client.get(f"{base}/api/v2/torrents/categories")
        torrents = await client.get(f"{base}/api/v2/torrents/info", params=_torrent_params(config, limit=500, sort="added_on", reverse="true"))
        version_resp.raise_for_status(); transfer.raise_for_status(); categories.raise_for_status(); torrents.raise_for_status()
    finally:
        await client.aclose()
    version = version_resp.text.strip()
    items = [_normalize_torrent(x) for x in torrents.json() if isinstance(x, dict)]
    counts: dict[str, int] = {}
    for item in items:
        state = item["state"] or "unknown"
        counts[state] = counts.get(state, 0) + 1
    return {
        "ok": True,
        "version": version,
        "api_mode": "start_stop" if _prefers_start_stop(version) else "pause_resume",
        "auth_mode": _auth_mode(config),
        "transfer": transfer.json(),
        "categories": categories.json(),
        "torrents": items,
        "counts": counts,
        "total": len(items),
        "show_noor_only": _show_noor_only(config),
        "noor_tag": _noor_tag(config),
        "min_file_size_mb": config.get("min_file_size_mb") or 0,
    }


async def _download_options(config: dict[str, Any]) -> dict[str, Any]:
    client = await _client(config)
    try:
        base = _base(config)
        categories_resp = await client.get(f"{base}/api/v2/torrents/categories")
        default_path_resp = await client.get(f"{base}/api/v2/app/defaultSavePath")
        categories_resp.raise_for_status()
        categories_raw = categories_resp.json()
        default_savepath = ""
        if default_path_resp.status_code < 400:
            default_savepath = default_path_resp.text.strip()
    finally:
        await client.aclose()
    categories: list[dict[str, str]] = []
    if isinstance(categories_raw, dict):
        for key, value in categories_raw.items():
            if isinstance(value, dict):
                name = str(value.get("name") or key)
                save_path = str(value.get("savePath") or value.get("save_path") or "")
            else:
                name = str(key)
                save_path = ""
            categories.append({"name": name, "save_path": save_path})
    categories.sort(key=lambda x: x["name"].lower())
    return {
        "ok": True,
        "downloader": PLUGIN_ID,
        "default_savepath": str(config.get("savepath") or default_savepath or ""),
        "default_category": str(config.get("category") or ""),
        "categories": categories,
        "supports_categories": True,
        "supports_savepath": True,
        "supports_rename": True,
        "supports_resource_preview": False,
        "supports_file_indices": False,
        "supports_small_file_filter": True,
        "file_indices": "",
        "small_file_filter": {
            "mode": "size_threshold_mb",
            "default_mb": _min_file_size_mb_value(config),
            "min_mb": 0,
            "max_mb": 4096,
            "keep_subtitles": True,
        },
    }


async def _torrents(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    params = _torrent_params(
        config,
        filter=payload.get("filter") or "all",
        category=payload.get("category") or "",
        sort=payload.get("sort") or "added_on",
        reverse="true" if payload.get("reverse", True) else "false",
        limit=int(payload.get("limit") or 200),
        offset=int(payload.get("offset") or 0),
    )
    client = await _client(config)
    try:
        resp = await client.get(f"{_base(config)}/api/v2/torrents/info", params=params)
        resp.raise_for_status()
    finally:
        await client.aclose()
    return {"ok": True, "items": [_normalize_torrent(x) for x in resp.json() if isinstance(x, dict)]}


async def _torrent_properties(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    h = str(payload.get("hash") or "")
    if not h:
        raise ValueError("missing hash")
    client = await _client(config)
    try:
        props = await client.get(f"{_base(config)}/api/v2/torrents/properties", params={"hash": h})
        files = await client.get(f"{_base(config)}/api/v2/torrents/files", params={"hash": h})
        props.raise_for_status(); files.raise_for_status()
    finally:
        await client.aclose()
    return {"ok": True, "properties": props.json(), "files": files.json()}


async def _post_torrent_action(client: httpx.AsyncClient, config: dict[str, Any], endpoints: str | list[str], data: dict[str, Any]) -> str:
    last_response: httpx.Response | None = None
    for endpoint in ([endpoints] if isinstance(endpoints, str) else endpoints):
        response = await client.post(f"{_base(config)}{endpoint}", data=data)
        if response.status_code not in {404, 405}:
            response.raise_for_status()
            return endpoint
        last_response = response
    if last_response is not None:
        last_response.raise_for_status()
    raise ValueError("qb action failed: no endpoint attempted")


async def _simple_action(config: dict[str, Any], endpoint: str | list[str], hashes: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if not hashes:
        raise ValueError("missing hash")
    data = {"hashes": hashes}
    if extra:
        data.update(extra)
    client = await _client(config)
    try:
        used_endpoint = await _post_torrent_action(client, config, endpoint, data)
    finally:
        await client.aclose()
    return {"ok": True, "endpoint": used_endpoint}


async def _control_action(config: dict[str, Any], action: str, hashes: str) -> dict[str, Any]:
    if not hashes:
        raise ValueError("missing hash")
    client = await _client(config)
    try:
        version = await _qb_version(client, config)
        used_endpoint = await _post_torrent_action(client, config, _torrent_control_endpoints(action, version), {"hashes": hashes})
    finally:
        await client.aclose()
    return {"ok": True, "version": version, "api_mode": "start_stop" if _prefers_start_stop(version) else "pause_resume", "endpoint": used_endpoint}


async def _apply_small_file_filter_to_hash(client: httpx.AsyncClient, config: dict[str, Any], torrent_hash: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    min_size = _min_file_size_bytes(config, payload)
    if min_size <= 0 or not torrent_hash:
        return {"ok": True, "changed": 0, "skipped": True}
    files: Any = []
    for attempt in range(6):
        files_resp = await client.get(f"{_base(config)}/api/v2/torrents/files", params={"hash": torrent_hash})
        files_resp.raise_for_status()
        files = files_resp.json()
        if files:
            break
        if attempt < 5:
            await asyncio.sleep(1)
    disabled: list[str] = []
    kept_subtitles = 0
    for idx, f in enumerate(files if isinstance(files, list) else []):
        name = str(f.get("name") or "")
        size = int(_num(f.get("size")))
        if _is_subtitle_file(name):
            kept_subtitles += 1
            continue
        if size and size < min_size:
            disabled.append(str(f.get("index", idx)))
    if disabled:
        resp = await client.post(
            f"{_base(config)}/api/v2/torrents/filePrio",
            data={"hash": torrent_hash, "id": "|".join(disabled), "priority": "0"},
        )
        resp.raise_for_status()
    return {"ok": True, "changed": len(disabled), "kept_subtitles": kept_subtitles, "min_file_size_mb": _min_file_size_mb_value(config, payload)}


async def _apply_small_file_filter_to_recent_noor(client: httpx.AsyncClient, config: dict[str, Any], title: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    min_size = _min_file_size_bytes(config, payload)
    if min_size <= 0:
        return {"ok": True, "changed": 0, "skipped": True}
    params = {"tag": _noor_tag(config), "sort": "added_on", "reverse": "true", "limit": 6}
    raw_items: Any = []
    for attempt in range(6):
        resp = await client.get(f"{_base(config)}/api/v2/torrents/info", params=params)
        resp.raise_for_status()
        raw_items = resp.json()
        if raw_items:
            break
        if attempt < 5:
            await asyncio.sleep(1)
    now = int(time.time())
    total_changed = 0
    processed = 0
    title_l = (title or "").strip().lower()
    candidates: list[dict[str, Any]] = []
    for raw in raw_items if isinstance(raw_items, list) else []:
        item = _normalize_torrent(raw)
        if item.get("added_on") and now - int(item["added_on"]) > 300:
            continue
        candidates.append(item)
    if title_l:
        matched = [
            item
            for item in candidates
            if title_l in item.get("name", "").lower() or item.get("name", "").lower() in title_l
        ]
        if matched:
            candidates = matched
    # Adding by URL does not return a hash. Keep the automatic post-submit
    # mutation intentionally narrow: the newest recent NOOR-tagged torrent only.
    for item in candidates[:1]:
        result = await _apply_small_file_filter_to_hash(client, config, item.get("hash", ""), payload)
        total_changed += int(result.get("changed") or 0)
        processed += 1
    return {"ok": True, "processed": processed, "changed": total_changed, "min_file_size_mb": _min_file_size_mb_value(config, payload)}


async def _apply_noor_filter(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    hashes = str(payload.get("hashes") or payload.get("hash") or "")
    client = await _client(config)
    try:
        if hashes:
            total = 0
            for h in [x for x in hashes.split("|") if x]:
                result = await _apply_small_file_filter_to_hash(client, config, h)
                total += int(result.get("changed") or 0)
            return {"ok": True, "changed": total}
        return await _apply_small_file_filter_to_recent_noor(client, config)
    finally:
        await client.aclose()


async def _create_category(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    save_path = str(payload.get("save_path") or payload.get("savePath") or "").strip()
    if not name:
        raise ValueError("missing category name")
    client = await _client(config)
    try:
        resp = await client.post(f"{_base(config)}/api/v2/torrents/createCategory", data={"category": name, "savePath": save_path})
        resp.raise_for_status()
    finally:
        await client.aclose()
    return {"ok": True}


async def _edit_category(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    save_path = str(payload.get("save_path") or payload.get("savePath") or "").strip()
    if not name:
        raise ValueError("missing category name")
    client = await _client(config)
    try:
        resp = await client.post(f"{_base(config)}/api/v2/torrents/editCategory", data={"category": name, "savePath": save_path})
        resp.raise_for_status()
    finally:
        await client.aclose()
    return {"ok": True}


async def _remove_categories(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    categories = payload.get("categories") or payload.get("category") or payload.get("name") or ""
    if isinstance(categories, list):
        categories = "\n".join(str(x) for x in categories if str(x).strip())
    categories = str(categories or "").strip()
    if not categories:
        raise ValueError("missing categories")
    client = await _client(config)
    try:
        resp = await client.post(f"{_base(config)}/api/v2/torrents/removeCategories", data={"categories": categories})
        resp.raise_for_status()
    finally:
        await client.aclose()
    return {"ok": True}


async def handle_action(action: str, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if action == "test":
        result = await test(config)
        return {"ok": result.ok, "message": result.message, "details": result.details or {}}
    if action == "overview":
        return await _overview(config)
    if action == "torrents":
        return await _torrents(config, payload)
    if action == "properties":
        return await _torrent_properties(config, payload)
    if action == "download_options":
        return await _download_options(config)
    hashes = str(payload.get("hashes") or payload.get("hash") or "")
    if action == "pause":
        return await _control_action(config, action, hashes)
    if action == "resume":
        return await _control_action(config, action, hashes)
    if action == "delete":
        return await _simple_action(config, "/api/v2/torrents/delete", hashes, {"deleteFiles": "true" if payload.get("deleteFiles") else "false"})
    if action == "recheck":
        return await _simple_action(config, "/api/v2/torrents/recheck", hashes)
    if action == "reannounce":
        return await _simple_action(config, "/api/v2/torrents/reannounce", hashes)
    if action == "apply_noor_filter":
        return await _apply_noor_filter(config, payload)
    if action == "create_category":
        return await _create_category(config, payload)
    if action == "edit_category":
        return await _edit_category(config, payload)
    if action == "remove_categories":
        return await _remove_categories(config, payload)
    raise ValueError(f"unsupported action: {action}")
