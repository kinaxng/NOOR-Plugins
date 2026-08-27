from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import uuid
import datetime as dt
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.core.runtime_paths import plugin_data_path
from app.plugins.contracts import PluginManifest, PluginTestResult

PLUGIN_ID = "subscription-core"


def _data_file() -> Path:
    return plugin_data_path("subscription-core", "subscriptions.json")


EVENT_LIMIT = 200
_scheduler_task: asyncio.Task | None = None
_scheduler_stop: asyncio.Event | None = None
_run_lock = asyncio.Lock()


def _select_enabled_downloader(preferred: str, compatible: list[str], runtime: Any) -> str:
    """Select the preferred compatible downloader only when it is actually enabled."""
    candidates: list[str] = []
    for plugin_id in (preferred, *compatible):
        plugin_id = str(plugin_id or "").strip()
        if plugin_id and plugin_id not in candidates:
            candidates.append(plugin_id)
    for plugin_id in candidates:
        if runtime.is_enabled(plugin_id):
            return plugin_id
    return ""


def _now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _ensure_store() -> dict[str, Any]:
    data_file = _data_file()
    data_file.parent.mkdir(parents=True, exist_ok=True)
    if not data_file.exists():
        data = {"version": 1, "subscriptions": [], "events": []}
        data_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    try:
        data = json.loads(data_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid store")
        data.setdefault("version", 1)
        data.setdefault("subscriptions", [])
        data.setdefault("events", [])
        return data
    except Exception:
        backup = data_file.with_suffix(f".{int(time.time())}.bak")
        try:
            data_file.replace(backup)
        except Exception:
            pass
        data = {"version": 1, "subscriptions": [], "events": []}
        data_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data


def _save(data: dict[str, Any]) -> None:
    data_file = _data_file()
    data_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = data_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(data_file)


def _image_candidates(*items: Any) -> list[str]:
    out: list[str] = []
    keys = ("fanart_url", "cover_url", "thumb_url", "image", "poster_url", "jacket_url", "backdrop_url")

    def append_url(url: str) -> None:
        text = str(url or "").strip()
        if text and text not in out:
            out.append(text)

    def expand_url(url: str) -> list[str]:
        text = str(url or "").strip()
        if not text:
            return []
        parsed = urlparse(text)
        inner = ""
        if parsed.path.rstrip("/").endswith("/api/image"):
            values = parse_qs(parsed.query).get("url") or []
            if values:
                inner = unquote(str(values[0] or "").strip())
        candidates: list[str] = []
        if inner and text:
            candidates.append(text)
        raw = inner or text
        if raw:
            candidates.append(raw)
        return candidates

    def push(value: Any) -> None:
        if not value:
            return
        if isinstance(value, dict):
            for key in keys:
                push(value.get(key))
            return
        if isinstance(value, (list, tuple)):
            for entry in value:
                push(entry)
            return
        for url in expand_url(str(value or "").strip()):
            append_url(url)

    for item in items:
        push(item)
    return out


def _event(data: dict[str, Any], subscription_id: str, level: str, message: str, payload: dict[str, Any] | None = None) -> None:
    events = data.setdefault("events", [])
    events.insert(0, {
        "id": uuid.uuid4().hex,
        "subscription_id": subscription_id,
        "level": level,
        "message": message,
        "payload": payload or {},
        "created_at": _now(),
    })
    del events[EVENT_LIMIT:]




def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _next_xunlei_quota_retry_at() -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    now = dt.datetime.now(tz)
    retry = (now + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return retry.isoformat()


def _local_date(value: str | None = None) -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    parsed = _parse_dt(value) if value else None
    current = (parsed or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    return current.date().isoformat()


def _normalize_quota_retry_at(value: Any) -> str:
    """Normalize old 24h retry timestamps to the same local date 00:05.

    Older records may contain e.g. 16:10 because the retry was effectively
    treated as 24 hours. Xunlei quota resets after midnight, so the retry gate
    should be the matching Asia/Shanghai date at 00:05.
    """
    parsed = _parse_dt(value)
    if not parsed:
        return _next_xunlei_quota_retry_at()
    tz = dt.timezone(dt.timedelta(hours=8))
    local = parsed.astimezone(tz)
    normalized = local.replace(hour=0, minute=5, second=0, microsecond=0)
    return normalized.isoformat()


def _normalize_waiting_quota_records(data: dict[str, Any]) -> bool:
    changed = False
    for sub in data.get("subscriptions") or []:
        if sub.get("status") != "waiting_quota" and sub.get("last_submit_error_kind") != "downloader_quota_limited":
            continue
        retry = str(sub.get("retry_after_at") or "").strip()
        if not retry:
            continue
        normalized = _normalize_quota_retry_at(retry)
        if normalized != retry:
            sub["retry_after_at"] = normalized
            sub["updated_at"] = _now()
            changed = True
    return changed


def _is_retry_due(sub: dict[str, Any]) -> bool:
    retry_at = _parse_dt(sub.get("retry_after_at"))
    if not retry_at:
        return True
    return dt.datetime.now(dt.timezone.utc) >= retry_at


def _is_xunlei_quota_limited(message: Any) -> bool:
    value = str(message or "").lower()
    patterns = (
        "task_create_count_limit",
        "免费下载任务额度",
        "免费下载任务数已用完",
        "今日3个免费下载",
        "今日 3 个免费下载",
        "下载次数限制",
        "下载次数已用完",
        "任务额度受限",
        "task daily limit",
        "count limit",
    )
    return any(pattern.lower() in value for pattern in patterns)


def _submit_error_kind(message: Any) -> str:
    value = str(message or "").lower()
    if _is_xunlei_quota_limited(message):
        return "downloader_quota_limited"
    if "unauthenticated" in value or "unauthorized" in value or "401" in value or "令牌未认证" in value or "已过期" in value:
        return "downloader_auth_failed"
    return "download_submit_failed"


def _public_error_message(message: Any) -> str:
    text = str(message or "").strip()
    if _is_xunlei_quota_limited(text):
        return "迅雷今日免费下载任务次数已用完，已保留订阅，次日有可下载次数后会继续尝试推送。"
    return text or "推送下载失败"


def _should_check_subscription(sub: dict[str, Any], *, force: bool = False) -> bool:
    if sub.get("status") in {"deleted", "submitted"} and not force:
        return False
    if sub.get("status") == "waiting_quota" and not force:
        return _is_retry_due(sub)
    return True


def _clear_submit_state(sub: dict[str, Any]) -> None:
    """Return a submitted subscription to normal monitoring without losing submit history."""
    sub["status"] = "active"
    sub["push_status"] = "idle"
    sub["last_submit_error"] = ""
    sub["last_submit_error_kind"] = ""
    sub["retry_after_at"] = ""


def _resource_submit_key(resource: dict[str, Any]) -> str:
    if not isinstance(resource, dict):
        return ""
    parts = [
        resource.get("provider"),
        resource.get("id"),
        resource.get("url") or resource.get("download_url") or resource.get("magnet"),
        resource.get("title"),
        resource.get("size_bytes"),
    ]
    return "|".join(str(x or "").strip() for x in parts)


def _remember_consumed_resource(sub: dict[str, Any], resource: dict[str, Any] | None) -> str:
    key = _resource_submit_key(resource or {})
    if not key:
        return ""
    consumed = [str(x) for x in (sub.get("consumed_resource_keys") or []) if str(x or "").strip()]
    if key not in consumed:
        consumed.insert(0, key)
        del consumed[30:]
    sub["consumed_resource_keys"] = consumed
    sub["last_consumed_resource_key"] = key
    sub["last_consumed_at"] = _now()
    return key


def _is_consumed_resource(sub: dict[str, Any], resource: dict[str, Any]) -> bool:
    key = _resource_submit_key(resource)
    return bool(key and key in {str(x) for x in (sub.get("consumed_resource_keys") or [])})


def _forget_consumed_resource(sub: dict[str, Any], resource: dict[str, Any] | None) -> None:
    key = _resource_submit_key(resource or {})
    if not key:
        return
    sub["consumed_resource_keys"] = [str(x) for x in (sub.get("consumed_resource_keys") or []) if str(x) != key]
    if str(sub.get("last_consumed_resource_key") or "") == key:
        sub["last_consumed_resource_key"] = ""


def _media_matches_resource_profile(media: dict[str, Any], resource: dict[str, Any] | None) -> bool:
    if not isinstance(resource, dict):
        return False
    media_profile = _media_quality_profile(media)
    resource_profile = _resource_quality_profile(resource)
    for feature in ("is_cracked", "has_subtitle", "is_new_model_uncensored_crack"):
        if resource_profile.get(feature) and not media_profile.get(feature):
            return False
    return True


def _file_fingerprint(path: Any) -> dict[str, Any]:
    try:
        stat = Path(str(path or "")).stat()
        return {"dev": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except (OSError, ValueError):
        return {}


def _same_path_was_replaced(sub: dict[str, Any], path: str) -> bool:
    current = _file_fingerprint(path)
    before = sub.get("pre_submit_file_fingerprint") if isinstance(sub.get("pre_submit_file_fingerprint"), dict) else {}
    if current and before:
        return current != before
    submitted_at = _parse_dt(sub.get("last_submit_at"))
    if not current or not submitted_at:
        return False
    return int(current.get("mtime_ns") or 0) > int(submitted_at.timestamp() * 1_000_000_000)


def _auto_submit_blocked_today(sub: dict[str, Any], resource_key: str) -> bool:
    if not resource_key:
        return False
    if str(sub.get("last_submit_resource_key") or "") != resource_key:
        return False
    last_date = str(sub.get("last_submit_local_date") or "").strip()
    return bool(last_date and last_date == _local_date())


def _log_system(level: str, message: str, payload: dict[str, Any] | None = None) -> None:
    try:
        from app.api.system import SystemLogManager
        suffix = ""
        if payload:
            bits = [f"{k}={v}" for k, v in payload.items() if v not in (None, "")]
            suffix = " — " + " ".join(bits[:6]) if bits else ""
        SystemLogManager.get_instance().add_log(level, f"[订阅中心] {message}{suffix}", source="plugin.subscription-core")
    except Exception:
        pass


def _schedule_immediate_check(config: dict[str, Any], sub_id: str) -> None:
    """Queue a non-blocking check after a subscription is created or touched from an entry point."""
    try:
        submit = bool(config.get("background_submit_on_match", True))
        asyncio.create_task(_run_due_checks(config, sub_id=sub_id, force=True, limit=0, submit=submit))
        _log_system("info", "已排入自动检测", {"subscription_id": sub_id, "submit": submit})
    except RuntimeError:
        # No running loop; the periodic scheduler will pick it up later.
        pass

def _extract_code(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"\b(FC2[-_ ]?(?:PPV[-_ ]?)?\d{4,9}|[A-Z]{2,8}[-_ ]?\d{2,7}|\d{6}[-_]\d{2,5})\b", text, re.I)
    if not match:
        return ""
    raw = re.sub(r"[_ ]+", "-", match.group(1).upper())
    fc2 = re.match(r"FC2-?(?:PPV-?)?(\d{4,9})$", raw, re.I)
    if fc2:
        return f"FC2-PPV-{fc2.group(1)}"
    compact = re.match(r"^([A-Z]{2,8})(\d{2,7})$", raw)
    if compact:
        return f"{compact.group(1)}-{compact.group(2)}"
    return raw


def _extract_raw_code(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"\b(FC2[-_ ]?(?:PPV[-_ ]?)?\d{4,9}|[A-Z]{2,8}[-_ ]?\d{2,7}|\d{6}[-_]\d{2,5})\b", text, re.I)
    return match.group(1).upper().replace(" ", "") if match else ""


def _norm_code(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _features(resource: dict[str, Any]) -> dict[str, bool]:
    features = resource.get("features") if isinstance(resource.get("features"), dict) else {}
    tags = " ".join(str(x) for x in (resource.get("tags") or []))
    text = "\n".join(str(resource.get(k) or "") for k in ("title", "subtitle", "provider_label")) + "\n" + tags
    has_subtitle = bool(features.get("has_subtitle") or re.search(r"中字|中文字幕|字幕", text, re.I))
    is_cracked = bool(features.get("is_cracked") or re.search(r"破解|无码破解|uncensored|crack", text, re.I))
    is_new_model_uncensored_crack = bool(
        features.get("is_new_model_uncensored_crack")
        or features.get("new_model_uncensored_crack")
        or (re.search(r"新模型", text, re.I) and re.search(r"无码破解|破解|uncensored|crack", text, re.I))
    )
    requirements = resource.get("requirements") if isinstance(resource.get("requirements"), dict) else {}
    is_private = bool(features.get("is_private_tracker") or requirements.get("accepts_private_tracker"))
    return {
        "has_subtitle": has_subtitle,
        "is_cracked": is_cracked,
        "is_new_model_uncensored_crack": is_new_model_uncensored_crack,
        "is_private_tracker": is_private,
    }


def _quality_text_from_resource(resource: dict[str, Any]) -> str:
    return " ".join(str(resource.get(k) or "") for k in ("title", "subtitle", "provider_label")) + " " + " ".join(str(x) for x in (resource.get("tags") or []))


def _resolution_rank(text: str) -> int:
    value = str(text or "")
    if re.search(r"\b(8k|4320)\b", value, re.I):
        return 4
    if re.search(r"\b(4k|2160|uhd)\b", value, re.I):
        return 3
    if re.search(r"\b(1080|fhd|full\s*hd|hd)\b|高清", value, re.I):
        return 2
    if re.search(r"\b(720)\b", value, re.I):
        return 1
    return 0


def _file_size(path: Any) -> int:
    try:
        p = Path(str(path or ""))
        return p.stat().st_size if p.exists() and p.is_file() else 0
    except Exception:
        return 0


def _media_quality_profile(media_item: dict[str, Any] | None, sub: dict[str, Any] | None = None) -> dict[str, Any]:
    tags = media_item.get("tags") if isinstance(media_item, dict) and isinstance(media_item.get("tags"), dict) else {}
    text = " ".join(str((media_item or {}).get(k) or "") for k in ("name", "path"))
    path = str((media_item or {}).get("path") or (sub or {}).get("current_file_path") or "")
    is_new_model_uncensored_crack = bool(
        tags.get("is_new_model_uncensored_crack")
        or ((sub or {}).get("current_is_new_model_uncensored_crack"))
        or (re.search(r"新模型", text, re.I) and re.search(r"无码破解|破解|uncensored|crack", text, re.I))
    )
    return {
        "is_cracked": bool(tags.get("is_cracked")),
        "has_subtitle": bool(tags.get("has_chinese") or int((media_item or {}).get("subtitle_count") or 0) > 0),
        "is_new_model_uncensored_crack": is_new_model_uncensored_crack,
        "resolution_rank": _resolution_rank(text),
        "size_bytes": int((sub or {}).get("current_size_bytes") or 0) or _file_size(path),
    }


def _resource_quality_profile(resource: dict[str, Any]) -> dict[str, Any]:
    features = _features(resource)
    return {
        "is_cracked": bool(features.get("is_cracked")),
        "has_subtitle": bool(features.get("has_subtitle")),
        "is_new_model_uncensored_crack": bool(features.get("is_new_model_uncensored_crack")),
        "resolution_rank": _resolution_rank(_quality_text_from_resource(resource)),
        "size_bytes": int(resource.get("size_bytes") or 0),
    }


def _quality_score_from_features(features: dict[str, bool], text: str = "") -> int:
    score = 0
    if features.get("is_cracked"):
        score += 40
    if features.get("has_subtitle"):
        score += 30
    if features.get("is_new_model_uncensored_crack"):
        score += 20
    # 洗版判断必须保守：只有当前库与候选都能稳定识别的版本特征才参与。
    # 分辨率/HD/文件大小先不参与洗版评分，避免候选标题写了 HD 而当前库缺少同等元数据时误判。
    return score


def _score_resource(resource: dict[str, Any]) -> int:
    """Resource preference score used for sorting candidates, not for upgrade decisions."""
    f = _features(resource)
    score = _quality_score_from_features(f, _quality_text_from_resource(resource))
    if f["is_private_tracker"]:
        score += 10
    try:
        gb = float(resource.get("size_bytes") or 0) / (1024 ** 3)
    except Exception:
        gb = 0
    score += min(20, int(gb * 2))
    return score


def _score_resource_quality(resource: dict[str, Any]) -> int:
    return _quality_score_from_features(_features(resource), _quality_text_from_resource(resource))


def _score_media_item(media_item: dict[str, Any] | None) -> int:
    if not isinstance(media_item, dict):
        return 0
    profile = _media_quality_profile(media_item)
    features = {
        "is_cracked": bool(profile.get("is_cracked")),
        "has_subtitle": bool(profile.get("has_subtitle")),
        "is_new_model_uncensored_crack": bool(profile.get("is_new_model_uncensored_crack")),
    }
    text = " ".join(str(media_item.get(k) or "") for k in ("name", "path"))
    return _quality_score_from_features(features, text)


def _upgrade_improvement(sub: dict[str, Any], resource: dict[str, Any], config: dict[str, Any]) -> tuple[bool, int, int, int, str]:
    candidate_score = _score_resource_quality(resource)
    if sub.get("type") != "upgrade":
        return True, int(sub.get("current_score") or 0), candidate_score, 0, "订阅任务不需要洗版比较"
    current_score = int(sub.get("current_score") or 0)
    threshold = max(0, int(config.get("upgrade_score_threshold") or 20))
    current_profile = {
        "is_cracked": bool(sub.get("current_is_cracked")),
        "has_subtitle": bool(sub.get("current_has_subtitle")),
        "is_new_model_uncensored_crack": bool(sub.get("current_is_new_model_uncensored_crack")),
        "resolution_rank": int(sub.get("current_resolution_rank") or 0),
        "size_bytes": int(sub.get("current_size_bytes") or 0),
    }
    candidate_profile = _resource_quality_profile(resource)
    if candidate_profile.get("is_cracked") and not current_profile.get("is_cracked"):
        return True, current_score, candidate_score, threshold, "破解版本优先"
    if candidate_profile.get("is_new_model_uncensored_crack") and not current_profile.get("is_new_model_uncensored_crack"):
        return True, current_score, candidate_score, threshold, "新模型无码破解优先"
    if candidate_score >= current_score + threshold:
        return True, current_score, candidate_score, threshold, f"版本特征提升 +{candidate_score - current_score}"
    same_features = (
        current_profile["is_cracked"] == candidate_profile["is_cracked"]
        and current_profile["has_subtitle"] == candidate_profile["has_subtitle"]
    )
    if not same_features:
        return False, current_score, candidate_score, threshold, "版本特征未达到洗版阈值"

    if (
        candidate_profile.get("is_new_model_uncensored_crack")
        and not current_profile.get("is_new_model_uncensored_crack")
    ):
        return True, current_score, candidate_score, threshold, "同等版本特征下新模型无码破解优先"

    cur_res = int(current_profile.get("resolution_rank") or 0)
    new_res = int(candidate_profile.get("resolution_rank") or 0)
    cur_size = int(current_profile.get("size_bytes") or 0)
    new_size = int(candidate_profile.get("size_bytes") or 0)
    if cur_res and new_res and new_res > cur_res:
        return True, current_score, candidate_score, threshold, "同等版本特征下分辨率提升"
    if cur_size and new_size:
        ratio = new_size / max(cur_size, 1)
        if ratio >= 1.35:
            return True, current_score, candidate_score, threshold, f"同等版本特征下体积提升 {ratio:.2f}x"
        return False, current_score, candidate_score, threshold, f"同等版本特征但体积提升不足 {ratio:.2f}x"
    return False, current_score, candidate_score, threshold, "同等版本特征但缺少可比质量依据"


def _code_lookup_values(*values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        variants = [
            text,
            text.replace("-", "_"),
            text.replace("_", "-"),
            _extract_raw_code(text),
            _extract_code(text),
        ]
        for variant in variants:
            candidate = str(variant or "").strip()
            if not candidate:
                continue
            key = candidate.upper()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
    return out


def _subscription_code_values(sub: dict[str, Any]) -> list[str]:
    return _code_lookup_values(sub.get("search_code"), sub.get("code"), sub.get("title"))


def _resource_matches_code(resource: dict[str, Any], code: str) -> bool:
    target = _norm_code(code)
    if not target:
        return True
    values = [
        resource.get("id"),
        resource.get("title"),
        resource.get("subtitle"),
        resource.get("url"),
        resource.get("query_key"),
    ]
    raw = resource.get("raw") if isinstance(resource.get("raw"), dict) else {}
    values.extend([raw.get("id"), raw.get("title"), raw.get("name"), raw.get("number"), raw.get("code")])
    for value in values:
        text = str(value or "")
        if target and target in _norm_code(text):
            return True
    return False


def _matches(sub: dict[str, Any], resource: dict[str, Any]) -> tuple[bool, str]:
    mode = str(sub.get("mode") or "loose")
    require_cracked = bool(sub.get("require_cracked"))
    require_subtitle = bool(sub.get("require_subtitle"))
    f = _features(resource)
    if mode == "strict":
        if require_cracked and not f["is_cracked"]:
            return False, "缺少破解"
        if require_subtitle and not f["has_subtitle"]:
            return False, "缺少中字"
    return True, "匹配"


def _preference_rank(sub: dict[str, Any], resource: dict[str, Any]) -> tuple[int, int, int]:
    f = _features(resource)
    if f.get("is_new_model_uncensored_crack") and f["has_subtitle"]:
        tier = -1
    elif f["is_cracked"] and f["has_subtitle"]:
        tier = 0
    elif f["is_cracked"]:
        tier = 1
    elif f["has_subtitle"]:
        tier = 2
    else:
        tier = 3
    return (tier, -_score_resource(resource), -int(resource.get("size_bytes") or 0))


async def _find_media(code: str, *aliases: Any) -> dict[str, Any] | None:
    try:
        from app.api.endpoints import media_library as media_api

        config = media_api._load_config()
        if not config.get("server_url") or not config.get("api_key"):
            return None
        libraries = await media_api._list_libraries(config)
        raw_enabled = str(config.get("enabled_library_ids") or "")
        enabled_ids = [x.strip() for x in raw_enabled.split(",") if x.strip()]
        target_ids = enabled_ids or [lib.get("id") for lib in libraries if lib.get("id")]
        queries = _code_lookup_values(code, *aliases)
        targets = {_norm_code(x) for x in queries if _norm_code(x)}
        if not queries or not targets:
            return None
        for library_id in target_ids:
            for query in queries:
                items, _ = await media_api._list_items(config, library_id, limit=12, offset=0, q=query)
                for item in items:
                    nfo = item.get("nfo") if isinstance(item.get("nfo"), dict) else {}
                    text = " ".join(str(x or "") for x in [nfo.get("num"), item.get("name"), nfo.get("title"), item.get("path")])
                    norm_text = _norm_code(text)
                    if any(target and target in norm_text for target in targets):
                        return item
    except Exception:
        return None
    return None


async def _select_imported_variant(media: dict[str, Any], resource: dict[str, Any], old_path: str) -> dict[str, Any]:
    """Resolve a deduplicated library representative to the actual imported variant."""
    try:
        from app.api.endpoints import media_library as media_api
        config = media_api._load_config()
        detail = await media_api._get_item(config, str(media.get("id") or ""))
    except Exception:
        return media
    candidates: list[dict[str, Any]] = []
    if detail:
        candidates.append({**media, **detail, "path": detail.get("file_path") or detail.get("path") or media.get("path")})
        for sibling in detail.get("siblings") or []:
            candidates.append({
                **media,
                **sibling,
                "path": sibling.get("file_path") or sibling.get("path") or "",
                "tags": sibling.get("tags") or {},
                "subtitle_count": sibling.get("subtitle_count") or 0,
            })
    matches = [candidate for candidate in candidates if _media_matches_resource_profile(candidate, resource)]
    if not matches:
        return media
    distinct = [candidate for candidate in matches if str(candidate.get("path") or "") != str(old_path or "")]
    pool = distinct or matches
    return max(pool, key=lambda candidate: (_score_media_item(candidate), int(candidate.get("size_bytes") or 0), str(candidate.get("path") or "")))


async def _search_resources(code: str, limit: int = 24) -> list[dict[str, Any]]:
    from app.plugins.runtime import runtime

    query = {"keyword": code, "q": code, "code": code, "number": code, "limit": limit, "mode": "deep", "page": 1, "max_items": 100}
    data = await runtime.search_resources(query, limit_per_plugin=limit)
    groups = data.get("groups") if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups or []:
        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = f"{item.get('provider')}:{item.get('id')}:{item.get('url')}"
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


def _classify(media_item: dict[str, Any] | None) -> str:
    return "upgrade" if media_item else "subscribe"


def _source_payload(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "source_plugin": str(payload.get("source_plugin") or payload.get("sourcePlugin") or "").strip(),
        "source_label": str(payload.get("source_label") or payload.get("sourceLabel") or "").strip(),
        "source_route": str(payload.get("source_route") or payload.get("sourceRoute") or "").strip(),
        "source_context": str(payload.get("source_context") or payload.get("sourceContext") or "").strip(),
    }


def _candidate_snapshot(resource: dict[str, Any], *, current_score: int = 0, threshold: int = 0, upgrade_reason: str = "") -> dict[str, Any]:
    features = resource.get("subscription_features") if isinstance(resource.get("subscription_features"), dict) else _features(resource)
    quality_score = int(resource.get("subscription_quality_score") if resource.get("subscription_quality_score") is not None else _score_resource_quality(resource))
    return {
        "provider": resource.get("provider"),
        "provider_label": resource.get("provider_label") or resource.get("provider") or "",
        "id": resource.get("id"),
        "title": resource.get("title") or "",
        "subtitle": resource.get("subtitle") or "",
        "size_bytes": int(resource.get("size_bytes") or 0),
        "score": int(resource.get("subscription_score") if resource.get("subscription_score") is not None else _score_resource(resource)),
        "quality_score": quality_score,
        "current_score": current_score,
        "improvement": quality_score - int(current_score or 0),
        "required_improvement": threshold,
        "upgrade_reason": upgrade_reason,
        "match_reason": resource.get("match_reason") or "",
        "features": features,
        "preferred_downloader": resource.get("preferred_downloader") or "",
        "compatible_downloaders": resource.get("compatible_downloaders") or [],
    }


def _public_subscription(sub: dict[str, Any]) -> dict[str, Any]:
    out = dict(sub)
    best = out.get("best_resource") if isinstance(out.get("best_resource"), dict) else {}
    if not out.get("fanart_url"):
        out["fanart_url"] = best.get("fanart_url") or best.get("cover_url") or out.get("cover_url") or ""
    out["image_candidates"] = _image_candidates(out, best)
    out["current_profile"] = {
        "score": int(out.get("current_score") or 0),
        "is_cracked": bool(out.get("current_is_cracked")),
        "has_subtitle": bool(out.get("current_has_subtitle")),
        "is_new_model_uncensored_crack": bool(out.get("current_is_new_model_uncensored_crack")),
        "resolution_rank": int(out.get("current_resolution_rank") or 0),
        "size_bytes": int(out.get("current_size_bytes") or 0),
        "path": out.get("current_file_path") or "",
    }
    if best:
        features = best.get("features") if isinstance(best.get("features"), dict) else {}
        out["candidate_profile"] = {
            "score": int(best.get("score") or 0),
            "is_cracked": bool(features.get("is_cracked")),
            "has_subtitle": bool(features.get("has_subtitle")),
            "resolution_rank": _resolution_rank(" ".join(str(best.get(k) or "") for k in ("title", "subtitle", "provider_label"))),
            "size_bytes": int(best.get("size_bytes") or 0),
            "provider": best.get("provider_label") or best.get("provider") or "",
            "title": best.get("title") or "",
            "reason": best.get("upgrade_reason") or "",
            "improvement": best.get("improvement"),
            "required_improvement": best.get("required_improvement"),
        }
    else:
        out["candidate_profile"] = None
    recent = out.get("recent_candidates") if isinstance(out.get("recent_candidates"), list) else []
    out["recent_candidates"] = recent[:8]
    return out


def _download_identity(sub: dict[str, Any]) -> dict[str, str]:
    result = sub.get("submitted_result") if isinstance(sub.get("submitted_result"), dict) else {}
    torrent = result.get("torrent") if isinstance(result.get("torrent"), dict) else {}
    task = result.get("task") if isinstance(result.get("task"), dict) else {}
    if not task:
        nested = result.get("result") if isinstance(result.get("result"), dict) else {}
        task = nested.get("task") if isinstance(nested.get("task"), dict) else {}
    best = sub.get("best_resource") if isinstance(sub.get("best_resource"), dict) else {}
    resource = best.get("resource") if isinstance(best.get("resource"), dict) else best
    return {
        "hash": str(torrent.get("hash") or "").lower(),
        "task_id": str(task.get("id") or ""),
        "name": str(torrent.get("name") or task.get("name") or sub.get("code") or "").strip().lower(),
        "url": str(resource.get("url") or best.get("url") or "").strip(),
    }


def _download_stage(downloader_id: str, task: dict[str, Any]) -> dict[str, Any]:
    progress = max(0.0, min(float(task.get("progress") or 0), 1.0))
    raw_state = str(task.get("state") or task.get("phase") or "").strip()
    state = raw_state.lower()
    if downloader_id == "qbittorrent":
        if progress >= 1 or state in {"uploading", "stalledup", "queuedup", "forcedup", "stoppedup", "pausedup", "checkingup"}:
            stage, label, tone = "completed", "下载完成 · 等待入库", "success"
        elif state in {"error", "missedfiles", "unknown"}:
            stage, label, tone = "error", "下载异常", "danger"
        elif state in {"stoppeddl", "pauseddl"}:
            stage, label, tone = "paused", "下载已暂停", "warning"
        elif state in {"metadl", "checkingdl", "queueddl", "forcedmeta"}:
            stage, label, tone = "queued", "等待下载", "info"
        else:
            stage, label, tone = "downloading", "下载中", "primary"
    else:
        if progress >= 1 or "complete" in state:
            stage, label, tone = "completed", "下载完成 · 等待入库", "success"
        elif "error" in state or "fail" in state:
            stage, label, tone = "error", "下载异常", "danger"
        elif "paused" in state:
            stage, label, tone = "paused", "下载已暂停", "warning"
        elif "pending" in state:
            stage, label, tone = "queued", "等待下载", "info"
        else:
            stage, label, tone = "downloading", "下载中", "primary"
    return {
        "available": True,
        "downloader_id": downloader_id,
        "stage": stage,
        "label": label,
        "tone": tone,
        "progress": progress,
        "state": raw_state,
        "name": str(task.get("name") or ""),
        "savepath": str(task.get("save_path") or task.get("savepath") or task.get("real_path") or ""),
        "speed": int(task.get("dlspeed") or task.get("speed") or 0),
        "message": str(task.get("message") or ""),
    }


async def _attach_download_statuses(subscriptions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.plugins.runtime import runtime

    public = [_public_subscription(sub) for sub in subscriptions]
    groups: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = {}
    for item in public:
        downloader_id = str(item.get("submitted_downloader_id") or "").strip()
        if item.get("push_status") == "submitted" and downloader_id:
            groups.setdefault(downloader_id, []).append((item, _download_identity(item)))
    for downloader_id, targets in groups.items():
        try:
            if downloader_id == "qbittorrent":
                response = await runtime.handle_action(downloader_id, "torrents", {"limit": 500})
                tasks = response.get("items") if isinstance(response, dict) else []
            elif downloader_id == "xunlei-remote":
                response = await runtime.handle_action(downloader_id, "tasks", {"phase": "all", "limit": 500})
                tasks = response.get("tasks") if isinstance(response, dict) else []
            else:
                continue
            tasks = [task for task in (tasks or []) if isinstance(task, dict)]
            for item, identity in targets:
                match = next((task for task in tasks if identity["hash"] and str(task.get("hash") or "").lower() == identity["hash"]), None)
                if not match:
                    match = next((task for task in tasks if identity["task_id"] and str(task.get("id") or "") == identity["task_id"]), None)
                if not match:
                    match = next((task for task in tasks if identity["url"] and str(task.get("url") or "") == identity["url"]), None)
                if not match:
                    match = next((task for task in tasks if identity["name"] and identity["name"] in str(task.get("name") or "").lower()), None)
                item["download_status"] = _download_stage(downloader_id, match) if match else {
                    "available": False, "downloader_id": downloader_id, "stage": "missing",
                    "label": "下载器中未找到任务", "tone": "warning", "progress": 0,
                }
        except Exception as exc:
            for item, _identity in targets:
                item["download_status"] = {
                    "available": False, "downloader_id": downloader_id, "stage": "unavailable",
                    "label": "下载器状态不可用", "tone": "warning", "progress": 0,
                    "message": _public_error_message(exc),
                }
    return public


async def _create_subscription(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    raw_code = _extract_raw_code(payload.get("code") or payload.get("number") or payload.get("title"))
    code = _extract_code(payload.get("code") or payload.get("number") or payload.get("title"))
    if not code:
        raise ValueError("缺少作品番号")
    data = _ensure_store()
    existing = next((s for s in data["subscriptions"] if _norm_code(s.get("code")) == _norm_code(code) and s.get("status") != "deleted"), None)
    if existing:
        changed = False
        for key in ("title", "cover_url", "fanart_url"):
            raw_value = payload.get(key)
            if key == "cover_url" and not raw_value:
                raw_value = payload.get("image")
            if key == "fanart_url" and not raw_value:
                raw_value = payload.get("backdrop_url")
            value = str(raw_value or "").strip()
            if value and not str(existing.get(key) or "").strip():
                existing[key] = value
                changed = True
        source = _source_payload(payload)
        existing.setdefault("source_history", [])
        source_event = {**source, "at": _now()}
        if any(source.values()):
            existing["last_source"] = source
            existing["source_history"].insert(0, source_event)
            del existing["source_history"][20:]
            changed = True
        if raw_code and not existing.get("search_code"):
            existing["search_code"] = raw_code
            changed = True
        if changed:
            existing["updated_at"] = _now()
            if any(source.values()):
                _event(data, existing.get("id", ""), "info", "订阅入口已更新", source)
            _save(data)
        _schedule_immediate_check(config, existing.get("id", ""))
        return {"ok": True, "subscription": _public_subscription(existing), "created": False}
    media_item = await _find_media(code, raw_code)
    sub_type = str(payload.get("type") or "auto")
    if sub_type == "auto":
        sub_type = _classify(media_item)
    now = _now()
    current_profile = _media_quality_profile(media_item)
    source = _source_payload(payload)
    sub = {
        "id": uuid.uuid4().hex,
        "code": code,
        "search_code": raw_code or code,
        "title": str(payload.get("title") or code),
        "cover_url": str(payload.get("cover_url") or payload.get("image") or (media_item or {}).get("poster_path") or ""),
        "fanart_url": str(payload.get("fanart_url") or payload.get("backdrop_url") or (media_item or {}).get("fanart_path") or payload.get("cover_url") or payload.get("image") or ""),
        "image_candidates": _image_candidates(payload, media_item),
        "type": sub_type,
        "mode": str(payload.get("mode") or config.get("default_mode") or "loose"),
        "require_cracked": bool(payload.get("require_cracked", config.get("default_require_cracked", False))),
        "require_subtitle": bool(payload.get("require_subtitle", config.get("default_require_subtitle", False))),
        "status": "active",
        "push_status": "idle",
        "submit_attempts": 0,
        "last_submit_at": "",
        "last_submit_local_date": "",
        "last_submit_resource_key": "",
        "last_submit_error": "",
        "last_submit_error_kind": "",
        "retry_after_at": "",
        "submitted_downloader_id": "",
        "submitted_result": None,
        "current_media_item_id": str((media_item or {}).get("id") or ""),
        "current_file_path": str((media_item or {}).get("path") or ""),
        "current_score": int(payload.get("current_score") or _score_media_item(media_item)),
        "current_is_cracked": bool(current_profile.get("is_cracked")),
        "current_has_subtitle": bool(current_profile.get("has_subtitle")),
        "current_is_new_model_uncensored_crack": bool(current_profile.get("is_new_model_uncensored_crack")),
        "current_resolution_rank": int(current_profile.get("resolution_rank") or 0),
        "current_size_bytes": int(current_profile.get("size_bytes") or 0),
        "best_resource": None,
        "last_checked_at": "",
        "last_source": source,
        "source_history": [{**source, "at": now}] if any(source.values()) else [],
        "created_at": now,
        "updated_at": now,
    }
    data["subscriptions"].insert(0, sub)
    _event(data, sub["id"], "info", f"创建{'洗版' if sub_type == 'upgrade' else '订阅'}：{code}", {"media_found": bool(media_item), **{k: v for k, v in source.items() if v}})
    _save(data)
    _schedule_immediate_check(config, sub["id"])
    return {"ok": True, "subscription": _public_subscription(sub), "created": True}



async def _submit_best_download(config: dict[str, Any], sub: dict[str, Any], resource: dict[str, Any], *, automatic: bool = True, force: bool = False) -> dict[str, Any]:
    from app.plugins.runtime import runtime

    if not resource:
        raise ValueError("没有可推送的匹配资源")
    if not _is_retry_due(sub):
        return {"ok": False, "deferred": True, "reason": "retry_not_due", "retry_after_at": sub.get("retry_after_at")}
    resource_key = _resource_submit_key(resource)
    if automatic and not force and _auto_submit_blocked_today(sub, resource_key):
        return {"ok": False, "deferred": True, "reason": "already_auto_submitted_today", "retry_after_at": ""}

    provider_id = str(resource.get("provider") or sub.get("best_resource", {}).get("provider") or "").strip()
    if not provider_id:
        raise ValueError("资源缺少来源插件")
    resolved = await runtime.resolve_resource_download(provider_id, resource)
    resolved_item = resolved.get("item") if isinstance(resolved.get("item"), dict) else resource
    url = str(resolved.get("url") or resolved_item.get("url") or "").strip()
    if not url:
        raise ValueError("资源链接解析失败")
    compatible = [str(x) for x in (resolved_item.get("compatible_downloaders") or []) if str(x or "").strip()]
    preferred = str(resolved_item.get("preferred_downloader") or "").strip()
    downloader_id = _select_enabled_downloader(preferred, compatible, runtime)
    if not downloader_id:
        choices = "、".join(([preferred] if preferred else []) + compatible)
        raise ValueError(f"没有已启用的兼容下载器{f'（{choices}）' if choices else ''}")

    savepath = str(config.get("default_savepath") or "").strip()

    sub["submit_attempts"] = int(sub.get("submit_attempts") or 0) + 1
    sub["last_submit_at"] = _now()
    sub["last_submit_local_date"] = _local_date()
    sub["last_submit_resource_key"] = resource_key
    sub["pre_submit_file_fingerprint"] = _file_fingerprint(sub.get("current_file_path"))
    payload = {
        "url": url,
        "urls": url,
        "title": sub.get("code") or resolved_item.get("query_key") or resolved_item.get("title") or "NOOR订阅任务",
        "name": sub.get("code") or resolved_item.get("query_key") or resolved_item.get("title") or "NOOR订阅任务",
        "rename": sub.get("code") or resolved_item.get("query_key") or resolved_item.get("title") or "",
        "source_plugin_id": provider_id,
        "subscription_id": sub.get("id"),
        "subscription_code": sub.get("code"),
    }
    if savepath:
        payload["savepath"] = savepath
    try:
        result = await runtime.submit_download(downloader_id, payload)
    except Exception as exc:
        message = str(exc) or "推送下载失败"
        kind = _submit_error_kind(message)
        sub["push_status"] = "quota_limited" if kind == "downloader_quota_limited" else "failed"
        sub["status"] = "waiting_quota" if kind == "downloader_quota_limited" else "submit_failed"
        sub["last_submit_error"] = _public_error_message(message)
        sub["last_submit_error_kind"] = kind
        sub["retry_after_at"] = _next_xunlei_quota_retry_at() if kind == "downloader_quota_limited" else ""
        sub["updated_at"] = _now()
        raise

    sub["push_status"] = "submitted"
    sub["status"] = "submitted"
    sub["last_submit_error"] = ""
    sub["last_submit_error_kind"] = ""
    sub["retry_after_at"] = ""
    sub["submitted_downloader_id"] = downloader_id
    sub["submitted_result"] = result
    sub["updated_at"] = _now()
    return {"ok": True, "downloader_id": downloader_id, "result": result}

async def _check_subscription(config: dict[str, Any], sub: dict[str, Any], *, submit: bool | None = None) -> dict[str, Any]:
    code = str(sub.get("search_code") or sub.get("code") or "")
    resources = await _search_resources(code, limit=24)
    candidates = []
    for item in resources:
        if not _resource_matches_code(item, code):
            continue
        if _is_consumed_resource(sub, item):
            continue
        ok, reason = _matches(sub, item)
        if not ok:
            continue
        entry = dict(item)
        entry["subscription_score"] = _score_resource(item)
        entry["subscription_quality_score"] = _score_resource_quality(item)
        entry["subscription_features"] = _features(item)
        entry["match_reason"] = reason
        candidates.append(entry)
    candidates.sort(key=lambda item: _preference_rank(sub, item))
    best = candidates[0] if candidates else None
    sub["last_checked_at"] = _now()
    sub["updated_at"] = sub["last_checked_at"]
    sub["last_candidate_count"] = len(candidates)
    if best:
        upgrade_ok, current_score, candidate_score, threshold, upgrade_reason = _upgrade_improvement(sub, best, config)
        snapshots: list[dict[str, Any]] = []
        for candidate in candidates[:8]:
            ok_for_upgrade, c_current, _c_score, c_threshold, c_reason = _upgrade_improvement(sub, candidate, config)
            snap = _candidate_snapshot(candidate, current_score=c_current, threshold=c_threshold, upgrade_reason=c_reason)
            snap["upgrade_ok"] = bool(ok_for_upgrade)
            snapshots.append(snap)
        sub["recent_candidates"] = snapshots
        if not upgrade_ok:
            sub["best_resource"] = {
                "provider": best.get("provider"),
                "provider_label": best.get("provider_label"),
                "id": best.get("id"),
                "title": best.get("title"),
                "subtitle": best.get("subtitle"),
                "url": best.get("url"),
                "size_bytes": best.get("size_bytes"),
                "cover_url": best.get("cover_url") or best.get("fanart_url") or "",
                "fanart_url": best.get("fanart_url") or best.get("cover_url") or "",
                "score": candidate_score,
                "current_score": current_score,
                "improvement": candidate_score - current_score,
                "required_improvement": threshold,
                "upgrade_reason": upgrade_reason,
                "features": best.get("subscription_features"),
                "preferred_downloader": best.get("preferred_downloader"),
                "compatible_downloaders": best.get("compatible_downloaders") or [],
                "resource": best,
            }
            sub["status"] = "active"
            sub["push_status"] = "idle" if sub.get("push_status") not in {"quota_limited"} else sub.get("push_status")
            return {"subscription": sub, "best": None, "candidates": candidates[:12], "submit_result": None, "submit_error": "", "skip_reason": f"洗版提升不足：{upgrade_reason}；当前 {current_score} 分，候选 {candidate_score} 分，需要至少 +{threshold}"}
        sub["best_resource"] = {
            "provider": best.get("provider"),
            "provider_label": best.get("provider_label"),
            "id": best.get("id"),
            "title": best.get("title"),
            "subtitle": best.get("subtitle"),
            "url": best.get("url"),
            "size_bytes": best.get("size_bytes"),
            "cover_url": best.get("cover_url") or best.get("fanart_url") or "",
            "fanart_url": best.get("fanart_url") or best.get("cover_url") or "",
            "score": best.get("subscription_score"),
            "current_score": current_score,
            "improvement": candidate_score - current_score,
            "required_improvement": threshold,
            "upgrade_reason": upgrade_reason,
            "features": best.get("subscription_features"),
            "preferred_downloader": best.get("preferred_downloader"),
            "compatible_downloaders": best.get("compatible_downloaders") or [],
            "resource": best,
        }
        if sub.get("push_status") not in {"submitted", "quota_limited"}:
            sub["status"] = "matched"
    else:
        sub["best_resource"] = None
        sub["recent_candidates"] = []
        if sub.get("status") == "matched":
            sub["status"] = "active"
    submit_result = None
    submit_error = ""
    should_submit = bool(config.get("auto_submit_on_match", True)) if submit is None else bool(submit)
    if best and should_submit and sub.get("push_status") != "submitted":
        try:
            submit_result = await _submit_best_download(config, sub, best, automatic=True, force=False)
        except Exception as exc:
            submit_error = _public_error_message(exc)
    return {"subscription": sub, "best": best, "candidates": candidates[:12], "submit_result": submit_result, "submit_error": submit_error}


async def test(config: dict[str, Any]) -> PluginTestResult:
    data = _ensure_store()
    return PluginTestResult(ok=True, message="订阅中心可用", details={"subscriptions": len(data.get("subscriptions") or [])})


async def _run_due_checks(config: dict[str, Any], *, sub_id: str = "", force: bool = False, limit: int = 10, submit: bool | None = None) -> dict[str, Any]:
    async with _run_lock:
        data = _ensure_store()
        normalized_waits = _normalize_waiting_quota_records(data)
        targets = [s for s in data["subscriptions"] if s.get("status") != "deleted" and (not sub_id or s.get("id") == sub_id)]
        targets = [s for s in targets if _should_check_subscription(s, force=force)]
        if limit > 0:
            targets = targets[:limit]
        results = []
        for sub in targets:
            result = await _check_subscription(config, sub, submit=submit)
            results.append({
                "id": sub.get("id"),
                "code": sub.get("code"),
                "status": sub.get("status"),
                "best": result.get("best"),
                "candidate_count": len(result.get("candidates") or []),
                "submit_result": result.get("submit_result"),
                "submit_error": result.get("submit_error"),
            })
            if result.get("submit_error"):
                level = "warning" if sub.get("last_submit_error_kind") == "downloader_quota_limited" else "error"
                _event(data, sub.get("id", ""), level, result.get("submit_error") or "推送下载失败", {"candidate_count": len(result.get("candidates") or []), "retry_after_at": sub.get("retry_after_at"), "error_kind": sub.get("last_submit_error_kind")})
            elif result.get("submit_result") and not result.get("submit_result", {}).get("deferred"):
                _event(data, sub.get("id", ""), "success", "检测到匹配资源并已提交下载器", {"candidate_count": len(result.get("candidates") or []), "downloader_id": result.get("submit_result", {}).get("downloader_id")})
            elif result.get("submit_result") and result.get("submit_result", {}).get("deferred"):
                reason = result.get("submit_result", {}).get("reason")
                if reason == "already_auto_submitted_today":
                    _event(data, sub.get("id", ""), "info", "今日已自动提交过同一资源，跳过重复推送", {"resource_key": sub.get("last_submit_resource_key")})
                else:
                    _event(data, sub.get("id", ""), "info", "等待下载器额度恢复后继续推送", {"retry_after_at": sub.get("retry_after_at")})
            elif result.get("skip_reason"):
                _event(data, sub.get("id", ""), "info", result.get("skip_reason"), {"candidate_count": len(result.get("candidates") or [])})
            else:
                _event(data, sub.get("id", ""), "success" if result.get("best") else "info", "检测到匹配资源" if result.get("best") else "本次未匹配到资源", {"candidate_count": len(result.get("candidates") or [])})
        if normalized_waits:
            _event(data, "", "info", "已修正迅雷额度等待时间到 00:05")
        _save(data)
        return {"ok": True, "checked": len(targets), "results": results}


async def _submit_existing_best(config: dict[str, Any], sub_id: str, *, force: bool = False, force_submit: bool = False) -> dict[str, Any]:
    async with _run_lock:
        data = _ensure_store()
        sub = next((s for s in data["subscriptions"] if s.get("id") == sub_id and s.get("status") != "deleted"), None)
        if not sub:
            raise ValueError("订阅不存在")
        if force:
            sub["retry_after_at"] = ""
        best = sub.get("best_resource") if isinstance(sub.get("best_resource"), dict) else {}
        resource = best.get("resource") if isinstance(best.get("resource"), dict) else None
        if not resource:
            result = await _check_subscription(config, sub)
            resource = result.get("best") or ((sub.get("best_resource") or {}).get("resource") if isinstance(sub.get("best_resource"), dict) else None)
        if not resource:
            _save(data)
            raise ValueError("当前没有可推送的匹配资源")
        if sub.get("type") == "upgrade" and not force_submit:
            upgrade_ok, current_score, candidate_score, threshold, reason = _upgrade_improvement(sub, resource, config)
            if not upgrade_ok:
                sub["last_submit_error"] = f"未达到洗版条件：{reason}"
                sub["last_submit_error_kind"] = "upgrade_not_improved"
                sub["updated_at"] = _now()
                _event(data, sub_id, "info", sub["last_submit_error"], {"current_score": current_score, "candidate_score": candidate_score, "required_improvement": threshold})
                _save(data)
                raise ValueError(sub["last_submit_error"])
        try:
            submitted = await _submit_best_download(config, sub, resource, automatic=False, force=force or force_submit)
            _event(data, sub_id, "success", "资源已重新提交下载器", {"downloader_id": submitted.get("downloader_id"), "force_submit": force_submit})
            _save(data)
            return {"ok": True, "subscription": _public_subscription(sub), "submit_result": submitted}
        except Exception as exc:
            msg = _public_error_message(exc)
            level = "warning" if sub.get("last_submit_error_kind") == "downloader_quota_limited" else "error"
            _event(data, sub_id, level, msg, {"retry_after_at": sub.get("retry_after_at"), "error_kind": sub.get("last_submit_error_kind")})
            _save(data)
            raise ValueError(msg) from exc


async def _scheduler_loop() -> None:
    global _scheduler_stop
    from app.plugins.runtime import runtime

    _scheduler_stop = asyncio.Event()
    _log_system("info", "自动检测调度已启动")
    while not _scheduler_stop.is_set():
        try:
            if runtime.is_enabled(PLUGIN_ID):
                config = runtime.get_config(PLUGIN_ID)
                if bool(config.get("auto_check_enabled")):
                    data = _ensure_store()
                    reconcile = await _reconcile_submitted(data, config=config, limit=5)
                    if reconcile.get("checked"):
                        _save(data)
                        _log_system("info", "入库确认完成", {"checked": reconcile.get("checked"), "confirmed": reconcile.get("confirmed")})
                    result = await _run_due_checks(config, limit=5, submit=bool(config.get("background_submit_on_match", True)))
                    if result.get("checked"):
                        _log_system("info", "自动检测完成", {"checked": result.get("checked")})
                minutes = max(10, min(int(config.get("check_interval_minutes") or 60), 1440))
            else:
                minutes = 30
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_system("error", f"自动检测失败：{exc}")
            minutes = 30
        try:
            await asyncio.wait_for(_scheduler_stop.wait(), timeout=minutes * 60)
        except asyncio.TimeoutError:
            pass
    _log_system("info", "自动检测调度已停止")


async def start_background(_config: dict[str, Any] | None = None) -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())


async def stop_background() -> None:
    global _scheduler_task, _scheduler_stop
    if _scheduler_stop:
        _scheduler_stop.set()
    if _scheduler_task:
        _scheduler_task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await _scheduler_task
    _scheduler_task = None
    _scheduler_stop = None


def background_tasks(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Expose the subscription scheduler to NOOR's unified task view."""
    config = config or {}
    data = _ensure_store()
    events = data.get("events") if isinstance(data.get("events"), list) else []
    latest = next((event for event in events if isinstance(event, dict)), {})
    interval = max(10, min(int(config.get("check_interval_minutes") or 60), 1440))
    enabled = bool(config.get("auto_check_enabled", True))
    running = bool(_scheduler_task and not _scheduler_task.done())
    last_message = str(latest.get("message") or "")
    last_level = str(latest.get("level") or "")
    status = "running" if running and enabled else ("idle" if enabled else "disabled")
    if last_level == "error":
        status = "failed"
    return [{
        "id": "subscription-core.scheduler",
        "title": "订阅自动检测",
        "status": status,
        "last_run_at": latest.get("created_at") or None,
        "last_finished_at": latest.get("created_at") or None,
        "summary": f"{len(data.get('subscriptions') or [])} 个订阅 · 每 {interval} 分钟检测",
        "detail": last_message,
        "metrics": {"subscriptions": len(data.get("subscriptions") or []), "interval_minutes": interval, "auto_check_enabled": enabled},
    }]


async def _refresh_current_media(data: dict[str, Any], sub: dict[str, Any]) -> dict[str, Any]:
    lookup_codes = _subscription_code_values(sub)
    media = await _find_media(*(lookup_codes or [str(sub.get("code") or "")]))
    if media:
        profile = _media_quality_profile(media)
        sub["type"] = "upgrade"
        sub["current_media_item_id"] = str(media.get("id") or "")
        sub["current_file_path"] = str(media.get("path") or "")
        sub["current_score"] = _score_media_item(media)
        sub["current_is_cracked"] = bool(profile.get("is_cracked"))
        sub["current_has_subtitle"] = bool(profile.get("has_subtitle"))
        sub["current_is_new_model_uncensored_crack"] = bool(profile.get("is_new_model_uncensored_crack"))
        sub["current_resolution_rank"] = int(profile.get("resolution_rank") or 0)
        sub["current_size_bytes"] = int(profile.get("size_bytes") or 0)
        if media.get("fanart_path") and not sub.get("fanart_url"):
            sub["fanart_url"] = str(media.get("fanart_path") or "")
        if media.get("poster_path") and not sub.get("cover_url"):
            sub["cover_url"] = str(media.get("poster_path") or "")
        best = sub.get("best_resource") if isinstance(sub.get("best_resource"), dict) else {}
        resource = best.get("resource") if isinstance(best.get("resource"), dict) else best
        if resource and not _media_matches_resource_profile(media, resource):
            _forget_consumed_resource(sub, resource)
        message = "已刷新当前媒体库版本"
        payload = {"media_found": True, "current_score": sub.get("current_score"), "size_bytes": sub.get("current_size_bytes")}
    else:
        sub["type"] = "subscribe"
        sub["current_media_item_id"] = ""
        sub["current_file_path"] = ""
        sub["current_score"] = 0
        sub["current_is_cracked"] = False
        sub["current_has_subtitle"] = False
        sub["current_is_new_model_uncensored_crack"] = False
        sub["current_resolution_rank"] = 0
        sub["current_size_bytes"] = 0
        message = "媒体库未找到当前版本，已转为订阅"
        payload = {"media_found": False}
    sub["updated_at"] = _now()
    _event(data, sub.get("id", ""), "info", message, payload)
    return sub


def _apply_media_profile_to_subscription(sub: dict[str, Any], media: dict[str, Any]) -> None:
    profile = _media_quality_profile(media)
    sub["type"] = "upgrade"
    sub["current_media_item_id"] = str(media.get("id") or "")
    sub["current_file_path"] = str(media.get("path") or "")
    sub["current_score"] = _score_media_item(media)
    sub["current_is_cracked"] = bool(profile.get("is_cracked"))
    sub["current_has_subtitle"] = bool(profile.get("has_subtitle"))
    sub["current_is_new_model_uncensored_crack"] = bool(profile.get("is_new_model_uncensored_crack"))
    sub["current_resolution_rank"] = int(profile.get("resolution_rank") or 0)
    sub["current_size_bytes"] = int(profile.get("size_bytes") or 0)
    if media.get("fanart_path") and not sub.get("fanart_url"):
        sub["fanart_url"] = str(media.get("fanart_path") or "")
    if media.get("poster_path") and not sub.get("cover_url"):
        sub["cover_url"] = str(media.get("poster_path") or "")


async def _reconcile_submitted(data: dict[str, Any], *, config: dict[str, Any] | None = None, sub_id: str = "", limit: int = 10) -> dict[str, Any]:
    """Check submitted subscriptions against the media library.

    Upgrade tasks are confirmed only when the submitted profile is visible. Once confirmed,
    the old hardlink/source chain is removed automatically with replacement-path protection.
    """
    targets = [
        s for s in data.get("subscriptions", [])
        if s.get("status") == "submitted" and s.get("push_status") == "submitted" and (not sub_id or s.get("id") == sub_id)
    ]
    if limit > 0:
        targets = targets[:limit]
    confirmed = 0
    pending = 0
    for sub in targets:
        lookup_codes = _subscription_code_values(sub)
        media = await _find_media(*(lookup_codes or [str(sub.get("code") or "")]))
        if not media:
            pending += 1
            sub["last_completion_checked_at"] = _now()
            sub["updated_at"] = sub["last_completion_checked_at"]
            _event(data, sub.get("id", ""), "info", "已检查入库状态：媒体库暂未发现新版本")
            continue

        old_type = str(sub.get("type") or "subscribe")
        old_path = str(sub.get("current_file_path") or "")
        new_path = str(media.get("path") or "")
        submitted_best = sub.get("best_resource") if isinstance(sub.get("best_resource"), dict) else {}
        submitted_resource = submitted_best.get("resource") if isinstance(submitted_best.get("resource"), dict) else submitted_best
        media = await _select_imported_variant(media, submitted_resource, old_path)
        new_path = str(media.get("path") or "")
        profile_matches = _media_matches_resource_profile(media, submitted_resource)
        changed_in_place = bool(old_path and new_path and Path(old_path) == Path(new_path) and _same_path_was_replaced(sub, new_path))
        if not profile_matches or (old_type == "upgrade" and old_path == new_path and not changed_in_place):
            pending += 1
            sub["last_completion_checked_at"] = _now()
            sub["updated_at"] = sub["last_completion_checked_at"]
            _event(data, sub.get("id", ""), "info", "已检查入库状态：当前媒体仍是旧版本")
            continue
        _apply_media_profile_to_subscription(sub, media)
        consumed_key = _remember_consumed_resource(sub, submitted_resource)
        if not consumed_key:
            _forget_consumed_resource(sub, submitted_resource)
        _clear_submit_state(sub)
        sub["last_completion_checked_at"] = _now()
        sub["updated_at"] = sub["last_completion_checked_at"]
        confirmed += 1

        if old_type == "upgrade" and old_path and new_path and old_path != new_path:
            cleanup = {
                "old_path": old_path,
                "new_path": new_path,
                "status": "running",
                "reason": "新版本已入库，正在自动清理旧版本。",
                "created_at": _now(),
            }
            sub["cleanup_suggestion"] = cleanup
            try:
                from app.api.endpoints import media_library
                cleanup["result"] = await asyncio.to_thread(media_library.delete_replaced_media_chain, old_path, new_path, str(sub.get("code") or ""))
                cleanup["status"] = "completed"
                cleanup["completed_at"] = _now()
                cleanup["reason"] = "新版本已入库，旧版本及其硬链接源链已自动清理。"
                _event(data, sub.get("id", ""), "success", "洗版完成：新版本已入库并自动删除旧版本", {"old_path": old_path, "new_path": new_path})
            except Exception as exc:
                cleanup["status"] = "failed"
                cleanup["failed_at"] = _now()
                cleanup["reason"] = f"新版本已入库，但旧版本自动清理失败: {exc}"
                _event(data, sub.get("id", ""), "error", cleanup["reason"], {"old_path": old_path, "new_path": new_path})
        elif old_type == "upgrade" and changed_in_place:
            sub["cleanup_suggestion"] = {"old_path": old_path, "new_path": new_path, "status": "not_required", "reason": "新版本已在原路径完成替换，无独立旧文件需清理。", "completed_at": _now()}
            _event(data, sub.get("id", ""), "success", "洗版完成：新版本已在原路径替换", {"path": new_path})
        elif old_type == "subscribe":
            _event(data, sub.get("id", ""), "success", "订阅作品已入库，已自动转为洗版监控", {"path": new_path, "consumed_resource_key": consumed_key})
        else:
            _event(data, sub.get("id", ""), "success", "提交任务已在媒体库确认入库", {"path": new_path, "consumed_resource_key": consumed_key})
    return {"ok": True, "checked": len(targets), "confirmed": confirmed, "pending": pending}


async def handle_action(action: str, payload: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    payload = payload or {}
    data = _ensure_store()
    if _normalize_waiting_quota_records(data):
        _save(data)
    if action == "overview":
        subs = [s for s in data.get("subscriptions") or [] if s.get("status") != "deleted"]
        public_subs = await _attach_download_statuses(subs)
        stats = {
            "total": len(subs),
            "subscribe": sum(1 for s in subs if s.get("type") == "subscribe"),
            "upgrade": sum(1 for s in subs if s.get("type") == "upgrade"),
            "matched": sum(1 for s in subs if s.get("status") == "matched"),
            "submitted": sum(1 for s in subs if s.get("status") == "submitted"),
            "waiting_quota": sum(1 for s in subs if s.get("status") == "waiting_quota"),
            "active": sum(1 for s in subs if s.get("status") == "active"),
            "cleanup_pending": sum(1 for s in subs if isinstance(s.get("cleanup_suggestion"), dict) and s.get("cleanup_suggestion", {}).get("status") == "pending"),
            "downloading": sum(1 for s in public_subs if (s.get("download_status") or {}).get("stage") in {"queued", "downloading", "paused"}),
            "downloaded": sum(1 for s in public_subs if (s.get("download_status") or {}).get("stage") == "completed"),
        }
        return {"ok": True, "stats": stats, "items": public_subs, "events": data.get("events", [])[:50], "defaults": {"mode": config.get("default_mode") or "loose", "require_cracked": bool(config.get("default_require_cracked", False)), "require_subtitle": bool(config.get("default_require_subtitle", False)), "savepath": str(config.get("default_savepath") or "")}}
    if action == "classify":
        raw_code = _extract_raw_code(payload.get("code") or payload.get("title"))
        code = _extract_code(payload.get("code") or payload.get("title"))
        media = await _find_media(code, raw_code) if code else None
        return {"ok": True, "code": code, "type": _classify(media), "media_item": media}
    if action == "create":
        return await _create_subscription(config, payload)
    if action == "refresh_current":
        sub_id = str(payload.get("id") or "")
        sub = next((s for s in data["subscriptions"] if s.get("id") == sub_id and s.get("status") != "deleted"), None)
        if not sub:
            raise ValueError("订阅不存在")
        await _refresh_current_media(data, sub)
        _save(data)
        return {"ok": True, "subscription": _public_subscription(sub)}
    if action == "confirm_submitted":
        result = await _reconcile_submitted(data, config=config, sub_id=str(payload.get("id") or ""), limit=0)
        _save(data)
        return result
    if action == "reconcile_submitted":
        result = await _reconcile_submitted(data, config=config, limit=int(payload.get("limit") or 20))
        _save(data)
        return result
    if action == "ack_cleanup":
        sub_id = str(payload.get("id") or "")
        sub = next((s for s in data["subscriptions"] if s.get("id") == sub_id and s.get("status") != "deleted"), None)
        if not sub:
            raise ValueError("订阅不存在")
        suggestion = sub.get("cleanup_suggestion") if isinstance(sub.get("cleanup_suggestion"), dict) else None
        if not suggestion:
            raise ValueError("没有待处理旧版本建议")
        suggestion["status"] = "acknowledged"
        suggestion["acknowledged_at"] = _now()
        sub["updated_at"] = suggestion["acknowledged_at"]
        _event(data, sub_id, "info", "已确认旧版本处理建议", {"old_path": suggestion.get("old_path"), "new_path": suggestion.get("new_path")})
        _save(data)
        return {"ok": True, "subscription": _public_subscription(sub)}
    if action == "apply_defaults":
        ids = [str(x) for x in (payload.get("ids") or []) if str(x or "").strip()]
        target_type = str(payload.get("type") or "").strip()
        apply_all = bool(payload.get("all"))
        if not ids and not target_type and not apply_all:
            raise ValueError("缺少要套用的订阅范围")
        changed = 0
        for sub in data["subscriptions"]:
            if sub.get("status") == "deleted":
                continue
            if ids and sub.get("id") not in ids:
                continue
            if target_type and sub.get("type") != target_type:
                continue
            sub["mode"] = str(config.get("default_mode") or "loose")
            sub["require_cracked"] = bool(config.get("default_require_cracked", False))
            sub["require_subtitle"] = bool(config.get("default_require_subtitle", False))
            sub["updated_at"] = _now()
            changed += 1
            _event(data, sub.get("id", ""), "info", "已套用默认订阅规则", {"mode": sub["mode"], "require_cracked": sub["require_cracked"], "require_subtitle": sub["require_subtitle"]})
        _save(data)
        return {"ok": True, "changed": changed}
    if action == "update":
        sub_id = str(payload.get("id") or "")
        sub = next((s for s in data["subscriptions"] if s.get("id") == sub_id), None)
        if not sub:
            raise ValueError("订阅不存在")
        for key in ["mode", "require_cracked", "require_subtitle", "status", "title"]:
            if key in payload:
                sub[key] = payload[key]
        sub["updated_at"] = _now()
        _event(data, sub_id, "info", "更新订阅设置", {k: payload.get(k) for k in payload.keys() if k != "id"})
        _save(data)
        return {"ok": True, "subscription": _public_subscription(sub)}
    if action == "refresh_cover":
        from app.plugins.runtime import runtime

        code = str(payload.get("code") or "").strip()
        sub_id = str(payload.get("id") or "").strip()
        sub = None
        if sub_id:
            sub = next((s for s in data["subscriptions"] if s.get("id") == sub_id and s.get("status") != "deleted"), None)
        if not sub and code:
            target = _norm_code(code)
            sub = next((s for s in data["subscriptions"] if s.get("status") != "deleted" and target in {_norm_code(v) for v in _subscription_code_values(s)}), None)
        if not sub:
            raise ValueError("订阅不存在")
        lookup_code = code or str(sub.get("code") or sub.get("search_code") or "")
        if not lookup_code:
            raise ValueError("缺少番号")
        if not runtime.is_enabled("javdb"):
            raise ValueError("JavDB 插件未启用")
        detail = await runtime.handle_action("javdb", "video", {"code": lookup_code, "refresh": True})
        item = detail.get("data") if isinstance(detail, dict) else {}
        if not isinstance(item, dict):
            item = {}
        cover_url = str(item.get("cover_url") or "")
        thumb_url = str(item.get("thumb_url") or "")
        fanart_url = cover_url or thumb_url
        if cover_url:
            sub["cover_url"] = cover_url
        if thumb_url:
            sub["thumb_url"] = thumb_url
        if fanart_url:
            sub["fanart_url"] = fanart_url
        candidates = _image_candidates(item, item.get("previews"), item.get("preview_images"))
        if candidates:
            sub["image_candidates"] = candidates
        sub["cover_refreshed_at"] = _now()
        sub["updated_at"] = sub["cover_refreshed_at"]
        _save(data)
        return {
            "ok": True,
            "subscription": _public_subscription(sub),
            "cover_url": cover_url,
            "thumb_url": thumb_url,
            "fanart_url": fanart_url,
            "image_candidates": candidates,
        }
    if action == "delete":
        sub_id = str(payload.get("id") or "")
        sub = next((s for s in data["subscriptions"] if s.get("id") == sub_id), None)
        if not sub:
            raise ValueError("订阅不存在")
        sub["status"] = "deleted"
        sub["updated_at"] = _now()
        _event(data, sub_id, "warning", "删除订阅")
        _save(data)
        return {"ok": True}
    if action == "check_once":
        return await _run_due_checks(config, sub_id=str(payload.get("id") or ""), force=bool(payload.get("force", True)), limit=0, submit=bool(payload.get("submit", config.get("auto_submit_on_match", True))))
    if action in {"submit_best", "retry_submit"}:
        return await _submit_existing_best(config, str(payload.get("id") or ""), force=bool(payload.get("force", True)), force_submit=bool(payload.get("force_submit", False)))
    if action == "force_submit":
        return await _submit_existing_best(config, str(payload.get("id") or ""), force=True, force_submit=True)
    if action == "reset_submit":
        sub_id = str(payload.get("id") or "")
        sub = next((s for s in data["subscriptions"] if s.get("id") == sub_id and s.get("status") != "deleted"), None)
        if not sub:
            raise ValueError("订阅不存在")
        sub["status"] = "active"
        sub["push_status"] = "idle"
        sub["last_submit_error"] = ""
        sub["last_submit_error_kind"] = ""
        sub["retry_after_at"] = ""
        sub["submitted_downloader_id"] = ""
        sub["submitted_result"] = None
        sub["updated_at"] = _now()
        _event(data, sub_id, "info", "订阅已恢复监控")
        _save(data)
        return {"ok": True, "subscription": _public_subscription(sub)}
    if action == "run_due":
        return await _run_due_checks(config, force=bool(payload.get("force", False)), limit=int(payload.get("limit") or 10), submit=bool(payload.get("submit", False)))
    if action == "evaluate_resource":
        resource = payload.get("resource") if isinstance(payload.get("resource"), dict) else payload
        return {"ok": True, "score": _score_resource(resource), "features": _features(resource)}
    raise ValueError(f"unsupported action: {action}")
