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
from .storage import (
    get_media,
    list_strm_records,
    register_service_token,
    revoke_other_service_tokens,
    save_strm,
)

DEFAULT_MEDIA_EXTENSIONS = {
    "mkv",
    "mp4",
    "avi",
    "mov",
    "ts",
    "m2ts",
    "wmv",
    "flv",
    "webm",
}
_url_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def media_extensions(config: dict[str, Any]) -> set[str]:
    value = str(config.get("media_extensions") or "")
    parsed = {
        part.strip().lower().lstrip(".") for part in value.split(",") if part.strip()
    }
    return parsed or set(DEFAULT_MEDIA_EXTENSIONS)


def is_media_file(item: dict[str, Any], config: dict[str, Any]) -> bool:
    if item.get("is_directory"):
        return False
    extension = (
        str(item.get("extension") or Path(str(item.get("name") or "")).suffix)
        .lower()
        .lstrip(".")
    )
    return extension in media_extensions(config)


def safe_stem(name: str, *, max_bytes: int = 180) -> str:
    stem = Path(str(name or "")).stem
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", stem).strip(" .")
    # Filesystems limit one path component by bytes, not Unicode characters.
    encoded = stem.encode("utf-8")[:max_bytes]
    while encoded:
        try:
            stem = encoded.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            encoded = encoded[: exc.start]
    else:
        stem = ""
    return stem.rstrip(" .") or "115-media"


def stream_token() -> str:
    current = plugin_secret_store.get_all(PLUGIN_ID).get("stream_service_token", "")
    if not current:
        current = secrets.token_urlsafe(32)
        plugin_secret_store.set(PLUGIN_ID, "stream_service_token", current)
    register_service_token(hashlib.sha256(current.encode()).hexdigest())
    return current


def rotate_stream_token(config: dict[str, Any]) -> dict[str, int]:
    """Rotate the scoped credential and update every known STRM inode in place."""
    current = secrets.token_urlsafe(32)
    current_hash = hashlib.sha256(current.encode()).hexdigest()
    register_service_token(current_hash)
    rewritten = 0
    seen_inodes: set[tuple[int, int]] = set()
    for record in list_strm_records():
        media = get_media(record.file_id)
        paths = [record.local_path]
        if media is not None and str(media.organized_path or "").endswith(".strm"):
            paths.append(media.organized_path)
        url = build_stream_url(config, record.file_id, current)
        for raw_path in paths:
            path = Path(raw_path)
            try:
                stat = path.stat()
                inode = (stat.st_dev, stat.st_ino)
                if inode in seen_inodes:
                    continue
                # Preserve hardlinks created by MDC-NG. Atomic replacement here
                # would rotate only one directory entry and leave Emby's link stale.
                with path.open("r+", encoding="utf-8") as handle:
                    handle.seek(0)
                    handle.write(url + "\n")
                    handle.truncate()
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(path, 0o666)
                seen_inodes.add(inode)
                rewritten += 1
            except OSError:
                continue
    plugin_secret_store.set(PLUGIN_ID, "stream_service_token", current)
    revoke_other_service_tokens(current_hash)
    _url_cache.clear()
    return {"rewritten": rewritten}


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


def strm_config_fingerprint(config: dict[str, Any]) -> str:
    """Invalidate generated output only when stable URL inputs change."""
    stable = "\n".join(
        [
            str(config.get("public_base_url") or "").strip().rstrip("/"),
            str(config.get("strm_directory") or "").strip(),
            "115-stream-v1",
        ]
    )
    return hashlib.sha256(stable.encode()).hexdigest()


def strm_output_is_valid(path: str | Path, *, file_id: str, expected_url: str) -> bool:
    target = Path(path)
    try:
        content = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return False
    return content == expected_url and f"/stream/{file_id}?" in content


def create_strm(
    config: dict[str, Any], item: dict[str, Any], *, owner_id: str = ""
) -> dict[str, Any]:
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
            target = (
                directory / f"{safe_stem(str(item.get('name') or ''))} [{file_id}].strm"
            )
    reused = strm_output_is_valid(target, file_id=file_id, expected_url=url)
    if not reused:
        temporary = target.with_suffix(".strm.tmp")
        temporary.write_text(url + "\n", encoding="utf-8")
        # MDC-NG commonly runs under another container UID/GID. Linux
        # fs.protected_hardlinks requires that process to have write access
        # before it may hardlink this generated file. Directory ACLs still
        # protect the NAS tree; the STRM itself remains non-executable.
        os.chmod(temporary, 0o666)
        temporary.replace(target)
    content_hash = hashlib.sha256((url + "\n").encode()).hexdigest()
    save_strm(
        file_id,
        str(target),
        sha1=str(item.get("sha1") or ""),
        size=int(item.get("size") or 0),
        content_hash=content_hash,
        config_fingerprint=strm_config_fingerprint(config),
        owner_id=owner_id,
    )
    return {
        "file_id": file_id,
        "path": str(target),
        "url": url,
        "status": "reused" if reused else "created",
    }


async def resolve_stream(
    config: dict[str, Any], file_id: str, *, token: str, user_agent: str
) -> dict[str, Any]:
    if not validate_stream_token(token):
        raise PermissionError("invalid or revoked stream token")
    media = get_media(file_id)
    if media is None or not media.pick_code:
        raise FileNotFoundError("115 media file not found")
    ua_hash = hashlib.sha256(
        str(user_agent or "NOOR-stream-client").encode()
    ).hexdigest()[:16]
    key = (str(file_id), ua_hash)
    cached = _url_cache.get(key)
    now = time.monotonic()
    delivery_mode = str(config.get("stream_delivery_mode") or "proxy").lower()
    if cached and cached[0] > now:
        result = dict(cached[1])
        # Delivery is a local policy, not a property of the signed 115 URL.
        # Apply the current setting even when the URL itself is cacheable so a
        # proxy/redirect switch takes effect immediately.
        result["mode"] = "redirect" if delivery_mode == "redirect" else "proxy"
        return result
    from .client import Client115

    resolved = await Client115(config).download_url(
        media.pick_code, user_agent=user_agent
    )
    # 115 signs download URLs against the User-Agent used to resolve them.
    # Emby's internal HTTP client does not reliably preserve that header after
    # a 302 and it does not consistently identify itself as Emby. Therefore
    # compatibility proxying is the safe default; users may explicitly select
    # redirect when every downstream client has passed the Range/seek test.
    proxy_required = delivery_mode != "redirect"
    result = {
        "url": resolved["url"],
        "file_id": str(file_id),
        "mode": "proxy" if proxy_required else "redirect",
        "upstream_headers": {"User-Agent": str(user_agent or "NOOR-stream-client")},
    }
    ttl = max(30, min(int(config.get("stream_url_cache_seconds") or 180), 300))
    _url_cache[key] = (now + ttl, result)
    if len(_url_cache) > 256:
        for stale_key, value in list(_url_cache.items()):
            if value[0] <= now:
                _url_cache.pop(stale_key, None)
    return result
