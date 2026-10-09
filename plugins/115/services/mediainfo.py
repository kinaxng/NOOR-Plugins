from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.core.runtime_paths import plugin_data_path

from .storage import MediaFile, emit_event, get_media, next_mediainfo, queue_mediainfo, update_mediainfo, utcnow
from .strm import build_stream_url, stream_token

SCHEMA_VERSION = 1


def _ratio(value: Any) -> float | None:
    text = str(value or "")
    try:
        if "/" in text:
            left, right = text.split("/", 1)
            return round(float(left) / float(right), 6) if float(right) else None
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def parse_ffprobe(payload: dict[str, Any]) -> dict[str, Any]:
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    result: dict[str, Any] = {
        "container": str(fmt.get("format_name") or "").split(",", 1)[0],
        "duration": float(fmt.get("duration") or 0),
        "bitrate": int(fmt.get("bit_rate") or 0),
        "size": int(fmt.get("size") or 0),
        "video": [], "audio": [], "subtitle": [],
    }
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        common = {
            "index": int(stream.get("index") or 0), "codec": str(stream.get("codec_name") or ""),
            "profile": str(stream.get("profile") or ""), "language": str(tags.get("language") or ""),
            "title": str(tags.get("title") or ""), "default": bool(disposition.get("default")),
            "forced": bool(disposition.get("forced")),
        }
        kind = stream.get("codec_type")
        if kind == "video":
            result["video"].append({**common, "level": stream.get("level"), "width": int(stream.get("width") or 0),
                "height": int(stream.get("height") or 0), "frame_rate": _ratio(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
                "bit_depth": int(stream.get("bits_per_raw_sample") or 0), "pixel_format": str(stream.get("pix_fmt") or ""),
                "color_space": str(stream.get("color_space") or ""), "color_transfer": str(stream.get("color_transfer") or ""),
                "color_primaries": str(stream.get("color_primaries") or ""), "side_data": stream.get("side_data_list") or []})
        elif kind == "audio":
            result["audio"].append({**common, "channels": int(stream.get("channels") or 0),
                "channel_layout": str(stream.get("channel_layout") or ""), "sample_rate": int(stream.get("sample_rate") or 0)})
        elif kind == "subtitle":
            result["subtitle"].append(common)
    return result


def enqueue(media: MediaFile) -> bool:
    _record, created = queue_mediainfo(media, schema_version=SCHEMA_VERSION)
    return created


async def run_ffprobe(url: str, *, timeout: int) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(30, min(timeout, 1800)))
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise TimeoutError("ffprobe timed out")
    if process.returncode:
        raise RuntimeError((stderr.decode(errors="replace") or "ffprobe failed")[:1000])
    return parse_ffprobe(json.loads(stdout.decode("utf-8")))


def _safe_probe_error(exc: Exception) -> str:
    message = str(exc)
    message = re.sub(r"([?&]token=)[^&\s]+", r"\1<redacted>", message)
    return message[:1000]


async def process_next(config: dict[str, Any]) -> dict[str, Any] | None:
    record = await asyncio.to_thread(next_mediainfo)
    if not record:
        return None
    media = await asyncio.to_thread(get_media, record.file_id)
    if not media:
        await asyncio.to_thread(update_mediainfo, record.file_id, status="failed", error_message="media identity missing")
        return {"file_id": record.file_id, "status": "failed"}
    max_attempts = max(1, min(int(config.get("mediainfo_retry_limit") or 3), 5))
    try:
        url = build_stream_url(config, media.file_id, stream_token())
        parsed = await run_ffprobe(url, timeout=int(config.get("mediainfo_timeout") or 300))
        cache_dir = plugin_data_path("115", "mediainfo")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{media.file_id}.json"
        document = {"provider": "115", "file_id": media.file_id, "sha1": media.sha1, "size": media.size,
            "schema_version": SCHEMA_VERSION, "probed_at": utcnow().isoformat(), "media": parsed}
        temporary = cache_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(cache_path)
        await asyncio.to_thread(update_mediainfo, media.file_id, status="ready", attempts=record.attempts + 1,
            next_retry_at=None, error_message="", json_path=str(cache_path), media=parsed, probed_at=utcnow())
        await asyncio.to_thread(emit_event, "115.mediainfo.ready", file_id=media.file_id,
            payload={"provider": "115", "file_id": media.file_id, "sha1": media.sha1, "size": media.size,
                "schema_version": SCHEMA_VERSION, "cache_path": str(cache_path)},
            dedupe_key=f"115.mediainfo.ready:{media.file_id}:{media.sha1}:{media.size}:v{SCHEMA_VERSION}")
        return {"file_id": media.file_id, "status": "ready", "cache_path": str(cache_path)}
    except Exception as exc:
        safe_error = _safe_probe_error(exc)
        attempts = record.attempts + 1
        terminal = attempts >= max_attempts
        delay = min(3600, 60 * (2 ** max(0, attempts - 1)))
        await asyncio.to_thread(update_mediainfo, record.file_id, status="failed" if terminal else "retry", attempts=attempts,
            next_retry_at=None if terminal else utcnow() + timedelta(seconds=delay), error_message=safe_error)
        return {"file_id": record.file_id, "status": "failed" if terminal else "retry", "error": safe_error}
