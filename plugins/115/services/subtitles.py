from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import httpx

from .client import Client115
from .storage import get_subtitle, save_subtitle

DEFAULT_SUBTITLE_EXTENSIONS = {"srt", "ass", "ssa", "sub", "vtt"}
SUBTITLE_MARKERS = {
    "chs",
    "cht",
    "chi",
    "zho",
    "zh",
    "cn",
    "eng",
    "en",
    "jpn",
    "jp",
    "forced",
    "sdh",
}


def subtitle_extensions(config: dict[str, Any]) -> set[str]:
    raw = str(config.get("subtitle_extensions") or "")
    parsed = {
        part.strip().lower().lstrip(".") for part in raw.split(",") if part.strip()
    }
    return parsed or set(DEFAULT_SUBTITLE_EXTENSIONS)


def is_subtitle_file(item: dict[str, Any], config: dict[str, Any]) -> bool:
    if item.get("is_directory"):
        return False
    extension = (
        str(item.get("extension") or Path(str(item.get("name") or "")).suffix)
        .lower()
        .lstrip(".")
    )
    return extension in subtitle_extensions(config)


def _matching_stem(name: str) -> str:
    parts = Path(str(name or "")).stem.lower().split(".")
    while len(parts) > 1 and parts[-1] in SUBTITLE_MARKERS:
        parts.pop()
    return re.sub(r"[\s._-]+", "", ".".join(parts))


def match_subtitle(media: dict[str, Any], subtitle: dict[str, Any]) -> bool:
    return str(media.get("parent_id") or "") == str(
        subtitle.get("parent_id") or ""
    ) and _matching_stem(str(media.get("name") or "")) == _matching_stem(
        str(subtitle.get("name") or "")
    )


def subtitle_output_path(
    strm_path: str, media: dict[str, Any], subtitle: dict[str, Any]
) -> Path:
    source_stem = Path(str(subtitle.get("name") or "")).stem
    media_stem = Path(str(media.get("name") or "")).stem
    qualifier = (
        source_stem[len(media_stem) :]
        if source_stem.lower().startswith(media_stem.lower())
        else ""
    )
    qualifier = re.sub(r"[^A-Za-z0-9._-]+", "", qualifier)[:40]
    extension = Path(str(subtitle.get("name") or "")).suffix.lower()
    strm = Path(strm_path)
    return strm.parent / f"{strm.stem}{qualifier}{extension}"


async def download_subtitle(
    client: Client115, item: dict[str, Any], *, max_bytes: int
) -> bytes:
    declared_size = int(item.get("size") or 0)
    if declared_size and declared_size > max_bytes:
        raise ValueError("subtitle exceeds configured size limit")
    resolved = await client.download_url(
        str(item.get("pick_code") or ""), user_agent=client.user_agent
    )
    async with httpx.AsyncClient(
        timeout=client.timeout, follow_redirects=True, trust_env=False
    ) as http:
        response = await http.get(
            resolved["url"], headers={"User-Agent": client.user_agent}
        )
        response.raise_for_status()
        content = response.content
    if len(content) > max_bytes:
        raise ValueError("subtitle exceeds configured size limit")
    return content


async def sync_subtitle(
    config: dict[str, Any],
    *,
    client: Client115,
    media: dict[str, Any],
    subtitle: dict[str, Any],
    strm_path: str,
) -> dict[str, Any]:
    target = subtitle_output_path(strm_path, media, subtitle)
    existing = get_subtitle(str(subtitle.get("file_id") or ""))
    identity_matches = bool(
        existing
        and existing.sha1 == str(subtitle.get("sha1") or "").upper()
        and existing.size == int(subtitle.get("size") or 0)
        and Path(existing.local_path) == target
        and target.exists()
    )
    if identity_matches:
        return {"file_id": existing.file_id, "path": str(target), "status": "reused"}
    max_bytes = max(
        1024 * 1024,
        min(
            int(config.get("subtitle_max_bytes") or 20 * 1024 * 1024), 100 * 1024 * 1024
        ),
    )
    content = await download_subtitle(client, subtitle, max_bytes=max_bytes)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(content)
    os.chmod(temporary, 0o666)
    temporary.replace(target)
    digest = hashlib.sha256(content).hexdigest()
    save_subtitle(
        str(subtitle.get("file_id") or ""),
        str(media.get("file_id") or ""),
        str(target),
        sha1=str(subtitle.get("sha1") or ""),
        size=int(subtitle.get("size") or len(content)),
        content_hash=digest,
    )
    return {
        "file_id": str(subtitle.get("file_id") or ""),
        "path": str(target),
        "status": "created",
    }
