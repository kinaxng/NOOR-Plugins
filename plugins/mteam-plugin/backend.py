from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import json
import mimetypes
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.runtime_paths import plugin_cache_path
from app.plugins.contracts import PluginManifest, PluginTestResult

PLUGIN_ID = "mteam-plugin"
PLUGIN_CACHE_DIR = plugin_cache_path(PLUGIN_ID)
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_BACKGROUND_LAST_STARTED_AT = ""
_BACKGROUND_LAST_FINISHED_AT = ""
_BACKGROUND_LAST_ERROR = ""

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_SIZE_BRACKET_RE = re.compile(r"\[[\d.]+\s*(?:B|KB|MB|GB|TB)\]$", re.IGNORECASE)


def _prefer_chinese_title(raw_title: str) -> str:
    title = html.unescape((raw_title or "").strip())
    if not title:
        return ""
    candidates: list[str] = []
    for content in _BRACKET_RE.findall(title):
        if "|" in content:
            for part in reversed([x.strip() for x in content.split("|") if x.strip()]):
                if _CJK_RE.search(part):
                    candidates.append(part)
                    break
        elif _CJK_RE.search(content) and len(content) >= 8:
            candidates.append(content.strip())
    if candidates:
        return candidates[-1]
    clean = re.sub(r"^\[[^\]]+\]", "", title).strip()
    clean = _SIZE_BRACKET_RE.sub("", clean).strip()
    clean = re.sub(r"\[[^\]]*restored[^\]]*\]", "", clean, flags=re.IGNORECASE).strip()
    return clean or title


def _with_display_title(item: dict[str, Any]) -> dict[str, Any]:
    item["display_title"] = _prefer_chinese_title(str(item.get("title") or ""))
    return item


def _plugin_cache_file(plugin_id: str, namespace: str, key: str) -> Path:
    safe_key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return PLUGIN_CACHE_DIR / f"{namespace}-{safe_key}.json"


def _read_cache(path: Path, ttl_days: int) -> dict[str, Any] | None:
    if ttl_days <= 0 or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        created_at = float(data.get("created_at") or 0)
        if created_at <= 0:
            return None
        if time.time() - created_at > ttl_days * 86400:
            return None
        return data
    except Exception:
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass



def _cover_cache_path(torrent_id: int) -> Path:
    return PLUGIN_CACHE_DIR / "covers" / f"{torrent_id}.json"


def _read_cover_cache(torrent_id: int, ttl_days: int) -> dict[str, Any] | None:
    return _read_cache(_cover_cache_path(torrent_id), ttl_days)


def _write_cover_cache(torrent_id: int, image_url: str, is_dmm: bool) -> None:
    if not image_url:
        return
    _write_cache(_cover_cache_path(torrent_id), {"created_at": time.time(), "image_url": image_url, "is_dmm": is_dmm})


def _local_cached_image_url(plugin_id: str, original_url: str, ttl_days: int) -> tuple[str, str] | None:
    if not original_url:
        return None
    image_id = _image_cache_id(original_url)
    meta = _read_cache(_image_meta_path(plugin_id, image_id), ttl_days)
    if not meta:
        return None
    filename = str(meta.get("filename") or "")
    if not filename or not (_image_cache_dir(plugin_id) / filename).exists():
        return None
    return f"/api/plugins/{plugin_id}/images/{image_id}", image_id


