from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from app.plugins.secrets import plugin_secret_store

from .client import PLUGIN_ID
from .storage import get_media, register_service_token, save_strm

DEFAULT_MEDIA_EXTENSIONS = {"mkv", "mp4", "avi", "mov", "ts", "m2ts", "wmv", "flv", "webm"}
_url_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def media_extensions(config: dict[str, Any]) -> set[str]:
    value = str(config.get("media_extensions") or "")
    parsed = {part.strip().lower().lstrip(".") for part in value.split(",") if part.strip()}
    return parsed or set(DEFAULT_MEDIA_EXTENSIONS)


def is_media_file(item: dict[str, Any], config: dict[str, Any]) -> bool:
    if item.get("is_directory"):
        return False
    extension = str(item.get("extension") or Path(str(item.get("name") or "")).suffix).lower().lstrip(".")
    return extension in media_extensions(config)


def safe_stem(name: str) -> str:
    stem = Path(str(name or "")).stem
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", stem).strip(" .")
    return stem[:180] or "115-media"


def stream_token() -> str:
    current = plugin_secret_store.get_all(PLUGIN_ID).get("stream_service_token", "")
    if not current:
        current = secrets.token_urlsafe(32)
        plugin_secret_store.set(PLUGIN_ID, "stream_service_token", current)
    register_service_token(hashlib.sha256(current.encode()).hexdigest())
    return current


def validate_stream_token(value: str) -> bool:
    from .storage import service_token_valid

    if not value:
        return False
    return service_token_valid(hashlib.sha256(value.encode()).hexdigest())


def build_stream_url(config: dict[str, Any], file_id: str, token: str) -> str:
    base = str(config.get("public_base_url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("请先配置 NOOR 外部访问地址")
    return f"{base}/api/plugins/115/stream/{file_id}?token={token}"


def create_strm(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    raw_directory = str(config.get("strm_directory") or "").strip()
    if not raw_directory:
        raise ValueError("请先配置 STRM incoming 目录")
    directory = Path(raw_directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    file_id = str(item.get("file_id") or "")
    token = stream_token()
    url = build_stream_url(config, file_id, token)
    target = directory / f"{safe_stem(str(item.get('name') or ''))}.strm"
    if target.exists():
        existing = target.read_text(encoding="utf-8").strip()
        if f"/stream/{file_id}?" not in existing:
            target = directory / f"{safe_stem(str(item.get('name') or ''))} [{file_id}].strm"
    temporary = target.with_suffix(".strm.tmp")
    temporary.write_text(url + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(target)
    save_strm(file_id, str(target))
    return {"file_id": file_id, "path": str(target), "url": url, "status": "created"}


async def resolve_stream(config: dict[str, Any], file_id: str, *, token: str, user_agent: str) -> dict[str, Any]:
    if not validate_stream_token(token):
        raise PermissionError("invalid or revoked stream token")
    media = get_media(file_id)
    if media is None or not media.pick_code:
        raise FileNotFoundError("115 media file not found")
    ua_hash = hashlib.sha256(str(user_agent or "NOOR-stream-client").encode()).hexdigest()[:16]
    key = (str(file_id), ua_hash)
    cached = _url_cache.get(key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return dict(cached[1])
    from .client import Client115
    resolved = await Client115(config).download_url(media.pick_code, user_agent=user_agent)
    result = {"url": resolved["url"], "file_id": str(file_id)}
    ttl = max(30, min(int(config.get("stream_url_cache_seconds") or 180), 300))
    _url_cache[key] = (now + ttl, result)
    if len(_url_cache) > 256:
        for stale_key, value in list(_url_cache.items()):
            if value[0] <= now:
                _url_cache.pop(stale_key, None)
    return result
