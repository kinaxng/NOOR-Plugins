from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from app.core.runtime_paths import plugin_cache_path
from app.plugins.contracts import PluginTestResult

PLUGIN_ID = "gfriends"
CACHE_DIR = plugin_cache_path(PLUGIN_ID)
INDEX_PATH = CACHE_DIR / "index.json"
IMAGE_DIR = CACHE_DIR / "images"
DEFAULT_FILETREE_URL = "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json"
DEFAULT_ASSET_BASE_URL = "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Content/"


def _timeout(config: dict[str, Any]) -> float:
    try:
        return max(3.0, min(float(config.get("timeout") or 20), 120.0))
    except Exception:
        return 20.0


def _filetree_url(config: dict[str, Any]) -> str:
    return str(config.get("filetree_url") or DEFAULT_FILETREE_URL).strip()


def _asset_base_url(config: dict[str, Any]) -> str:
    return str(config.get("asset_base_url") or DEFAULT_ASSET_BASE_URL).strip().rstrip("/") + "/"


def _cache_days(config: dict[str, Any]) -> int:
    try:
        return max(1, min(int(config.get("cache_days") or 30), 365))
    except Exception:
        return 30


def _index_ttl_seconds(config: dict[str, Any]) -> int:
    try:
        return max(3600, min(int(config.get("index_ttl_hours") or 24) * 3600, 168 * 3600))
    except Exception:
        return 24 * 3600


def _normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"[\s\u3000·・_\-~～/／\\|,，.。'\"“”‘’]+", "", text)
    return text


def _name_candidates(value: str) -> list[str]:
    stem = Path(str(value or "")).stem.strip()
    names: list[str] = []
    if stem:
        names.append(stem)
        base = re.sub(r"[-_\s]*(?:\d{1,3}|[（(]\d{1,3}[）)])$", "", stem).strip()
        if base and base != stem:
            names.append(base)
    for match in re.finditer(r"[\(（]([^\)）]+)[\)）]", stem):
        for part in re.split(r"[/／,，、|]", match.group(1)):
            part = part.strip()
            if part:
                names.append(part)
    cleaned = re.sub(r"[\(（][^\)）]+[\)）]", "", stem).strip()
    if cleaned and cleaned != stem:
        names.append(cleaned)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = _normalize_name(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _record_score(record: dict[str, Any]) -> int:
    folder = str(record.get("folder") or "")
    file = str(record.get("file") or "")
    score = 0
    source_scores = [
        ("8-GRAPHIS", 100),
        ("y-AVDC", 95),
        ("z-DMM", 85),
        ("y-Minnano", 75),
        ("z-ラグジュTV", 65),
        ("z-Derekhsu", 20),
    ]
    for token, value in source_scores:
        if token in folder:
            score += value
            break
    if re.search(r"[-_]\d{1,3}\.", file):
        score += 3
    if "AI-Fix" in str(record.get("url") or ""):
        score += 1
    return score


def _choose_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {}
    return max(records, key=_record_score)


def _sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=_record_score, reverse=True)