def _apply_known_cover_cache(items: list[dict[str, Any]], ttl_days: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        copy = dict(item)
        tid = _extract_torrent_id(copy)
        if tid:
            cached_cover = _read_cover_cache(tid, ttl_days)
            if cached_cover and cached_cover.get("image_url"):
                cover_url = str(cached_cover.get("image_url") or "")
                copy["api_resolved_image_url"] = cover_url
                copy["image_url"] = cover_url
                copy["image_source"] = "dmm" if cached_cover.get("is_dmm") else (urlparse(cover_url).hostname or "")
                if cached_cover.get("is_dmm"):
                    copy["is_av"] = True
        out.append(copy)
    return out


def _attach_local_cached_images(plugin_id: str, items: list[dict[str, Any]], ttl_days: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        copy = dict(item)
        original = str(copy.get("original_image_url") or copy.get("image_url") or "")
        copy["original_image_url"] = original
        cached = _local_cached_image_url(plugin_id, original, ttl_days)
        if cached:
            copy["image_url"] = cached[0]
            copy["cached_image_id"] = cached[1]
            copy["image_cached"] = True
        elif original and not original.startswith(f"/api/plugins/{plugin_id}/images/"):
            # Plugin pages should not hotlink remote or encoded cover hosts. Background cache fills this later.
            copy["image_url"] = ""
            copy["image_cached"] = False
        else:
            copy["image_cached"] = False
        out.append(copy)
    return out


async def _background_cover_refresh(items: list[dict[str, Any]], config: dict[str, Any], ttl_days: int, timeout: float) -> None:
    global _BACKGROUND_LAST_ERROR, _BACKGROUND_LAST_FINISHED_AT
    max_resolve = max(0, min(int(config.get("max_api_cover_resolve") or 40), 80))
    max_images = max(0, min(int(config.get("max_image_cache_per_fetch") or 40), 80))
    referer = str(config.get("detail_origin") or config.get("base_url") or "")
    resolved = 0
    cached_images = 0
    try:
        for item in items:
            copy = dict(item)
            tid = _extract_torrent_id(copy)
            if tid and not _read_cover_cache(tid, ttl_days) and resolved < max_resolve:
                resolved += 1
                api_url, is_dmm = await _resolve_cover_via_mteam_api(copy, config, min(timeout, 3.0))
                if api_url:
                    _write_cover_cache(tid, api_url, is_dmm)
                    copy["image_url"] = api_url
                    copy["image_source"] = "dmm" if is_dmm else (urlparse(api_url).hostname or "")
                    if is_dmm:
                        copy["is_av"] = True
            original = str(copy.get("original_image_url") or copy.get("image_url") or "")
            if original and cached_images < max_images and not _local_cached_image_url(PLUGIN_ID, original, ttl_days):
                cached = await _ensure_cached_image(PLUGIN_ID, original, ttl_days, min(timeout, 3.0), referer=referer)
                if cached:
                    cached_images += 1
            if resolved >= max_resolve and cached_images >= max_images:
                break
        _BACKGROUND_LAST_ERROR = ""
    except Exception as exc:
        _BACKGROUND_LAST_ERROR = str(exc)
        raise
    finally:
        _BACKGROUND_LAST_FINISHED_AT = dt.datetime.now(dt.timezone.utc).isoformat()


def _schedule_background_refresh(items: list[dict[str, Any]], config: dict[str, Any], ttl_days: int, timeout: float) -> None:
    global _BACKGROUND_LAST_STARTED_AT
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _BACKGROUND_LAST_STARTED_AT = dt.datetime.now(dt.timezone.utc).isoformat()
    task = loop.create_task(_background_cover_refresh(items[:50], dict(config), ttl_days, timeout))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(lambda t: _BACKGROUND_TASKS.discard(t))


async def background_tasks(config: dict[str, Any]) -> list[dict[str, Any]]:
    running = len([task for task in _BACKGROUND_TASKS if not task.done()])
    status = "running" if running else ("failed" if _BACKGROUND_LAST_ERROR else "idle")
    max_resolve = max(0, min(int(config.get("max_api_cover_resolve") or 40), 80))
    max_images = max(0, min(int(config.get("max_image_cache_per_fetch") or 40), 80))
    return [{
        "id": "mteam-plugin.cover-refresh",
        "plugin_id": PLUGIN_ID,
        "plugin_name": "M-Team",
        "title": "封面后台补缓存",
        "status": status,
        "enabled": True,
        "last_run_at": _BACKGROUND_LAST_STARTED_AT,
        "last_started_at": _BACKGROUND_LAST_STARTED_AT,
        "last_finished_at": _BACKGROUND_LAST_FINISHED_AT,
        "progress": None if running else (100 if _BACKGROUND_LAST_STARTED_AT else 0),
        "summary": f"活动任务 {running}，每次最多解析 {max_resolve} 个封面，缓存 {max_images} 张图片",
        "detail": _BACKGROUND_LAST_ERROR,
        "metrics": {
            "活动任务": running,
            "解析上限": max_resolve,
            "缓存上限": max_images,
            "缓存天数": int(config.get("cache_days") or 7),
        },
        "can_run": False,
    }]


def _image_cache_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _image_cache_dir(plugin_id: str) -> Path:
    return PLUGIN_CACHE_DIR / "images"


def _image_meta_path(plugin_id: str, image_id: str) -> Path:
    return _image_cache_dir(plugin_id) / f"{image_id}.json"


def _image_file_path(plugin_id: str, image_id: str, ext: str = ".img") -> Path:
    ext = ext if ext.startswith(".") and len(ext) <= 8 else ".img"
    return _image_cache_dir(plugin_id) / f"{image_id}{ext}"


def _guess_image_ext(url: str, content_type: str = "") -> str:
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) if content_type else None
    if guessed in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def get_cached_plugin_image(plugin_id: str, image_id: str) -> tuple[Path, str] | None:
    if not re.fullmatch(r"[a-f0-9]{64}", image_id or ""):
        return None
    meta_path = _image_meta_path(plugin_id, image_id)
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        filename = str(meta.get("filename") or "")
        mime = str(meta.get("mime") or "image/jpeg")
        path = _image_cache_dir(plugin_id) / filename
        if not path.exists() or not path.is_file():
            return None
        return path, mime
    except Exception:
        return None


def clear_cached_plugin_images(plugin_id: str) -> dict[str, Any]:
    image_dir = _image_cache_dir(plugin_id)
    if not image_dir.exists():
        return {"ok": True, "deleted_files": 0, "deleted_bytes": 0}
    deleted_files = 0
    deleted_bytes = 0
    for path in image_dir.rglob("*"):
        if path.is_file():
            try:
                deleted_bytes += path.stat().st_size
                deleted_files += 1
            except Exception:
                pass
    shutil.rmtree(image_dir, ignore_errors=True)
    return {"ok": True, "deleted_files": deleted_files, "deleted_bytes": deleted_bytes}


async def _ensure_cached_image(plugin_id: str, url: str, ttl_days: int, timeout: float, referer: str = "") -> dict[str, Any] | None:
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    image_id = _image_cache_id(url)
    meta_path = _image_meta_path(plugin_id, image_id)
    cached = _read_cache(meta_path, ttl_days)
    if cached:
        filename = str(cached.get("filename") or "")
        path = _image_cache_dir(plugin_id) / filename
        if filename and path.exists():
            return {"image_id": image_id, "filename": filename, "mime": cached.get("mime") or "image/jpeg"}

    headers = {"User-Agent": "Mozilla/5.0 NOOR image cache"}
    if referer:
        headers["Referer"] = referer
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if not content_type.lower().startswith("image/"):
            return None
        data = resp.content
        if not data:
            return None
        # Keep this cache for thumbnails/backdrops; avoid accidentally storing huge files.
        if len(data) > 15 * 1024 * 1024:
            return None

    ext = _guess_image_ext(url, content_type)
    filename = f"{image_id}{ext}"
    image_dir = _image_cache_dir(plugin_id)
    image_dir.mkdir(parents=True, exist_ok=True)
    file_path = image_dir / filename
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(file_path)
    mime = content_type.split(";")[0].strip() or mimetypes.guess_type(filename)[0] or "image/jpeg"
    meta = {"created_at": time.time(), "source_url": url, "filename": filename, "mime": mime, "size": len(data)}
    _write_cache(meta_path, meta)
    return {"image_id": image_id, "filename": filename, "mime": mime}


async def _cache_feed_images(plugin_id: str, items: list[dict[str, Any]], ttl_days: int, timeout: float, max_images: int = 24, referer: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cached_count = 0
    for item in items:
        copy = dict(item)
        original = str(copy.get("original_image_url") or copy.get("image_url") or "")
        copy["original_image_url"] = original
        if original:
            if cached_count < max_images:
                try:
                    cached = await _ensure_cached_image(plugin_id, original, ttl_days, min(timeout, 3.0), referer=referer)
                    if cached:
                        cached_count += 1
                        copy["cached_image_id"] = cached["image_id"]
                        copy["image_url"] = f"/api/plugins/{plugin_id}/images/{cached['image_id']}"
                        copy["image_cached"] = True
                    else:
                        copy["image_url"] = ""
                        copy["image_cached"] = False
                except Exception:
                    copy["image_url"] = ""
                    copy["image_cached"] = False
            else:
                copy["image_url"] = ""
                copy["image_cached"] = False
        out.append(copy)
    return out


def _image_urls(raw_html: str) -> list[str]:
    if not raw_html:
        return []
    return [_clean_url(html.unescape(m.strip())) for m in _IMG_RE.findall(raw_html) if m.strip()]


def _clean_url(url: str) -> str:
    return (url or "").strip().rstrip(").,;")


def _first_image_url(raw_html: str) -> str:
    urls = _image_urls(raw_html)
    return urls[0] if urls else ""


def _first_dmm_image_url(raw_html: str) -> str:
    for url in _image_urls(raw_html):
        if _is_dmm_image(url):
            return url
    return ""


def _strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = _TAG_RE.sub(" ", raw_html)
    return html.unescape(re.sub(r"\s+", " ", text).strip())


def _is_dmm_image(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(_clean_url(url))
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
    except Exception:
        return False
    is_dmm = host == "dmm.co.jp" or host.endswith(".dmm.co.jp") or host == "dmm.com" or host.endswith(".dmm.com")
    return is_dmm and path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"))



def _extract_torrent_id(item: dict[str, Any]) -> int | None:
    for key in ("guid", "id", "torrentId"):
        raw = str(item.get(key) or "").strip()
        if raw.isdigit():
            return int(raw)
    for key in ("link", "comments"):
        raw = str(item.get(key) or "")
        match = re.search(r"(?:detail/|[?&]id=)(\d+)", raw)
        if match:
            return int(match.group(1))
    return None


def _first_http_image_from_value(value: Any) -> str:
    if isinstance(value, str):
        urls = _image_urls(value)
        if urls:
            return urls[0]
        match = re.search(r'https?://[^\s"\'<>\]]+', value)
        return _clean_url(match.group(0)) if match else ""
    if isinstance(value, dict):
        priority = ("imageList", "cover", "image", "img", "poster", "smallCover", "bigCover", "url", "description", "descr")
        for key in priority:
            if key in value:
                found = _first_http_image_from_value(value[key])
                if found:
                    return found
        for child in value.values():
            found = _first_http_image_from_value(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _first_http_image_from_value(child)
            if found:
                return found
    return ""


def _find_dmm_image_in_value(value: Any) -> str:
    if isinstance(value, str):
        for url in _image_urls(value):
            if _is_dmm_image(url):
                return url
        if _is_dmm_image(value):
            return _clean_url(value)
        match = re.search(r"https?://[^\s\"'<>]+", value)
        if match and _is_dmm_image(match.group(0)):
            return _clean_url(match.group(0))
        return ""
    if isinstance(value, dict):
        priority = ("imageList", "cover", "image", "img", "poster", "smallCover", "bigCover", "url", "description", "descr")
        for key in priority:
            if key in value:
                found = _find_dmm_image_in_value(value[key])
                if found:
                    return found
        for child in value.values():
            found = _find_dmm_image_in_value(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_dmm_image_in_value(child)
            if found:
                return found
    return ""


async def _resolve_cover_via_mteam_api(item: dict[str, Any], config: dict[str, Any], timeout: float) -> tuple[str, bool]:
    token = (config.get("api_key") or "").strip()
    if not token:
        return "", False
    torrent_id = _extract_torrent_id(item)
    if not torrent_id:
        return "", False
    base_url = (config.get("base_url") or "https://test2.m-team.cc").rstrip("/")
    origin = (config.get("detail_origin") or "https://kp.m-team.cc").strip()
    headers = {"Accept": "application/json", "x-api-key": token}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
            resp = await client.post(f"{base_url}/api/torrent/detail", headers=headers, params={"id": torrent_id, "origin": origin})
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return "", False
    dmm = _find_dmm_image_in_value(data)
    if dmm:
        return dmm, True
    any_image = _first_http_image_from_value(data)
    return any_image, False


async def _fetch_rss_url(rss_url: str, timeout: float) -> list[dict[str, Any]]:
    if not rss_url:
        return []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        resp = await client.get(rss_url)
        resp.raise_for_status()
    root = ET.fromstring(resp.text)
    return [_normalize_rss_item(item) for item in root.findall(".//item")]


async def _enrich_covers_via_api(items: list[dict[str, Any]], config: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    if not config.get("resolve_dmm_cover_via_api", True):
        return items
    max_resolve = max(0, min(int(config.get("max_api_cover_resolve") or 12), 50))
    out: list[dict[str, Any]] = []
    resolved_attempts = 0
    for item in items:
        copy = dict(item)
        needs_api_cover = (not copy.get("is_av")) or not copy.get("image_url") or "img.m-team.cc" in str(copy.get("image_url") or "")
        if needs_api_cover and resolved_attempts < max_resolve:
            resolved_attempts += 1
            api_url, is_dmm = await _resolve_cover_via_mteam_api(copy, config, min(timeout, 3.0))
            if api_url:
                copy["api_resolved_image_url"] = api_url
                copy["image_url"] = api_url
                copy["image_source"] = "dmm" if is_dmm else (urlparse(api_url).hostname or "")
                if is_dmm:
                    copy["is_av"] = True
        out.append(copy)
    return out


def _is_likely_av_category(item: dict[str, Any]) -> bool:
    text = f"{item.get('category') or ''} {item.get('category_domain') or ''}".lower()
    return "cat=429" in text or "av" in text or "adult" in text or "uncensored" in text or "無碼" in text or "有码" in text or "有碼" in text


def _normalize_rss_item(item: ET.Element) -> dict[str, Any]:
    description = (item.findtext("description") or "").strip()
    image_url = _first_dmm_image_url(description) or _first_image_url(description)
    enclosure = item.find("enclosure")
    enclosure_url = html.unescape(enclosure.attrib.get("url", "").strip()) if enclosure is not None else ""
    enclosure_type = enclosure.attrib.get("type", "").strip() if enclosure is not None else ""
    size_bytes = 0
    if enclosure is not None:
        try:
            size_bytes = int(enclosure.attrib.get("length") or 0)
        except Exception:
            size_bytes = 0
    category = (item.findtext("category") or "").strip()
    category_el = item.find("category")
    category_domain = category_el.attrib.get("domain", "").strip() if category_el is not None else ""
    return _with_display_title({
        "title": html.unescape((item.findtext("title") or "").strip()),
        "link": html.unescape((item.findtext("link") or "").strip()),
        "pubDate": (item.findtext("pubDate") or "").strip(),
        "guid": html.unescape((item.findtext("guid") or "").strip()),
        "description": description,
        "plain_description": _strip_html(description),
        "image_url": image_url,
        "image_source": "dmm" if _is_dmm_image(image_url) else (urlparse(image_url).hostname or "" if image_url else ""),
        "is_av": _is_dmm_image(image_url),
        "category": html.unescape(category),
        "category_domain": html.unescape(category_domain),
        "size_bytes": size_bytes,
        "enclosure_url": enclosure_url,
        "enclosure_type": enclosure_type,
        "download_url": enclosure_url,
    })


def _normalize_api_item(it: dict[str, Any], base_url: str) -> dict[str, Any]:
    title = str(it.get("name") or it.get("title") or it.get("subject") or "").strip()
    link = str(it.get("link") or it.get("url") or it.get("downloadUrl") or "").strip()
    tid = it.get("id") or it.get("torrentId")
    if not link and tid and base_url:
        link = f"{base_url}/detail/{tid}"
    pub = str(it.get("createdDate") or it.get("createdAt") or it.get("pubDate") or "")
    description = str(it.get("smallDescr") or it.get("description") or "")
    image_url = str(it.get("image") or it.get("cover") or it.get("poster") or it.get("imageUrl") or "").strip()
    return _with_display_title({
        "title": title,
        "link": link,
        "pubDate": pub,
        "guid": str(tid or it.get("guid") or ""),
        "description": description,
        "plain_description": _strip_html(description),
        "image_url": image_url,
        "image_source": "dmm" if _is_dmm_image(image_url) else (urlparse(image_url).hostname or "" if image_url else ""),
        "is_av": _is_dmm_image(image_url),
    })


def _apply_feed_filters(items: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    if bool(config.get("filter_dmm_images", False)):
        # DMM image is the strongest signal. For M-Team RSS category pages, category 429/AV
        # is also a legal AV signal; cover can be resolved through the official API later.
        return [x for x in items if x.get("is_av") or _is_likely_av_category(x)]
    return items



async def fetch_rss_items(manifest: PluginManifest, config: dict[str, Any], limit: int = 30, *, force_refresh: bool = False) -> dict[str, Any]:
    mode = (config.get("mode") or "rss").strip().lower()
    timeout = float(config.get("timeout", 15))
    image_cache_days = int(config.get("cache_days") or 7)
    limit = max(1, min(int(limit or 30), 500))

    rss_url = (config.get("rss_url") or "").strip()
    fetched_items: list[dict[str, Any]] = []
    source = ""

    if rss_url:
        try:
            fetched_items = await _fetch_rss_url(rss_url, timeout)
            source = "rss"
        except Exception:
            fetched_items = []

    if not fetched_items and (mode == "api" or config.get("api_token") or config.get("api_key")):
        base_url = (config.get("base_url") or "").rstrip("/")
        path = config.get("api_search_path") or "/api/torrent/search"
        method = (config.get("api_method") or "POST").upper()
        if base_url:
            headers = {"Accept": "application/json"}
            token = (config.get("api_key") or "").strip()
            if token:
                headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
            size = max(limit, 50)
            payload = {"pageNumber": 1, "pageSize": min(size, 100), "visible": 1}
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
                if method == "GET":
                    resp = await client.get(f"{base_url}{path}", headers=headers, params=payload)
                else:
                    resp = await client.post(f"{base_url}{path}", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            raw_items: list[Any] = []
            if isinstance(data, dict):
                for key in ("data", "items", "results", "rows"):
                    val = data.get(key)
                    if isinstance(val, list):
                        raw_items = val
                        break
                if not raw_items and isinstance(data.get("data"), dict):
                    inner = data.get("data")
                    for key in ("data", "items", "results", "rows"):
                        val = inner.get(key)
                        if isinstance(val, list):
                            raw_items = val
                            break
            elif isinstance(data, list):
                raw_items = data
            fetched_items = [_normalize_api_item(it, base_url) for it in raw_items if isinstance(it, dict)]
            source = "api"

    fetched_items = _apply_known_cover_cache(fetched_items, image_cache_days)
    filtered = _apply_feed_filters(fetched_items, config)
    visible = _attach_local_cached_images(manifest.id, filtered, image_cache_days)
    _schedule_background_refresh(filtered, config, image_cache_days, timeout)
    image_cached_count = sum(1 for x in visible if x.get("image_cached"))
    return {
        "items": visible[:limit],
        "total": len(visible),
        "source": source,
        "image_cached_count": image_cached_count,
        "image_cache_days": image_cache_days,
        "background_refresh": True,
    }


async def test(config: dict[str, Any]) -> PluginTestResult:
    try:
        manifest = PluginManifest(id=PLUGIN_ID, name="M-Team", type="rss_source")
        data = await fetch_rss_items(manifest, config, limit=3, force_refresh=True)
        return PluginTestResult(ok=True, message="rss reachable", details={"items": len(data.get("items", [])), "total": data.get("total", 0), "image_cached_count": data.get("image_cached_count", 0)})
    except Exception as e:
        return PluginTestResult(ok=False, message=f"rss failed: {e}")


def _resource_code(value: Any) -> str:
    text = str(value or "").upper().replace("_", "-")
    match = re.search(r"\b([A-Z]{2,10})[- ]?(\d{2,8})\b", text)
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def _resource_number(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _resource_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for path in (("data", "data"), ("data", "items"), ("data",), ("items",)):
        current: Any = payload
        for key in path:
            current = current.get(key) if isinstance(current, dict) else None
        if isinstance(current, list):
            return [item for item in current if isinstance(item, dict)]
    return []


def _resource_download_url(config: dict[str, Any], item: dict[str, Any]) -> str:
    for key in ("downloadUrl", "download_url", "url", "torrentUrl", "torrent_url"):
        value = str(item.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    torrent_id = str(item.get("id") or item.get("torrentId") or "").strip()
    passkey = str(config.get("passkey") or "").strip()
    return f"https://kp.m-team.cc/download/{torrent_id}/{passkey}" if torrent_id and passkey else ""


def _normalize_resource(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    title = str(item.get("name") or item.get("title") or "").strip()
    torrent_id = str(item.get("id") or item.get("torrentId") or "").strip()
    if not title or not torrent_id:
        return None
    code = next((value for value in (
        _resource_code(item.get("smallDescr")),
        _resource_code(item.get("description")),
        _resource_code(title),
        _resource_code((item.get("dmmInfo") or {}).get("productNumber") if isinstance(item.get("dmmInfo"), dict) else ""),
        _resource_code(item.get("dmmCode")),
    ) if value), "")
    discount = str(item.get("discount") or item.get("discountType") or "")
    tags = ["PT", *([discount] if discount else []), *(["置顶"] if item.get("sticky") else [])]
    return {
        "id": f"mteam:{torrent_id}",
        "kind": "torrent",
        "query_key": code or title,
        "title": title,
        "subtitle": str(item.get("createdDate") or item.get("created_at") or ""),
        "url": _resource_download_url(config, item),
        "size_bytes": _resource_number(item.get("size") or item.get("sizeBytes") or item.get("fileSize")),
        "file_count": _resource_number(item.get("numFiles") or item.get("fileCount")),
        "tags": tags,
        "cover_url": str(item.get("poster") or item.get("image") or item.get("cover") or ""),
        "source_url": str(item.get("detailUrl") or item.get("detail_url") or ""),
        "features": {"has_subtitle": False, "is_cracked": False, "is_private_tracker": True},
        "requirements": {"accepts_private_tracker": True, "accepts_http_torrent": True},
        "compatible_downloaders": ["qbittorrent", "transmission"],
        "preferred_downloader": "qbittorrent",
        "metadata": {"source_plugin": PLUGIN_ID, "torrent_id": torrent_id, "video_code": code, "site": "M-Team", "raw": item},
    }


async def search_resources(query: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    keyword = str(query.get("keyword") or query.get("q") or query.get("code") or query.get("number") or "").strip()
    if not keyword:
        return {"items": []}
    limit = max(5, min(_resource_number(query.get("limit")) or 30, 100))
    payload = await _mteam_post(config, "/api/torrent/search", json_body={
        "pageNumber": max(1, _resource_number(query.get("page")) or 1),
        "pageSize": limit,
        "keyword": keyword,
        "mode": "adult",
        "status": "NORMAL",
        "withCache": True,
    })
    resources = [_normalize_resource(config, item) for item in _resource_items(payload)]
    return {"items": [item for item in resources if item], "raw": payload}


async def resolve_resource_download(resource: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    url = str(resource.get("url") or "").strip()
    if not url:
        raise ValueError("M-Team 资源没有可用下载地址；请填写 Passkey 或确认 API Key 权限")
    return {"item": resource, "url": url}

def get_cached_image(image_id: str) -> tuple[Path, str] | None:
    return get_cached_plugin_image(PLUGIN_ID, image_id)

def clear_image_cache() -> dict[str, Any]:
    result = clear_cached_plugin_images(PLUGIN_ID)
    covers_dir = PLUGIN_CACHE_DIR / "covers"
    cover_files = 0
    if covers_dir.exists():
        cover_files = sum(1 for x in covers_dir.rglob("*") if x.is_file())
        shutil.rmtree(covers_dir, ignore_errors=True)
    result["deleted_cover_cache_files"] = cover_files
    return result


def _subtitle_cache_path(subtitle_id: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(subtitle_id or ""))
    return PLUGIN_CACHE_DIR / "subtitles" / f"mteam-{safe_id}.srt"


def _read_subtitle_cache(subtitle_id: str) -> dict[str, Any] | None:
    path = _subtitle_cache_path(subtitle_id)
    if not path.exists() or not path.is_file():
        return None
    content = path.read_bytes()
    return {"content": content.decode("utf-8", errors="replace"), "filename": path.name, "bytes": content}


def _write_subtitle_cache(subtitle_id: str, content: bytes) -> None:
    path = _subtitle_cache_path(subtitle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

async def search_subtitles(config: dict[str, Any], video_code: str) -> list[dict[str, Any]]:
    if not config.get("subtitle_search_enabled"):
        return []
    base_url = (config.get("base_url") or "https://test2.m-team.cc").rstrip("/")
    token = (config.get("api_key") or "").strip()
    if not token or not video_code:
        return []
    headers = {"Accept": "application/json", "x-api-key": token}
    limit = max(1, min(int(config.get("subtitle_search_limit") or 20), 100))
    payload = {"pageNumber": 1, "pageSize": limit, "keyword": video_code}
    async with httpx.AsyncClient(timeout=float(config.get("timeout", 15)), follow_redirects=True, trust_env=False) as client:
        resp = await client.post(f"{base_url}/api/subtitle/search", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    raw_items: list[Any] = []
    if isinstance(data, dict):
        val = data.get("data")
        if isinstance(val, dict):
            for key in ("data", "items", "results", "rows"):
                if isinstance(val.get(key), list):
                    raw_items = val[key]
                    break
        elif isinstance(val, list):
            raw_items = val
        if not raw_items:
            for key in ("items", "results", "rows"):
                if isinstance(data.get(key), list):
                    raw_items = data[key]
                    break
    elif isinstance(data, list):
        raw_items = data
    out: list[dict[str, Any]] = []
    verify_downloads = bool(config.get("verify_subtitle_downloads", True))
    for item in raw_items[:limit]:
        if not isinstance(item, dict):
            continue
        sid = item.get("id") or item.get("subtitleId")
        name = str(item.get("filename") or item.get("name") or item.get("title") or "").strip()
        ext = str(item.get("ext") or Path(name).suffix.lstrip(".") or "srt").strip(".").lower()
        if not sid or not name:
            continue
        if verify_downloads:
            try:
                await fetch_subtitle_content(config, str(sid))
            except Exception:
                continue
        out.append({
            "id": f"mteam-subtitle:{sid}",
            "filename": name,
            "ext": f".{ext}" if ext else ".srt",
            "language": _mteam_subtitle_lang(item.get("lang")),
            "source": "M-Team",
            "source_key": PLUGIN_ID,
            "source_type": "remote_search",
            "url": f"mteam://subtitle/{sid}",
            "score": _mteam_subtitle_score(video_code, item),
        })
    return out


def _mteam_subtitle_lang(lang: Any) -> str:
    # M-Team uses numeric language ids; common Chinese ids observed in API are 25/28.
    value = str(lang or "").strip()
    return {"25": "中文", "28": "中文"}.get(value, value or "未知")


def _mteam_subtitle_score(video_code: str, item: dict[str, Any]) -> float:
    haystack = f"{item.get('name') or ''} {item.get('filename') or ''}".lower()
    code = (video_code or "").lower()
    if code and code in haystack:
        return 0.92
    return 0.62


async def fetch_subtitle_content(config: dict[str, Any], subtitle_id: str) -> dict[str, Any]:
    base_url = (config.get("base_url") or "https://test2.m-team.cc").rstrip("/")
    token = (config.get("api_key") or "").strip()
    if not token or not subtitle_id:
        raise ValueError("missing m-team api key or subtitle id")
    cached = _read_subtitle_cache(subtitle_id)
    if cached:
        return cached
    headers = {"Accept": "application/octet-stream,application/json", "x-api-key": token}
    async with httpx.AsyncClient(timeout=float(config.get("timeout", 30)), follow_redirects=True, trust_env=False) as client:
        resp = await client.get(f"{base_url}/api/subtitle/dl", headers=headers, params={"id": subtitle_id})
        resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        data = resp.json()

        raise ValueError(str(data.get("message") or "m-team subtitle download failed"))
    filename = f"mteam-{subtitle_id}.srt"
    _write_subtitle_cache(subtitle_id, resp.content)
    return {"content": resp.text, "filename": filename, "bytes": resp.content}

_ALBUM_ID_RE = re.compile(r"(?:albumId=|album/|albumId/|id=)(\d+)")


def _albums_store_path() -> Path:
    return PLUGIN_CACHE_DIR / "albums.json"


def _normalize_album_entry(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        raw_id = value.get("album_id") or value.get("id")
        rss_url = str(value.get("rss_url") or "").strip()
        alias = str(value.get("alias") or "").strip()
        album_id = int(raw_id) if str(raw_id or "").isdigit() else _parse_album_id(rss_url)
        if not album_id:
            return None
        return {"album_id": album_id, "rss_url": rss_url, "alias": alias}
    if str(value or "").isdigit():
        return {"album_id": int(value), "rss_url": "", "alias": ""}
    return None


def _read_album_entries() -> list[dict[str, Any]]:
    path = _albums_store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("albums") if isinstance(data, dict) and isinstance(data.get("albums"), list) else None
        if raw is None:
            raw = data.get("album_ids", []) if isinstance(data, dict) else data
        entries: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            entry = _normalize_album_entry(item)
            if entry and all(x["album_id"] != entry["album_id"] for x in entries):
                entries.append(entry)
        return entries
    except Exception:
        return []


def _write_album_entries(entries: list[dict[str, Any]]) -> None:
    path = _albums_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean: list[dict[str, Any]] = []
    for entry in entries:
        normalized = _normalize_album_entry(entry)
        if not normalized:
            continue
        idx = next((i for i, x in enumerate(clean) if x["album_id"] == normalized["album_id"]), -1)
        if idx >= 0:
            if normalized.get("rss_url"):
                clean[idx]["rss_url"] = normalized["rss_url"]
            clean[idx]["alias"] = normalized.get("alias") or clean[idx].get("alias") or ""
        else:
            clean.append(normalized)
    path.write_text(json.dumps({"albums": clean}, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_album_ids() -> list[int]:
    return [int(x["album_id"]) for x in _read_album_entries()]


def _write_album_ids(ids: list[int]) -> None:
    _write_album_entries([{"album_id": int(album_id), "rss_url": ""} for album_id in ids])


def _parse_album_id(raw: str) -> int | None:
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw)
    query_id = parse_qs(urlparse(raw).query).get("albumId", [""])[0]
    if str(query_id).isdigit():
        return int(query_id)
    match = _ALBUM_ID_RE.search(raw)
    if match:
        return int(match.group(1))
    return None


def _mteam_headers(config: dict[str, Any]) -> dict[str, str]:
    token = (config.get("api_key") or "").strip()
    return {"Accept": "application/json", "x-api-key": token}


async def _mteam_post(config: dict[str, Any], path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url = (config.get("base_url") or "https://test2.m-team.cc").rstrip("/")
    async with httpx.AsyncClient(timeout=float(config.get("timeout", 15)), follow_redirects=True, trust_env=False) as client:
        resp = await client.post(f"{base_url}{path}", headers=_mteam_headers(config), params=params, json=json_body)
        resp.raise_for_status()
        data = resp.json()
    if str(data.get("code")) not in {"0", "SUCCESS"}:
        raise ValueError(data.get("message") or "M-Team API failed")
    return data.get("data") or {}


def _normalize_album_torrent(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    base_url = (config.get("base_url") or "https://test2.m-team.cc").rstrip("/")
    tid = str(item.get("id") or item.get("torrentId") or "")
    title = str(item.get("name") or item.get("title") or item.get("subject") or "").strip()
    image_list = item.get("imageList") if isinstance(item.get("imageList"), list) else []
    image_url = ""
    for url in image_list:
        if isinstance(url, str) and _is_dmm_image(url):
            image_url = url
            break
    if not image_url and image_list:
        image_url = str(image_list[0] or "")
    size = 0
    try:
        size = int(item.get("size") or 0)
    except Exception:
        size = 0
    category = str(item.get("category") or "")
    return _with_display_title({
        "title": title,
        "link": f"{base_url}/detail/{tid}" if tid else base_url,
        "pubDate": str(item.get("createdDate") or ""),
        "guid": tid,
        "description": str(item.get("smallDescr") or ""),
        "plain_description": str(item.get("smallDescr") or ""),
        "image_url": image_url,
        "image_source": "dmm" if _is_dmm_image(image_url) else (urlparse(image_url).hostname or "" if image_url else ""),
        "is_av": True,
        "category": category,
        "size_bytes": size,
        "download_url": "",
        "enclosure_url": "",
    })


def _is_mteam_rss_url(raw: str) -> bool:
    if not raw:
        return False
    parsed = urlparse(raw)
    return parsed.scheme in {"http", "https"} and "/api/rss/fetch" in parsed.path


async def _load_album(config: dict[str, Any], album_id: int, rss_url: str = "", alias: str = "") -> dict[str, Any]:
    detail = await _mteam_post(config, "/api/album/albumDetail", params={"albumId": album_id})
    timeout = float(config.get("timeout", 15))
    items: list[dict[str, Any]] = []
    source = "api"
    if _is_mteam_rss_url(rss_url):
        try:
            items = await _fetch_rss_url(rss_url, timeout)
            items = _apply_known_cover_cache(items, int(config.get("cache_days") or 7))
            items = await _enrich_covers_via_api(items, config, timeout)
            source = "rss"
        except Exception:
            items = []
            source = "api"

    if not items:
        torrents_data = await _mteam_post(
            config,
            "/api/album/albumTorrentSearch",
            json_body={"pageNumber": 1, "pageSize": 100, "albumId": album_id, "status": "NORMAL"},
        )
        raw_items = torrents_data.get("data") if isinstance(torrents_data, dict) else []
        items = [_normalize_album_torrent(x, config) for x in raw_items if isinstance(x, dict)]

    image_cache_days = int(config.get("cache_days") or 7)
    referer = str(config.get("detail_origin") or config.get("base_url") or "")
    max_images = max(0, min(int(config.get("max_image_cache_per_fetch") or 2), 20))
    items = await _cache_feed_images(
        PLUGIN_ID,
        items,
        image_cache_days,
        timeout,
        max_images=max_images,
        referer=referer,
    )
    _schedule_background_refresh(items, config, image_cache_days, float(config.get("timeout", 15)))
    original_title = str(detail.get("title") or f"片单 {album_id}")
    return {
        "id": str(album_id),
        "title": alias or original_title,
        "original_title": original_title,
        "alias": alias,
        "intro": str(detail.get("intro") or ""),
        "count": int(detail.get("count") or len(items) or 0),
        "size": int(detail.get("size") or 0),
        "items": items,
        "source": source,
        "rss_url": rss_url,
    }


async def _list_albums(config: dict[str, Any]) -> dict[str, Any]:
    entries = _read_album_entries()
    if not entries:
        # Give the page a useful first state in test/dev without forcing the user to know an id.
        try:
            data = await _mteam_post(config, "/api/album/albumSearch", json_body={"pageNumber": 1, "pageSize": 10, "status": "NORMAL", "adult": True})
            raw = data.get("data") if isinstance(data, dict) else []
            entries = [{"album_id": int(x.get("id")), "rss_url": ""} for x in raw if isinstance(x, dict) and str(x.get("id") or "").isdigit()]
        except Exception:
            entries = []
    albums = []
    for entry in entries[:10]:
        album_id = int(entry["album_id"])
        try:
            albums.append(await _load_album(config, album_id, str(entry.get("rss_url") or ""), str(entry.get("alias") or "")))
        except Exception as e:
            albums.append({"id": str(album_id), "title": f"片单 {album_id}", "intro": str(e), "count": 0, "size": 0, "items": []})
    return {"albums": albums}


async def _add_album(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    raw = str(payload.get("url") or payload.get("album_url") or payload.get("albumId") or payload.get("album_id") or "")
    album_id = _parse_album_id(raw)
    if not album_id:
        raise ValueError("无法从片单地址中识别 albumId")
    rss_url = raw.strip() if _is_mteam_rss_url(raw.strip()) else ""
    entries = _read_album_entries()
    idx = next((i for i, x in enumerate(entries) if int(x["album_id"]) == album_id), -1)
    if idx >= 0:
        if rss_url:
            entries[idx]["rss_url"] = rss_url
    else:
        entries.append({"album_id": album_id, "rss_url": rss_url})
    _write_album_entries(entries)
    entry = next((x for x in entries if int(x["album_id"]) == album_id), {"rss_url": "", "alias": ""})
    album = await _load_album(config, album_id, rss_url or str(entry.get("rss_url") or ""), str(entry.get("alias") or ""))
    return {"ok": True, "album": album, "albums": entries}


async def _rename_album(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    album_id = _parse_album_id(str(payload.get("album_id") or payload.get("id") or ""))
    if not album_id:
        raise ValueError("缺少 albumId")
    alias = str(payload.get("alias") or payload.get("title") or "").strip()[:80]
    entries = _read_album_entries()
    idx = next((i for i, x in enumerate(entries) if int(x["album_id"]) == album_id), -1)
    if idx < 0:
        raise ValueError("片单不存在")
    entries[idx]["alias"] = alias
    _write_album_entries(entries)
    album = await _load_album(config, album_id, str(entries[idx].get("rss_url") or ""), alias)
    return {"ok": True, "album": album, "albums": entries}


async def _remove_album(payload: dict[str, Any]) -> dict[str, Any]:
    album_id = _parse_album_id(str(payload.get("album_id") or payload.get("id") or ""))
    if not album_id:
        raise ValueError("缺少 albumId")
    entries = [x for x in _read_album_entries() if int(x["album_id"]) != album_id]
    _write_album_entries(entries)
    return {"ok": True, "album_id": str(album_id), "albums": entries}


async def handle_action(action: str, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if action in {"search", "resource_search"}:
        return await search_resources(payload, config)
    if action == "albums":
        return await _list_albums(config)
    if action == "add_album":
        return await _add_album(config, payload)
    if action == "rename_album":
        return await _rename_album(config, payload)
    if action == "remove_album":
        return await _remove_album(payload)
    raise ValueError(f"unsupported action: {action}")
