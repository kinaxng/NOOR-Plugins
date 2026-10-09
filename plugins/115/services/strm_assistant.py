from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from app.api.endpoints.media_library_helpers import get_config, headers, server_url

from .storage import MediaFile, MediaInfoRecord


SIDECAR_SUFFIX = "-mediainfo.json"

SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    "catchup_mode": {"file": "Strm Assistant.json", "path": ("GeneralOptions", "CatchupMode"), "default": True, "type": "boolean", "group": "入库", "label": "追更模式", "help": "发现新媒体时自动执行所选处理流程；115 自动入库需要开启。"},
    "extract_workers": {"file": "Strm Assistant.json", "path": ("GeneralOptions", "MaxConcurrentCount"), "default": 1, "type": "number", "min": 1, "max": 4, "group": "性能", "label": "远程媒体并发", "help": "神医助手自行读取远程媒体时的并发数。115 建议保持 1，避免网盘风控。"},
    "local_workers": {"file": "Strm Assistant.json", "path": ("GeneralOptions", "Tier2MaxConcurrentCount"), "default": 1, "type": "number", "min": 1, "max": 8, "group": "性能", "label": "本地任务并发", "help": "本地文件或无需访问网盘的任务并发数。"},
    "single_thread_delay": {"file": "Strm Assistant.json", "path": ("GeneralOptions", "CooldownDurationSeconds"), "default": 0, "type": "number", "min": 0, "max": 60, "group": "性能", "label": "任务间隔（秒）", "help": "每个处理项之间的冷却时间；遇到限流时可以适当增加。"},
    "persist_mode": {"file": "Strm Assistant_MediaInfoExtractOptions.json", "path": ("PersistMediaInfoMode",), "default": "Restore", "type": "select", "options": [{"value": "Restore", "label": "恢复优先（推荐）"}, {"value": "Default", "label": "提取并持久化"}, {"value": "None", "label": "关闭"}], "group": "MediaInfo", "label": "MediaInfo 持久化模式", "help": "恢复优先会先读取 NOOR 生成的 sidecar，避免 Emby 再次探测 115。"},
    "mediainfo_json_root": {"file": "Strm Assistant_MediaInfoExtractOptions.json", "path": ("MediaInfoJsonRootFolder",), "default": "", "type": "string", "group": "MediaInfo", "label": "独立 JSON 根目录", "help": "留空表示读取 STRM 旁的 sidecar，也是 NOOR 当前工作流的推荐值。"},
    "include_extras": {"file": "Strm Assistant_MediaInfoExtractOptions.json", "path": ("IncludeExtra",), "default": False, "type": "boolean", "group": "MediaInfo", "label": "包含花絮", "help": "同时处理预告、花絮等额外视频；会增加远程请求。"},
    "image_capture": {"file": "Strm Assistant_MediaInfoExtractOptions.json", "path": ("EnableImageCapture",), "default": False, "type": "boolean", "group": "MediaInfo", "label": "提取 STRM 封面", "help": "从视频截帧生成封面；AV 已由 MDC-NG 提供图片，通常无需开启。"},
    "merge_versions": {"file": "Strm Assistant_ExperienceEnhanceOptions.json", "path": ("MergeMultiVersion",), "default": False, "type": "boolean", "group": "媒体库", "label": "自动合并多版本", "help": "将同目录中可识别的不同版本合并为一个 Emby 项目。"},
    "unlock_intro_skip": {"file": "Strm Assistant_IntroSkipOptions.json", "path": ("UnlockIntroSkip",), "default": False, "type": "boolean", "group": "片头片尾", "label": "片头指纹增强", "help": "启用神医助手的片头片尾指纹增强；与 115 秒播没有直接关系。"},
    "play_session_intro": {"file": "Strm Assistant_IntroSkipOptions.json", "path": ("EnableIntroSkip",), "default": False, "type": "boolean", "group": "片头片尾", "label": "播放会话跳过片头", "help": "播放时应用已识别的片头片尾标记。"},
    "episode_refresh_days": {"file": "Strm Assistant_MetadataEnhanceOptions.json", "path": ("EpisodeRefreshLookbackDays",), "default": 365, "type": "number", "min": 1, "max": 3650, "group": "元数据", "label": "剧集回看天数", "help": "刷新剧集元数据时向前检查的时间范围；AV 电影库通常用不到。"},
}