def _payload_name_candidates(values: list[Any]) -> list[str]:
    names: list[str] = []
    for value in values:
        for part in re.split(r"[、,，/／|]+", str(value or "")):
            part = part.strip()
            if part:
                names.append(part)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = _normalize_name(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _content_url(base_url: str, folder: str, filename: str) -> str:
    clean_filename = str(filename or "").strip()
    filename_path, sep, filename_query = clean_filename.partition("?")
    path = "/".join(quote(part.strip("/")) for part in [folder, filename_path] if part)
    url = urljoin(base_url, path)
    if sep:
        url = f"{url}?{filename_query}"
    return url


def _build_index(filetree: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    content = filetree.get("Content") if isinstance(filetree, dict) else {}
    if not isinstance(content, dict):
        raise ValueError("Filetree.json 缺少 Content")
    base_url = _asset_base_url(config)
    alias_candidates: dict[str, list[dict[str, Any]]] = {}
    total_images = 0
    for folder, entries in content.items():
        if not isinstance(entries, dict):
            continue
        folder_name = str(folder or "").strip("/")
        for display_file, actual_file in entries.items():
            display = str(display_file or "").strip()
            actual = str(actual_file or display_file or "").strip()
            if not display or not actual:
                continue
            total_images += 1
            url = _content_url(base_url, folder_name, actual)
            names = _name_candidates(display)
            if not names:
                continue
            record = {
                "name": names[0],
                "aliases": names,
                "folder": folder_name,
                "file": display,
                "url": url,
            }
            for name in names:
                key = _normalize_name(name)
                if key:
                    alias_candidates.setdefault(key, []).append(record)
    aliases = {key: _choose_record(records) for key, records in alias_candidates.items()}
    candidate_aliases = {key: _sort_records(records)[:24] for key, records in alias_candidates.items()}
    info = filetree.get("Information") if isinstance(filetree.get("Information"), dict) else {}
    return {
        "created_at": time.time(),
        "source": _filetree_url(config),
        "asset_base_url": base_url,
        "information": info,
        "total_images": total_images,
        "alias_count": len(aliases),
        "aliases": aliases,
        "candidate_aliases": candidate_aliases,
        "candidate_count": sum(len(records) for records in alias_candidates.values()),
    }


def _read_index() -> dict[str, Any] | None:
    if not INDEX_PATH.exists():
        return None
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_index(index: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(INDEX_PATH)


async def _sync_index(config: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    cached = _read_index()
    if cached and not force and time.time() - float(cached.get("created_at") or 0) < _index_ttl_seconds(config):
        return cached
    async with httpx.AsyncClient(timeout=_timeout(config), follow_redirects=True, trust_env=False) as client:
        res = await client.get(_filetree_url(config), headers={"Accept": "application/json", "User-Agent": "NOOR-Gfriends/0.1"})
        res.raise_for_status()
        data = res.json()
    index = _build_index(data, config)
    _write_index(index)
    return index


def _image_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _image_meta_path(image_id: str) -> Path:
    return IMAGE_DIR / f"{image_id}.json"


def _guess_ext(url: str, content_type: str = "") -> str:
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if ext:
        return ".jpg" if ext == ".jpe" else ext
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"} else ".jpg"


def _read_image_meta(image_id: str, ttl_days: int) -> dict[str, Any] | None:
    path = _image_meta_path(image_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("created_at") or 0) > ttl_days * 86400:
            return None
        filename = str(data.get("filename") or "")
        if filename and (IMAGE_DIR / filename).exists():
            return data
    except Exception:
        return None
    return None


def _write_image_meta(image_id: str, payload: dict[str, Any]) -> None:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _image_meta_path(image_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_image_meta_path(image_id))


async def _cached_avatar_url(url: str, config: dict[str, Any]) -> str:
    if not url.lower().startswith(("http://", "https://")):
        return ""
    image_id = _image_id(url)
    ttl_days = _cache_days(config)
    meta = _read_image_meta(image_id, ttl_days)
    if meta:
        return f"/api/plugins/{PLUGIN_ID}/images/{image_id}"
    async with httpx.AsyncClient(timeout=_timeout(config), follow_redirects=True, trust_env=False) as client:
        res = await client.get(url, headers={"Accept": "image/*,*/*;q=0.8", "User-Agent": "NOOR-Gfriends/0.1"})
        res.raise_for_status()
        content_type = str(res.headers.get("content-type") or "image/jpeg")
        ext = _guess_ext(url, content_type)
        filename = f"{image_id}{ext}"
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        (IMAGE_DIR / filename).write_bytes(res.content)
    _write_image_meta(image_id, {"created_at": time.time(), "url": url, "filename": filename, "content_type": content_type})
    return f"/api/plugins/{PLUGIN_ID}/images/{image_id}"


async def _resolve(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    names = _payload_name_candidates([payload.get("name"), *(payload.get("aliases") if isinstance(payload.get("aliases"), list) else [])])
    keys = [_normalize_name(name) for name in names if _normalize_name(name)]
    if not keys:
        return {"ok": False, "matched": "", "url": "", "source": PLUGIN_ID}
    index = await _sync_index(config, force=False)
    aliases = index.get("aliases") if isinstance(index.get("aliases"), dict) else {}
    record = None
    matched_key = ""
    for key in keys:
        if key in aliases:
            record = aliases[key]
            matched_key = key
            break
    if not isinstance(record, dict):
        return {"ok": False, "matched": "", "url": "", "source": PLUGIN_ID, "index": _stats(index)}
    remote_url = str(record.get("url") or "")
    url = remote_url
    if config.get("cache_images", True):
        try:
            url = await _cached_avatar_url(remote_url, config) or remote_url
        except Exception:
            url = remote_url
    return {
        "ok": True,
        "url": url,
        "remote_url": remote_url,
        "source": PLUGIN_ID,
        "matched": record.get("name") or matched_key,
        "aliases": record.get("aliases") or [],
        "record": {k: record.get(k) for k in ("folder", "file")},
        "index": _stats(index),
    }


async def _candidates(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    names = _payload_name_candidates([payload.get("name"), *(payload.get("aliases") if isinstance(payload.get("aliases"), list) else [])])
    keys = [_normalize_name(name) for name in names if _normalize_name(name)]
    limit = max(1, min(int(payload.get("limit") or 24), 80))
    if not keys:
        return {"ok": False, "items": [], "index": _stats()}
    index = await _sync_index(config, force=False)
    if not isinstance(index.get("candidate_aliases"), dict):
        index = await _sync_index(config, force=True)
    aliases = index.get("aliases") if isinstance(index.get("aliases"), dict) else {}
    candidate_aliases = index.get("candidate_aliases") if isinstance(index.get("candidate_aliases"), dict) else {}
    collected: dict[str, dict[str, Any]] = {}
    for key in keys:
        for record in candidate_aliases.get(key) or []:
            if isinstance(record, dict) and str(record.get("url") or ""):
                collected.setdefault(str(record.get("url")), {**record, "matched_key": key})
        record = aliases.get(key)
        if isinstance(record, dict) and str(record.get("url") or ""):
            collected.setdefault(str(record.get("url")), {**record, "matched_key": key})
    # The compact runtime index stores the best image per alias. Walk those best
    # records to expose alternate source folders for the same submitted names.
    for key, record in aliases.items():
        if not isinstance(record, dict):
            continue
        record_names = [_normalize_name(name) for name in (record.get("aliases") or [])]
        if key in keys or any(name in keys for name in record_names):
            url = str(record.get("url") or "")
            if url:
                collected.setdefault(url, {**record, "matched_key": key})
    records = _sort_records(list(collected.values()))[:limit]
    items: list[dict[str, Any]] = []
    for record in records:
        remote_url = str(record.get("url") or "")
        url = remote_url
        if config.get("cache_images", True):
            try:
                url = await _cached_avatar_url(remote_url, config) or remote_url
            except Exception:
                url = remote_url
        items.append({
            "url": url,
            "remote_url": remote_url,
            "name": record.get("name") or "",
            "aliases": record.get("aliases") or [],
            "folder": record.get("folder") or "",
            "file": record.get("file") or "",
            "score": _record_score(record),
        })
    return {"ok": True, "items": items, "index": _stats(index)}


def _stats(index: dict[str, Any] | None = None) -> dict[str, Any]:
    data = index or _read_index() or {}
    return {
        "created_at": data.get("created_at") or 0,
        "source": data.get("source") or "",
        "total_images": int(data.get("total_images") or 0),
        "alias_count": int(data.get("alias_count") or 0),
        "information": data.get("information") if isinstance(data.get("information"), dict) else {},
    }


def get_cached_image(image_id: str):
    safe = re.sub(r"[^a-fA-F0-9]", "", str(image_id or ""))
    if not safe:
        return None
    meta = _read_image_meta(safe, 3650)
    if not meta:
        return None
    filename = str(meta.get("filename") or "")
    path = IMAGE_DIR / filename
    if not filename or not path.exists():
        return None
    return path, str(meta.get("content_type") or mimetypes.guess_type(path.name)[0] or "image/jpeg")


def clear_image_cache():
    count = 0
    if IMAGE_DIR.exists():
        for path in IMAGE_DIR.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)
                count += 1
    return {"ok": True, "deleted": count}


async def test(config: dict[str, Any]) -> PluginTestResult:
    index = await _sync_index(config, force=False)
    return PluginTestResult(ok=True, message="Gfriends index ready", details=_stats(index))


async def handle_action(action: str, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if action == "test":
        result = await test(config)
        return {"ok": result.ok, "message": result.message, "details": result.details or {}}
    if action == "sync":
        index = await _sync_index(config, force=bool(payload.get("force")))
        return {"ok": True, "index": _stats(index)}
    if action == "stats":
        index = await _sync_index(config, force=False) if payload.get("ensure") else _read_index()
        return {"ok": True, "index": _stats(index)}
    if action == "resolve":
        return await _resolve(config, payload)
    if action == "candidates":
        return await _candidates(config, payload)
    raise ValueError(f"unsupported action: {action}")
