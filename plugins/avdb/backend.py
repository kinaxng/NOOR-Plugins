from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.runtime_paths import plugin_cache_path
from app.plugins.contracts import PluginTestResult


PLUGIN_ID = "avdb"
PLUGIN_CACHE_DIR = plugin_cache_path("avdb")


def _base(config: dict[str, Any]) -> str:
    return str(config.get("base_url") or "").strip().rstrip("/")


def _timeout(config: dict[str, Any]) -> float:
    try:
        return max(3.0, min(float(config.get("timeout") or 15), 60.0))
    except (TypeError, ValueError):
        return 15.0


def _headers(config: dict[str, Any]) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "NOOR-AVDB-Plugin/0.1"}
    access_token = str(config.get("access_token") or "").strip()
    api_key = str(config.get("api_key") or "").strip()
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    elif api_key:
        headers["X-API-Key"] = api_key
    return headers


def _code(value: Any) -> str:
    text = str(value or "").upper().replace("_", "-")
    match = re.search(r"\b([A-Z]{2,10})[- ]?(\d{2,8})\b", text)
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def _number(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "data", "results", "torrents", "articles"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _list(value)
            if nested:
                return nested
    return []


def _url(item: dict[str, Any]) -> str:
    for key in ("magnet", "download_url", "downloadUrl", "torrent_url", "torrentUrl", "url"):
        value = str(item.get(key) or "").strip()
        if value.startswith(("magnet:?", "http://", "https://")):
            return value
    return ""


def _normalize(item: dict[str, Any], index: int) -> dict[str, Any] | None:
    title = str(item.get("title") or item.get("name") or item.get("filename") or "").strip()
    url = _url(item)
    if not title or not url:
        return None
    code = _code(item.get("code") or item.get("number") or title)
    tags_raw = item.get("tags") or item.get("labels") or []
    tags = [str(value).strip() for value in tags_raw] if isinstance(tags_raw, list) else [str(tags_raw).strip()] if tags_raw else []
    feature_text = " ".join([title, *tags])
    is_cracked = bool(re.search(r"破解|uncensored\s*(?:crack|leak)|crack|leak|流出", feature_text, re.I))
    has_subtitle = bool(re.search(r"中字|中文字幕|中文|字幕|\b(?:chs|cht)\b", feature_text, re.I))
    private = bool(item.get("private") or item.get("is_private") or item.get("private_tracker"))
    requirements: dict[str, bool] = {}
    if url.startswith("magnet:?"):
        requirements["accepts_public_magnet"] = not private
    else:
        requirements["accepts_http_torrent"] = True
    if private:
        requirements["accepts_private_tracker"] = True
    compatible = ["qbittorrent", "transmission"] if private else ["xunlei-remote", "qbittorrent", "transmission"]
    return {
        "id": f"avdb:{item.get('id') or item.get('torrent_id') or index}", "kind": "torrent", "query_key": code or title,
        "title": title, "subtitle": " · ".join(part for part in (str(item.get("size") or item.get("size_text") or ""), str(item.get("date") or item.get("created_at") or ""), "AVDB") if part),
        "url": url, "size_bytes": _number(item.get("size_bytes") or item.get("bytes") or item.get("size")), "file_count": _number(item.get("file_count")),
        "tags": tags, "cover_url": str(item.get("cover_url") or item.get("image_url") or item.get("image") or ""), "source_url": str(item.get("source_url") or item.get("link") or ""),
        "features": {"has_subtitle": has_subtitle, "is_cracked": is_cracked, "is_private_tracker": private},
        "requirements": requirements, "compatible_downloaders": compatible, "preferred_downloader": "qbittorrent" if private else "xunlei-remote",
        "metadata": {"source_plugin": PLUGIN_ID, "video_code": code, "site": "AVDB", "raw": item},
    }


async def _search(config: dict[str, Any], keyword: str) -> dict[str, Any]:
    base = _base(config)
    if not base:
        raise ValueError("请先填写实际 AVDB API 地址")
    path = "/" + str(config.get("torrents_path") or "/api/v1/articles/torrents").lstrip("/")
    async with httpx.AsyncClient(timeout=_timeout(config), follow_redirects=True, trust_env=False) as client:
        response = await client.get(f"{base}{path}", headers=_headers(config), params={"keyword": keyword} if keyword else None)
    if response.status_code >= 400:
        raise ValueError(f"AVDB API 请求失败 ({response.status_code})：{response.text[:240]}")
    content_type = response.headers.get("content-type") or ""
    if "json" not in content_type.lower():
        raise ValueError("AVDB API 返回的不是 JSON；请确认没有把 DBOnline 前端地址填入此处")
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("AVDB API 返回了无效 JSON") from exc
    return data if isinstance(data, dict) else {"items": data}


async def search_resources(query: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    keyword = str(query.get("keyword") or query.get("q") or query.get("code") or query.get("number") or "").strip()
    if not keyword:
        return {"items": []}
    payload = await _search(config, keyword)
    limit = max(1, min(_number(query.get("limit")) or 24, 100))
    items = [_normalize(item, index) for index, item in enumerate(_list(payload))]
    return {"items": [item for item in items if item][:limit], "raw": payload}


async def resolve_resource_download(resource: dict[str, Any], _config: dict[str, Any]) -> dict[str, Any]:
    url = str(resource.get("url") or "").strip()
    if not url:
        raise ValueError("AVDB 资源没有下载链接")
    return {"item": resource, "url": url}


async def test(config: dict[str, Any]) -> PluginTestResult:
    try:
        payload = await _search(config, "")
        return PluginTestResult(ok=True, message="avdb connected", details={"items": len(_list(payload))})
    except Exception as exc:
        return PluginTestResult(ok=False, message=f"avdb failed: {exc}")


async def handle_action(action: str, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if action in {"search", "resource_search"}:
        return await search_resources(payload, config)
    if action == "test":
        result = await test(config)
        return {"ok": result.ok, "message": result.message, "details": result.details or {}}
    raise ValueError(f"unsupported action: {action}")