TASK_LABELS = {
    "MediaInfoExtractTask": ("提取 MediaInfo", "访问媒体内容并补齐缺失流信息；115 全库执行会增加网盘读取。"),
    "MediaInfoPersistTask": ("持久化 MediaInfo", "把 Emby 已有媒体信息保存成 JSON。"),
    "CheckMissingMediaInfoTask": ("检查缺失 MediaInfo", "扫描缺少媒体流的项目；大型媒体库不建议频繁执行。"),
    "ScanExternalSubtitleTask": ("扫描外挂字幕", "检查媒体目录中的外挂字幕。"),
    "VideoThumbnailExtractTask": ("提取视频缩略图", "生成预览缩略图，会读取大量视频片段。"),
    "ExtractStrmPrimaryImageTask": ("提取 STRM 封面", "从 STRM 视频截取主图。"),
    "MergeMultiVersionTask": ("合并多版本", "按神医助手规则合并同一作品的多个版本。"),
    "ExtractIntroFingerprintTask": ("提取片头指纹", "为剧集生成片头片尾指纹。"),
}


def _configuration_directory() -> Path | None:
    candidates = [
        Path(os.environ["STRM_ASSISTANT_CONFIG_DIR"]) if os.environ.get("STRM_ASSISTANT_CONFIG_DIR") else None,
        Path("/volume1/docker/emby/config/plugins/configurations"),
        Path("/var/lib/emby/plugins/configurations"),
        Path("/config/plugins/configurations"),
    ]
    return next((path for path in candidates if path and (path / "Strm Assistant.json").is_file()), None)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _nested_get(value: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _nested_set(value: dict[str, Any], path: tuple[str, ...], setting: Any) -> None:
    current = value
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = setting


def settings() -> dict[str, Any]:
    directory = _configuration_directory()
    files: dict[str, dict[str, Any]] = {}
    values: dict[str, Any] = {}
    for setting_id, definition in SETTING_DEFINITIONS.items():
        file_name = str(definition["file"])
        if file_name not in files:
            files[file_name] = _read_json(directory / file_name) if directory else {}
        values[setting_id] = _nested_get(files[file_name], definition["path"], definition["default"])
    writable = bool(
        directory
        and all(
            (directory / file_name).is_file()
            and os.access(directory / file_name, os.W_OK)
            for file_name in files
        )
    )
    return {
        "available": bool(directory),
        "writable": writable,
        "source": "file" if directory else "unavailable",
        "values": values,
        "definitions": [{"id": key, **{k: v for k, v in definition.items() if k not in {"file", "path"}}} for key, definition in SETTING_DEFINITIONS.items()],
        "recommended": {"catchup_mode": True, "extract_workers": 1, "persist_mode": "Restore", "mediainfo_json_root": "", "include_extras": False, "image_capture": False},
    }


def update_settings(values: dict[str, Any]) -> dict[str, Any]:
    directory = _configuration_directory()
    if not directory:
        raise FileNotFoundError("未找到神医助手配置目录")
    changed_files: dict[str, dict[str, Any]] = {}
    for setting_id, raw_value in values.items():
        definition = SETTING_DEFINITIONS.get(setting_id)
        if not definition:
            continue
        file_name = str(definition["file"])
        document = changed_files.setdefault(file_name, _read_json(directory / file_name))
        if definition["type"] == "boolean":
            value: Any = bool(raw_value)
        elif definition["type"] == "number":
            value = max(int(definition.get("min", 0)), min(int(raw_value), int(definition.get("max", raw_value))))
        else:
            value = str(raw_value or "")
        _nested_set(document, definition["path"], value)
    if not changed_files:
        raise ValueError("没有可保存的神医助手设置")
    for file_name, document in changed_files.items():
        target = directory / file_name
        # These files normally belong to the Emby container's root user. NOOR is
        # deliberately granted ACLs on the exact files, not on the whole config
        # directory, so update the existing inode without requiring create/delete
        # permission on sibling plugin configurations.
        with target.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    result = settings()
    result["restart_required"] = True
    return result


def _codec(value: Any) -> str:
    aliases = {"h264": "h264", "hevc": "hevc", "h265": "hevc", "aac": "aac"}
    text = str(value or "").strip().lower()
    return aliases.get(text, text)


def _stream_payload(stream: dict[str, Any], stream_type: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "Codec": _codec(stream.get("codec")),
        "Profile": str(stream.get("profile") or "") or None,
        "Type": stream_type,
        "Index": int(stream.get("index") or 0),
        "Language": str(stream.get("language") or "") or None,
        "Title": str(stream.get("title") or "") or None,
        "IsDefault": bool(stream.get("default")),
        "IsForced": bool(stream.get("forced")),
        "IsExternal": False,
        "Protocol": "File",
    }
    if stream_type == "Video":
        result.update(
            Width=int(stream.get("width") or 0) or None,
            Height=int(stream.get("height") or 0) or None,
            BitDepth=int(stream.get("bit_depth") or 0) or None,
            PixelFormat=str(stream.get("pixel_format") or "") or None,
            ColorSpace=str(stream.get("color_space") or "") or None,
            ColorTransfer=str(stream.get("color_transfer") or "") or None,
            ColorPrimaries=str(stream.get("color_primaries") or "") or None,
            AverageFrameRate=float(stream.get("frame_rate") or 0) or None,
            RealFrameRate=float(stream.get("frame_rate") or 0) or None,
            Level=int(stream.get("level") or 0) or None,
        )
    elif stream_type == "Audio":
        result.update(
            Channels=int(stream.get("channels") or 0) or None,
            ChannelLayout=str(stream.get("channel_layout") or "") or None,
            SampleRate=int(stream.get("sample_rate") or 0) or None,
        )
    return {key: value for key, value in result.items() if value is not None}


def build_persisted_mediainfo(media: MediaFile, record: MediaInfoRecord) -> list[dict[str, Any]]:
    info = dict(record.media or {})
    suffix = Path(media.name).suffix.lstrip(".").lower()
    container = str(info.get("container") or suffix).lower()
    if container in {"mov", "mov,mp4,m4a,3gp,3g2,mj2"} and suffix in {"mp4", "m4v"}:
        container = suffix
    streams = [
        *(_stream_payload(item, "Video") for item in info.get("video") or []),
        *(_stream_payload(item, "Audio") for item in info.get("audio") or []),
        *(_stream_payload(item, "Subtitle") for item in info.get("subtitle") or []),
    ]
    duration = float(info.get("duration") or 0)
    size = int(info.get("size") or media.size or 0)
    if not duration or not size or not any(item.get("Type") in {"Video", "Audio"} for item in streams):
        raise ValueError("MediaInfo 缺少时长、大小或音视频流")
    return [
        {
            "MediaSourceInfo": {
                "Protocol": "File",
                "Type": "Default",
                "Container": container,
                "Size": size,
                "RunTimeTicks": int(round(duration * 10_000_000)),
                "Bitrate": int(info.get("bitrate") or 0),
                "IsRemote": True,
                "SupportsDirectPlay": True,
                "SupportsDirectStream": True,
                "SupportsTranscoding": True,
                "SupportsProbing": False,
                "MediaStreams": streams,
            },
            "Chapters": [],
        }
    ]


def local_organized_path(config: dict[str, Any], server_path: str) -> Path:
    server_incoming = PurePosixPath(str(config.get("organizer_incoming_path") or ""))
    local_incoming = Path(str(config.get("strm_directory") or ""))
    if not str(server_incoming) or not str(local_incoming):
        raise ValueError("未配置 STRM 本地目录或 MDC-NG incoming 路径")
    # The two incoming directories are the same mount as seen by MDC-NG and NOOR.
    # Their parents therefore form a stable namespace mapping for categorized output.
    try:
        relative = PurePosixPath(server_path).relative_to(server_incoming.parent)
    except ValueError as exc:
        raise ValueError("整理结果不在已配置的 STRM 根目录内") from exc
    return local_incoming.parent.joinpath(*relative.parts)


def sidecar_path(config: dict[str, Any], server_path: str) -> Path:
    organized = local_organized_path(config, server_path)
    return organized.with_name(f"{organized.stem}{SIDECAR_SUFFIX}")


def write_sidecar(
    config: dict[str, Any], server_path: str, media: MediaFile, record: MediaInfoRecord
) -> Path:
    target = sidecar_path(config, server_path)
    if not target.parent.is_dir():
        raise FileNotFoundError(f"整理目录不存在：{target.parent}")
    document = build_persisted_mediainfo(media, record)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


async def status(timeout: float = 8.0) -> dict[str, Any]:
    config = get_config()
    if not str(config.get("server_url") or "").strip() or not str(config.get("api_key") or "").strip():
        return {"available": False, "status": "emby_not_configured", "mode": "sidecar_restore"}
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(
                f"{server_url(config)}/emby/Plugins",
                headers=headers(str(config.get("api_key") or "")),
            )
        response.raise_for_status()
        plugins = response.json() if isinstance(response.json(), list) else []
        plugin = next(
            (
                item
                for item in plugins
                if "strm assistant" in str(item.get("Name") or "").lower()
                or "strmassistant" in str(item.get("Name") or "").replace(" ", "").lower()
            ),
            None,
        )
        local_settings = settings()
        return {
            "available": bool(plugin),
            "status": "ready" if plugin else "not_installed",
            "name": str((plugin or {}).get("Name") or ""),
            "version": str((plugin or {}).get("Version") or ""),
            "mode": "sidecar_restore",
            "sync_api": False,
            "settings_available": local_settings["available"],
            "settings_writable": local_settings["writable"],
        }
    except Exception as exc:
        return {
            "available": False,
            "status": "unreachable",
            "mode": "sidecar_restore",
            "message": str(exc)[:300],
        }


async def scheduled_tasks(timeout: float = 8.0) -> dict[str, Any]:
    config = get_config()
    if not str(config.get("server_url") or "").strip() or not str(config.get("api_key") or "").strip():
        return {"available": False, "items": [], "message": "Emby 尚未配置"}
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.get(
            f"{server_url(config)}/emby/ScheduledTasks",
            headers=headers(str(config.get("api_key") or "")),
        )
    response.raise_for_status()
    payload = response.json()
    raw_items = payload if isinstance(payload, list) else payload.get("Items") or []
    items = []
    for item in raw_items:
        key = str(item.get("Key") or "")
        if key not in TASK_LABELS:
            continue
        label, description = TASK_LABELS[key]
        last = item.get("LastExecutionResult") if isinstance(item.get("LastExecutionResult"), dict) else {}
        items.append({
            "id": str(item.get("Id") or ""),
            "key": key,
            "label": label,
            "description": description,
            "state": str(item.get("State") or "Idle").lower(),
            "progress": float(item.get("CurrentProgressPercentage") or 0),
            "last_status": str(last.get("Status") or ""),
            "last_started_at": str(last.get("StartTimeUtc") or ""),
            "last_ended_at": str(last.get("EndTimeUtc") or ""),
            "risk": "high" if key in {"MediaInfoExtractTask", "VideoThumbnailExtractTask", "CheckMissingMediaInfoTask", "ExtractIntroFingerprintTask"} else "normal",
        })
    return {"available": True, "items": items}


async def run_task(task_id: str, timeout: float = 8.0) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    tasks = await scheduled_tasks(timeout=timeout)
    task = next((item for item in tasks.get("items", []) if item["id"] == task_id), None)
    if not task:
        raise LookupError("未找到神医助手任务")
    config = get_config()
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(
            f"{server_url(config)}/emby/ScheduledTasks/Running/{task_id}",
            headers=headers(str(config.get("api_key") or "")),
        )
    response.raise_for_status()
    return {"ok": True, "task": task}
