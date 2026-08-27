from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from app.plugins.contracts import DashboardWidget, PluginManifest, PluginTestResult

PLUGIN_ID = "javdb"
LATEST_CACHE_TTL = 120
LATEST_PAGE_CACHE_TTL = 600
VIDEO_DETAIL_CACHE_TTL = 1800
FILTER_PAGE_CACHE_TTL = 300
ACTOR_OPTIONS_CACHE_TTL = 21600
SERIES_OPTIONS_CACHE_TTL = 900
DETAIL_ENRICH_CONCURRENCY = 8
LATEST_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
LATEST_PAGE_CACHE: dict[tuple[str, str, str, str, int, int], dict[str, Any]] = {}
VIDEO_DETAIL_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
FILTER_PAGE_CACHE: dict[tuple[str, str, str, str, int, int], dict[str, Any]] = {}
ACTOR_OPTIONS_CACHE: dict[tuple[str], dict[str, Any]] = {}
SERIES_OPTIONS_CACHE: dict[tuple[str], dict[str, Any]] = {}


class JavDBUpstreamError(RuntimeError):
    """DBOnline/JavDB returned an application-level failure."""


class JavDBHttpError(RuntimeError):
    """DBOnline/JavDB returned a non-2xx response."""


class JavDBTimeoutError(RuntimeError):
    """DBOnline/JavDB did not respond before the configured timeout."""


def _base(config: dict[str, Any]) -> str:
    return str(config.get("base_url") or "").strip().rstrip("/")


def _api_base(config: dict[str, Any]) -> str:
    return _base(config).rstrip("/") + "/api"


def _cache_get(cache: dict[Any, dict[str, Any]], key: Any, ttl: int) -> Any | None:
    cached = cache.get(key)
    if not cached:
        return None
    if time.time() - float(cached.get("ts") or 0) >= ttl:
        cache.pop(key, None)
        return None
    return cached.get("value")


def _cache_set(cache: dict[Any, dict[str, Any]], key: Any, value: Any) -> Any:
    cache[key] = {"ts": time.time(), "value": value}
    return value


def _headers(config: dict[str, Any]) -> dict[str, str]:
    key = str(config.get("api_key") or "").strip()
    headers = {"Accept": "application/json", "User-Agent": "NOOR-JavDB-Plugin/0.1"}
    if key:
        headers["X-API-Key"] = key
    return headers


def _timeout(config: dict[str, Any]) -> float:
    try:
        return max(3.0, min(float(config.get("timeout") or 15), 60.0))
    except Exception:
        return 15.0


async def _request(config: dict[str, Any], method: str, path: str, *, params: dict[str, Any] | None = None, json: dict[str, Any] | None = None) -> Any:
    base = _api_base(config)
    url = urljoin(base + "/", path.lstrip("/"))
    async with httpx.AsyncClient(timeout=_timeout(config), follow_redirects=True, trust_env=False) as client:
        try:
            resp = await client.request(method, url, params=params, json=json, headers=_headers(config))
        except httpx.TimeoutException as exc:
            raise JavDBTimeoutError(f"JavDB API timeout after {_timeout(config):.0f}s: {url} params={params or {}}") from exc
        except httpx.RequestError as exc:
            raise JavDBHttpError(f"JavDB API request failed: {url} params={params or {}} error={exc}") from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = resp.text[:500].strip()
            raise JavDBHttpError(f"JavDB API HTTP {resp.status_code}: {url} params={params or {}} body={body}") from exc
        return resp.json()


def _data(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("success") is False:
        raise JavDBUpstreamError(str(payload.get("error") or payload.get("message") or "JavDB 接口返回失败"))
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        raise JavDBUpstreamError(str(payload.get("msg") or payload.get("message") or "JavDB 接口返回失败"))
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _abs(config: dict[str, Any], value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("/"):
        return _base(config) + raw
    return raw


def _code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    return raw


def _looks_like_video_code(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    return bool(re.search(r"\b([A-Z]{2,8}[-_ ]?\d{2,7}|FC2[-_ ]?(?:PPV[-_ ]?)?\d{4,9}|\d{6}[-_]\d{2,5})\b", raw, re.I))


def _extract_video_code(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = re.search(r"\b([A-Z]{2,8}[-_ ]?\d{2,7}|FC2[-_ ]?(?:PPV[-_ ]?)?\d{4,9}|\d{6}[-_]\d{2,5})\b", raw, re.I)
    if not match:
        return ""
    return re.sub(r"[_ ]+", "-", match.group(1).upper())


def _movie_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        for key in ("movies", "videos", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    if isinstance(data, list):
        return data
    return []


def _is_cracked_movie(item: dict[str, Any]) -> bool:
    if bool(item.get("is_cracked") or item.get("cracked")):
        return True
    text_parts = []
    tags = item.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict):
                text_parts.append(tag.get("name"))
            else:
                text_parts.append(tag)
    text = " ".join(str(x or "") for x in text_parts).lower()
    return any(keyword in text for keyword in ("破解", "破解版", "无码破解", "uncensored leak"))


def _contains_any_text(value: Any, keywords: tuple[str, ...]) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        text = value.lower()
        return any(keyword in text for keyword in keywords)
    if isinstance(value, dict):
        return any(_contains_any_text(v, keywords) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_any_text(v, keywords) for v in value)
    return _contains_any_text(str(value), keywords)


def _detail_has_cnsub(detail: dict[str, Any]) -> bool:
    if bool(detail.get("has_cnsub") or detail.get("has_subtitle") or detail.get("has_magnet_subtitle") or detail.get("play_subtitle")):
        return True
    subtitle_keywords = ("中字", "字幕", "中文", "中文字幕", "chs", "cht")
    if _contains_any_text(detail.get("tags"), subtitle_keywords):
        return True
    if _contains_any_text(detail.get("categories"), subtitle_keywords):
        return True
    magnets = detail.get("magnets") if isinstance(detail.get("magnets"), list) else []
    return any(_contains_any_text(magnet.get("tags"), subtitle_keywords) or _contains_any_text(magnet.get("name"), subtitle_keywords) for magnet in magnets if isinstance(magnet, dict))


def _detail_is_cracked(detail: dict[str, Any]) -> bool:
    cracked_keywords = ("破解", "破解版", "无码破解", "uncensored leak")
    if bool(detail.get("is_cracked") or detail.get("cracked")):
        return True
    if _contains_any_text(detail.get("tags"), cracked_keywords):
        return True
    if _contains_any_text(detail.get("categories"), cracked_keywords):
        return True
    magnets = detail.get("magnets") if isinstance(detail.get("magnets"), list) else []
    return any(_contains_any_text(magnet.get("tags"), cracked_keywords) or _contains_any_text(magnet.get("name"), cracked_keywords) for magnet in magnets if isinstance(magnet, dict))


def _merge_latest_detail(config: dict[str, Any], item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(item)
    raw = dict(item.get("raw") or {})
    raw["detail_enriched"] = True
    merged["raw"] = raw
    merged["has_cnsub"] = bool(merged.get("has_cnsub") or _detail_has_cnsub(detail))
    merged["is_cracked"] = bool(merged.get("is_cracked") or _detail_is_cracked(detail))
    merged["can_play"] = bool(merged.get("can_play") or detail.get("can_play"))
    play_subtitle = int(detail.get("play_subtitle") or 0)
    if play_subtitle:
        merged["play_subtitle"] = max(int(merged.get("play_subtitle") or 0), play_subtitle)
    if not merged.get("cover_url"):
        merged["cover_url"] = _abs(config, detail.get("cover_url"))
    if not merged.get("thumb_url"):
        merged["thumb_url"] = _abs(config, detail.get("thumb_url"))
    if isinstance(detail.get("library"), dict):
        merged["library"] = detail.get("library") or merged.get("library") or {}
    magnets = detail.get("magnets") if isinstance(detail.get("magnets"), list) else []
    if magnets and not int(merged.get("magnets_count") or 0):
        merged["magnets_count"] = len(magnets)
    return merged


async def _enrich_latest_item(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    if raw.get("detail_enriched"):
        return item
    code = str(item.get("code") or item.get("number") or "").strip()
    if not code:
        raw["detail_enriched"] = True
        enriched = dict(item)
        enriched["raw"] = raw
        return enriched
    try:
        detail = await _video(config, code)
    except Exception:
        raw["detail_enriched"] = True
        raw["detail_enrich_error"] = True
        enriched = dict(item)
        enriched["raw"] = raw
        return enriched
    return _merge_latest_detail(config, item, detail if isinstance(detail, dict) else {})


def _latest_api_filter(filter_name: str) -> str:
    value = str(filter_name or "all").strip() or "all"
    if value == "cnsub":
        return "subtitle"
    return value


def _latest_preferred_api_filter(requested_filters: list[str]) -> str:
    filters = set(requested_filters or [])
    if "cnsub" in filters:
        return "subtitle"
    if "magnets" in filters:
        return "magnets"
    return "all"


async def _latest_page_items(config: dict[str, Any], latest_type: str, sort_by: str, page: int, *, scan_limit: int = 80, filter_by: str = "all") -> list[dict[str, Any]]:
    api_filter = _latest_api_filter(filter_by)
    cache_key = (_base(config), str(latest_type), str(sort_by), api_filter, int(page), int(scan_limit))
    cached = _cache_get(LATEST_PAGE_CACHE, cache_key, LATEST_PAGE_CACHE_TTL)
    if cached is not None:
        return list(cached)
    batch = _data(await _request(config, "GET", "/latest", params={"page": page, "limit": scan_limit, "type": latest_type, "sort_by": sort_by, "filter_by": api_filter}))
    movies = [x for x in _movie_list(batch) if isinstance(x, dict)]
    items = [_normalize_movie(config, movie) for movie in movies]
    _cache_set(LATEST_PAGE_CACHE, cache_key, items)
    return list(items)


async def _enrich_page_items_for_filter(config: dict[str, Any], items: list[dict[str, Any]], requested_filters: list[str]) -> list[dict[str, Any]]:
    wanted = set(requested_filters or [])
    if not ({"cnsub", "cracked"} & wanted):
        return items

    semaphore = asyncio.Semaphore(DETAIL_ENRICH_CONCURRENCY)

    async def enrich_one(item: dict[str, Any]) -> dict[str, Any]:
        if _matches_latest_filters(item, list(wanted)):
            return item
        async with semaphore:
            return await _enrich_latest_item(config, item)

    return list(await asyncio.gather(*(enrich_one(item) for item in items)))


def _normalize_movie(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    number = _code(item.get("number") or item.get("code"))
    title = str(item.get("title") or item.get("origin_title") or number or item.get("id") or "").strip()
    magnets_count = item.get("magnets_count")
    if magnets_count is None:
        magnets_count = item.get("magnet_count")
    return {
        "id": str(item.get("id") or item.get("video_id") or number or title),
        "number": number,
        "code": number,
        "title": title,
        "origin_title": item.get("origin_title") or "",
        "display_title": f"{number} {title}".strip(),
        "cover_url": _abs(config, item.get("cover_url")),
        "thumb_url": _abs(config, item.get("thumb_url")),
        "release_date": item.get("release_date") or item.get("date") or "",
        "duration": item.get("duration") or 0,
        "score": item.get("score") or item.get("ranking") or 0,
        "ranking": item.get("ranking") or 0,
        "magnets_count": magnets_count or 0,
        "has_cnsub": bool(item.get("has_cnsub") or item.get("has_subtitle") or item.get("has_magnet_subtitle") or item.get("play_subtitle")),
        "play_subtitle": int(item.get("play_subtitle") or 0),
        "can_play": bool(item.get("can_play")),
        "is_cracked": _is_cracked_movie(item),
        "library": item.get("library") if isinstance(item.get("library"), dict) else {},
        "raw": item,
    }


def _sort_video_items(items: list[dict[str, Any]], sort_key: str, order: str) -> list[dict[str, Any]]:
    reverse = str(order or "desc").lower() == "desc"

    def release_value(item: dict[str, Any]) -> str:
        return str(item.get("release_date") or "")

    def title_value(item: dict[str, Any]) -> str:
        return str(item.get("display_title") or item.get("title") or "")

    if sort_key == "score":
        return sorted(items, key=lambda item: (float(item.get("score") or 0), release_value(item), title_value(item)), reverse=reverse)
    if sort_key in {"date", "release", "created", "updated"}:
        return sorted(items, key=lambda item: (release_value(item), title_value(item)), reverse=reverse)
    return sorted(items, key=lambda item: title_value(item), reverse=reverse)


def _in_library(item: dict[str, Any]) -> bool:
    library = item.get("library")
    return bool(library.get("in_library")) if isinstance(library, dict) else False


def _matches_latest_filter(item: dict[str, Any], requested_filter: str) -> bool:
    if requested_filter in {"", "all"}:
        return True
    if requested_filter == "magnets":
        return int(item.get("magnets_count") or 0) > 0
    if requested_filter == "cnsub":
        return bool(item.get("has_cnsub") or item.get("play_subtitle"))
    if requested_filter == "cracked":
        return bool(item.get("is_cracked"))
    if requested_filter == "playable":
        return bool(item.get("can_play"))
    if requested_filter == "library":
        return _in_library(item)
    if requested_filter == "not_library":
        return not _in_library(item)
    return True


def _normalize_latest_filters(payload_filters: Any, requested_filter: str) -> list[str]:
    filters: list[str] = []
    if isinstance(payload_filters, (list, tuple, set)):
        for entry in payload_filters:
            value = str(entry or "").strip()
            if value and value not in {"all"} and value not in filters:
                filters.append(value)
    requested = str(requested_filter or "").strip()
    if requested and requested not in {"all"} and requested not in filters:
        filters.append(requested)
    return filters


def _matches_latest_filters(item: dict[str, Any], requested_filters: list[str]) -> bool:
    return all(_matches_latest_filter(item, value) for value in requested_filters)




async def _enrich_items_for_filters(config: dict[str, Any], items: list[dict[str, Any]], requested_filters: list[str]) -> list[dict[str, Any]]:
    wanted = set(requested_filters or [])
    if not ({"cnsub", "cracked"} & wanted):
        return items

    semaphore = asyncio.Semaphore(DETAIL_ENRICH_CONCURRENCY)

    async def enrich_one(item: dict[str, Any]) -> dict[str, Any]:
        if _matches_latest_filters(item, list(wanted)):
            return item
        async with semaphore:
            return await _enrich_latest_item(config, item)

    return list(await asyncio.gather(*(enrich_one(item) for item in items)))


async def _filter_ranked_items_page(config: dict[str, Any], items: list[dict[str, Any]], requested_filters: list[str], *, page: int, limit: int) -> dict[str, Any]:
    normalized_filters = _normalize_latest_filters(requested_filters, "")
    if not normalized_filters:
        return {"ok": True, "items": items, "total": len(items), "raw": {"client_filtered": False}}
    enriched = await _enrich_items_for_filters(config, items, normalized_filters)
    filtered = [item for item in enriched if _matches_latest_filters(item, normalized_filters)]
    start = (page - 1) * limit
    return {"ok": True, "items": filtered[start:start + limit], "total": len(filtered), "raw": {"client_filtered": "ranking_filters", "filters": normalized_filters}}


async def _top250_page_items(
    config: dict[str, Any],
    *,
    page: int,
    limit: int,
    top_type: str,
    type_value: str,
    ignore_watched: bool,
    start_rank: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # DBOnline's /top250 endpoint becomes noticeably slower at large limits and
    # can hit the plugin timeout at limit=80. Keep this endpoint capped in the
    # host plugin instead of letting arbitrary frontend page-size changes break
    # the whole tab.
    safe_limit = max(1, min(int(limit or 48), 48))
    data = _data(await _request(config, "GET", "/top250", params={
        "page": page,
        "limit": safe_limit,
        "type": top_type,
        "type_value": type_value,
        "ignore_watched": ignore_watched,
        "start_rank": start_rank,
    }))
    return [_normalize_movie(config, x) for x in _movie_list(data) if isinstance(x, dict)], data if isinstance(data, dict) else {}


async def _collect_latest_items(config: dict[str, Any], latest_type: str, sort_by: str, *, scan_limit: int = 80, max_scan_pages: int = 24) -> list[dict[str, Any]]:
    cache_key = (str(latest_type), str(sort_by))
    cached = _cache_get(LATEST_CACHE, cache_key, LATEST_CACHE_TTL)
    if cached is not None:
        return list(cached)
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_page_signatures: set[tuple[str, ...]] = set()
    for scan_page in range(1, max_scan_pages + 1):
        page_items = await _latest_page_items(config, latest_type, sort_by, scan_page, scan_limit=scan_limit)
        if not page_items:
            break
        signature = tuple(str(item.get("id") or item.get("code") or item.get("number") or "") for item in page_items)
        if signature and signature in seen_page_signatures:
            break
        seen_page_signatures.add(signature)
        added = 0
        for item in page_items:
            item_id = str(item.get("id") or item.get("code") or item.get("number") or "")
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)
            items.append(item)
            added += 1
        if added == 0:
            break
    _cache_set(LATEST_CACHE, cache_key, items)
    return list(items)


async def _scan_latest_filtered_page(
    config: dict[str, Any],
    latest_type: str,
    sort_by: str,
    requested_filters: list[str],
    *,
    page: int,
    limit: int,
    scan_limit: int = 80,
    max_scan_pages: int = 24,
) -> dict[str, Any]:
    normalized_filters = tuple(_normalize_latest_filters(requested_filters, ""))
    api_filter = _latest_preferred_api_filter(list(normalized_filters))
    cache_key = (_base(config), str(latest_type), str(sort_by), api_filter, normalized_filters, int(page), int(limit))
    cached = _cache_get(FILTER_PAGE_CACHE, cache_key, FILTER_PAGE_CACHE_TTL)
    if cached is not None:
        return dict(cached)

    start = (page - 1) * limit
    lookahead_pages = 0 if normalized_filters else 1
    target_count = max(limit, page * limit + lookahead_pages * limit)
    filtered_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_page_signatures: set[tuple[str, ...]] = set()
    exhausted = True
    deadline = time.time() + (12.0 if normalized_filters else 20.0)

    for scan_page in range(1, max_scan_pages + 1):
        page_items = await _latest_page_items(config, latest_type, sort_by, scan_page, scan_limit=scan_limit, filter_by=api_filter)
        if not page_items:
            break
        signature = tuple(str(item.get("id") or item.get("code") or item.get("number") or "") for item in page_items)
        if signature and signature in seen_page_signatures:
            break
        seen_page_signatures.add(signature)

        unique_additions = 0
        page_items = await _enrich_page_items_for_filter(config, page_items, list(normalized_filters))
        for item in page_items:
            item_id = str(item.get("id") or item.get("code") or item.get("number") or "")
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)
            unique_additions += 1

            if _matches_latest_filters(item, list(normalized_filters)):
                filtered_items.append(item)
                if len(filtered_items) >= target_count:
                    exhausted = False
                    break

        if not unique_additions or not exhausted:
            break
        if time.time() >= deadline:
            exhausted = False
            break

    total = len(filtered_items) if exhausted else max(len(filtered_items) + 1, target_count + 1)
    result = {
        "ok": True,
        "items": filtered_items[start:start + limit],
        "total": total,
        "raw": {
            "client_filtered": list(normalized_filters),
            "api_filter": api_filter,
            "partial_total": not exhausted,
            "scan_pages": len(seen_page_signatures),
            "buffered_until_page": page + lookahead_pages,
        },
    }
    _cache_set(FILTER_PAGE_CACHE, cache_key, result)
    return dict(result)


def _normalize_actor(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or item.get("external_id") or item.get("name") or ""),
        "name": str(item.get("name") or ""),
        "name_zht": str(item.get("name_zht") or ""),
        "other_name": str(item.get("other_name") or ""),
        "avatar_url": _abs(config, item.get("avatar_url")),
        "uncensored": bool(item.get("uncensored")),
        "raw": item,
    }


def _normalize_magnet(magnet: dict[str, Any], code: str) -> dict[str, Any]:
    tags = magnet.get("tags") if isinstance(magnet.get("tags"), list) else []
    size_mb = float(magnet.get("size_mb") or 0)
    return {
        "name": magnet.get("name") or code,
        "magnet": magnet.get("magnet") or "",
        "size_mb": size_mb,
        "size_bytes": int(size_mb * 1024 * 1024),
        "file_count": magnet.get("file_count") or 0,
        "date": magnet.get("date") or "",
        "tags": tags,
        "site": magnet.get("site") or "JavDB",
        "source_key": magnet.get("source_key") or PLUGIN_ID,
        "chinese": any("字幕" in str(x) or "中字" in str(x) for x in tags),
        "hd": any("高清" in str(x).upper() or "HD" in str(x).upper() for x in tags),
    }


def _resource_requirements_from_url(url: str, *, private_tracker: bool = False) -> dict[str, Any]:
    raw = str(url or "").strip()
    requirements: dict[str, Any] = {}
    if raw.startswith("magnet:?"):
        requirements["accepts_public_magnet"] = True
    elif raw.startswith("http://") or raw.startswith("https://"):
        requirements["accepts_http_torrent"] = True
    if private_tracker:
        requirements["accepts_private_tracker"] = True
    return requirements


def _resource_from_javdb_magnet(video: dict[str, Any], magnet: dict[str, Any], index: int) -> dict[str, Any]:
    code = str(video.get("code") or video.get("number") or "")
    title = str(video.get("display_title") or video.get("title") or code or magnet.get("name") or f"{code} #{index + 1}").strip()
    subtitle_parts = [str(x) for x in (magnet.get("size_mb") and f"{magnet.get('size_mb')} MB", magnet.get("date"), magnet.get("site") or "JavDB") if x]
    tags = [str(tag) for tag in (magnet.get("tags") or []) if str(tag or "").strip()]
    url = str(magnet.get("magnet") or "").strip()
    requirements = _resource_requirements_from_url(url)
    compatible_downloaders = ["qbittorrent", "transmission"]
    if requirements.get("accepts_public_magnet"):
        compatible_downloaders.insert(0, "xunlei-remote")
    return {
        "id": f"javdb:{code or video.get('id') or 'video'}:{index}",
        "kind": "torrent",
        "query_key": code,
        "title": str(magnet.get("name") or title),
        "subtitle": " · ".join(subtitle_parts),
        "url": url,
        "size_bytes": int(magnet.get("size_bytes") or 0),
        "file_count": int(magnet.get("file_count") or 0),
        "tags": tags,
        "cover_url": str(video.get("cover_url") or video.get("thumb_url") or ""),
        "fanart_url": str(video.get("cover_url") or video.get("thumb_url") or ""),
        "source_url": str(video.get("link") or ""),
        "features": {
            "has_subtitle": bool(magnet.get("chinese")),
            "is_cracked": any("破解" in tag for tag in tags),
            "is_private_tracker": False,
        },
        "requirements": requirements,
        "compatible_downloaders": compatible_downloaders,
        "preferred_downloader": "xunlei-remote" if requirements.get("accepts_public_magnet") else "qbittorrent",
        "metadata": {
            "source_plugin": PLUGIN_ID,
            "video_code": code,
            "video_title": title,
            "site": str(magnet.get("site") or "JavDB"),
            "source_key": str(magnet.get("source_key") or PLUGIN_ID),
        },
    }


async def _resource_search(config: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_code = payload.get("code") or payload.get("number") or ""
    raw_keyword = payload.get("keyword") or payload.get("q") or ""
    code = _extract_video_code(raw_code) or _extract_video_code(raw_keyword)
    title = str(payload.get("title") or "").strip()
    limit = max(1, min(int(payload.get("limit") or 6), 12))
    expected_magnets_count = max(0, int(payload.get("expected_magnets_count") or 0))
    candidates: list[dict[str, Any]] = []

    if code:
        try:
            video = await _video(config, code)
            if expected_magnets_count > 0 and len(video.get("magnets") or []) < expected_magnets_count:
                video = await _video(config, code, refresh=True)
            if video:
                candidates.append(video)
        except Exception:
            pass

    if not candidates and (code or title or raw_keyword):
        keyword = code or title or str(raw_keyword or "").strip()
        searched = await _search(config, keyword, 1, limit)
        search_items = [item for item in (searched.get("items") or []) if isinstance(item, dict)]

        async def load_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
            item_code = _code(item.get("code") or item.get("number") or "")
            if not item_code:
                return None
            try:
                return await _video(config, item_code)
            except Exception:
                return None

        loaded = await asyncio.gather(*(load_candidate(item) for item in search_items[: max(limit, 12)]))
        candidates.extend([item for item in loaded if isinstance(item, dict)])

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for video in candidates:
        magnets = [_normalize_magnet(x, str(video.get("code") or video.get("number") or "")) for x in (video.get("magnets") or []) if isinstance(x, dict)]
        for idx, magnet in enumerate(magnets):
            item = _resource_from_javdb_magnet(video, magnet, idx)
            key = str(item.get("url") or item.get("id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= limit:
                return out
    return out


async def search_resources(query: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """PluginRuntime resource-search contract used by search and subscriptions."""
    resolved_config = config or {}
    payload = query or {}
    items = await _resource_search(resolved_config, payload)
    keyword = str(payload.get("keyword") or payload.get("q") or payload.get("code") or payload.get("number") or "").strip()
    covers: dict[str, str] = {}
    if keyword and not _extract_video_code(keyword):
        try:
            searched = await _search(resolved_config, keyword, 1, 50)
            for movie in searched.get("items") or []:
                code = _code(movie.get("code") or movie.get("number") or "")
                cover = str(movie.get("cover_url") or movie.get("thumb_url") or "").strip()
                if code and cover:
                    covers[code] = cover
        except Exception:
            pass
    return {"items": items, "covers": covers}


async def _resource_search_paged(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    keyword = str(payload.get("keyword") or payload.get("q") or payload.get("title") or payload.get("code") or payload.get("number") or "").strip()
    if not keyword:
        return {"items": [], "page": 1, "has_more": False}
    page = max(1, int(payload.get("page") or 1))
    limit = max(1, min(int(payload.get("limit") or 24), 24))
    max_items = max(limit, min(int(payload.get("max_items") or 100), 100))
    start = (page - 1) * limit
    target = min(max_items, start + limit)
    movie_limit = max(12, min(int(payload.get("movie_limit") or 24), 50))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    search_page = 1
    exhausted = False

    while len(out) < target + 1 and search_page <= 20:
        searched = await _search(config, keyword, search_page, movie_limit)
        movies = [x for x in (searched.get("items") or []) if isinstance(x, dict)]
        if not movies:
            exhausted = True
            break
        async def load_movie_detail(movie: dict[str, Any]) -> dict[str, Any] | None:
            item_code = _code(movie.get("code") or movie.get("number") or "")
            if not item_code or int(movie.get("magnets_count") or 0) <= 0:
                return None
            try:
                return await _video(config, item_code)
            except Exception:
                return None

        loaded_videos = await asyncio.gather(*(load_movie_detail(movie) for movie in movies))
        for video in [x for x in loaded_videos if isinstance(x, dict)]:
            item_code = _code(video.get("code") or video.get("number") or "")
            magnets = [_normalize_magnet(x, str(video.get("code") or video.get("number") or item_code)) for x in (video.get("magnets") or []) if isinstance(x, dict)]
            for idx, magnet in enumerate(magnets):
                item = _resource_from_javdb_magnet(video, magnet, idx)
                key = str(item.get("url") or item.get("id") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(item)
                if len(out) >= target + 1:
                    break
            if len(out) >= target + 1:
                break
        if len(movies) < movie_limit:
            exhausted = True
            break
        search_page += 1

    page_items = out[start:start + limit]
    has_more = len(out) > target and target < max_items
    return {
        "items": page_items,
        "page": page,
        "limit": limit,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
        "total_hint": len(out) if exhausted else None,
        "max_items": max_items,
    }


async def test(config: dict[str, Any]) -> PluginTestResult:
    try:
        payload = await _request(config, "GET", "/stats")
        data = _data(payload) or {}
        return PluginTestResult(ok=True, message="JavDB connected", details=data if isinstance(data, dict) else {})
    except Exception as e:
        return PluginTestResult(ok=False, message=str(e), details={})


async def fetch_rss_items(manifest: PluginManifest, config: dict[str, Any], limit: int = 30, force_refresh: bool = False) -> dict[str, Any]:
    data = _data(await _request(config, "GET", "/latest", params={"page": 1, "limit": limit, "type": "all", "sort_by": "update", "filter_by": "magnets"}))
    items = [_normalize_movie(config, x) for x in _movie_list(data) if isinstance(x, dict)]
    return {"items": items, "total": len(items), "source": manifest.id}


async def build_widget(config: dict[str, Any]) -> DashboardWidget | None:
    result = await _list_action(config, "recommend", {"page": 1, "limit": 18})
    items = list(result.get("items") or [])
    if not items:
        return None
    payload_items: list[dict[str, Any]] = []
    for item in items[:18]:
        payload_items.append({
            "code": item.get("code") or item.get("number") or "",
            "title": item.get("title") or item.get("origin_title") or item.get("code") or "",
            "cover_url": item.get("cover_url") or item.get("thumb_url") or "",
            "release_date": item.get("release_date") or "",
            "magnets_count": int(item.get("magnets_count") or 0),
            "has_cnsub": bool(item.get("has_cnsub") or item.get("play_subtitle")),
            "is_cracked": bool(item.get("is_cracked")),
        })
    return DashboardWidget(
        plugin_id=PLUGIN_ID,
        key="javdb-recommend",
        title="JavDB 推荐",
        badge=f"{len(payload_items)} 部",
        payload={
            "kind": "media-carousel",
            "route": "/plugins/javdb",
            "items": payload_items,
        },
    )


async def _search(config: dict[str, Any], q: str, page: int = 1, limit: int = 24) -> dict[str, Any]:
    if not str(q or "").strip():
        return {"items": [], "total": 0}
    data = _data(await _request(config, "GET", "/search", params={"q": q, "page": page, "limit": limit}))
    items = [_normalize_movie(config, x) for x in _movie_list(data) if isinstance(x, dict)]
    return {"items": items, "total": int(data.get("total") or len(items)) if isinstance(data, dict) else len(items), "raw": data}


async def _video(config: dict[str, Any], code: str, *, refresh: bool = False) -> dict[str, Any]:
    code_key = str(code or "").strip().upper()
    cache_key = (_base(config), code_key)
    if not refresh:
        cached = _cache_get(VIDEO_DETAIL_CACHE, cache_key, VIDEO_DETAIL_CACHE_TTL)
        if cached is not None:
            cached_detail = dict(cached)
            cached_magnets = cached_detail.get("magnets")
            if isinstance(cached_magnets, list) or cached_detail.get("_magnets_refresh_checked"):
                return cached_detail
            refresh = True
    path = f"/video/{code}"
    params = {"refresh": "true"} if refresh else None
    data = _data(await _request(config, "GET", path, params=params))
    if not isinstance(data, dict):
        return {}
    out = dict(data)
    out["cover_url"] = _abs(config, out.get("cover_url"))
    out["thumb_url"] = _abs(config, out.get("thumb_url"))
    out["previews"] = [_abs(config, x) for x in (out.get("previews") or [])]
    if refresh and not isinstance(out.get("magnets"), list):
        out["_magnets_refresh_checked"] = True
    _cache_set(VIDEO_DETAIL_CACHE, cache_key, out)
    return dict(out)


async def _related_movies(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    rel_type = str(payload.get("rel_type") or "").strip()
    rel_id = str(payload.get("rel_id") or payload.get("id") or "").strip()
    page = max(1, int(payload.get("page") or 1))
    limit = max(1, min(int(payload.get("limit") or 24), 48))
    sort_by = payload.get("sort_by") or "release"
    order_by = payload.get("order_by") or "desc"
    path_map = {
        "actor": "/actors/{id}/movies",
        "series": "/series/{id}/movies",
        "maker": "/makers/{id}/movies",
        "publisher": "/publishers/{id}/movies",
        "director": "/directors/{id}/movies",
        "list": "/lists/{id}/movies",
    }
    if rel_type == "category":
        data = _data(await _request(config, "GET", "/videos/filter", params={"category_id": rel_id, "page": page, "limit": limit}))
    elif rel_type in path_map:
        data = _data(await _request(config, "GET", path_map[rel_type].format(id=rel_id), params={"page": page, "limit": limit, "sort_by": sort_by, "order_by": order_by, "filter": payload.get("filter") or ""}))
    else:
        raise ValueError(f"unsupported relation type: {rel_type}")
    items = [_normalize_movie(config, x) for x in _movie_list(data) if isinstance(x, dict)]
    total = int(data.get("total") or data.get("total_count") or 0) if isinstance(data, dict) else 0
    if not total:
        # DBOnline's relation endpoint returns current_page/movies but no
        # total/has_more. Keep pagination alive while a full page is returned.
        upstream_page_size = len(items)
        if upstream_page_size >= limit:
            total = page * limit + 1
        else:
            total = (page - 1) * limit + upstream_page_size
    return {"ok": True, "items": items, "total": total, "raw": data}


def _series_entries(value: Any) -> list[dict[str, str]]:
    """Normalize the optional series field returned by a JavDB movie detail."""
    entries = value if isinstance(value, list) else [value]
    normalized: list[dict[str, str]] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("label") or "").strip()
            external_id = str(entry.get("external_id") or entry.get("id") or "").strip()
        else:
            name = str(entry or "").strip()
            external_id = ""
        if name:
            normalized.append({"id": external_id or name, "name": name})
    return normalized


async def _series_options(config: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded series directory from recent movie details."""
    cache_key = (_base(config),)
    cached = _cache_get(SERIES_OPTIONS_CACHE, cache_key, SERIES_OPTIONS_CACHE_TTL)
    if cached is not None:
        return dict(cached)

    recent = await _latest_page_items(config, "all", "update", 1, scan_limit=48)
    semaphore = asyncio.Semaphore(DETAIL_ENRICH_CONCURRENCY)

    async def load_detail(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        code = str(item.get("code") or item.get("number") or "").strip()
        if not code:
            return item, {}
        try:
            async with semaphore:
                return item, await _video(config, code)
        except Exception:
            return item, {}

    rows = await asyncio.gather(*(load_detail(item) for item in recent))
    grouped: dict[str, dict[str, Any]] = {}
    for item, detail in rows:
        for series in _series_entries(detail.get("series")):
            key = str(series["id"]).lower()
            current = grouped.setdefault(key, {
                "id": series["id"],
                "name": series["name"],
                "recent_work_count": 0,
                "latest_release_date": "",
                "cover_url": "",
            })
            current["recent_work_count"] += 1
            release_date = str(item.get("release_date") or detail.get("release_date") or detail.get("date") or "")
            if release_date >= str(current["latest_release_date"]):
                current["latest_release_date"] = release_date
                current["cover_url"] = str(item.get("cover_url") or detail.get("cover_url") or "")

    items = sorted(
        grouped.values(),
        key=lambda item: (
            str(item.get("latest_release_date") or ""),
            int(item.get("recent_work_count") or 0),
            str(item.get("name") or ""),
        ),
        reverse=True,
    )
    return _cache_set(SERIES_OPTIONS_CACHE, cache_key, {
        "ok": True,
        "items": items,
        "total": len(items),
        "sample_size": len(recent),
    })


async def _list_action(config: dict[str, Any], action: str, payload: dict[str, Any]) -> dict[str, Any]:
    page = max(1, int(payload.get("page") or 1)); limit = max(1, min(int(payload.get("limit") or 24), 80))
    if action == "rankings":
        data = _data(await _request(config, "GET", "/rankings", params={"period": payload.get("period") or "daily", "type": int(payload.get("type") or 0)}))
        items = [_normalize_movie(config, x) for x in _movie_list(data) if isinstance(x, dict)]
        requested_filters = _normalize_latest_filters(payload.get("filters"), str(payload.get("filter_by") or ""))
        if requested_filters:
            return await _filter_ranked_items_page(config, items, requested_filters, page=page, limit=limit)
    elif action == "videos":
        requested_filter = str(payload.get("filter") or "").strip()
        actor_ids = [str(x).strip() for x in (payload.get("actor_ids") or []) if str(x).strip()]
        if requested_filter in {"library", "not_library"}:
            # DB Online exposes a filter parameter here, but on the current local
            # build it returns the same total set for both values. The record set
            # is small, so normalize the behavior in the plugin by filtering the
            # cached/opened video records ourselves.
            wanted = requested_filter == "library"
            all_items: list[dict[str, Any]] = []
            scan_page = 1
            total_pages = 1
            while scan_page <= total_pages and scan_page <= 50:
                batch = _data(await _request(config, "GET", "/videos", params={"page": scan_page, "pageSize": 80, "sort": payload.get("sort") or "update", "order": payload.get("order") or "desc"}))
                if isinstance(batch, dict):
                    total_pages = int(batch.get("total_pages") or total_pages)
                for item in _movie_list(batch):
                    if not isinstance(item, dict):
                        continue
                    in_library = bool((item.get("library") or {}).get("in_library")) if isinstance(item.get("library"), dict) else False
                    if in_library == wanted:
                        all_items.append(_normalize_movie(config, item))
                scan_page += 1
            start = (page - 1) * limit
            return {"ok": True, "items": all_items[start:start + limit], "total": len(all_items), "raw": {"client_filtered": requested_filter}}
        if len(actor_ids) > 1:
            merged: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for actor_id in actor_ids[:8]:
                actor_page = 1
                actor_total_pages = 1
                while actor_page <= actor_total_pages and actor_page <= 50:
                    actor_params: dict[str, Any] = {"page": actor_page, "pageSize": 80}
                    if payload.get("sort"):
                        actor_params["sort"] = payload.get("sort")
                    if payload.get("order"):
                        actor_params["order"] = payload.get("order")
                    actor_params["actor_id"] = actor_id
                    for key in ("min_score", "user_score", "filter"):
                        if payload.get(key) not in (None, ""):
                            actor_params[key] = payload.get(key)
                    if isinstance(payload.get("category_ids"), list) and payload.get("category_ids"):
                        actor_params["category_ids"] = ",".join(str(x) for x in payload.get("category_ids"))
                    batch = _data(await _request(config, "GET", "/videos", params=actor_params))
                    if isinstance(batch, dict):
                        actor_total_pages = int(batch.get("total_pages") or actor_total_pages)
                    for item in _movie_list(batch):
                        if not isinstance(item, dict):
                            continue
                        normalized = _normalize_movie(config, item)
                        key = str(normalized.get("id") or normalized.get("code") or "")
                        if not key or key in seen_ids:
                            continue
                        seen_ids.add(key)
                        merged.append(normalized)
                    actor_page += 1
            merged = _sort_video_items(merged, str(payload.get("sort") or "created"), str(payload.get("order") or "desc"))
            start = (page - 1) * limit
            return {"ok": True, "items": merged[start:start + limit], "total": len(merged), "raw": {"client_filtered": "actor_ids_union", "actor_ids": actor_ids}}
        params: dict[str, Any] = {"page": page, "pageSize": limit}
        if payload.get("sort"):
            params["sort"] = payload.get("sort")
        if payload.get("order"):
            params["order"] = payload.get("order")
        for key in ("min_score", "user_score", "actor_id", "filter"):
            if payload.get(key) not in (None, ""):
                params[key] = payload.get(key)
        if isinstance(payload.get("category_ids"), list) and payload.get("category_ids"):
            params["category_ids"] = ",".join(str(x) for x in payload.get("category_ids"))
        data = _data(await _request(config, "GET", "/videos", params=params))
    elif action == "latest":
        requested_filter = str(payload.get("filter_by") or "all").strip() or "all"
        requested_filters = _normalize_latest_filters(payload.get("filters"), requested_filter)
        latest_type = payload.get("type") or "all"
        sort_by = payload.get("sort_by") or "update"
        if not requested_filters:
            # Do not scan the full feed on initial page load. Load enough rows
            # for the current page plus a small lookahead so pagination remains
            # useful without making the plugin wait on dozens of upstream calls.
            return await _scan_latest_filtered_page(config, str(latest_type), str(sort_by), [], page=page, limit=limit)
        else:
            # DB Online's latest endpoint currently ignores filter_by for several
            # modes. Scan only until the current page is satisfied so the plugin
            # returns before the frontend's request timeout.
            return await _scan_latest_filtered_page(config, str(latest_type), str(sort_by), requested_filters, page=page, limit=limit)
    elif action == "top250":
        type_value = str(payload.get("type_value") or "")
        years = {str(y) for y in range(2008, 2101)}
        top_type = str(payload.get("type") or "")
        if not top_type:
            top_type = "video_type" if type_value in {"0", "1", "2", "3"} else "year" if type_value in years else "all"
        requested_filters = _normalize_latest_filters(payload.get("filters"), str(payload.get("filter_by") or ""))
        if requested_filters:
            scan_items: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            scan_page = 1
            while scan_page <= 6 and len(scan_items) < max(limit * page * 3, 120):
                page_movies, batch = await _top250_page_items(
                    config,
                    page=scan_page,
                    limit=48,
                    top_type=top_type,
                    type_value=type_value,
                    ignore_watched=bool(payload.get("ignore_watched") or False),
                    start_rank=int(payload.get("start_rank") or 1),
                )
                if not page_movies:
                    break
                for item in page_movies:
                    key = str(item.get("id") or item.get("code") or item.get("number") or "")
                    if key and key in seen_ids:
                        continue
                    if key:
                        seen_ids.add(key)
                    scan_items.append(item)
                if isinstance(batch, dict) and batch.get("has_more") is False:
                    break
                scan_page += 1
            return await _filter_ranked_items_page(config, scan_items, requested_filters, page=page, limit=limit)
        items, data = await _top250_page_items(
            config,
            page=page,
            limit=limit,
            top_type=top_type,
            type_value=type_value,
            ignore_watched=bool(payload.get("ignore_watched") or False),
            start_rank=int(payload.get("start_rank") or 1),
        )
        total = int(data.get("total") or len(items)) if isinstance(data, dict) else len(items)
        if isinstance(data, dict) and bool(data.get("has_more")) and total <= len(items):
            total = max(page * len(items) + 1, len(items) + 1)
        return {"ok": True, "items": items, "total": total, "raw": data}
    elif action == "recommend":
        data = _data(await _request(config, "GET", "/recommend", params={"page": page, "limit": limit}))
    else:
        data = {}
    items = [_normalize_movie(config, x) for x in _movie_list(data) if isinstance(x, dict)]
    total = int(data.get("total") or len(items)) if isinstance(data, dict) else len(items)
    if action == "top250" and isinstance(data, dict) and bool(data.get("has_more")) and total <= len(items):
        total = max(page * limit + 1, len(items) + 1)
    return {"ok": True, "items": items, "total": total, "raw": data}


def _torrent_contribution(config: dict[str, Any], video: dict[str, Any], magnet: dict[str, Any], idx: int) -> dict[str, Any] | None:
    code = _code(video.get("code") or video.get("number"))
    url = str(magnet.get("magnet") or "")
    if not code or not url:
        return None
    key = f"javdb:{code}:{idx}:{abs(hash(url))}"
    torrent_ref = f"torrent:{idx}"
    labels = list(magnet.get("tags") or [])
    if magnet.get("chinese") and "中文字幕" not in labels:
        labels.append("中文字幕")
    entities = [{
        "alias": torrent_ref,
        "type": "torrent",
        "key": key,
        "label": str(magnet.get("name") or code),
        "summary": str(video.get("title") or ""),
        "source": PLUGIN_ID,
        "confidence": 88,
        "data": {
            "plugin_id": PLUGIN_ID,
            "link": f"javdb://video/{code}",
            "download_url": url,
            "download_available": True,
            "image_url": video.get("cover_url") or video.get("thumb_url") or "",
            "size_bytes": magnet.get("size_bytes") or 0,
            "seeders": 0,
            "labels": labels,
            "code": code,
            "code_aliases": [code],
            "date": magnet.get("date") or "",
            "site": magnet.get("site") or "JavDB",
            "source_key": magnet.get("source_key") or PLUGIN_ID,
        },
    }, {"alias": f"code:{code}", "type": "video_code", "key": code, "label": code, "source": PLUGIN_ID, "confidence": 90}]
    edges = [{"from": torrent_ref, "type": "HAS_CODE", "to": f"code:{code}", "confidence": 90}]
    for actor in video.get("actors") or []:
        name = str(actor.get("name") if isinstance(actor, dict) else actor or "").strip()
        if not name:
            continue
        ref = f"actor:{name.lower()}"
        data = (
            {
                "external_id": actor.get("external_id"),
                "avatar_url": _abs(config, actor.get("avatar_url")),
                "gender": actor.get("gender") or "",
            }
            if isinstance(actor, dict)
            else {}
        )
        entities.append({"alias": ref, "type": "actor", "key": name.lower(), "label": name, "source": PLUGIN_ID, "confidence": 86, "data": data})
        edges.append({"from": torrent_ref, "type": "HAS_ACTOR", "to": ref, "confidence": 82})
    for rel, typ, obj in [("HAS_STUDIO", "studio", video.get("maker")), ("HAS_LABEL", "label", video.get("publisher")), ("IN_SERIES", "series", video.get("series")), ("HAS_DIRECTOR", "director", video.get("director"))]:
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        name = str(obj.get("name"))
        ref = f"{typ}:{name.lower()}"
        entities.append({"alias": ref, "type": typ, "key": name.lower(), "label": name, "source": PLUGIN_ID, "confidence": 84, "data": {"external_id": obj.get("external_id")}})
        edges.append({"from": torrent_ref, "type": rel, "to": ref, "confidence": 80})
    for cat in video.get("categories") or []:
        if not isinstance(cat, dict) or not cat.get("name"):
            continue
        name = str(cat.get("name")); ref = f"genre:{name.lower()}"
        entities.append({"alias": ref, "type": "genre", "key": name.lower(), "label": name, "source": PLUGIN_ID, "confidence": 76, "data": {"external_id": cat.get("external_id")}})
        edges.append({"from": torrent_ref, "type": "HAS_GENRE", "to": ref, "confidence": 72})
    return {"source": PLUGIN_ID, "source_name": "JavDB", "entities": entities, "edges": edges, "scores": [{"entity": torrent_ref, "type": "torrent_metadata_quality", "value": 92 if video.get("cover_url") else 78, "reason": "基于 JavDB 视频详情与磁链元数据"}]}


async def build_knowledge_contributions(config: dict[str, Any], limit: int = 100, context: dict[str, Any] | None = None) -> dict[str, Any]:
    if not bool(config.get("knowledge_active_search", True)):
        return {"items": []}
    items: list[dict[str, Any]] = []
    try:
        actor_data = _data(await _request(config, "GET", "/actors", params={"type": 0}))
        actors = [_normalize_actor(config, x) for x in (actor_data.get("actors") if isinstance(actor_data, dict) else actor_data or []) if isinstance(x, dict)]
        actor_entities = []
        for actor in actors[:120]:
            name = str(actor.get("name") or "").strip()
            if not name:
                continue
            actor_entities.append({
                "alias": f"actor:{name.lower()}",
                "type": "actor",
                "key": name.lower(),
                "label": name,
                "source": PLUGIN_ID,
                "confidence": 84,
                "data": {
                    "external_id": actor.get("id") or "",
                    "avatar_url": actor.get("avatar_url") or "",
                    "name_zht": actor.get("name_zht") or "",
                    "other_name": actor.get("other_name") or "",
                    "uncensored": bool(actor.get("uncensored")),
                },
            })
        if actor_entities:
            items.append({
                "source": PLUGIN_ID,
                "source_name": "JavDB",
                "entities": actor_entities,
                "edges": [],
                "scores": [],
            })
    except Exception:
        pass
    codes = [str(x).strip().upper() for x in (context or {}).get("video_codes", []) if str(x).strip()]
    max_codes = max(1, min(int(config.get("knowledge_search_code_limit") or 40), 160))
    per_code = max(1, min(int(config.get("knowledge_search_per_code") or 8), 30))
    for code in codes[:max_codes]:
        try:
            video = await _video(config, code)
        except Exception:
            continue
        magnets = [_normalize_magnet(x, code) for x in (video.get("magnets") or []) if isinstance(x, dict)]
        for idx, magnet in enumerate(magnets[:per_code]):
            item = _torrent_contribution(config, video, magnet, idx)
            if item:
                items.append(item)
            if len(items) >= limit:
                return {"items": items}
    return {"items": items}


async def handle_action(action: str, payload: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    payload = payload or {}
    if action == "test":
        result = await test(config)
        return {"ok": result.ok, "message": result.message, "details": result.details or {}}
    if action in {"rankings", "latest", "videos", "top250", "recommend"}:
        return await _list_action(config, action, payload)
    if action == "search":
        return {"ok": True, **await _search(config, str(payload.get("q") or payload.get("keyword") or ""), int(payload.get("page") or 1), int(payload.get("limit") or 24))}
    if action == "video":
        code = str(payload.get("code") or "")
        expected_magnets_count = int(payload.get("expected_magnets_count") or 0)
        refresh = bool(payload.get("refresh"))
        data = await _video(config, code, refresh=refresh)
        if expected_magnets_count > 0 and len(data.get("magnets") or []) < expected_magnets_count:
            data = await _video(config, code, refresh=True)
        return {"ok": True, "data": data}
    if action == "related_movies":
        return await _related_movies(config, payload)
    if action == "actors":
        try:
            data = _data(await _request(config, "GET", "/actors", params={"type": int(payload.get("type") or 0)}))
        except JavDBUpstreamError:
            data = _data(await _request(config, "GET", "/options/actors"))
        actors = [_normalize_actor(config, x) for x in (data.get("actors") if isinstance(data, dict) else data or []) if isinstance(x, dict)]
        return {"ok": True, "items": actors, "total": len(actors)}
    if action == "actor_options":
        cache_key = (_base(config),)
        cached = _cache_get(ACTOR_OPTIONS_CACHE, cache_key, ACTOR_OPTIONS_CACHE_TTL)
        if cached is not None:
            return dict(cached)
        data = _data(await _request(config, "GET", "/options/actors"))
        ranked_by_id: dict[str, dict[str, Any]] = {}
        for actor_type in (0, 1, 2, 3):
            try:
                ranked_data = _data(await _request(config, "GET", "/actors", params={"type": actor_type}))
                ranked_items = ranked_data.get("actors") if isinstance(ranked_data, dict) else ranked_data
                for rank_index, actor in enumerate(ranked_items or []):
                    if not isinstance(actor, dict):
                        continue
                    normalized = _normalize_actor(config, actor)
                    actor_id = str(normalized.get("id") or "").strip()
                    if actor_id and actor_id not in ranked_by_id:
                        normalized["ranking_type"] = actor_type
                        normalized["ranking_order"] = rank_index
                        ranked_by_id[actor_id] = normalized
            except Exception:
                continue
        items = []
        for actor in (data or []):
            if not isinstance(actor, dict):
                continue
            actor_id = str(actor.get("external_id") or actor.get("id") or "").strip()
            name = str(actor.get("name") or "").strip()
            if not actor_id or not name or name in {"---", "???"}:
                continue
            ranked = ranked_by_id.get(actor_id) or {}
            items.append({
                "id": actor_id,
                "external_id": actor_id,
                "name": name,
                "name_zht": ranked.get("name_zht") or "",
                "other_name": ranked.get("other_name") or "",
                "avatar_url": ranked.get("avatar_url") or "",
                "uncensored": bool(ranked.get("uncensored")),
                "ranking_type": ranked.get("ranking_type"),
                "ranking_order": ranked.get("ranking_order"),
                "raw": actor,
            })
        items.sort(key=lambda item: (
            0 if item.get("ranking_type") is not None else 1,
            int(item.get("ranking_type") if item.get("ranking_type") is not None else 999),
            int(item.get("ranking_order") if item.get("ranking_order") is not None else 999999),
            str(item.get("name") or "").lower(),
        ))
        return _cache_set(ACTOR_OPTIONS_CACHE, cache_key, {"ok": True, "items": items, "total": len(items)})
    if action == "series_options":
        return await _series_options(config)
    if action == "actor_movies":
        actor_id = str(payload.get("actor_id") or payload.get("id") or "")
        page = max(1, int(payload.get("page") or 1)); limit = max(1, min(int(payload.get("limit") or 24), 80))
        data = _data(await _request(config, "GET", f"/actors/{actor_id}/movies", params={"page": page, "limit": limit, "sort_by": payload.get("sort_by") or "release"}))
        return {"ok": True, "items": [_normalize_movie(config, x) for x in _movie_list(data) if isinstance(x, dict)], "raw": data}
    if action == "stats":
        return {"ok": True, "data": _data(await _request(config, "GET", "/stats"))}
    if action == "categories":
        return {"ok": True, "items": _data(await _request(config, "GET", "/options/categories")) or []}
    if action == "magnet_stats":
        return {"ok": True, "data": _data(await _request(config, "GET", "/external-magnets/stats"))}
    if action == "subtitle_stats":
        return {"ok": True, "data": _data(await _request(config, "GET", "/subtitle/stats"))}
    if action == "downloaders":
        return {"ok": True, "data": _data(await _request(config, "GET", "/downloaders"))}
    if action == "download_records":
        page = max(1, int(payload.get("page") or 1)); limit = max(1, min(int(payload.get("limit") or 24), 80))
        return {"ok": True, "data": _data(await _request(config, "GET", "/download-records", params={"page": page, "limit": limit}))}
    if action == "image_stats":
        return {"ok": True, "data": _data(await _request(config, "GET", "/image/stats"))}
    if action == "library_stats":
        return {"ok": True, "data": _data(await _request(config, "GET", "/library/cache/stats"))}
    if action == "download_options":
        bindings = config.get("downloader_binding") or []
        if isinstance(bindings, str): bindings = [bindings] if bindings != "none" else []
        return {"ok": True, "downloader_binding": bindings, "default_downloader": str(config.get("default_downloader") or "none")}
    if action == "resource_search":
        if str(payload.get("mode") or "").strip() == "deep":
            return {"ok": True, **await _resource_search_paged(config, payload)}
        items = await _resource_search(config, payload)
        raw_keyword = payload.get("keyword") or payload.get("q") or payload.get("title") or ""
        has_more = bool(raw_keyword) and not _extract_video_code(raw_keyword) and len(items) >= 12
        return {"ok": True, "items": items, "has_more": has_more, "next_page": 2 if has_more else None, "max_items": 100 if has_more else None}
    if action == "knowledge_contributions":
        return await build_knowledge_contributions(config, int(payload.get("limit") or 100), context=payload.get("context") or {})
    raise ValueError(f"unsupported action: {action}")
