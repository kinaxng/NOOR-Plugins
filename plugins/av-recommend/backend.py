from __future__ import annotations

import ast
import asyncio
import contextlib
import copy
import datetime as dt
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import async_session_maker
from app.core.models import EmbyItemCache
from app.core.runtime_paths import plugin_data_path
from app.knowledge.intelligence import actor_alias_names, actor_alias_revision, actor_identity_key, canonical_actor_name, canonical_preference_category, clear_preference_events, preference_behavior_summary, record_preference_event, search_intent_summary, semantic_tokens, work_similarity_recall_evaluation, work_similarity_status
from app.knowledge.models import KnowledgeActionState, KnowledgeEdge, KnowledgeEntity, WorkProfile
from app.plugins.contracts import PluginManifest, PluginTestResult

PLUGIN_ID = "av-recommend"


def _data_file() -> Path:
    return plugin_data_path("av-recommend", "feedback.json")


def _candidate_pool_file() -> Path:
    return plugin_data_path("av-recommend", "candidate_pool.json")


def _title_profile_file() -> Path:
    return plugin_data_path("av-recommend", "title_profile.json")


def _recommendation_cache_file() -> Path:
    return plugin_data_path("av-recommend", "recommendations_cache.json")


TITLE_PROFILE_VERSION = 2
RECOMMENDATION_ALGORITHM_VERSION = 37
PERSONALIZED_MODEL_VERSION = "personal-v37"
STABLE_MODEL_VERSION = "stable-v1"
CONVERSION_STAGE_VALUES = {
    "detail_view": 0.15,
    "feedback:like": 0.35,
    "subscription": 0.60,
    "download_intent": 0.75,
    "download_submitted": 0.85,
    "library_imported": 1.0,
    "upgrade_completed": 1.0,
}
SESSION_INTENT_HALF_LIFE_MS = 3 * 60 * 60 * 1000
SESSION_INTENT_MAX_AGE_MS = 12 * 60 * 60 * 1000
SESSION_INTENT_EVENT_WEIGHTS = {
    "detail_view": 0.35,
    "feedback:like": 0.8,
    "subscription": 1.0,
    "download_intent": 1.15,
    "download_submitted": 1.25,
}
QUALIFIED_CONVERSION_THRESHOLD = 0.50
VERIFIED_CONVERSION_THRESHOLD = 0.95
DEFAULT_CACHE_TTL = 1800
_CACHE: dict[str, Any] = {"entries": {}}
_LIVE_LIBRARY_CODES_CACHE: dict[str, Any] = {"ts": 0.0, "key": "", "codes": set(), "warning": ""}
_pool_lock = asyncio.Lock()
_recommendation_generation_locks = {"latest": asyncio.Lock(), "full": asyncio.Lock()}
_recommendation_refresh_tasks: dict[str, asyncio.Task[Any]] = {}
_scheduler_task: asyncio.Task[None] | None = None
_scheduler_stop: asyncio.Event | None = None
_prewarm_state: dict[str, Any] = {"status": "idle", "last_started_at": None, "last_finished_at": None, "last_error": "", "modes": []}
_profile_enrichment_task: asyncio.Task[None] | None = None
_profile_enrichment_pending: dict[str, dict[str, Any]] = {}
_profile_enrichment_attempts: dict[str, float] = {}
_profile_enrichment_state: dict[str, Any] = {"status": "idle", "queued": 0, "enriched": 0, "failed": 0, "coverage_queued": 0, "coverage_enriched": 0, "coverage_failed": 0, "coverage_last_queued_at": 0.0, "last_finished_at": None, "last_error": ""}
_title_profile_refresh_task: asyncio.Task[None] | None = None


def _recommendation_cache_id(cache_key: str) -> str:
    return hashlib.sha256(cache_key.encode("utf-8")).hexdigest()


def _load_recommendation_cache() -> dict[str, Any]:
    path = _recommendation_cache_file()
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "entries": {}}


def _recommendation_cache_get(cache_key: str, ttl_seconds: int = DEFAULT_CACHE_TTL, *, allow_stale: bool = False, annotate: bool = False, max_stale_seconds: int = 21600) -> dict[str, Any] | None:
    cache_id = _recommendation_cache_id(cache_key)
    entries = _CACHE.setdefault("entries", {})
    entry = entries.get(cache_id)
    if not isinstance(entry, dict):
        entry = _load_recommendation_cache().get("entries", {}).get(cache_id)
        if isinstance(entry, dict):
            entries[cache_id] = entry
    if not isinstance(entry, dict):
        return None
    age_seconds = max(0.0, time.time() - float(entry.get("ts") or 0))
    invalidated = bool(entry.get("invalidated_at"))
    stale = invalidated or age_seconds >= ttl_seconds
    if stale and (not allow_stale or age_seconds >= ttl_seconds + max_stale_seconds):
        return None
    value = entry.get("value")
    if not isinstance(value, dict):
        return None
    result = copy.deepcopy(value)
    if annotate:
        result["cache_status"] = {"status": "stale" if stale else "fresh", "age_seconds": round(age_seconds, 1), "refreshing": False}
    return result


def _recommendation_cache_put(cache_key: str, value: dict[str, Any], *, source_mode: str = "") -> None:
    cache_id = _recommendation_cache_id(cache_key)
    data = _load_recommendation_cache()
    entries = data.setdefault("entries", {})
    entries[cache_id] = {"ts": time.time(), "value": value, "source_mode": source_mode}
    data["entries"] = dict(
        sorted(entries.items(), key=lambda item: float((item[1] or {}).get("ts") or 0), reverse=True)[:8]
    )
    path = _recommendation_cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    _CACHE["entries"] = dict(data["entries"])


def _latest_mode_snapshot(source_mode: str, requested_limit: int, *, ttl_seconds: int = DEFAULT_CACHE_TTL, max_age_seconds: int = 23400) -> dict[str, Any] | None:
    """Return a bounded persisted snapshot without waiting for a generation lock."""
    entries = _load_recommendation_cache().get("entries", {})
    ranked = sorted(
        (entry for entry in entries.values() if isinstance(entry, dict) and str(entry.get("source_mode") or "") == source_mode),
        key=lambda entry: float(entry.get("ts") or 0),
        reverse=True,
    )
    for entry in ranked:
        age_seconds = max(0.0, time.time() - float(entry.get("ts") or 0))
        value = entry.get("value")
        if age_seconds > max_age_seconds or not isinstance(value, dict):
            continue
        result = copy.deepcopy(value)
        items = result.get("items") if isinstance(result.get("items"), list) else []
        stored_limit = max(len(items), int(result.get("requested_limit") or 0))
        if stored_limit < requested_limit:
            continue
        result["items"] = items[:requested_limit]
        result["total"] = len(result["items"])
        model_version = str((result.get("model") or {}).get("version") or "")
        stale = bool(entry.get("invalidated_at")) or age_seconds >= ttl_seconds or int(result.get("algorithm_version") or 0) != RECOMMENDATION_ALGORITHM_VERSION or model_version not in {PERSONALIZED_MODEL_VERSION, STABLE_MODEL_VERSION}
        result["cache_status"] = {"status": "stale" if stale else "fresh", "age_seconds": round(age_seconds, 1), "refreshing": stale}
        return result
    return None


def _invalidate_recommendation_cache(*, modes: set[str] | None = None, hard: bool = True, reason: str = "changed") -> None:
    data = _load_recommendation_cache()
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    now = time.time()
    retained: dict[str, Any] = {}
    for cache_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        mode = str(entry.get("source_mode") or "")
        targeted = not modes or not mode or mode in modes
        if targeted and hard:
            continue
        if targeted:
            entry = {**entry, "invalidated_at": now, "stale_reason": reason}
        retained[cache_id] = entry
    data["entries"] = retained
    _CACHE["entries"] = dict(retained)
    path = _recommendation_cache_file()
    if not retained:
        with contextlib.suppress(OSError):
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

CODE_RE = re.compile(r"\b(FC2[-_ ]?(?:PPV[-_ ]?)?\d{4,9}|[A-Z]{2,8}[-_ ]?\d{2,7}|\d{6}[-_]\d{2,5})\b", re.I)
GENERIC_CATEGORY_KEYWORDS = (
    "单体作品",
    "精选综合",
    "美少女电影",
    "高清",
    "高画质",
    "独家",
    "推荐",
    "热门",
    "有码",
    "无码",
    "4K",
    "8K",
    "FHD",
    "HD",
    "VR",
    "HDR",
    "60FPS",
)
TITLE_NOISE_PATTERNS = (
    r"\b(?:4k|8k|fhd|hd|uhd|vr|hdr|60fps|1080p|2160p)\b",
    r"(?:高画質|高画质|高清|超清|完全版|完整版|ノーカット|無修正|无码|有码|中文字幕|中字|字幕|破解|流出)",
    r"(?:独占|独家|先行配信|配信限定|限定配信|最新作|新作|人気|話題|おすすめ|推荐)",
)
TITLE_TERM_STOPWORDS = {
    "作品", "動画", "映画", "完全", "高清", "高画質", "高画质", "中文字幕", "字幕", "无码", "有码",
    "水卜", "さくら", "水ト", "ちゃん", "さん", "彼女", "女優", "セックス", "SEX", "AV",
    "する", "され", "した", "ない", "よう", "この", "その", "から", "まで", "ため", "こと",
    "に住", "住む", "住む地", "助け", "助ける", "女子", "秘密", "女囚",
    "雷系", "系女", "雷系女", "系女子", "地雷系女", "雷系女子", "的女", "ング", "アへ", "我的",
}
TITLE_TRAIT_PATTERNS: tuple[dict[str, Any], ...] = (
    {"name": "人妻", "group": "relationship", "weight": 1.0, "patterns": ("人妻", "既婚", "若妻", "奥さん", "奥様", "married")},
    {"name": "熟女", "group": "actor_type", "weight": 0.9, "patterns": ("熟女", "美熟女", "淑女", "アラサー", "アラフォー", "おばさん")},
    {"name": "素人", "group": "style", "weight": 0.9, "patterns": ("素人", "しろうと", "一般人", "amateur", "初撮り")},
    {"name": "新人", "group": "style", "weight": 0.8, "patterns": ("新人", "デビュー", "初登場", "初出演")},
    {"name": "制服", "group": "costume", "weight": 0.85, "patterns": ("制服", "セーラー", "ブレザー", "コスプレ", "cosplay")},
    {"name": "学生", "group": "role", "weight": 0.8, "patterns": ("女子校生", "jk", "学生", "女子大生", "校生")},
    {"name": "教师", "group": "role", "weight": 0.85, "patterns": ("教師", "先生", "女教師", "家庭教師", "講師")},
    {"name": "护士", "group": "role", "weight": 0.85, "patterns": ("看護師", "ナース", "护士", "nurse")},
    {"name": "OL", "group": "role", "weight": 0.8, "patterns": ("OL", "オフィス", "会社員", "職場", "職員", "受付嬢", "秘書")},
    {"name": "偶像", "group": "role", "weight": 0.75, "patterns": ("アイドル", "地下アイドル", "idol", "グラドル")},
    {"name": "出差旅行", "group": "scene", "weight": 0.75, "patterns": ("出張", "旅行", "温泉", "旅館", "ホテル", "宿泊")},
    {"name": "剧情", "group": "style", "weight": 0.75, "patterns": ("ドラマ", "剧情", "物語", "ストーリー", "演技", "台本")},
    {"name": "纪录实录", "group": "style", "weight": 0.65, "patterns": ("ドキュメント", "密着", "記録", "実録", "纪录")},
    {"name": "企划", "group": "style", "weight": 0.55, "patterns": ("企画", "企划", "検証", "チャレンジ", "実験")},
    {"name": "系列企划", "group": "style", "weight": 0.6, "patterns": ("総集編", "ベスト", "BEST", "合集", "傑作選", "精选")},
    {"name": "NTR", "group": "theme", "weight": 0.95, "patterns": ("NTR", "寝取", "ねとられ", "寝取られ", "寝取り")},
    {"name": "痴女", "group": "theme", "weight": 0.9, "patterns": ("痴女", "逆ナン", "誘惑", "挑発", "攻め")},
    {"name": "职场", "group": "scene", "weight": 0.75, "patterns": ("職場", "会社", "オフィス", "上司", "部下", "同僚", "社長")},
    {"name": "邻居", "group": "scene", "weight": 0.85, "patterns": ("隣人", "邻居", "鄰居", "近所", "邻家", "隣の", "隔壁")},
    {"name": "家庭亲属", "group": "scene", "weight": 0.75, "patterns": ("義母", "義姉", "義妹", "母", "姉", "妹", "家庭", "继母", "義父", "義兄")},
    {"name": "巨乳", "group": "body", "weight": 0.8, "patterns": ("巨乳", "爆乳", "美乳", "Gカップ", "Hカップ", "Iカップ", "Jカップ", "big tits", "big breast")},
    {"name": "苗条", "group": "body", "weight": 0.65, "patterns": ("スレンダー", "美脚", "細身", "モデル体型")},
    {"name": "运动", "group": "scene", "weight": 0.65, "patterns": ("スポーツ", "ジム", "ヨガ", "水泳", "競泳", "体操")},
    {"name": "脏乱房间", "group": "scene", "weight": 0.88, "patterns": ("汚部屋", "污部屋", "ゴミ部屋", "ゴミ屋敷", "脏房间", "髒房間")},
    {"name": "地雷系", "group": "style", "weight": 0.84, "patterns": ("地雷系", "量産型", "病みかわ", "メンヘラ", "病娇", "病嬌")},
    {"name": "逃脱囚禁", "group": "theme", "weight": 0.78, "patterns": ("脱獄", "逃獄", "逃狱", "監禁", "囚禁", "拘束", "監禁部屋")},
    {"name": "怀孕", "group": "theme", "weight": 0.85, "patterns": ("妊娠", "孕ませ", "怀孕", "懷孕", "受胎", "中出し妊娠", "孕婦", "孕妇")},
    {"name": "强制", "group": "theme", "weight": 0.82, "patterns": ("強姦", "强奸", "強制", "レイプ", "レ×プ", "無理やり", "無理矢理", "脅して", "脅迫", "轮奸", "輪姦")},
    {"name": "药物", "group": "theme", "weight": 0.72, "patterns": ("媚薬", "春薬", "薬漬け", "ザーメン", "大量注入", "药物", "媚药", "催眠薬", "睡眠薬")},
    {"name": "催眠", "group": "theme", "weight": 0.78, "patterns": ("催眠", "洗脳", "洗脑", "マインドコントロール")},
    {"name": "女佣", "group": "role", "weight": 0.78, "patterns": ("メイド", "女佣", "女僕", "女仆", "家政婦", "家事代行")},
    {"name": "中出", "group": "theme", "weight": 0.82, "patterns": ("中出し", "中出", "内射", "膣内射精", "種付け", "种付")},
    {"name": "肛交", "group": "theme", "weight": 0.72, "patterns": ("アナル", "ケツ穴", "肛交", "肛門", "后庭")},
    {"name": "性处理", "group": "theme", "weight": 0.74, "patterns": ("性処理", "便女", "肉便器", "処理係", "泄欲")},
    {"name": "万引少女", "group": "scene", "weight": 0.68, "patterns": ("万引き", "偷窃", "偷竊", "店長", "コンビニ")},
    {"name": "羞辱惩罚", "group": "theme", "weight": 0.7, "patterns": ("お仕置き", "懲罰", "惩罚", "羞辱", "分からせ")},
    {"name": "高潮绝顶", "group": "theme", "weight": 0.66, "patterns": ("絶頂", "イキ", "イキ狂", "高潮", "痙攣", "快感")},
    {"name": "美体肉感", "group": "body", "weight": 0.62, "patterns": ("肉感", "恵体", "極上ボディ", "美ボディ", "ドエロボディ")},
)


def _image_candidates(*items: Any) -> list[str]:
    out: list[str] = []
    keys = ("fanart_url", "cover_url", "thumb_url", "image", "poster_url", "jacket_url", "preview_url", "image_candidates")

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


def _pool_path() -> Path:
    return _candidate_pool_file()


def _subscription_path() -> Path:
    return plugin_data_path("subscription-core", "subscriptions.json")


def _pool() -> dict[str, Any]:
    try:
        data = json.loads(_pool_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_pool(pool: dict[str, Any]) -> None:
    path = _pool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pool["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _candidate_pool_background_stale(pool: dict[str, Any], background: dict[str, Any]) -> bool:
    if not background.get("running"):
        return False
    if background.get("finished_at"):
        return True
    started_at = background.get("started_at")
    last_scan_at = (pool.get("last_full_scan") or {}).get("at")
    try:
        if started_at and last_scan_at:
            started = dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            last_scan = dt.datetime.fromisoformat(str(last_scan_at).replace("Z", "+00:00"))
            if started < last_scan:
                return True
        started = dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt.timezone.utc)
        age = dt.datetime.now(dt.timezone.utc) - started.astimezone(dt.timezone.utc)
        return age.total_seconds() > 24 * 3600
    except (TypeError, ValueError):
        return True


def _pool_scan_due(pool: dict[str, Any], interval_minutes: int = 360) -> bool:
    previous = (pool.get("last_full_scan") or {}).get("at")
    try:
        value = dt.datetime.fromisoformat(str(previous).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - value.astimezone(dt.timezone.utc)).total_seconds() >= interval_minutes * 60
    except (TypeError, ValueError):
        return True


def _scan_interval_minutes(config: dict[str, Any]) -> int:
    """Read both the historical and temporary names used during recovery."""
    raw = config.get("full_scan_interval_minutes")
    if raw in (None, ""):
        raw = config.get("scan_interval_minutes")
    try:
        value = int(raw or 360)
    except (TypeError, ValueError):
        value = 360
    return max(30, min(value, 1440))


def _config_number(config: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    raw = config.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _preference_strength(config: dict[str, Any], key: str, legacy_key: str) -> float:
    if config.get(key) is not None:
        return _config_number(config, key, 100, 0, 100)
    return 100 if config.get(legacy_key, True) else -1


def _merge_candidate(existing: dict[str, Any] | None, item: dict[str, Any], source: str, label: str) -> dict[str, Any]:
    current = dict(existing or {})
    if not current:
        current.update(item)
        current["first_seen_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    else:
        for key, value in item.items():
            if value not in (None, "", [], {}) and (not current.get(key) or key in {"magnets_count", "has_cnsub", "is_cracked"}):
                current[key] = max(int(current.get(key) or 0), int(value or 0)) if key == "magnets_count" else bool(current.get(key) or value) if key in {"has_cnsub", "is_cracked"} else value
    tags = current.get("source_tags") if isinstance(current.get("source_tags"), list) else []
    if not any(isinstance(tag, dict) and tag.get("id") == source for tag in tags):
        tags.append({"id": source, "label": label, "date": dt.date.today().isoformat()})
    current["source_tags"] = tags[:16]
    current["last_seen_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    current["is_today_increment"] = bool(current.get("first_seen_at", "").startswith(dt.date.today().isoformat()))
    _ensure_title_profile(current)
    return current


def _candidate_pool_stats(pool: dict[str, Any]) -> dict[str, Any]:
    items = pool.get("items") if isinstance(pool.get("items"), dict) else {}
    return {
        "total": len(items),
        "today_increment": sum(bool(item.get("is_today_increment")) for item in items.values() if isinstance(item, dict)),
        "last_full_scan": pool.get("last_full_scan") or {},
        "background": pool.get("background") or {},
    }


def _backfill_candidate_pool_title_profiles(pool: dict[str, Any]) -> int:
    items = pool.get("items") if isinstance(pool.get("items"), dict) else {}
    changed = 0
    for code, item in list(items.items()):
        if not isinstance(item, dict):
            continue
        profile = item.get("title_profile")
        if isinstance(profile, dict) and int(profile.get("version") or 0) == TITLE_PROFILE_VERSION:
            continue
        _ensure_title_profile(item)
        items[code] = item
        changed += 1
    if changed:
        pool["items"] = items
    return changed


def _candidate_profile_gaps(item: dict[str, Any]) -> list[str]:
    gaps = []
    if not str(item.get("cover_url") or item.get("thumb_url") or "").strip():
        gaps.append("cover")
    if not (item.get("actors") or []):
        gaps.append("actors")
    if not (item.get("categories") or []):
        gaps.append("categories")
    title = str(item.get("title") or item.get("display_title") or "").strip()
    if not title or title == _candidate_code(item):
        gaps.append("title")
    return gaps


async def _run_profile_enrichment(config: dict[str, Any]) -> None:
    global _profile_enrichment_task
    from app.plugins.runtime import runtime

    enriched = failed = 0
    _profile_enrichment_state.update({"status": "running", "last_error": ""})
    try:
        while _profile_enrichment_pending:
            batch_codes = list(_profile_enrichment_pending)[:12]
            batch_jobs = {code: dict(_profile_enrichment_pending.get(code) or {}) for code in batch_codes}
            for code in batch_codes:
                _profile_enrichment_pending.pop(code, None)
            _profile_enrichment_state["queued"] = len(_profile_enrichment_pending)
            semaphore = asyncio.Semaphore(3)

            async def load(code: str) -> tuple[str, dict[str, Any] | None, str]:
                _profile_enrichment_attempts[code] = time.time()
                try:
                    async with semaphore:
                        result = await asyncio.wait_for(runtime.handle_action("javdb", "video", {"code": code}), timeout=12)
                    data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
                    return code, data if isinstance(data, dict) else None, ""
                except Exception as exc:
                    return code, None, str(exc)[:300]

            loaded = await asyncio.gather(*(load(code) for code in batch_codes))
            async with _pool_lock:
                pool = _pool()
                items = pool.get("items") if isinstance(pool.get("items"), dict) else {}
                for code, data, error in loaded:
                    item = items.get(code) if isinstance(items.get(code), dict) else {"code": code, "number": code}
                    item["profile_enrichment_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                    item["profile_enrichment_reason"] = str(batch_jobs.get(code, {}).get("reason") or "candidate_gap")
                    if not data:
                        item["profile_enrichment_error"] = error or "未返回作品详情"
                        failed += 1
                        if batch_jobs.get(code, {}).get("reason") == "offline_no_path":
                            _profile_enrichment_state["coverage_failed"] = int(_profile_enrichment_state.get("coverage_failed") or 0) + 1
                    else:
                        item = _merge_candidate(item, data, "core-profile", "Core 画像补全")
                        item["detail"] = data
                        item["actors"] = _names(data.get("actors")) or item.get("actors") or []
                        item["categories"] = _names(data.get("categories")) or item.get("categories") or []
                        item["cover_url"] = data.get("cover_url") or data.get("thumb_url") or item.get("cover_url") or ""
                        item["fanart_url"] = data.get("fanart_url") or data.get("cover_url") or item.get("fanart_url") or ""
                        item["profile_enrichment_error"] = ""
                        enriched += 1
                        if batch_jobs.get(code, {}).get("reason") == "offline_no_path":
                            _profile_enrichment_state["coverage_enriched"] = int(_profile_enrichment_state.get("coverage_enriched") or 0) + 1
                    items[code] = item
                pool["items"] = items
                _save_pool(pool)
        if enriched:
            from app.knowledge.intelligence import build_work_similarity_index
            await build_work_similarity_index(force=True)
            _invalidate_recommendation_cache(hard=False, reason="profile-enrichment")
            await _prewarm_recommendations(config, force=True, include_full=False)
        _profile_enrichment_state.update({"status": "idle", "enriched": enriched, "failed": failed, "last_finished_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _profile_enrichment_state.update({"status": "failed", "last_error": str(exc)[:500], "last_finished_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    finally:
        _profile_enrichment_task = None
        if _profile_enrichment_pending and not (_scheduler_stop and _scheduler_stop.is_set()):
            _profile_enrichment_task = asyncio.create_task(_run_profile_enrichment(dict(config)))


def _queue_profile_enrichment(config: dict[str, Any], items: list[dict[str, Any]], *, max_accept: int = 48, reason: str = "candidate_gap") -> int:
    global _profile_enrichment_task
    now = time.time()
    max_accept = max(1, min(int(max_accept or 1), 48))
    if reason == "offline_no_path" and now - float(_profile_enrichment_state.get("coverage_last_queued_at") or 0) < 6 * 3600:
        return 0
    pool_items = (_pool().get("items") or {}) if reason == "offline_no_path" else {}
    if reason == "offline_no_path":
        latest_persisted_at = 0.0
        for persisted in pool_items.values():
            if not isinstance(persisted, dict):
                continue
            with contextlib.suppress(ValueError, TypeError):
                latest_persisted_at = max(latest_persisted_at, dt.datetime.fromisoformat(str(persisted.get("profile_enrichment_at") or "")).timestamp())
        if now - latest_persisted_at < 6 * 3600:
            return 0
    accepted = 0
    for item in items:
        code = _candidate_code(item)
        requested_gaps = item.get("profile_gaps") if isinstance(item.get("profile_gaps"), list) else _candidate_profile_gaps(item)
        actionable_gaps = [gap for gap in requested_gaps if gap in {"cover", "actors", "categories", "title"}]
        persisted = pool_items.get(code) if isinstance(pool_items.get(code), dict) else {}
        persisted_at = 0.0
        with contextlib.suppress(ValueError, TypeError):
            persisted_at = dt.datetime.fromisoformat(str(persisted.get("profile_enrichment_at") or "")).timestamp()
        if not code or not actionable_gaps or now - max(float(_profile_enrichment_attempts.get(code) or 0), persisted_at) < 6 * 3600:
            continue
        if code not in _profile_enrichment_pending:
            _profile_enrichment_pending[code] = {"gaps": actionable_gaps, "queued_at": now, "reason": reason}
            accepted += 1
        if accepted >= max_accept or len(_profile_enrichment_pending) >= 48:
            break
    if reason == "offline_no_path" and accepted:
        _profile_enrichment_state["coverage_last_queued_at"] = now
        _profile_enrichment_state["coverage_queued"] = int(_profile_enrichment_state.get("coverage_queued") or 0) + accepted
    _profile_enrichment_state["queued"] = len(_profile_enrichment_pending)
    if _profile_enrichment_pending and (_profile_enrichment_task is None or _profile_enrichment_task.done()):
        _profile_enrichment_task = asyncio.create_task(_run_profile_enrichment(dict(config)))
    return accepted


def _profile_enrichment_public_state() -> dict[str, Any]:
    state = dict(_profile_enrichment_state)
    coverage_rows = [
        item for item in ((_pool().get("items") or {}).values())
        if isinstance(item, dict) and item.get("profile_enrichment_reason") == "offline_no_path"
    ]
    if coverage_rows:
        state["coverage_queued"] = max(int(state.get("coverage_queued") or 0), len(coverage_rows))
        state["coverage_enriched"] = max(int(state.get("coverage_enriched") or 0), sum(not item.get("profile_enrichment_error") for item in coverage_rows))
        state["coverage_failed"] = max(int(state.get("coverage_failed") or 0), sum(bool(item.get("profile_enrichment_error")) for item in coverage_rows))
    return state


def _subscription_codes() -> set[str]:
    try:
        data = json.loads(_subscription_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    rows = data.get("subscriptions") if isinstance(data, dict) else data
    return {_norm_code(row.get("code") if isinstance(row, dict) else row) for row in (rows or []) if _norm_code(row.get("code") if isinstance(row, dict) else row)}


def _media_item_codes(item: dict[str, Any]) -> set[str]:
    values = [
        item.get("name"),
        item.get("path"),
        item.get("file_path"),
        item.get("title"),
        item.get("originaltitle"),
        item.get("original_title"),
    ]
    nfo = item.get("nfo") if isinstance(item.get("nfo"), dict) else {}
    values.extend([
        nfo.get("num"),
        nfo.get("title"),
        nfo.get("originaltitle"),
        nfo.get("original_title"),
    ])
    out: set[str] = set()
    for value in values:
        code = _norm_code(value)
        if code:
            out.add(code)
    return out


async def _emby_cache_codes(db: Any) -> set[str]:
    try:
        rows = await db.execute(select(EmbyItemCache.items_json))
    except SQLAlchemyError:
        return set()
    out: set[str] = set()
    for raw in rows.scalars().all():
        try:
            items = json.loads(raw or "[]")
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                out.update(_media_item_codes(item))
    return out


def _code_fingerprint(codes: set[str]) -> str:
    if not codes:
        return ""
    digest = hashlib.sha1()
    for code in sorted(codes):
        digest.update(code.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest() -> PluginManifest:
    return PluginManifest(**json.loads((Path(__file__).with_name("plugin.json")).read_text(encoding="utf-8")))


manifest = _manifest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm_code(value: Any) -> str:
    text = str(value or "")
    match = CODE_RE.search(text)
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


def _norm_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _feedback_codes(entries: Any) -> set[str]:
    return {_norm_code(x.get("code") if isinstance(x, dict) else x) for x in (entries or []) if _norm_code(x.get("code") if isinstance(x, dict) else x)}


def _feedback_counter(entries: Any, key: str) -> Counter:
    counter: Counter = Counter()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        for value in entry.get(key) or []:
            text = str(value or "").strip()
            if text:
                if key == "actors":
                    text = actor_identity_key(text)
                elif key == "categories":
                    text = canonical_preference_category(text)
                if not text:
                    continue
                counter[text] += 1
    return counter


def _feedback_topic_counter(entries: Any) -> Counter:
    counter: Counter = Counter()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        topic = entry.get("interest_topic") or entry.get("interest_topic_hypothesis") or {}
        topic_id = str(topic.get("id") or "").strip() if isinstance(topic, dict) else ""
        if topic_id:
            counter[topic_id] += 1
    return counter


def _candidate_code(item: dict[str, Any]) -> str:
    return _norm_code(
        item.get("code")
        or item.get("number")
        or item.get("num")
        or item.get("display_title")
        or item.get("title")
        or item.get("original_title")
        or item.get("name")
        or item.get("id")
    )


def _media_library_item_codes(item: dict[str, Any]) -> set[str]:
    values: list[Any] = [
        item.get("code"),
        item.get("number"),
        item.get("num"),
        item.get("name"),
        item.get("title"),
        item.get("original_title"),
        item.get("file_path"),
        item.get("path"),
        item.get("emby_path"),
    ]
    provider_ids = item.get("provider_ids")
    if isinstance(provider_ids, dict):
        values.extend(provider_ids.values())
    nfo = item.get("nfo")
    if isinstance(nfo, dict):
        values.extend(nfo.get(key) for key in ("num", "id", "code", "title", "originaltitle"))
    siblings = item.get("siblings")
    if isinstance(siblings, list):
        for sibling in siblings:
            if isinstance(sibling, dict):
                values.extend(sibling.get(key) for key in ("label", "name", "file_path", "path"))
    codes = {_norm_code(value) for value in values if _norm_code(value)}
    combined = " ".join(str(value or "") for value in values)
    code = _norm_code(combined)
    if code:
        codes.add(code)
    return codes


def _media_library_cache_key(config: dict[str, Any]) -> str:
    try:
        from app.api.endpoints.media_library_helpers import load_config

        media_config = load_config()
    except Exception:
        media_config = {}
    return json.dumps({
        "server_url": media_config.get("server_url") or "",
        "api_key": bool(media_config.get("api_key")),
        "user_id": media_config.get("user_id") or "",
        "enabled_library_ids": media_config.get("enabled_library_ids") or "",
        "limit": config.get("library_exclusion_scan_limit") or "",
    }, ensure_ascii=False, sort_keys=True)


async def _live_library_codes(config: dict[str, Any], *, force: bool = False) -> tuple[set[str], str]:
    """Fetch a lightweight Emby code set so recommendations track fresh library changes.

    Knowledge Core is still the primary profile source.  This is only a TTL
    exclusion guard for recently added media that has not been indexed yet.
    """
    cache_key = _media_library_cache_key(config)
    now = time.time()
    if not force and _LIVE_LIBRARY_CODES_CACHE.get("key") == cache_key and now - float(_LIVE_LIBRARY_CODES_CACHE.get("ts") or 0) < 300:
        return set(_LIVE_LIBRARY_CODES_CACHE.get("codes") or set()), str(_LIVE_LIBRARY_CODES_CACHE.get("warning") or "")
    try:
        from app.api.endpoints import media_library
        from app.api.endpoints.media_library_helpers import load_config

        media_config = load_config()
        if not media_config.get("server_url") or not media_config.get("api_key"):
            return set(), ""
        enabled = [value.strip() for value in str(media_config.get("enabled_library_ids") or "").split(",") if value.strip()]
        limit = max(100, min(int(config.get("library_exclusion_scan_limit") or 5000), 20000))
        page_limit = 500
        codes: set[str] = set()
        targets: list[dict[str, Any]] = []
        if enabled:
            targets = [{"id": value} for value in enabled]
        else:
            targets = await media_library._list_libraries(media_config)
        for library in targets:
            library_id = library.get("id")
            offset = 0
            while len(codes) < limit:
                items, _ = await media_library._list_items(
                    media_config,
                    library_id=library_id,
                    limit=min(page_limit, limit - len(codes)),
                    offset=offset,
                    force_refresh=force,
                )
                if not items:
                    break
                for item in items:
                    if isinstance(item, dict):
                        codes.update(_media_library_item_codes(item))
                offset += len(items)
                if len(items) < page_limit:
                    break
        _LIVE_LIBRARY_CODES_CACHE.update({"ts": now, "key": cache_key, "codes": set(codes), "warning": ""})
        return codes, ""
    except Exception as exc:
        warning = f"实时媒体库排除失败：{exc}"
        _LIVE_LIBRARY_CODES_CACHE.update({"ts": now, "key": cache_key, "codes": set(), "warning": warning})
        return set(), warning


def _ensure_store() -> dict[str, Any]:
    data_file = _data_file()
    data_file.parent.mkdir(parents=True, exist_ok=True)
    if not data_file.exists():
        data = {"version": 3, "ignored": [], "liked": [], "disliked": [], "exposures": {}, "exposure_batches": [], "session_intents": []}
        data_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    try:
        data = json.loads(data_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid feedback store")
        data["version"] = max(3, int(data.get("version") or 0))
        data.setdefault("ignored", [])
        data.setdefault("liked", [])
        data.setdefault("disliked", [])
        data.setdefault("exposures", {})
        data.setdefault("exposure_batches", [])
        data.setdefault("session_intents", [])
        return data
    except Exception:
        backup = data_file.with_suffix(f".{int(time.time())}.bak")
        try:
            data_file.replace(backup)
        except Exception:
            pass
        return _ensure_store()


def _save_store(data: dict[str, Any]) -> None:
    data_file = _data_file()
    data_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = data_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(data_file)


def _record_session_intent(store: dict[str, Any], payload: dict[str, Any], event_type: str, *, now_ms: int | None = None) -> bool:
    weight = float(SESSION_INTENT_EVENT_WEIGHTS.get(event_type) or 0)
    code = _norm_code(payload.get("code"))
    if weight <= 0 or not code:
        return False
    now = int(now_ms or _now_ms())
    topic = payload.get("interest_topic") or payload.get("interest_topic_hypothesis") or {}
    topic_id = str(topic.get("id") or "").strip()[:64] if isinstance(topic, dict) else ""
    actors = list(dict.fromkeys(actor_identity_key(value) for value in payload.get("actors") or [] if actor_identity_key(value)))[:8]
    categories = list(dict.fromkeys(canonical_preference_category(value) for value in payload.get("categories") or [] if canonical_preference_category(value)))[:12]
    rows = [row for row in store.get("session_intents") or [] if isinstance(row, dict) and now - int(row.get("created_at") or 0) <= SESSION_INTENT_MAX_AGE_MS]
    if any(row.get("code") == code and row.get("event_type") == event_type and now - int(row.get("created_at") or 0) < 60_000 for row in rows[-12:]):
        store["session_intents"] = rows[-300:]
        return False
    rows.append({"code": code, "event_type": event_type, "created_at": now, "weight": weight, "actors": actors, "categories": categories, "topic_id": topic_id})
    store["session_intents"] = rows[-300:]
    return True


def _session_intent_summary(store: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    now = int(now_ms or _now_ms())
    actors: Counter = Counter()
    categories: Counter = Counter()
    topics: Counter = Counter()
    retained: list[dict[str, Any]] = []
    for row in store.get("session_intents") or []:
        if not isinstance(row, dict):
            continue
        age = max(0, now - int(row.get("created_at") or 0))
        if age > SESSION_INTENT_MAX_AGE_MS:
            continue
        retained.append(row)
        effective = float(row.get("weight") or 0) * math.pow(0.5, age / SESSION_INTENT_HALF_LIFE_MS)
        for identity in row.get("actors") or []:
            if identity:
                actors[str(identity)] += effective
        for category in row.get("categories") or []:
            if category:
                categories[str(category)] += effective
        if row.get("topic_id"):
            topics[str(row["topic_id"])] += effective
    latest = max((int(row.get("created_at") or 0) for row in retained), default=0)
    # Event insertion changes the revision; ordinary decay is refreshed by the
    # recommendation TTL instead of creating a different cache key every second.
    revision_source = f"{len(retained)}:{latest}"
    return {
        "event_count": len(retained),
        "actors": dict(actors),
        "categories": dict(categories),
        "topics": dict(topics),
        "revision": hashlib.sha256(revision_source.encode("utf-8")).hexdigest()[:16],
    }


def _contextual_intent_gate(session: dict[str, Any], search: dict[str, Any]) -> dict[str, Any]:
    dimensions = [
        session.get("actors") or {}, session.get("categories") or {}, session.get("topics") or {},
        search.get("actors") or {}, search.get("categories") or {}, search.get("terms") or {}, search.get("combinations") or {},
    ]
    active_dimensions = [
        [max(0.0, float(value or 0)) for value in dimension.values() if float(value or 0) > 0]
        for dimension in dimensions if isinstance(dimension, dict) and dimension
    ]
    active_dimensions = [values for values in active_dimensions if values]
    event_count = int(session.get("event_count") or 0) + int(search.get("event_count") or 0)
    if not active_dimensions or event_count <= 0:
        return {"active": False, "gate": 0.0, "event_count": 0, "reliability": 0.0, "concentration": 0.0, "agreement": 0.0}
    concentration = sum(max(values) / max(sum(values), 1e-9) for values in active_dimensions) / len(active_dimensions)
    reliability = 1 - math.exp(-event_count / 2.5)
    actor_agreement = bool(set(session.get("actors") or {}) & set(search.get("actors") or {}))
    category_agreement = bool(set(session.get("categories") or {}) & set(search.get("categories") or {}))
    agreement = (int(actor_agreement) + int(category_agreement)) / 2
    gate = min(0.24, 0.18 * reliability * (0.65 + 0.35 * concentration) * (1 + 0.2 * agreement))
    return {
        "active": gate >= 0.025,
        "gate": round(gate, 4),
        "event_count": event_count,
        "reliability": round(reliability, 4),
        "concentration": round(concentration, 4),
        "agreement": round(agreement, 3),
    }


def _exposure_fatigue(store: dict[str, Any], *, now_ms: int | None = None) -> dict[str, dict[str, float]]:
    now_ms = int(now_ms or _now_ms())
    hour_ms = 3600 * 1000
    day_ms = 24 * hour_ms
    fatigue: dict[str, dict[str, float]] = {}
    for raw_code, row in (store.get("exposures") or {}).items():
        if not isinstance(row, dict) or _conversion_value(row) >= QUALIFIED_CONVERSION_THRESHOLD:
            continue
        code = _norm_code(raw_code)
        if not code:
            continue
        batches = int(row.get("batch_count") or 0)
        first_seen = int(row.get("first_seen_at") or now_ms)
        history = [item for item in (row.get("impression_history") or []) if isinstance(item, dict) and int(item.get("at") or 0) > 0]
        if not history and row.get("last_seen_at"):
            history = [{"at": int(row.get("last_seen_at") or now_ms), "rank": int(row.get("last_rank") or 0)}]
        short = daily = 0.0
        for impression in history:
            age = max(0, now_ms - int(impression.get("at") or now_ms))
            rank = int(impression.get("rank") or 0)
            rank_factor = 1.0 if 0 < rank <= 10 else 0.75 if 0 < rank <= 24 else 0.45
            if age <= 12 * hour_ms:
                short += 0.65 * rank_factor * math.pow(0.5, age / (3 * hour_ms))
            if age <= 3 * day_ms:
                daily += 0.28 * rank_factor * math.pow(0.5, age / (36 * hour_ms))
        short = min(1.8, short)
        daily = min(1.8, daily)
        long_term = min(3.5, 0.65 + (batches - 3) * 0.4) if batches >= 3 and now_ms - first_seen >= 7 * day_ms else 0.0
        last_seen = int(row.get("last_seen_at") or first_seen)
        if long_term and now_ms - last_seen > 7 * day_ms:
            long_term *= math.pow(0.5, (now_ms - last_seen - 7 * day_ms) / (14 * day_ms))
        engagement_multiplier = max(0.6, 1 - _conversion_value(row) * 1.5)
        total = min(6.0, (short + daily + long_term) * engagement_multiplier)
        if total >= 0.1:
            fatigue[code] = {
                "total": round(total, 2),
                "short": round(short * engagement_multiplier, 2),
                "daily": round(daily * engagement_multiplier, 2),
                "long": round(long_term * engagement_multiplier, 2),
                "engagement_multiplier": round(engagement_multiplier, 3),
            }
    return fatigue


def _exposure_penalties(store: dict[str, Any], *, now_ms: int | None = None) -> dict[str, float]:
    return {code: float(row["total"]) for code, row in _exposure_fatigue(store, now_ms=now_ms).items()}


def _mark_exposure_converted(store: dict[str, Any], code: Any, event_type: str = "interaction") -> bool:
    canonical = _norm_code(code)
    row = (store.get("exposures") or {}).get(canonical)
    event_type = str(event_type or "interaction")[:64]
    value = float(CONVERSION_STAGE_VALUES.get(event_type, 0.0))
    if not canonical or not isinstance(row, dict) or value <= 0:
        return False
    current_value = _conversion_value(row)
    if value <= current_value:
        return False
    now = _now_ms()
    row["converted_at"] = int(row.get("converted_at") or now)
    row["last_conversion_at"] = now
    row["conversion_event"] = event_type
    row["conversion_stage"] = event_type
    row["conversion_value"] = value
    row["converted_model"] = str(row.get("converted_model") or row.get("last_model") or "unknown")
    row["converted_rank"] = int(row.get("converted_rank") or row.get("last_rank") or 0)
    row["converted_routes"] = list(row.get("converted_routes") or row.get("last_routes") or [])
    row["converted_strategy"] = str(row.get("converted_strategy") or row.get("last_strategy") or "ranking")
    row["converted_topic_ids"] = list(row.get("converted_topic_ids") or row.get("last_topic_ids") or [])
    history = [item for item in (row.get("conversion_history") or []) if isinstance(item, dict)]
    history.append({"stage": event_type, "value": value, "at": now})
    row["conversion_history"] = history[-12:]
    return True


def _conversion_value(row: dict[str, Any]) -> float:
    if not isinstance(row, dict):
        return 0.0
    if row.get("conversion_value") is not None:
        return max(0.0, min(1.0, float(row.get("conversion_value") or 0.0)))
    legacy_stage = str(row.get("conversion_stage") or row.get("conversion_event") or "")
    if legacy_stage in CONVERSION_STAGE_VALUES:
        return CONVERSION_STAGE_VALUES[legacy_stage]
    return 1.0 if row.get("converted_at") or row.get("converted_model") else 0.0


def _sync_core_conversion_stages(store: dict[str, Any], stages: Any) -> int:
    if not isinstance(stages, dict):
        return 0
    upgraded = 0
    for code, stage in stages.items():
        if isinstance(stage, dict) and _mark_exposure_converted(store, code, str(stage.get("stage") or "")):
            upgraded += 1
    return upgraded


def _record_exposure_batch(store: dict[str, Any], batch_id: str, items: list[dict[str, Any]]) -> int:
    batch_id = str(batch_id or "").strip()[:160]
    batches = [str(value) for value in (store.get("exposure_batches") or [])]
    if not batch_id or batch_id in batches:
        return 0
    now = _now_ms()
    exposures = store.setdefault("exposures", {})
    recorded = 0
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        code = _norm_code(item.get("code"))
        if not code:
            continue
        row = exposures.get(code) if isinstance(exposures.get(code), dict) else {}
        if _conversion_value(row) >= VERIFIED_CONVERSION_THRESHOLD:
            continue
        rank = max(0, int(item.get("rank") or 0))
        row.update({
            "code": code,
            "first_seen_at": int(row.get("first_seen_at") or now),
            "last_seen_at": now,
            "batch_count": int(row.get("batch_count") or 0) + 1,
            "actors": [str(value).strip() for value in (item.get("actors") or []) if str(value or "").strip()][:8],
            "categories": [str(value).strip() for value in (item.get("categories") or []) if str(value or "").strip()][:12],
        })
        impression_history = [entry for entry in (row.get("impression_history") or []) if isinstance(entry, dict)]
        impression_history.append({"at": now, "rank": rank, "batch_id": batch_id})
        row["impression_history"] = impression_history[-48:]
        routes = [str(value).strip()[:64] for value in (item.get("recall_sources") or []) if str(value or "").strip()]
        route_evidence = row.setdefault("routes", {})
        for route in dict.fromkeys(routes):
            evidence = route_evidence.get(route) if isinstance(route_evidence.get(route), dict) else {}
            evidence.update({
                "batch_count": int(evidence.get("batch_count") or 0) + 1,
                "first_seen_at": int(evidence.get("first_seen_at") or now),
                "last_seen_at": now,
                "last_rank": max(0, int(item.get("rank") or 0)),
            })
            route_evidence[route] = evidence
        row["last_routes"] = list(dict.fromkeys(routes))
        strategy = "exploration" if item.get("is_exploration") else "ranking"
        strategies = row.setdefault("strategies", {})
        strategy_evidence = strategies.get(strategy) if isinstance(strategies.get(strategy), dict) else {}
        strategy_evidence.update({
            "batch_count": int(strategy_evidence.get("batch_count") or 0) + 1,
            "first_seen_at": int(strategy_evidence.get("first_seen_at") or now),
            "last_seen_at": now,
            "last_rank": max(0, int(item.get("rank") or 0)),
        })
        strategies[strategy] = strategy_evidence
        row["last_strategy"] = strategy
        row["exploration_kind"] = str(item.get("exploration_kind") or "")[:64]
        topic = item.get("interest_topic") or item.get("interest_topic_hypothesis") or {}
        topic_id = str(topic.get("id") or "")[:64] if isinstance(topic, dict) else ""
        topic_confidence = float(topic.get("confidence") or 0) if isinstance(topic, dict) else 0.0
        topic_support = int(topic.get("support") or 0) if isinstance(topic, dict) else 0
        topic_ids: list[str] = []
        if topic_id and topic_confidence >= 0.35 and topic_support >= 2:
            topic_evidence = row.setdefault("topics", {})
            evidence = topic_evidence.get(topic_id) if isinstance(topic_evidence.get(topic_id), dict) else {}
            evidence.update({
                "label": str(topic.get("label") or topic_id)[:160],
                "batch_count": int(evidence.get("batch_count") or 0) + 1,
                "first_seen_at": int(evidence.get("first_seen_at") or now),
                "last_seen_at": now,
                "last_rank": max(0, int(item.get("rank") or 0)),
                "confidence": round(topic_confidence, 3),
                "support": topic_support,
            })
            topic_evidence[topic_id] = evidence
            topic_ids.append(topic_id)
        row["last_topic_ids"] = topic_ids
        model_version = str(item.get("model_version") or "unknown")[:64]
        score_value = float(item.get("score") or 0)
        models = row.setdefault("models", {})
        model = models.get(model_version) if isinstance(models.get(model_version), dict) else {}
        model.update({
            "batch_count": int(model.get("batch_count") or 0) + 1,
            "first_seen_at": int(model.get("first_seen_at") or now),
            "last_seen_at": now,
            "last_rank": rank,
            "best_rank": min([value for value in (int(model.get("best_rank") or 0), rank) if value > 0] or [0]),
            "score_sum": round(float(model.get("score_sum") or 0) + score_value, 3),
        })
        models[model_version] = model
        shadow_models = row.setdefault("shadow_models", {})
        raw_shadow_ranks = item.get("shadow_ranks") if isinstance(item.get("shadow_ranks"), dict) else {}
        for shadow_version, shadow_rank_value in list(raw_shadow_ranks.items())[:4]:
            shadow_version = str(shadow_version or "")[:64]
            shadow_rank = max(0, int(shadow_rank_value or 0))
            if not shadow_version or shadow_rank <= 0:
                continue
            evidence = shadow_models.get(shadow_version) if isinstance(shadow_models.get(shadow_version), dict) else {}
            evidence.update({
                "batch_count": int(evidence.get("batch_count") or 0) + 1,
                "first_seen_at": int(evidence.get("first_seen_at") or now),
                "last_seen_at": now,
                "last_rank": shadow_rank,
                "best_rank": min([value for value in (int(evidence.get("best_rank") or 0), shadow_rank) if value > 0] or [0]),
            })
            shadow_models[shadow_version] = evidence
        row["last_model"] = model_version
        row["last_rank"] = rank
        exposures[code] = row
        recorded += 1
    store["exposure_batches"] = [batch_id, *batches][:128]
    retained = [
        (code, row) for code, row in exposures.items()
        if isinstance(row, dict) and (not row.get("converted_at") or now - int(row.get("converted_at") or now) < 90 * 86400 * 1000)
    ]
    retained.sort(key=lambda pair: int(pair[1].get("last_seen_at") or 0), reverse=True)
    store["exposures"] = dict(retained[:2000])
    return recorded


def _wilson_interval(successes: int, trials: int) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 1.0
    z = 1.96
    rate = successes / trials
    denominator = 1 + z * z / trials
    center = (rate + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt((rate * (1 - rate) + z * z / (4 * trials)) / trials) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _model_evaluation(store: dict[str, Any]) -> dict[str, Any]:
    models: dict[str, dict[str, Any]] = {}
    for row in (store.get("exposures") or {}).values():
        if not isinstance(row, dict):
            continue
        converted_model = str(row.get("converted_model") or "")
        for version, evidence in (row.get("models") or {}).items():
            if not isinstance(evidence, dict):
                continue
            metric = models.setdefault(str(version), {"exposed": 0, "impressions": 0, "converted": 0, "verified": 0, "conversion_value_sum": 0.0, "top10_converted": 0, "reciprocal_rank_sum": 0.0})
            metric["exposed"] += 1
            metric["impressions"] += int(evidence.get("batch_count") or 0)
            if converted_model == version:
                rank = int(row.get("converted_rank") or evidence.get("last_rank") or 0)
                value = _conversion_value(row)
                metric["conversion_value_sum"] += value
                qualified = value >= QUALIFIED_CONVERSION_THRESHOLD
                metric["converted"] += int(qualified)
                metric["verified"] += int(value >= VERIFIED_CONVERSION_THRESHOLD)
                metric["top10_converted"] += int(qualified and 0 < rank <= 10)
                metric["reciprocal_rank_sum"] += 1 / rank if qualified and rank > 0 else 0
    for metric in models.values():
        exposed = int(metric["exposed"])
        converted = int(metric["converted"])
        lower, upper = _wilson_interval(converted, exposed)
        metric.update({
            "conversion_rate": round(converted / max(exposed, 1), 4),
            "weighted_conversion_rate": round(float(metric["conversion_value_sum"]) / max(exposed, 1), 4),
            "conversion_interval": {"lower": round(lower, 4), "upper": round(upper, 4)},
            "top10_rate": round(int(metric["top10_converted"]) / max(converted, 1), 4),
            "mrr": round(float(metric.pop("reciprocal_rank_sum")) / max(converted, 1), 4),
        })
    return {"models": models, "minimum_comparison_sample": 30, "generated_at": _now_ms()}


def _shadow_model_evaluation(store: dict[str, Any]) -> dict[str, Any]:
    versions = (PERSONALIZED_MODEL_VERSION, STABLE_MODEL_VERSION)
    metrics = {
        version: {"shared_exposures": 0, "qualified": 0, "verified": 0, "top10": 0, "discounted_gain": 0.0, "reciprocal_rank_sum": 0.0, "rank_sum": 0}
        for version in versions
    }
    paired_exposures = paired_qualified = personal_wins = stable_wins = ties = 0
    for row in (store.get("exposures") or {}).values():
        if not isinstance(row, dict):
            continue
        shadow_models = row.get("shadow_models") if isinstance(row.get("shadow_models"), dict) else {}
        evidence = {version: shadow_models.get(version) for version in versions}
        if not all(isinstance(evidence.get(version), dict) and int((evidence[version] or {}).get("last_rank") or 0) > 0 for version in versions):
            continue
        paired_exposures += 1
        value = _conversion_value(row)
        qualified = value >= QUALIFIED_CONVERSION_THRESHOLD
        verified = value >= VERIFIED_CONVERSION_THRESHOLD
        ranks = {version: int((evidence[version] or {}).get("last_rank") or 0) for version in versions}
        for version in versions:
            rank = ranks[version]
            metric = metrics[version]
            metric["shared_exposures"] += 1
            metric["rank_sum"] += rank
            metric["qualified"] += int(qualified)
            metric["verified"] += int(verified)
            metric["top10"] += int(qualified and rank <= 10)
            metric["discounted_gain"] += value / math.log2(rank + 1) if rank > 0 else 0.0
            metric["reciprocal_rank_sum"] += 1 / rank if qualified and rank > 0 else 0.0
        if qualified:
            paired_qualified += 1
            if ranks[PERSONALIZED_MODEL_VERSION] < ranks[STABLE_MODEL_VERSION]:
                personal_wins += 1
            elif ranks[STABLE_MODEL_VERSION] < ranks[PERSONALIZED_MODEL_VERSION]:
                stable_wins += 1
            else:
                ties += 1
    for metric in metrics.values():
        exposed = int(metric["shared_exposures"])
        qualified = int(metric["qualified"])
        metric["average_rank"] = round(int(metric.pop("rank_sum")) / max(exposed, 1), 2)
        metric["gain_per_exposure"] = round(float(metric["discounted_gain"]) / max(exposed, 1), 5)
        metric["top10_rate"] = round(int(metric["top10"]) / max(qualified, 1), 4)
        metric["mrr"] = round(float(metric.pop("reciprocal_rank_sum")) / max(qualified, 1), 4)
        metric["discounted_gain"] = round(float(metric["discounted_gain"]), 4)
    decisive = personal_wins + stable_wins
    personal_interval = _wilson_interval(personal_wins, decisive)
    stable_interval = _wilson_interval(stable_wins, decisive)
    personal_gain = float(metrics[PERSONALIZED_MODEL_VERSION]["gain_per_exposure"])
    stable_gain = float(metrics[STABLE_MODEL_VERSION]["gain_per_exposure"])
    recommended_policy = "collecting"
    reason = "至少需要 20 个共享转化和 12 个非平局样本"
    if paired_qualified >= 20 and decisive >= 12:
        if personal_interval[0] > 0.5 and personal_gain > stable_gain * 1.03:
            recommended_policy, reason = "personal", "个性化影子排名在共享转化上显著优于稳定基线"
        elif stable_interval[0] > 0.5 and stable_gain > personal_gain * 1.03:
            recommended_policy, reason = "stable", "稳定基线影子排名在共享转化上显著优于个性化模型"
        else:
            recommended_policy, reason = "inconclusive", "共享转化尚未形成显著胜负"
    return {
        "models": metrics,
        "paired_exposures": paired_exposures,
        "paired_qualified": paired_qualified,
        "decisive": decisive,
        "wins": {"personal": personal_wins, "stable": stable_wins, "ties": ties},
        "win_intervals": {
            "personal": {"lower": round(personal_interval[0], 4), "upper": round(personal_interval[1], 4)},
            "stable": {"lower": round(stable_interval[0], 4), "upper": round(stable_interval[1], 4)},
        },
        "recommended_policy": recommended_policy,
        "reason": reason,
        "minimum_qualified_sample": 20,
        "minimum_decisive_sample": 12,
    }


def _route_evaluation(store: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Estimate route value with fractional attribution and stratified controls."""
    now_ms = int(now_ms or _now_ms())
    mature_age_ms = 7 * 86400 * 1000
    routes: dict[str, dict[str, Any]] = {}
    eligible_rows = 0
    converted_rows = 0
    conversion_value_sum = 0.0
    cohorts: list[dict[str, Any]] = []
    for row in (store.get("exposures") or {}).values():
        if not isinstance(row, dict):
            continue
        value = _conversion_value(row)
        converted = value >= QUALIFIED_CONVERSION_THRESHOLD
        mature = now_ms - int(row.get("first_seen_at") or now_ms) >= mature_age_ms
        if value <= 0 and not mature:
            continue
        eligible_rows += 1
        converted_rows += int(converted)
        conversion_value_sum += value
        converted_routes = set(row.get("converted_routes") or row.get("last_routes") or []) if value > 0 else set()
        row_routes = {str(route) for route, evidence in (row.get("routes") or {}).items() if isinstance(evidence, dict)}
        credited_routes = converted_routes & row_routes
        credit_share = 1 / max(1, len(credited_routes))
        strategy = str(row.get("converted_strategy") or row.get("last_strategy") or "ranking")
        rank = int(row.get("converted_rank") or row.get("last_rank") or 0)
        rank_bucket = "top10" if 0 < rank <= 10 else "top24" if rank <= 24 else "tail"
        cohorts.append({"value": value, "qualified": converted, "routes": row_routes, "stratum": f"{strategy}:{rank_bucket}"})
        for route, evidence in (row.get("routes") or {}).items():
            if not isinstance(evidence, dict):
                continue
            metric = routes.setdefault(str(route), {"exposed": 0, "impressions": 0, "converted": 0, "verified": 0, "conversion_value_sum": 0.0})
            metric["exposed"] += 1
            metric["impressions"] += int(evidence.get("batch_count") or 0)
            attributed = credit_share if route in credited_routes else 0.0
            metric["converted"] += attributed if converted else 0.0
            metric["verified"] += attributed if value >= VERIFIED_CONVERSION_THRESHOLD else 0.0
            metric["conversion_value_sum"] += value * attributed
    global_rate = (conversion_value_sum + 2) / (eligible_rows + 10)
    for route, metric in routes.items():
        exposed = int(metric["exposed"])
        converted = float(metric["converted"])
        posterior = (float(metric["conversion_value_sum"]) + 2) / (exposed + 10)
        reliability = exposed / (exposed + 30)
        strata: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"treated": [], "control": []})
        for cohort in cohorts:
            arm = "treated" if route in cohort["routes"] else "control"
            strata[cohort["stratum"]][arm].append(float(cohort["value"]))
        lift_sum = 0.0
        comparable = 0.0
        comparable_strata = 0
        for arms in strata.values():
            treated, control = arms["treated"], arms["control"]
            if not treated or not control:
                continue
            # Harmonic support prevents a large treatment arm with one control
            # row from looking like strong counterfactual evidence.
            support = 2 * len(treated) * len(control) / (len(treated) + len(control))
            lift_sum += (sum(treated) / len(treated) - sum(control) / len(control)) * support
            comparable += support
            comparable_strata += 1
        counterfactual_lift = lift_sum / comparable if comparable else 0.0
        counterfactual_reliability = comparable / (comparable + 20)
        adaptive = exposed >= 20
        factor = 1.0
        if adaptive:
            factor += (posterior - global_rate) * 1.2 * reliability
            factor += counterfactual_lift * 0.8 * counterfactual_reliability
            factor = max(0.85, min(1.15, factor))
        lower, upper = _wilson_interval(converted, exposed)
        metric.update({
            "conversion_rate": round(converted / max(exposed, 1), 4),
            "weighted_conversion_rate": round(float(metric["conversion_value_sum"]) / max(exposed, 1), 4),
            "posterior_rate": round(posterior, 4),
            "conversion_interval": {"lower": round(lower, 4), "upper": round(upper, 4)},
            "reliability": round(reliability, 3),
            "weight": round(factor, 3),
            "adaptation_status": "active" if adaptive else "observing",
            "counterfactual": {
                "lift": round(counterfactual_lift, 4),
                "comparable_support": round(comparable, 1),
                "strata": comparable_strata,
                "reliability": round(counterfactual_reliability, 3),
            },
        })
    return {
        "routes": routes,
        "eligible": eligible_rows,
        "converted": converted_rows,
        "prior": {"alpha": 2, "beta": 8},
        "counterfactual_method": "strategy_rank_stratified_fractional_attribution",
        "minimum_adaptation_sample": 20,
        "generated_at": now_ms,
    }


def _exploration_evaluation(store: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Compare exploration and normal ranking using converted or mature cohorts."""
    now_ms = int(now_ms or _now_ms())
    mature_age_ms = 7 * 86400 * 1000
    cohorts = {"exploration": {"exposed": 0, "impressions": 0, "converted": 0, "verified": 0, "conversion_value_sum": 0.0}, "ranking": {"exposed": 0, "impressions": 0, "converted": 0, "verified": 0, "conversion_value_sum": 0.0}}
    for row in (store.get("exposures") or {}).values():
        if not isinstance(row, dict):
            continue
        value = _conversion_value(row)
        converted = value >= QUALIFIED_CONVERSION_THRESHOLD
        if value <= 0 and now_ms - int(row.get("first_seen_at") or now_ms) < mature_age_ms:
            continue
        converted_strategy = str(row.get("converted_strategy") or row.get("last_strategy") or "ranking") if value > 0 else ""
        for strategy, evidence in (row.get("strategies") or {}).items():
            if strategy not in cohorts or not isinstance(evidence, dict):
                continue
            cohorts[strategy]["exposed"] += 1
            cohorts[strategy]["impressions"] += int(evidence.get("batch_count") or 0)
            cohorts[strategy]["converted"] += int(converted and strategy == converted_strategy)
            cohorts[strategy]["verified"] += int(value >= VERIFIED_CONVERSION_THRESHOLD and strategy == converted_strategy)
            cohorts[strategy]["conversion_value_sum"] += value if strategy == converted_strategy else 0.0
    for metric in cohorts.values():
        exposed, converted = int(metric["exposed"]), int(metric["converted"])
        posterior = (float(metric["conversion_value_sum"]) + 2) / (exposed + 10)
        lower, upper = _wilson_interval(converted, exposed)
        metric.update({
            "conversion_rate": round(converted / max(exposed, 1), 4),
            "weighted_conversion_rate": round(float(metric["conversion_value_sum"]) / max(exposed, 1), 4),
            "posterior_rate": round(posterior, 4),
            "conversion_interval": {"lower": round(lower, 4), "upper": round(upper, 4)},
            "reliability": round(exposed / (exposed + 20), 3),
        })
    return {"cohorts": cohorts, "minimum_adaptation_sample": 20, "prior": {"alpha": 2, "beta": 8}, "generated_at": now_ms}


def _topic_evaluation(store: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Evaluate eligible topic hypotheses after conversion or seven-day maturity."""
    now_ms = int(now_ms or _now_ms())
    mature_age_ms = 7 * 86400 * 1000
    topics: dict[str, dict[str, Any]] = {}
    eligible = 0
    for row in (store.get("exposures") or {}).values():
        if not isinstance(row, dict):
            continue
        value = _conversion_value(row)
        if value <= 0 and now_ms - int(row.get("first_seen_at") or now_ms) < mature_age_ms:
            continue
        eligible += 1
        converted_topic_ids = set(row.get("converted_topic_ids") or []) if value > 0 else set()
        observed_topic_ids = {str(topic_id) for topic_id, evidence in (row.get("topics") or {}).items() if isinstance(evidence, dict)}
        credited = converted_topic_ids & observed_topic_ids
        share = 1 / max(1, len(credited))
        for topic_id, evidence in (row.get("topics") or {}).items():
            if not isinstance(evidence, dict):
                continue
            metric = topics.setdefault(str(topic_id), {"label": evidence.get("label") or topic_id, "exposed": 0, "impressions": 0, "conversion_value_sum": 0.0, "converted": 0.0})
            metric["exposed"] += 1
            metric["impressions"] += int(evidence.get("batch_count") or 0)
            if topic_id in credited:
                metric["conversion_value_sum"] += value * share
                metric["converted"] += share if value >= QUALIFIED_CONVERSION_THRESHOLD else 0.0
    total_exposed = sum(int(metric["exposed"]) for metric in topics.values())
    for metric in topics.values():
        exposed = int(metric["exposed"])
        posterior = (float(metric["conversion_value_sum"]) + 1) / (exposed + 6)
        uncertainty = math.sqrt(max(0.0, posterior * (1 - posterior)) / max(exposed + 6, 1))
        metric.update({
            "posterior_rate": round(posterior, 4),
            "reliability": round(exposed / (exposed + 15), 3),
            "underexposure": round(1 / math.sqrt(exposed + 1), 4),
            "ucb": round(min(1.0, posterior + 1.28 * uncertainty), 4),
            "weight": round(max(0.85, min(1.15, 1 + (posterior - 1 / 6) * (exposed / (exposed + 15)))), 3) if exposed >= 15 else 1.0,
            "adaptation_status": "active" if exposed >= 15 else "observing",
        })
    return {"topics": topics, "eligible": eligible, "total_exposed": total_exposed, "minimum_adaptation_sample": 15, "prior": {"alpha": 1, "beta": 5}, "generated_at": now_ms}


def _negative_neighbor_seed_weights(store: dict[str, Any], *, now_ms: int | None = None) -> dict[str, float]:
    now_ms = int(now_ms or _now_ms())
    weights: dict[str, float] = {}
    for item in store.get("disliked") or []:
        if not isinstance(item, dict):
            continue
        code = _norm_code(item.get("code"))
        if not code:
            continue
        age_days = max(0.0, (now_ms - int(item.get("created_at") or now_ms)) / 86400 / 1000)
        weights[code] = max(weights.get(code, 0.0), max(0.25, math.pow(0.5, age_days / 180)))
    return weights


def _positive_neighbor_seed_weights(profile: dict[str, Any], behavior: dict[str, Any], store: dict[str, Any]) -> tuple[dict[str, float], dict[str, int]]:
    weights = {_norm_code(code): max(0.05, float(weight)) for code, weight in (profile.get("code_weights") or {}).items() if _norm_code(code)}
    sources = {"library": len(weights), "behavior": 0, "liked": 0}
    for raw_code, raw_weight in (behavior.get("codes") or {}).items():
        code = _norm_code(raw_code)
        if not code:
            continue
        weight = min(3.0, max(0.05, float(raw_weight or 0)))
        weights[code] = max(weights.get(code, 0.0), weight)
        sources["behavior"] += 1
    for row in store.get("liked") or []:
        code = _norm_code(row.get("code") if isinstance(row, dict) else row)
        if code:
            weights[code] = max(weights.get(code, 0.0), 2.0)
            sources["liked"] += 1
    return weights, sources


def _select_ranking_model(config: dict[str, Any], store: dict[str, Any]) -> dict[str, str]:
    requested = str(store.get("ranking_model_override") or config.get("ranking_model_policy") or "auto").strip().lower()
    if requested in {"personal", "personalized"}:
        return {"policy": "personal", "version": PERSONALIZED_MODEL_VERSION, "mode": "manual", "reason": "已手动选择个性化模型"}
    if requested == "stable":
        return {"policy": "stable", "version": STABLE_MODEL_VERSION, "mode": "manual", "reason": "已手动回退稳定模型"}
    evaluation = _model_evaluation(store).get("models") or {}
    personal = evaluation.get(PERSONALIZED_MODEL_VERSION) or {}
    stable = evaluation.get(STABLE_MODEL_VERSION) or {}
    if int(personal.get("exposed") or 0) >= 50 and float((personal.get("conversion_interval") or {}).get("upper") or 1) < 0.05:
        return {"policy": "stable", "version": STABLE_MODEL_VERSION, "mode": "auto", "reason": "个性化模型转化置信上界低于安全线"}
    if int(personal.get("exposed") or 0) >= 30 and int(stable.get("exposed") or 0) >= 30:
        personal_upper = float((personal.get("conversion_interval") or {}).get("upper") or 1)
        stable_lower = float((stable.get("conversion_interval") or {}).get("lower") or 0)
        if personal_upper < stable_lower:
            return {"policy": "stable", "version": STABLE_MODEL_VERSION, "mode": "auto", "reason": "稳定模型转化区间显著更优"}
    shadow = _shadow_model_evaluation(store)
    if shadow.get("recommended_policy") == "stable":
        return {"policy": "stable", "version": STABLE_MODEL_VERSION, "mode": "auto", "reason": str(shadow.get("reason") or "影子评估推荐稳定模型")}
    if shadow.get("recommended_policy") == "personal":
        return {"policy": "personal", "version": PERSONALIZED_MODEL_VERSION, "mode": "auto", "reason": str(shadow.get("reason") or "影子评估推荐个性化模型")}
    return {"policy": "personal", "version": PERSONALIZED_MODEL_VERSION, "mode": "auto", "reason": "个性化模型处于安全区间"}


def _text_has_subtitle(value: Any) -> bool:
    text = _feature_value_text(value)
    return bool(re.search(r"中字|中文字幕|中文|字幕|sub", text, re.I))


def _text_has_cracked(value: Any) -> bool:
    text = _feature_value_text(value)
    return bool(re.search(r"破解|无码破解|uncensored|crack|leak|流出", text, re.I))


def _detail_has_cracked_signal(value: Any) -> bool:
    if not isinstance(value, dict):
        return _text_has_cracked(value)
    if bool(value.get("is_cracked") or value.get("cracked")):
        return True
    return any(
        _text_has_cracked(value.get(key))
        for key in ("tags", "categories", "magnets", "resources")
    )


def _feature_value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        parts: list[str] = []
        for key, nested in value.items():
            if key in {"has_subtitle", "has_cnsub", "play_subtitle", "has_magnet_subtitle"} and bool(nested):
                parts.append("中文字幕")
            elif key in {"is_cracked", "new_model_uncensored_crack"} and bool(nested):
                parts.append("破解")
            else:
                parts.append(_feature_value_text(nested))
        return " ".join(part for part in parts if part)
    if isinstance(value, (list, tuple, set)):
        return " ".join(_feature_value_text(item) for item in value)
    if isinstance(value, bool):
        return ""
    return str(value or "")


def _generic_category_factor(name: Any) -> float:
    text = str(name or "").strip()
    if not text:
        return 0.0
    upper_text = text.upper()
    if any(keyword in text or keyword.upper() in upper_text for keyword in GENERIC_CATEGORY_KEYWORDS):
        return 0.28
    if len(text) <= 1:
        return 0.35
    return 1.0


def _title_text(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value or "").strip()
    parts: list[str] = []
    for key in ("display_title", "title", "originaltitle", "name", "label", "summary", "number", "code"):
        text = str(value.get(key) or "").strip()
        if text:
            parts.append(text)
    for container_name in ("data", "detail", "nfo"):
        container = value.get(container_name)
        if not isinstance(container, dict):
            continue
        for key in ("display_title", "title", "originaltitle", "sorttitle", "name", "plot", "outline"):
            text = str(container.get(key) or "").strip()
            if text:
                parts.append(text)
    return " ".join(dict.fromkeys(parts))


def _clean_title_text(value: Any) -> str:
    text = _title_text(value)
    if not text:
        return ""
    text = CODE_RE.sub(" ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\[\]【】『』「」()（）＜＞<>]", " ", text)
    for pattern in TITLE_NOISE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _pattern_in_title(text: str, pattern: str) -> bool:
    if not pattern:
        return False
    if re.fullmatch(r"[A-Za-z0-9_ -]+", pattern):
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(pattern)}(?![A-Za-z0-9])", text, re.I))
    return pattern.lower() in text.lower()


def _analyze_title(value: Any) -> dict[str, Any]:
    raw = _title_text(value)
    clean = _clean_title_text(value)
    if not clean:
        return {"version": TITLE_PROFILE_VERSION, "raw": raw, "clean": "", "tags": [], "labels": [], "confidence": 0}
    tags: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trait in TITLE_TRAIT_PATTERNS:
        name = str(trait["name"])
        if name in seen:
            continue
        matched = [pattern for pattern in trait.get("patterns") or () if _pattern_in_title(clean, str(pattern))]
        if not matched:
            continue
        seen.add(name)
        tags.append({
            "name": name,
            "key": _norm_key(name),
            "group": trait.get("group") or "title",
            "weight": float(trait.get("weight") or 1.0),
            "matched": matched[:3],
        })
    confidence = min(100, round(sum(float(tag.get("weight") or 0) for tag in tags) * 28))
    return {
        "version": TITLE_PROFILE_VERSION,
        "raw": raw,
        "clean": clean,
        "tags": tags[:12],
        "labels": [str(tag["name"]) for tag in tags[:12]],
        "confidence": confidence,
    }


def _title_trait_labels(analysis: Any, limit: int = 12, min_weight: float = 0.0) -> list[str]:
    if not isinstance(analysis, dict):
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for tag in analysis.get("tags") or []:
        if not isinstance(tag, dict) or float(tag.get("weight") or 0) < min_weight:
            continue
        name = str(tag.get("name") or "").strip()
        key = _norm_key(name)
        if not name or key in seen:
            continue
        seen.add(key)
        labels.append(name)
        if len(labels) >= limit:
            break
    return labels


def _title_mined_terms(value: Any, limit: int = 36) -> list[str]:
    text = _clean_title_text(value)
    if not text:
        return []
    text = re.sub(r"(?:した|して|する|される|され|れる|られ|ない|に|を|が|と|の|で|へ|から|まで|より|そして|また|その|この)", " ", text)
    chunks = re.findall(r"[\u3040-\u30ff\u3400-\u9fffA-Za-z]{2,12}", text)
    terms: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        candidates: list[str] = [chunk]
        if len(chunk) > 4:
            for size in (2, 3, 4):
                candidates.extend(chunk[index:index + size] for index in range(0, len(chunk) - size + 1))
        for term in candidates:
            if len(term) < 2 or len(term) > 4:
                continue
            if term in TITLE_TERM_STOPWORDS or any(stop in term for stop in TITLE_TERM_STOPWORDS if len(stop) >= 3):
                continue
            if re.fullmatch(r"[\u3040-\u309f]+", term) or re.search(r"^[ぁ-ん]+|[ぁ-ん]+$", term):
                continue
            if re.fullmatch(r"[A-Za-z]+", term) and len(term) < 3:
                continue
            if re.search(r"(?:カップ|タイトル|サンプル|プレビュー)$", term):
                continue
            key = _norm_key(term)
            if key in seen:
                continue
            seen.add(key)
            terms.append(term)
            if len(terms) >= limit:
                return terms
    return terms


def _profile_title_term_matches(item: Any, profile_terms: Counter, limit: int = 8) -> list[dict[str, Any]]:
    matches = [
        {"name": term, "count": float(profile_terms.get(term) or 0)}
        for term in _title_mined_terms(item, 80)
        if float(profile_terms.get(term) or 0) > 0
    ]
    matches.sort(key=lambda row: (float(row["count"]), len(str(row["name"]))), reverse=True)
    return matches[:limit]


def _semantic_profile_matches(item: Any, profile_terms: Counter, media_count: int, limit: int = 8, excluded_names: set[str] | None = None) -> list[dict[str, Any]]:
    weighted = semantic_tokens(_title_text(item)).get("weighted") or {}
    excluded_names = excluded_names or set()
    matches = [
        {
            "name": str(term),
            "count": float(profile_terms.get(term) or 0),
            "weight": float(weight or 0),
            "relevance": math.log2(float(profile_terms.get(term) or 0) + 1) * max(0.08, math.log((media_count + 1) / (float(profile_terms.get(term) or 0) + 1))) * float(weight or 0),
        }
        for term, weight in weighted.items()
        if len(str(term)) >= 2 and float(profile_terms.get(term) or 0) > 0 and not _is_actor_name_term(str(term), excluded_names)
    ]
    matches = [row for row in matches if float(row["count"]) / max(media_count, 1) < 0.48]
    matches.sort(key=lambda row: (float(row["relevance"]), len(str(row["name"]))), reverse=True)
    selected: list[dict[str, Any]] = []
    for match in matches:
        name = str(match["name"])
        if any(name in str(row["name"]) or str(row["name"]) in name for row in selected):
            continue
        selected.append(match)
        if len(selected) >= limit:
            break
    return selected


def _is_actor_name_term(term: str, actor_names: set[str]) -> bool:
    normalized = _norm_key(term)
    return any(
        normalized and actor_key and (normalized == actor_key or normalized in actor_key or actor_key in normalized)
        for actor_key in (_norm_key(actor) for actor in actor_names)
    )


def _actor_name_keys(actor_names: set[str]) -> set[str]:
    return {_norm_key(re.sub(r"[\s\u3000・·._\-]", "", name)) for name in actor_names if str(name or "").strip()}


def _work_profile_actor_names(work: WorkProfile) -> set[str]:
    names: set[str] = set()
    for facts in (work.facts or {}).values():
        if not isinstance(facts, dict):
            continue
        for actor in facts.get("actors") or facts.get("actresses") or []:
            name = actor.get("name") if isinstance(actor, dict) else actor
            if str(name or "").strip():
                names.add(str(name).strip())
    return names


def _prune_title_term_counter(counter: Counter) -> Counter:
    trait_labels = {str(trait.get("name") or "") for trait in TITLE_TRAIT_PATTERNS}
    items = [(str(term), float(count)) for term, count in counter.items() if str(term).strip() and float(count or 0) > 0]
    containing_max: dict[str, float] = defaultdict(float)
    for longer, longer_count in items:
        for width in (1, 2):
            if len(longer) <= width:
                continue
            for index in range(len(longer) - width + 1):
                fragment = longer[index:index + width]
                containing_max[fragment] = max(containing_max[fragment], longer_count)
    kept: Counter = Counter()
    for term, count in sorted(items, key=lambda row: (len(row[0]), -row[1])):
        if term in trait_labels:
            continue
        if len(term) <= 2 and containing_max.get(term, 0) >= count * 0.5:
            continue
        kept[term] = count
    return kept


def _ensure_title_profile(item: dict[str, Any]) -> dict[str, Any]:
    profile = item.get("title_profile")
    if isinstance(profile, dict) and int(profile.get("version") or 0) == TITLE_PROFILE_VERSION:
        return profile
    profile = _analyze_title(item)
    item["title_profile"] = profile
    item["title_traits"] = _title_trait_labels(profile)
    return profile


def _merge_title_traits(categories: list[str], title_traits: list[str], limit: int = 16) -> list[str]:
    return _unique_names([*(categories or []), *(title_traits or [])], limit)


def _entity_payload(entity: KnowledgeEntity) -> dict[str, Any]:
    return {
        "id": entity.id,
        "type": entity.entity_type,
        "key": entity.key,
        "label": entity.label,
        "summary": entity.summary,
        "data": entity.data or {},
        "source": entity.source,
        "confidence": entity.confidence,
    }


def _media_preference_weight(entity: KnowledgeEntity) -> float:
    data = entity.data if isinstance(entity.data, dict) else {}
    observed_at = None
    raw_created = str(data.get("date_created") or "").strip()
    if raw_created:
        with contextlib.suppress(ValueError):
            observed_at = dt.datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
    observed_at = observed_at or entity.updated_at or entity.created_at
    if not observed_at:
        return 1.0
    try:
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=dt.timezone.utc)
        age_days = max(0.0, (dt.datetime.now(dt.timezone.utc) - observed_at.astimezone(dt.timezone.utc)).total_seconds() / 86400)
    except Exception:
        return 1.0
    age_days = math.floor(age_days)
    if age_days <= 90:
        return 1.0
    return max(0.35, math.pow(0.5, (age_days - 90) / 540))


def _title_profile_media_payload(item: KnowledgeEntity) -> dict[str, Any]:
    data = item.data or {}
    return {
        "label": item.label,
        "summary": item.summary,
        "data": data,
        "nfo": data.get("nfo") if isinstance(data, dict) else {},
        "title": data.get("title") if isinstance(data, dict) else "",
        "originaltitle": data.get("originaltitle") if isinstance(data, dict) else "",
        "name": data.get("name") if isinstance(data, dict) else "",
    }


def _title_profile_signature(media: list[KnowledgeEntity], media_weights: dict[str, float]) -> str:
    rows = []
    for item in media:
        observed = item.updated_at or item.created_at
        rows.append({
            "id": item.id,
            "updated_at": observed.isoformat() if observed else "",
            "weight": round(float(media_weights.get(item.id, 1.0)), 4),
            "title": _clean_title_text(_title_profile_media_payload(item)),
        })
    raw = json.dumps({"version": TITLE_PROFILE_VERSION, "rows": rows}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_title_profile_cache(signature: str | None = None) -> dict[str, Counter] | None:
    title_profile_file = _title_profile_file()
    try:
        data = json.loads(title_profile_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or int(data.get("version") or 0) != TITLE_PROFILE_VERSION:
        return None
    if signature and data.get("signature") != signature:
        return None
    return {
        "title_traits": Counter({str(key): float(value) for key, value in (data.get("title_traits") or {}).items()}),
        "title_terms": Counter({str(key): float(value) for key, value in (data.get("title_terms") or {}).items()}),
    }


def _save_title_profile_cache(signature: str, title_traits: Counter, title_terms: Counter, media_count: int) -> None:
    title_profile_file = _title_profile_file()
    title_profile_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": TITLE_PROFILE_VERSION,
        "signature": signature,
        "media_count": media_count,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "title_traits": {str(key): round(float(value), 4) for key, value in title_traits.items()},
        "title_terms": {str(key): round(float(value), 4) for key, value in title_terms.items()},
    }
    temporary = title_profile_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(title_profile_file)


def _schedule_title_profile_refresh(signature: str, media: list[KnowledgeEntity], media_weights: dict[str, float], actor_names: set[str]) -> None:
    global _title_profile_refresh_task
    if _title_profile_refresh_task and not _title_profile_refresh_task.done():
        return

    async def refresh() -> None:
        try:
            rebuilt = await asyncio.to_thread(_build_title_profile, list(media), dict(media_weights), set(actor_names))
            _save_title_profile_cache(signature, rebuilt["title_traits"], rebuilt["title_terms"], len(media))
            _invalidate_recommendation_cache(hard=False, reason="title-profile-refresh")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    _title_profile_refresh_task = asyncio.create_task(refresh())


def _build_title_profile(media: list[KnowledgeEntity], media_weights: dict[str, float], actor_names: set[str]) -> dict[str, Counter]:
    title_traits: Counter = Counter()
    title_terms: Counter = Counter()
    actor_keys = _actor_name_keys(actor_names)
    for item in media:
        payload = _title_profile_media_payload(item)
        weight = media_weights.get(item.id, 1.0)
        for tag in _analyze_title(payload).get("tags") or []:
            name = str(tag.get("name") or "").strip()
            if name:
                title_traits[name] += weight * float(tag.get("weight") or 1.0)
        for term in _title_mined_terms(payload, 80):
            term_key = _norm_key(re.sub(r"[\s\u3000・·._\-]", "", str(term)))
            if term_key not in actor_keys:
                title_terms[term] += weight
    return {"title_traits": title_traits, "title_terms": _prune_title_term_counter(title_terms)}


async def _library_profile() -> dict[str, Any]:
    empty_profile = {
        "media_count": 0,
        "codes": set(),
        "media_by_code": {},
        "code_weights": {},
        "actors": Counter(),
        "actor_identities": Counter(),
        "genres": Counter(),
        "tags": Counter(),
        "studios": Counter(),
        "series": Counter(),
        "directors": Counter(),
        "title_traits": Counter(),
        "title_terms": Counter(),
        "semantic_terms": Counter(),
        "actor_category": Counter(),
        "category_pairs": Counter(),
        "local_features": {},
        "top_media": [],
    }
    async with async_session_maker() as db:
        cached_codes = await _emby_cache_codes(db)
        empty_profile["codes"].update(cached_codes)
        try:
            media_rows = await db.execute(select(KnowledgeEntity).where(KnowledgeEntity.entity_type == "media_item"))
        except SQLAlchemyError:
            return empty_profile
        media = list(media_rows.scalars().all())
        media_ids = [item.id for item in media]
        media_weights = {item.id: _media_preference_weight(item) for item in media}
        profile = {
            "media_count": len(media),
            "codes": set(),
            "media_by_code": {},
            "code_weights": {},
            "actors": Counter(),
            "actor_identities": Counter(),
            "genres": Counter(),
            "tags": Counter(),
            "studios": Counter(),
            "series": Counter(),
            "directors": Counter(),
            "title_traits": Counter(),
            "title_terms": Counter(),
            "semantic_terms": Counter(),
            "actor_category": Counter(),
            "category_pairs": Counter(),
            "local_features": {},
            "top_media": [_entity_payload(item) for item in media[:8]],
        }
        profile["codes"].update(cached_codes)
        if not media_ids:
            return profile
        try:
            rows = await db.execute(
                select(KnowledgeEdge, KnowledgeEntity)
                .join(KnowledgeEntity, KnowledgeEntity.id == KnowledgeEdge.target_entity_id)
                .where(KnowledgeEdge.source_entity_id.in_(media_ids))
            )
        except SQLAlchemyError:
            return profile
        media_by_id = {item.id: item for item in media}
        relations_by_media: dict[str, dict[str, set[str]]] = defaultdict(lambda: {
            "actors": set(),
            "categories": set(),
            "studios": set(),
        })
        semantic_work_weights: dict[str, float] = {}
        for edge_index, (edge, target) in enumerate(rows.all()):
            if edge_index and edge_index % 128 == 0:
                await asyncio.sleep(0)
            rel = edge.relation_type
            if rel == "HAS_CODE":
                code = _norm_code(target.label or target.key)
                if code:
                    profile["codes"].add(code)
                    profile["media_by_code"][code] = _entity_payload(media_by_id.get(edge.source_entity_id)) if media_by_id.get(edge.source_entity_id) else None
                    profile["code_weights"][code] = max(float(profile["code_weights"].get(code) or 0), float(media_weights.get(edge.source_entity_id, 1.0)))
                    semantic_work_weights[code] = max(semantic_work_weights.get(code, 0), media_weights.get(edge.source_entity_id, 1.0))
            elif rel == "HAS_ACTOR":
                actor_name = canonical_actor_name(target.label)
                profile["actors"][actor_name] += media_weights.get(edge.source_entity_id, 1.0)
                profile["actor_identities"][actor_identity_key(target.label)] += media_weights.get(edge.source_entity_id, 1.0)
                relations_by_media[edge.source_entity_id]["actors"].add(actor_name)
            elif rel == "HAS_GENRE":
                profile["genres"][target.label] += media_weights.get(edge.source_entity_id, 1.0)
                relations_by_media[edge.source_entity_id]["categories"].add(target.label)
            elif rel == "HAS_TAG":
                profile["tags"][target.label] += media_weights.get(edge.source_entity_id, 1.0)
                relations_by_media[edge.source_entity_id]["categories"].add(target.label)
            elif rel in {"HAS_STUDIO", "HAS_LABEL"}:
                profile["studios"][target.label] += media_weights.get(edge.source_entity_id, 1.0)
                relations_by_media[edge.source_entity_id]["studios"].add(target.label)
            elif rel == "IN_SERIES":
                profile["series"][target.label] += media_weights.get(edge.source_entity_id, 1.0)
            elif rel == "HAS_DIRECTOR":
                profile["directors"][target.label] += media_weights.get(edge.source_entity_id, 1.0)
        for media_id, rels in relations_by_media.items():
            weight = media_weights.get(media_id, 1.0)
            for actor in rels["actors"]:
                for category in rels["categories"]:
                    profile["actor_category"][(actor, category)] += weight
            meaningful_categories = sorted(category for category in rels["categories"] if _generic_category_factor(category) >= 0.5)
            for left, right in combinations(meaningful_categories[:12], 2):
                profile["category_pairs"][(left, right)] += weight
        actor_names = {str(name) for name in profile["actors"] if str(name or "").strip()}
        semantic_actor_names = set(actor_names)
        semantic_actor_names.update(actor_alias_names())
        try:
            actor_rows = await db.execute(select(KnowledgeEntity.label).where(KnowledgeEntity.entity_type == "actor"))
            semantic_actor_names.update(str(name).strip() for name in actor_rows.scalars() if str(name or "").strip())
        except SQLAlchemyError:
            pass
        semantic_actor_keys = _actor_name_keys(semantic_actor_names)
        signature = _title_profile_signature(media, media_weights)
        title_profile = _load_title_profile_cache(signature)
        if title_profile is None:
            title_profile = _load_title_profile_cache() or {"title_traits": Counter(), "title_terms": Counter()}
            _schedule_title_profile_refresh(signature, media, media_weights, actor_names)
        profile["title_traits"] = title_profile["title_traits"]
        if semantic_work_weights:
            try:
                work_rows = await db.execute(select(WorkProfile).where(WorkProfile.code.in_(semantic_work_weights)))
                for work in work_rows.scalars().all():
                    weight = semantic_work_weights.get(work.code, 1.0)
                    work_actor_keys = _actor_name_keys(_work_profile_actor_names(work))
                    weighted_terms = (work.tokens or {}).get("weighted") if isinstance(work.tokens, dict) else {}
                    for term, term_weight in (weighted_terms or {}).items():
                        term_key = _norm_key(re.sub(r"[\s\u3000・·._\-]", "", str(term)))
                        if len(str(term)) >= 2 and term_key not in semantic_actor_keys and term_key not in work_actor_keys:
                            profile["semantic_terms"][str(term)] += weight * float(term_weight or 0)
                profile["semantic_terms"] = Counter(dict(profile["semantic_terms"].most_common(2400)))
            except SQLAlchemyError:
                pass
        # Keep the existing profile response field for frontend compatibility,
        # but use Intelligence Core as the single source of title semantics.
        # The former local n-gram miner produced fragments such as "ンダー"
        # and also caused the same title preference to be scored twice.
        profile["title_terms"] = profile["semantic_terms"]
        for item in media:
            data = item.data or {}
            code = _norm_code(json.dumps(data, ensure_ascii=False) + " " + item.label)
            if code:
                profile["local_features"][code] = {
                    "has_subtitle": _text_has_subtitle(data),
                    "is_cracked": _detail_has_cracked_signal(data),
                }
        return profile


def _top(counter: Counter, limit: int = 12) -> list[dict[str, Any]]:
    return [{"name": str(name), "count": round(float(count), 1)} for name, count in counter.most_common(limit)]


def _names(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("label") or item.get("title") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            out.append(name)
    return out


def _name_one(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("label") or value.get("title") or "").strip()
    if isinstance(value, list):
        names = _names(value)
        return names[0] if names else ""
    text = str(value or "").strip()
    if text.startswith(("{", "[")):
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return text
        return _name_one(parsed)
    return text


def _unique_names(items: Any, limit: int = 20) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in _names(items) if isinstance(items, list) else [str(x or "").strip() for x in (items or [])]:
        value = str(name or "").strip()
        key = _norm_key(value)
        if not value or key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _combined_category_count(name: str, genre_counter: Counter, tag_counter: Counter) -> int:
    return max(int(genre_counter.get(name, 0)), int(tag_counter.get(name, 0)))


def _preference_confidence(count: int, media_count: int) -> float:
    """Return a bounded confidence curve for a media-library signal.

    A single hit should be a clue, not a strong conclusion. Repeated hits become
    meaningful, but the curve saturates so one very common actor/tag does not
    dominate the whole recommendation page.
    """
    if count <= 0 or media_count <= 0:
        return 0.0
    frequency = min(1.0, count / max(media_count, 1))
    repeat = min(1.0, math.log2(count + 1) / 5.0)
    return max(0.0, min(1.0, repeat * 0.78 + frequency * 0.22))


def _score_bucket(value: float) -> str:
    if value >= 28:
        return "strong"
    if value >= 14:
        return "medium"
    if value > 0:
        return "weak"
    return "none"


async def _javdb_candidates(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    try:
        from app.plugins.runtime import runtime
        if not runtime.is_enabled("javdb"):
            return [], ["JavDB 插件未启用，暂时无法生成候选推荐。"]
        if not config.get("candidate_latest_enabled", True):
            return [], ["最新更新候选源已关闭，请切换到完整推荐。"]
        candidate_limit = max(12, min(int(config.get("candidate_limit") or 48), 120))
        detail_limit = max(0, min(int(config.get("detail_limit") or 36), 80))
        requests = [
            ("latest", "最新更新", {"page": 1, "limit": candidate_limit, "type": "all", "filter_by": "magnets", "filters": ["magnets"], "sort_by": "update"}),
            ("rankings", "日榜", {"page": 1, "limit": max(18, candidate_limit // 3), "period": "daily", "type": 0}),
            ("rankings", "周榜", {"page": 1, "limit": max(18, candidate_limit // 3), "period": "weekly", "type": 0}),
            ("rankings", "月榜", {"page": 1, "limit": max(18, candidate_limit // 3), "period": "monthly", "type": 0}),
        ]
        by_code: dict[str, dict[str, Any]] = {}
        for action, label, payload in requests:
            try:
                data = await runtime.handle_action("javdb", action, payload)
                for item in data.get("items") or []:
                    code = _norm_code(item.get("code") or item.get("number") or item.get("display_title") or item.get("title"))
                    if not code:
                        continue
                    current = by_code.get(code)
                    if not current:
                        next_item = dict(item)
                        # Match the JavDB plugin media card: the horizontal
                        # cover is cover_url/thumb_url, not preview screenshots.
                        next_item["fanart_url"] = next_item.get("cover_url") or next_item.get("thumb_url") or ""
                        next_item["source_tags"] = [{"id": label, "label": label, "date": dt.date.today().isoformat()}]
                        by_code[code] = next_item
                    else:
                        current["magnets_count"] = max(int(current.get("magnets_count") or 0), int(item.get("magnets_count") or 0))
                        current["has_cnsub"] = bool(current.get("has_cnsub") or item.get("has_cnsub") or item.get("play_subtitle"))
                        current["is_cracked"] = bool(current.get("is_cracked") or item.get("is_cracked"))
                        tags = current.get("source_tags") if isinstance(current.get("source_tags"), list) else []
                        if not any(isinstance(tag, dict) and tag.get("id") == label for tag in tags):
                            current["source_tags"] = [*tags, {"id": label, "label": label, "date": dt.date.today().isoformat()}]
                        if not current.get("fanart_url"):
                            current["fanart_url"] = current.get("cover_url") or current.get("thumb_url") or item.get("cover_url") or item.get("thumb_url") or ""
            except Exception as exc:
                warnings.append(f"JavDB {action} 拉取失败：{exc}")
        items = list(by_code.values())
        semaphore = asyncio.Semaphore(6)

        async def enrich(item: dict[str, Any]) -> dict[str, Any]:
            code = _norm_code(item.get("code") or item.get("number") or item.get("display_title"))
            if not code:
                return item
            async with semaphore:
                try:
                    detail = await runtime.handle_action("javdb", "video", {"code": code})
                    data = detail.get("data") if isinstance(detail, dict) else {}
                    if isinstance(data, dict):
                        item["detail"] = data
                        item["actors"] = _names(data.get("actors"))
                        item["categories"] = _names(data.get("categories"))
                        item["maker"] = _name_one(data.get("maker") or data.get("publisher") or "")
                        item["series"] = _name_one(data.get("series"))
                        item["director"] = _name_one(data.get("director"))
                        item["cover_url"] = data.get("cover_url") or item.get("cover_url")
                        item["fanart_url"] = item.get("cover_url") or data.get("cover_url") or item.get("thumb_url") or data.get("thumb_url") or ""
                        item["image_candidates"] = _image_candidates(data, item, data.get("preview_images"))
                        item["has_cnsub"] = bool(item.get("has_cnsub") or _text_has_subtitle(data))
                        item["is_cracked"] = bool(item.get("is_cracked") or _detail_has_cracked_signal(data))
                        magnets = data.get("magnets") if isinstance(data.get("magnets"), list) else []
                        if magnets:
                            item["magnets_count"] = max(int(item.get("magnets_count") or 0), len(magnets))
                            item["best_resource_size_mb"] = max([float(x.get("size_mb") or 0) for x in magnets if isinstance(x, dict)] or [0])
                        _ensure_title_profile(item)
                except Exception:
                    pass
            return item

        enriched = await asyncio.gather(*(enrich(item) for item in items[:detail_limit]))
        by_code.update({_norm_code(item.get("code") or item.get("number")): item for item in enriched if _norm_code(item.get("code") or item.get("number"))})
        return list(by_code.values()), warnings
    except Exception as exc:
        return [], [f"候选拉取失败：{exc}"]


def _candidate_pool_requests(config: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    pages = max(1, min(int(config.get("full_scan_pages") or 5), 30))
    requests: list[tuple[str, str, dict[str, Any]]] = []
    if config.get("candidate_latest_enabled", True):
        requests.append(("latest", "最新更新", {"page": 1, "limit": 48, "type": "all", "filter_by": "magnets", "sort_by": "update"}))
    if config.get("candidate_rankings_enabled", True):
        requests.extend([
            ("rankings", "日榜", {"page": 1, "limit": 24, "period": "daily", "type": 0}),
            ("rankings", "周榜", {"page": 1, "limit": 24, "period": "weekly", "type": 0}),
            ("rankings", "月榜", {"page": 1, "limit": 24, "period": "monthly", "type": 0}),
        ])
    if config.get("candidate_recommend_enabled", True):
        requests.append(("recommend", "JavDB 推荐", {"page": 1, "limit": 24}))
    if config.get("candidate_videos_enabled", True):
        requests.extend(("videos", f"完整库 P{page}", {"page": page, "limit": 80, "sort": "update", "order": "desc"}) for page in range(1, pages + 1))
    return requests


async def _scan_candidate_pool(config: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    from app.plugins.runtime import runtime

    if not runtime.is_enabled("javdb"):
        return {"ok": False, "message": "JavDB 插件未启用"}
    async with _pool_lock:
        pool = _pool()
        background = pool.get("background") if isinstance(pool.get("background"), dict) else {}
        if background.get("running") and not force:
            if _candidate_pool_background_stale(pool, background):
                background = {**background, "running": False, "finished_at": background.get("finished_at") or background.get("started_at")}
                pool["background"] = background
                _save_pool(pool)
            else:
                return {"ok": True, "skipped": True, "reason": "running", "pool": _candidate_pool_stats(pool)}
        pool["background"] = {**background, "running": True, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "last_error": ""}
        _save_pool(pool)

    pages = max(1, min(int(config.get("full_scan_pages") or 5), 30))
    requests = _candidate_pool_requests(config)
    scanned = added = updated = detail_updated = 0
    seen_codes: list[str] = []
    warnings: list[str] = ["所有候选源已关闭"] if not requests else []
    try:
        for action, label, request_payload in requests:
            try:
                response = await runtime.handle_action("javdb", action, request_payload)
            except Exception as exc:
                warnings.append(f"{label}: {exc}")
                continue
            values = response.get("items") if isinstance(response, dict) else []
            async with _pool_lock:
                pool = _pool()
                items = pool.get("items") if isinstance(pool.get("items"), dict) else {}
                for value in values or []:
                    if not isinstance(value, dict):
                        continue
                    code = _candidate_code(value)
                    if not code:
                        continue
                    if code not in seen_codes:
                        seen_codes.append(code)
                    existed = code in items
                    items[code] = _merge_candidate(items.get(code), value, f"{action}:{label}", label)
                    scanned += 1
                    added += 0 if existed else 1
                    updated += 1 if existed else 0
                pool["items"] = items
                _save_pool(pool)

        detail_limit = max(0, min(int(config.get("detail_limit") or 36), 80))
        detail_targets = seen_codes[:detail_limit]
        detail_semaphore = asyncio.Semaphore(4)

        async def enrich_pool_detail(code: str) -> None:
            nonlocal detail_updated
            async with detail_semaphore:
                try:
                    detail = await runtime.handle_action("javdb", "video", {"code": code})
                except Exception:
                    return
            data = detail.get("data") if isinstance(detail, dict) else {}
            if not isinstance(data, dict):
                return
            async with _pool_lock:
                pool = _pool()
                pool_items = pool.get("items") if isinstance(pool.get("items"), dict) else {}
                item = pool_items.get(code)
                if not isinstance(item, dict):
                    return
                item["detail"] = data
                item["actors"] = _names(data.get("actors"))
                item["categories"] = _names(data.get("categories"))
                item["maker"] = _name_one(data.get("maker") or data.get("publisher") or "") or item.get("maker") or ""
                item["series"] = _name_one(data.get("series")) or item.get("series") or ""
                item["director"] = _name_one(data.get("director")) or item.get("director") or ""
                item["cover_url"] = data.get("cover_url") or item.get("cover_url")
                item["fanart_url"] = item.get("cover_url") or data.get("cover_url") or item.get("thumb_url") or data.get("thumb_url") or ""
                item["image_candidates"] = _image_candidates(data, data.get("preview_images")) or item.get("image_candidates") or []
                item["has_cnsub"] = bool(item.get("has_cnsub") or _text_has_subtitle(data))
                item["is_cracked"] = bool(item.get("is_cracked") or _detail_has_cracked_signal(data))
                magnets = data.get("magnets") if isinstance(data.get("magnets"), list) else []
                if magnets:
                    item["magnets_count"] = max(int(item.get("magnets_count") or 0), len(magnets))
                    item["best_resource_size_mb"] = max([float(x.get("size_mb") or 0) for x in magnets if isinstance(x, dict)] or [0])
                _ensure_title_profile(item)
                pool_items[code] = item
                pool["items"] = pool_items
                _save_pool(pool)
                detail_updated += 1

        if detail_targets:
            await asyncio.gather(*(enrich_pool_detail(code) for code in detail_targets))

        async with _pool_lock:
            pool = _pool()
            pool["last_full_scan"] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "pages": pages, "scanned": scanned, "added": added, "updated": updated, "detail_updated": detail_updated, "warnings": warnings[:8]}
            pool["background"] = {**(pool.get("background") or {}), "running": False, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(), "last_error": "", "last_added": added, "last_scanned": scanned, "last_detail_updated": detail_updated}
            _save_pool(pool)
            _invalidate_recommendation_cache(modes={"full"}, hard=False, reason="candidate-pool-scan")
            return {"ok": True, "scanned": scanned, "added": added, "updated": updated, "detail_updated": detail_updated, "warnings": warnings, "pool": _candidate_pool_stats(pool)}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        async with _pool_lock:
            pool = _pool()
            pool["background"] = {**(pool.get("background") or {}), "running": False, "failed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "last_error": str(exc)}
            _save_pool(pool)
        raise


async def _refresh_candidate_cover(code_value: Any) -> dict[str, Any]:
    from app.plugins.runtime import runtime

    code = _norm_code(code_value)
    if not code:
        raise ValueError("缺少番号")
    if not runtime.is_enabled("javdb"):
        raise ValueError("JavDB 插件未启用")
    detail = await runtime.handle_action("javdb", "video", {"code": code, "refresh": True})
    data = detail.get("data") if isinstance(detail, dict) else {}
    if not isinstance(data, dict):
        data = {}
    cover_url = str(data.get("cover_url") or "")
    thumb_url = str(data.get("thumb_url") or "")
    fanart_url = cover_url or thumb_url
    candidates = _image_candidates(data, data.get("previews"), data.get("preview_images"))
    async with _pool_lock:
        pool = _pool()
        pool_items = pool.get("items") if isinstance(pool.get("items"), dict) else {}
        item = pool_items.get(code)
        if not isinstance(item, dict):
            item = {"code": code, "number": code}
        if cover_url:
            item["cover_url"] = cover_url
        if thumb_url:
            item["thumb_url"] = thumb_url
        if fanart_url:
            item["fanart_url"] = fanart_url
        if candidates:
            item["image_candidates"] = candidates
        item["detail"] = data or item.get("detail") or {}
        item["cover_refreshed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        pool_items[code] = item
        pool["items"] = pool_items
        _save_pool(pool)
    _invalidate_recommendation_cache(hard=False, reason="cover-refresh")
    return {
        "ok": True,
        "code": code,
        "cover_url": cover_url,
        "thumb_url": thumb_url,
        "fanart_url": fanart_url,
        "image_candidates": candidates,
    }


async def _scheduler_loop() -> None:
    global _scheduler_stop
    _scheduler_stop = asyncio.Event()
    while not _scheduler_stop.is_set():
        try:
            from app.plugins.runtime import runtime
            config = runtime.get_config(PLUGIN_ID)
            minutes = _scan_interval_minutes(config)
            enabled = config.get("full_scan_background_enabled", True)
            scanned_pool = False
            # Warm the interactive page before any overdue full-pool
            # maintenance. A large scan must never delay the normal route.
            await _prewarm_recommendations(
                config,
                force=bool(_prewarm_state.get("last_finished_at")),
                include_full=False,
            )
            if enabled and _pool_scan_due(_pool(), minutes):
                await _scan_candidate_pool(config)
                scanned_pool = True
            if scanned_pool:
                await _prewarm_recommendations(config, force=True, include_full=True)
            cache_minutes = int(_config_number(config, "recommendation_cache_minutes", 30, 5, 1440))
            wake_minutes = max(4, min(minutes, int(cache_minutes * 0.8)))
        except asyncio.CancelledError:
            raise
        except Exception:
            wake_minutes = 10
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_scheduler_stop.wait(), timeout=wake_minutes * 60)


async def start_background(_config: dict[str, Any] | None = None) -> None:
    global _scheduler_task
    pool = _pool()
    background = pool.get("background") if isinstance(pool.get("background"), dict) else {}
    if _candidate_pool_background_stale(pool, background):
        pool["background"] = {**background, "running": False, "finished_at": background.get("finished_at") or background.get("started_at")}
        _save_pool(pool)
    if not _scheduler_task or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())


async def stop_background() -> None:
    global _scheduler_task, _scheduler_stop, _profile_enrichment_task, _title_profile_refresh_task
    if _scheduler_stop:
        _scheduler_stop.set()
    if _scheduler_task:
        _scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _scheduler_task
    _scheduler_task = None
    if _profile_enrichment_task:
        _profile_enrichment_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _profile_enrichment_task
    _profile_enrichment_task = None
    if _title_profile_refresh_task:
        _title_profile_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _title_profile_refresh_task
    _title_profile_refresh_task = None
    for task in list(_recommendation_refresh_tasks.values()):
        task.cancel()
    if _recommendation_refresh_tasks:
        await asyncio.gather(*list(_recommendation_refresh_tasks.values()), return_exceptions=True)
    _recommendation_refresh_tasks.clear()
    _scheduler_stop = None


def _candidate_label(item: dict[str, Any], code: str = "") -> str:
    return str(item.get("display_title") or item.get("title") or item.get("number") or item.get("code") or code or "").strip()


def _record_filter(diagnostics: list[dict[str, Any]] | None, item: dict[str, Any], code: str, reason: str, detail: str = "") -> None:
    if diagnostics is None:
        return
    diagnostics.append({
        "code": code or _norm_code(item.get("code") or item.get("number") or item.get("display_title") or item.get("title")),
        "title": _candidate_label(item, code),
        "reason": reason,
        "detail": detail,
    })


def _filtered_summary(diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter = Counter(str(item.get("reason") or "unknown") for item in diagnostics)
    labels = {
        "missing_code": "缺少番号",
        "ignored": "已忽略",
        "disliked": "不感兴趣",
        "upgrade_not_improved": "已入库提升不足",
        "score_too_low": "评分过低",
    }
    return {
        "total": len(diagnostics),
        "reasons": [
            {"reason": key, "label": labels.get(key, key), "count": int(count)}
            for key, count in counts.most_common()
        ],
        "examples": diagnostics[:12],
    }


def _candidate_score(item: dict[str, Any], profile: dict[str, Any], config: dict[str, Any], feedback: dict[str, Any], diagnostics: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    code = _norm_code(item.get("code") or item.get("number") or item.get("display_title") or item.get("title"))
    ignored = feedback.get("ignored_codes") or set()
    liked = feedback.get("liked_codes") or set()
    disliked = feedback.get("disliked_codes") or set()
    disliked_actors: Counter = feedback.get("disliked_actors") or Counter()
    disliked_categories: Counter = feedback.get("disliked_categories") or Counter()
    liked_actors: Counter = feedback.get("liked_actors") or Counter()
    liked_categories: Counter = feedback.get("liked_categories") or Counter()
    behavior_codes: Counter = feedback.get("behavior_codes") or Counter()
    behavior_actors: Counter = feedback.get("behavior_actors") or Counter()
    behavior_categories: Counter = feedback.get("behavior_categories") or Counter()
    trend_actors: dict[str, float] = feedback.get("trend_actors") or {}
    trend_categories: dict[str, float] = feedback.get("trend_categories") or {}
    outcome_model: dict[str, Any] = feedback.get("outcome_model") or {}
    route_weights: dict[str, float] = feedback.get("route_weights") or {}
    interest_topics: list[dict[str, Any]] = feedback.get("interest_topics") or []
    topic_weights: dict[str, float] = feedback.get("topic_weights") or {}
    liked_topics: Counter = feedback.get("liked_topics") or Counter()
    disliked_topics: Counter = feedback.get("disliked_topics") or Counter()
    session_intent: dict[str, Any] = feedback.get("session_intent") or {}
    search_intent: dict[str, Any] = feedback.get("search_intent") or {}
    context_gate: dict[str, Any] = feedback.get("context_gate") or {}
    exposure_penalties: dict[str, float] = feedback.get("exposure_penalties") or {}
    exposure_fatigue: dict[str, dict[str, float]] = feedback.get("exposure_fatigue") or {}
    if not code:
        _record_filter(diagnostics, item, code, "missing_code", "候选缺少可识别番号")
        return None
    if code in ignored:
        _record_filter(diagnostics, item, code, "ignored", "用户已忽略")
        return None
    if code in disliked:
        _record_filter(diagnostics, item, code, "disliked", "用户已标记不感兴趣")
        return None
    in_library = code in profile.get("codes", set()) or bool((item.get("library") or {}).get("in_library") if isinstance(item.get("library"), dict) else False)
    actors = _unique_names([canonical_actor_name(name) for name in (item.get("actors") or [])], 12)
    actor_identities = [actor_identity_key(name) for name in actors]
    base_categories = _unique_names([canonical_preference_category(name) for name in (item.get("categories") or [])], 16)
    title_profile = _ensure_title_profile(item)
    title_traits = _title_trait_labels(title_profile, limit=10, min_weight=0.55)
    categories = _merge_title_traits(base_categories, title_traits, 18)
    maker = _name_one(item.get("maker") or item.get("publisher") or item.get("studio") or "")
    series = _name_one(item.get("series"))
    director = _name_one(item.get("director"))
    score = 0.0
    reasons: list[str] = []
    personalized_score = 0.0
    actor_preference_score = 0.0
    category_preference_score = 0.0
    relationship_preference_score = 0.0
    semantic_preference_score = 0.0
    feedback_score = 0.0
    actionability_score = 0.0
    quality_score = 0.0
    penalty_score = 0.0
    outcome_calibration_score = 0.0
    route_calibration_score = 0.0
    trend_preference_score = 0.0
    interest_topic_score = 0.0
    session_intent_score = 0.0
    search_intent_score = 0.0
    context_mixture_penalty = 0.0
    context_alignment = 0.0
    matched_search_combinations: list[dict[str, Any]] = []
    topic_feedback_adjustment = 0.0
    matched_interest_topic: dict[str, Any] | None = None
    passive_exposure_penalty = float(exposure_penalties.get(code) or 0)
    if passive_exposure_penalty > 0:
        score -= passive_exposure_penalty
        penalty_score += passive_exposure_penalty
        fatigue_detail = exposure_fatigue.get(code) or {}
        if float(fatigue_detail.get("long") or 0) >= 0.5:
            reasons.append("长期看过但尚未行动")
        elif float(fatigue_detail.get("daily") or 0) >= 0.5:
            reasons.append("近期重复出现，适度轮换")
        else:
            reasons.append("刚刚展示过，暂时轮换")

    actor_counter: Counter = profile.get("actor_identities") or profile.get("actors") or Counter()
    genre_counter: Counter = profile.get("genres") or Counter()
    tag_counter: Counter = profile.get("tags") or Counter()
    studio_counter: Counter = profile.get("studios") or Counter()
    series_counter: Counter = profile.get("series") or Counter()
    director_counter: Counter = profile.get("directors") or Counter()
    title_trait_counter: Counter = profile.get("title_traits") or Counter()
    semantic_term_counter: Counter = profile.get("semantic_terms") or Counter()
    actor_category_counter: Counter = profile.get("actor_category") or Counter()
    category_pair_counter: Counter = profile.get("category_pairs") or Counter()
    media_count = max(int(profile.get("media_count") or 0), 1)

    actor_hits = [(name, actor_counter.get(identity, 0)) for name, identity in zip(actors, actor_identities) if actor_counter.get(identity, 0) > 0]
    if actor_hits:
        best_name, best_count = max(actor_hits, key=lambda x: x[1])
        confidence = _preference_confidence(best_count, media_count)
        boost = min(32, (3 if best_count <= 1 else 7) + math.log2(best_count + 1) * 7.2 + confidence * 6)
        score += boost
        personalized_score += boost
        actor_preference_score += boost
        reasons.append(f"{'演员偏好' if best_count > 1 else '演员线索'}：{best_name} 已有 {best_count} 部")
        if len(actor_hits) >= 2:
            boost = min(8, len(actor_hits) * 2.5)
            score += boost
            personalized_score += boost
            actor_preference_score += boost
            reasons.append(f"多演员命中：{len(actor_hits)} 位")

    feedback_actor_boost = 0.0
    feedback_actor_penalty = 0.0
    for actor in actor_identities:
        feedback_actor_boost += min(8, liked_actors.get(actor, 0) * 4)
        # A single dislike is a weak signal; repeated selected dislike is a real
        # user preference. Keep it soft to avoid "误杀" an actor that only
        # appeared in a bad title once.
        disliked_count = disliked_actors.get(actor, 0)
        if disliked_count:
            feedback_actor_penalty += min(18, 4 + disliked_count * 6)
    if feedback_actor_boost:
        score += feedback_actor_boost
        personalized_score += feedback_actor_boost
        actor_preference_score += feedback_actor_boost
        feedback_score += feedback_actor_boost
        reasons.append("正反馈演员")
    if feedback_actor_penalty:
        score -= feedback_actor_penalty
        penalty_score += feedback_actor_penalty
        reasons.append("负反馈演员降权")

    behavior_actor_strength = sum(float(behavior_actors.get(actor) or 0) for actor in actor_identities)
    if behavior_actor_strength > 0:
        boost = min(5, math.log2(behavior_actor_strength + 1) * 2.2)
        score += boost
        personalized_score += boost
        actor_preference_score += boost
        feedback_score += boost
        reasons.append("近期互动演员")

    actor_trend = sum(float(trend_actors.get(identity) or 0) for identity in actor_identities)
    if actor_trend:
        adjustment = max(-2.5, min(4.5, actor_trend * 10))
        score += adjustment
        personalized_score += adjustment
        actor_preference_score += adjustment
        trend_preference_score += adjustment
        if adjustment >= 0.8:
            reasons.append("近期演员兴趣上升")

    category_hits = []
    for name in base_categories:
        count = _combined_category_count(name, genre_counter, tag_counter)
        if count > 0:
            category_hits.append((name, count, _generic_category_factor(name)))
    if category_hits:
        sorted_hits = sorted(category_hits, key=lambda x: x[1] * x[2], reverse=True)
        names = "/".join(name for name, _, factor in sorted_hits[:3] if factor >= 0.5)
        boost = min(20, sum(min(6.0, math.sqrt(count) * 2.6 + _preference_confidence(count, media_count) * 1.8) * factor for _, count, factor in sorted_hits[:5]))
        score += boost
        personalized_score += boost
        category_preference_score += boost
        if names:
            reasons.append(f"类型匹配：{names}")

    title_trait_hits = [(name, float(title_trait_counter.get(name) or 0)) for name in title_traits if float(title_trait_counter.get(name) or 0) > 0]
    if title_trait_hits:
        sorted_traits = sorted(title_trait_hits, key=lambda row: row[1], reverse=True)
        boost = min(10, sum(min(4.0, math.sqrt(count) * 1.7 + _preference_confidence(int(round(count)), media_count) * 1.2) for _, count in sorted_traits[:4]))
        score += boost
        personalized_score += boost
        category_preference_score += boost
        reasons.append("标题题材：" + "/".join(name for name, _ in sorted_traits[:3]))

    semantic_hits = _semantic_profile_matches(item, semantic_term_counter, media_count, 8, set(actors))
    if semantic_hits:
        top_relevance = float(semantic_hits[0]["relevance"])
        second_relevance = float(semantic_hits[1]["relevance"]) if len(semantic_hits) > 1 else 0.0
        boost = min(7, max(0, top_relevance - 15) * 0.3 + max(0, second_relevance - 18) * 0.12)
        if boost >= 1.2:
            score += boost
            personalized_score += boost
            semantic_preference_score += boost
            reasons.append("语义画像：" + "/".join(str(hit["name"]) for hit in semantic_hits[:3]))

    feedback_category_boost = 0.0
    feedback_category_penalty = 0.0
    for category in categories:
        category_key = canonical_preference_category(category)
        factor = _generic_category_factor(category)
        feedback_category_boost += min(6, liked_categories.get(category_key, 0) * 3 * factor)
        disliked_count = disliked_categories.get(category_key, 0)
        if disliked_count:
            feedback_category_penalty += min(16, (3 + disliked_count * 5) * factor)
    if feedback_category_boost:
        score += feedback_category_boost
        personalized_score += feedback_category_boost
        category_preference_score += feedback_category_boost
        feedback_score += feedback_category_boost
        reasons.append("正反馈类型")
    if feedback_category_penalty:
        score -= feedback_category_penalty
        penalty_score += feedback_category_penalty
        reasons.append("负反馈类型降权")

    behavior_category_strength = sum(float(behavior_categories.get(category) or 0) * _generic_category_factor(category) for category in categories)
    if behavior_category_strength > 0:
        boost = min(4, math.log2(behavior_category_strength + 1) * 1.8)
        score += boost
        personalized_score += boost
        category_preference_score += boost
        feedback_score += boost
        reasons.append("近期互动题材")

    category_trend = sum(float(trend_categories.get(category) or 0) * _generic_category_factor(category) for category in categories)
    if category_trend:
        adjustment = max(-2.0, min(3.5, category_trend * 8))
        score += adjustment
        personalized_score += adjustment
        category_preference_score += adjustment
        trend_preference_score += adjustment
        if adjustment >= 0.8:
            reasons.append("近期题材兴趣上升")

    candidate_actor_ids = set(actor_identities)
    candidate_categories = {canonical_preference_category(category) for category in categories if canonical_preference_category(category)}
    for topic in interest_topics:
        if not isinstance(topic, dict):
            continue
        topic_actor_ids = {str(row.get("identity") or "") for row in topic.get("actors") or [] if isinstance(row, dict) and row.get("identity")}
        topic_categories = {str(name) for name in topic.get("categories") or [] if str(name or "").strip()}
        actor_matches = candidate_actor_ids & topic_actor_ids
        category_matches = candidate_categories & topic_categories
        signal_count = int(bool(actor_matches)) + min(2, len(category_matches))
        if signal_count < 2:
            continue
        confidence = float(topic.get("confidence") or 0)
        stable_strength = float(topic.get("strength") or 0)
        recent_strength = float(topic.get("recent_strength") or 0)
        momentum = float(topic.get("momentum") or 0)
        coherence = min(1.0, signal_count / 3)
        topic_score = min(5.0, (stable_strength * 8 + recent_strength * 6 + max(-0.1, momentum) * 4 + signal_count * 0.45) * confidence * coherence)
        if topic_score > interest_topic_score:
            interest_topic_score = topic_score
            matched_interest_topic = {
                "id": topic.get("id"),
                "label": topic.get("label"),
                "confidence": round(confidence, 3),
                "support": int(topic.get("support") or 0),
                "actor_matches": sorted(actor_matches),
                "category_matches": sorted(category_matches),
                "score": round(topic_score, 2),
                "momentum": round(momentum, 4),
            }
    interest_topic_hypothesis = dict(matched_interest_topic or {})
    if matched_interest_topic:
        topic_id = str(matched_interest_topic.get("id") or "")
        interest_topic_score *= float(topic_weights.get(topic_id) or 1.0)
        topic_feedback_adjustment = min(1.5, liked_topics.get(topic_id, 0) * 0.6) - min(2.5, disliked_topics.get(topic_id, 0) * 0.8)
    if interest_topic_score >= 0.5 and matched_interest_topic:
        score += interest_topic_score
        personalized_score += interest_topic_score
        relationship_preference_score += interest_topic_score
        reasons.append(f"兴趣主题：{matched_interest_topic['label']}")
    else:
        interest_topic_score = 0.0
        matched_interest_topic = None

    if topic_feedback_adjustment:
        score += topic_feedback_adjustment
        personalized_score += topic_feedback_adjustment
        if topic_feedback_adjustment > 0:
            feedback_score += topic_feedback_adjustment
            reasons.append("组合主题正反馈")
        else:
            penalty_score += abs(topic_feedback_adjustment)
            reasons.append("组合主题负反馈降权")

    session_actor_strength = sum(float((session_intent.get("actors") or {}).get(identity) or 0) for identity in actor_identities)
    session_category_strength = sum(float((session_intent.get("categories") or {}).get(category) or 0) * _generic_category_factor(category) for category in candidate_categories)
    session_topic_id = str((matched_interest_topic or interest_topic_hypothesis).get("id") or "")
    session_topic_strength = float((session_intent.get("topics") or {}).get(session_topic_id) or 0)
    session_intent_score = min(2.8, session_actor_strength * 1.8) + min(2.0, session_category_strength * 0.8) + min(2.0, session_topic_strength * 1.5)
    if session_intent_score >= 0.25:
        score += session_intent_score
        personalized_score += session_intent_score
        trend_preference_score += session_intent_score
        reasons.append("当前兴趣方向")

    search_actor_strength = sum(float((search_intent.get("actors") or {}).get(identity) or 0) for identity in actor_identities)
    search_category_strength = sum(float((search_intent.get("categories") or {}).get(category) or 0) * _generic_category_factor(category) for category in candidate_categories)
    candidate_terms = semantic_tokens(_title_text(item)).get("weighted") or {}
    search_term_strength = sum(float(weight) * float(candidate_terms.get(term) or 0) for term, weight in (search_intent.get("terms") or {}).items())
    candidate_search_signals = {f"actor:{identity}" for identity in actor_identities} | {f"category:{category}" for category in candidate_categories} | {f"term:{term}" for term in candidate_terms}
    for key, strength in (search_intent.get("combinations") or {}).items():
        members = str(key).removeprefix("combo:").split("|")
        if len(members) != 2 or not set(members) <= candidate_search_signals:
            continue
        matched_search_combinations.append({
            "id": str(key),
            "label": str((search_intent.get("combination_labels") or {}).get(key) or " × ".join(members)),
            "strength": round(float(strength or 0), 3),
        })
    matched_search_combinations.sort(key=lambda row: float(row["strength"]), reverse=True)
    combination_strength = sum(float(row["strength"]) for row in matched_search_combinations[:3])
    search_intent_score = min(2.2, search_actor_strength * 1.1) + min(1.5, search_category_strength * 0.6) + min(1.8, search_term_strength * 0.35) + min(1.8, combination_strength * 0.5)
    if search_intent_score >= 0.25:
        score += search_intent_score
        personalized_score += search_intent_score
        trend_preference_score += search_intent_score
        reasons.append("当前组合搜索方向" if matched_search_combinations else "当前搜索方向")

    outcome_signals: list[tuple[float, float]] = []
    actor_outcomes = outcome_model.get("actors") if isinstance(outcome_model.get("actors"), dict) else {}
    category_outcomes = outcome_model.get("categories") if isinstance(outcome_model.get("categories"), dict) else {}
    for identity in actor_identities:
        row = actor_outcomes.get(identity) if isinstance(actor_outcomes.get(identity), dict) else None
        if row:
            outcome_signals.append((float(row.get("rate") or 0.5), float(row.get("reliability") or 0)))
    for category in categories:
        row = category_outcomes.get(category) if isinstance(category_outcomes.get(category), dict) else None
        if row:
            outcome_signals.append((float(row.get("rate") or 0.5), float(row.get("reliability") or 0) * _generic_category_factor(category)))
    signal_weight = sum(weight for _rate, weight in outcome_signals)
    if signal_weight > 0:
        calibrated_rate = sum(rate * weight for rate, weight in outcome_signals) / signal_weight
        reliability = min(1.0, signal_weight / 3)
        outcome_calibration_score = max(-6.0, min(6.0, (calibrated_rate - 0.5) * 18 * reliability))
        score += outcome_calibration_score
        personalized_score += outcome_calibration_score
        if outcome_calibration_score >= 0.8:
            reasons.append("验证结果匹配")
        elif outcome_calibration_score <= -0.8:
            reasons.append("历史转化较弱")

    combo_hits = []
    for actor, actor_identity in zip(actors, actor_identities):
        for category in categories:
            count = actor_category_counter.get((actor, category), 0)
            factor = _generic_category_factor(category)
            if count >= 2 and factor >= 0.5:
                actor_count = max(actor_counter.get(actor_identity, 0), 1)
                category_count = max(_combined_category_count(category, genre_counter, tag_counter), 1)
                expected = actor_count * category_count / media_count
                lift = (count + 0.5) / (expected + 0.5)
                combo_hits.append((actor, category, count, factor, lift))
    if combo_hits:
        actor, category, count, factor, lift = max(combo_hits, key=lambda x: (math.log2(max(x[4], 1)) * x[3], x[2]))
        significance = max(0.0, math.log2(max(lift, 1)) - 0.35)
        support = 1 - math.exp(-(count - 1) / 2)
        boost = min(12, significance * 4.5 * support * factor + min(3, math.log2(count) * factor))
        if lift >= 1.35 and boost >= 1:
            score += boost
            personalized_score += boost
            relationship_preference_score += boost
            reasons.append(f"组合偏好：{actor} + {category} · {count} 次 · 提升 {lift:.1f}×")

    category_pair_hits = []
    meaningful_categories = sorted(set(category for category in categories if _generic_category_factor(category) >= 0.5))
    for left, right in combinations(meaningful_categories[:12], 2):
        count = float(category_pair_counter.get((left, right)) or category_pair_counter.get((right, left)) or 0)
        if count < 2:
            continue
        left_count = max(_combined_category_count(left, genre_counter, tag_counter), 1)
        right_count = max(_combined_category_count(right, genre_counter, tag_counter), 1)
        expected = left_count * right_count / media_count
        lift = (count + 0.5) / (expected + 0.5)
        if lift >= 1.3:
            category_pair_hits.append((left, right, count, lift))
    if category_pair_hits:
        left, right, count, lift = max(category_pair_hits, key=lambda row: (math.log2(row[3]), row[2]))
        support = 1 - math.exp(-(count - 1) / 2.5)
        boost = min(8, max(0, math.log2(lift) - 0.3) * 3.5 * support + min(2, math.log2(count)))
        if boost >= 1:
            score += boost
            personalized_score += boost
            relationship_preference_score += boost
            reasons.append(f"题材组合：{left} + {right} · 提升 {lift:.1f}×")

    # If the candidate has no familiar actor but several strong preferred tags,
    # mark it as a controlled discovery rather than letting it look random.
    if not actor_hits and category_hits:
        strong_category_count = sum(1 for _, count, factor in category_hits if count >= 2 and factor >= 0.5)
        if strong_category_count >= 2:
            reasons.append("类型探索")

    if maker and studio_counter.get(maker, 0):
        count = studio_counter.get(maker, 0)
        boost = min(9, 3 + math.log2(count + 1) * 3)
        score += boost
        personalized_score += boost
        relationship_preference_score += boost
        reasons.append(f"厂牌匹配：{maker}")

    if series and series_counter.get(series, 0):
        count = float(series_counter.get(series, 0))
        boost = min(14, 5 + math.log2(count + 1) * 4)
        score += boost
        personalized_score += boost
        relationship_preference_score += boost
        reasons.append(f"系列偏好：{series}")

    if director and director_counter.get(director, 0):
        count = float(director_counter.get(director, 0))
        boost = min(8, 2 + math.log2(count + 1) * 2.5)
        score += boost
        personalized_score += boost
        relationship_preference_score += boost
        reasons.append(f"导演匹配：{director}")

    magnets_count = int(item.get("magnets_count") or 0)
    if magnets_count > 0:
        boost = 6 + min(5, magnets_count)
        score += boost
        actionability_score += boost
        reasons.append(f"有 {magnets_count} 个磁链")

    if item.get("has_cnsub"):
        subtitle_strength = _preference_strength(config, "prefer_subtitle_strength", "prefer_subtitle")
        boost = 5 if subtitle_strength < 0 else 3 + subtitle_strength / 100 * 6
        score += boost
        actionability_score += boost
        reasons.append("中字资源")
    if item.get("is_cracked"):
        crack_strength = _preference_strength(config, "prefer_cracked_strength", "prefer_cracked")
        boost = 5 if crack_strength < 0 else 4 + crack_strength / 100 * 6
        score += boost
        actionability_score += boost
        reasons.append("破解特征")

    size_mb = float(item.get("best_resource_size_mb") or 0)
    if size_mb > 0:
        boost = min(4, max(0, math.log(max(size_mb, 1), 2) - 10))
        score += boost
        quality_score += boost
        if size_mb >= 4096:
            reasons.append(f"资源体积 {size_mb / 1024:.1f}GB")

    if code in liked:
        score += 20
        feedback_score += 20
        reasons.append("已标记喜欢")
    behavior_code_strength = float(behavior_codes.get(code) or 0)
    if behavior_code_strength > 0:
        boost = min(5, behavior_code_strength * 2.2)
        score += boost
        personalized_score += boost
        feedback_score += boost
        reasons.append("近期查看意向")
    neighbor_score = float(item.get("neighbor_score") or 0)
    if neighbor_score > 0:
        graph_recall = int(item.get("neighbor_hop_count") or 1) > 1
        route_factor = float(route_weights.get("core-graph" if graph_recall else "core-neighbor") or 1.0)
        boost = min(14, math.log2(1 + neighbor_score) * 5.2 * route_factor)
        score += boost
        personalized_score += boost
        relationship_preference_score += boost
        evidence_rows = item.get("neighbor_evidence") if isinstance(item.get("neighbor_evidence"), list) else []
        labels = []
        for evidence_row in evidence_rows[:2]:
            for reason in (evidence_row.get("reasons") or [])[:2]:
                label = str(reason.get("label") or "").strip() if isinstance(reason, dict) else ""
                if label and label not in labels:
                    labels.append(label)
        hop_count = int(item.get("neighbor_hop_count") or 1)
        via_codes = list(dict.fromkeys(str(row.get("via_code") or "").strip() for row in evidence_rows if str(row.get("via_code") or "").strip()))
        relation_reason = "图谱传播" + ("：经 " + "/".join(via_codes[:2]) if hop_count > 1 and via_codes else "") if hop_count > 1 else "邻域相似" + ("：" + "/".join(labels[:3]) if labels else "")
        reasons.insert(min(2, len(reasons)), relation_reason)
    neighbor_negative_score = float(item.get("neighbor_negative_score") or 0)
    if neighbor_negative_score > 0:
        penalty = min(12, math.log2(1 + neighbor_negative_score) * 5.5)
        score -= penalty
        penalty_score += penalty
        reasons.append("与不感兴趣作品相似")
    recall_sources = list(item.get("recall_sources") or [])
    learned_route_weights = [float(route_weights.get(route) or 1.0) for route in recall_sources if route in route_weights]
    if learned_route_weights:
        route_calibration_score = max(-2.0, min(2.0, (sum(learned_route_weights) / len(learned_route_weights) - 1) * 8))
        score += route_calibration_score
        personalized_score += route_calibration_score
        if route_calibration_score >= 0.8:
            reasons.append("召回路线转化较好")
        elif route_calibration_score <= -0.8:
            reasons.append("召回路线谨慎降权")
    release = str(item.get("release_date") or item.get("date") or "")
    if release.startswith("2026"):
        score += 4
        quality_score += 4
        reasons.append("近期作品")
    elif release.startswith("2025"):
        score += 2
        quality_score += 2

    cold_start_strength = float(config.get("_cold_start_strength") or 0)
    if cold_start_strength > 0:
        cold_boost = min(8, (min(magnets_count, 5) * 0.8 + min(actionability_score, 12) * 0.25 + min(quality_score, 6) * 0.35) * cold_start_strength)
        score += cold_boost
        quality_score += cold_boost
        if cold_boost >= 1.5:
            reasons.append("冷启动：优先可用与多样性")

    if media_count >= 10 and personalized_score < 10:
        score -= 12
        penalty_score += 12
        reasons.append("个性化命中较弱")

    # Generic-only candidates are often popular feed noise. If all category
    # labels are broad labels and no actor/studio signal exists, keep them from
    # floating to the top only because they have resources.
    meaningful_categories = [x for x in categories if _generic_category_factor(x) >= 0.5]
    if media_count >= 10 and not actor_hits and not maker and not series and not director and not meaningful_categories:
        score -= 10
        penalty_score += 10
        reasons.append("标签过泛降权")

    if context_gate.get("active"):
        context_alignment = min(1.0, (session_intent_score + search_intent_score) / 5.0)
        portrait_score = max(0.0, actor_preference_score + category_preference_score + relationship_preference_score + semantic_preference_score + max(0.0, outcome_calibration_score))
        context_mixture_penalty = min(8.0, portrait_score * float(context_gate.get("gate") or 0) * (1 - context_alignment))
        if context_mixture_penalty >= 0.25:
            score -= context_mixture_penalty
            penalty_score += context_mixture_penalty
            reasons.append("当前意图与长期画像暂时分流")

    local_features = (profile.get("local_features") or {}).get(code) or {}
    if in_library:
        current_has_sub = bool(local_features.get("has_subtitle"))
        current_cracked = bool(local_features.get("is_cracked"))
        improved = []
        if item.get("has_cnsub") and not current_has_sub:
            improved.append("补中字")
        if item.get("is_cracked") and not current_cracked:
            improved.append("补破解")
        if size_mb >= 4096:
            improved.append("更高体积版本")
        if not improved:
            _record_filter(diagnostics, item, code, "upgrade_not_improved", "已入库作品没有识别到中字、破解或更高体积版本提升")
            return None
        score += 10
        reasons.insert(0, "洗版：" + " / ".join(improved[:3]))

    policy_adjustment = relationship_preference_score * 0.45 + semantic_preference_score * 0.3 + outcome_calibration_score
    personal_ranking_score = score
    stable_ranking_score = score - policy_adjustment
    if str(config.get("_ranking_policy") or "personal") == "stable":
        score -= policy_adjustment
        personalized_score -= policy_adjustment
        relationship_preference_score *= 0.55
        semantic_preference_score *= 0.7
        outcome_calibration_score = 0.0

    score = max(0, min(92, round(score)))
    if score <= 0:
        _record_filter(diagnostics, item, code, "score_too_low", "综合评分小于等于 0")
        return None
    match_bucket = _score_bucket(personalized_score)
    evidence_reliability = 1 - math.exp(-media_count / 20)
    raw_confidence = personalized_score * 1.6 + actionability_score * 0.45 - penalty_score * 0.7
    confidence = max(0, min(100, round(raw_confidence * (0.52 + evidence_reliability * 0.48))))
    uncertainty_radius = round(24 * (1 - evidence_reliability))
    factor_rows = [
        {"type": "actor", "label": "演员偏好", "score": round(actor_preference_score, 1)},
        {"type": "category", "label": "题材偏好", "score": round(category_preference_score, 1)},
        {"type": "relationship", "label": "作品关系", "score": round(relationship_preference_score, 1), "evidence": list(item.get("neighbor_evidence") or [])[:3]},
        {"type": "semantic", "label": "标题语义", "score": round(semantic_preference_score, 1)},
        {"type": "trend", "label": "近期趋势", "score": round(trend_preference_score, 1)},
        {"type": "topic", "label": "组合兴趣主题", "score": round(interest_topic_score, 1), "evidence": matched_interest_topic or {}},
        {"type": "session", "label": "当前兴趣方向", "score": round(session_intent_score, 1)},
        {"type": "search", "label": "当前搜索方向", "score": round(search_intent_score, 1), "evidence": matched_search_combinations[:3]},
        {"type": "context", "label": "长期/即时混合门控", "score": round(-context_mixture_penalty, 1), "evidence": {"alignment": round(context_alignment, 3), **context_gate}},
        {"type": "outcome", "label": "入库结果校准", "score": round(outcome_calibration_score, 1)},
        {"type": "resource", "label": "资源可用性", "score": round(actionability_score, 1)},
        {"type": "quality", "label": "作品质量", "score": round(quality_score, 1)},
    ]
    factor_rows = sorted((row for row in factor_rows if abs(float(row.get("score") or 0)) >= 0.1), key=lambda row: abs(float(row["score"])), reverse=True)
    explanation = {
        "version": 1,
        "summary": reasons[:5],
        "factors": factor_rows,
        "counterfactors": ([{"type": "penalty", "label": "负向与被动曝光信号", "score": round(-penalty_score, 1)}] if penalty_score else []),
        "confidence": {"value": confidence, "lower": max(0, confidence - uncertainty_radius), "upper": min(100, confidence + uncertainty_radius), "reliability": round(evidence_reliability, 3)},
        "provenance": {"recall_sources": recall_sources, "portrait_sources": dict(item.get("field_sources") or item.get("portrait_sources") or {})},
    }
    return {
        "code": code,
        "title": item.get("title") or item.get("display_title") or code,
        "display_title": item.get("display_title") or f"{code} {item.get('title') or ''}".strip(),
        "cover_url": item.get("cover_url") or item.get("thumb_url") or "",
        "fanart_url": item.get("fanart_url") or item.get("cover_url") or item.get("thumb_url") or "",
        "image_candidates": _image_candidates(item, item.get("detail")),
        "release_date": release,
        "actors": actors[:6],
        "categories": categories[:8],
        "title_traits": title_traits[:8],
        "title_profile": {
            "labels": title_traits[:8],
            "confidence": title_profile.get("confidence") or 0,
            "tags": (title_profile.get("tags") or [])[:8],
        },
        "maker": maker,
        "series": series,
        "director": director,
        "score": score,
        "ranking_scores": {
            PERSONALIZED_MODEL_VERSION: max(0, min(92, round(personal_ranking_score))),
            STABLE_MODEL_VERSION: max(0, min(92, round(stable_ranking_score))),
        },
        "personalized_score": round(personalized_score, 1),
        "actionability_score": round(actionability_score, 1),
        "quality_score": round(quality_score, 1),
        "penalty_score": round(penalty_score, 1),
        "match_level": match_bucket,
        "confidence": confidence,
        "confidence_interval": {"lower": max(0, confidence - uncertainty_radius), "upper": min(100, confidence + uncertainty_radius), "reliability": round(evidence_reliability, 3)},
        "recommendation_explanation": explanation,
        "outcome_calibration": round(outcome_calibration_score, 1),
        "route_calibration": round(route_calibration_score, 1),
        "interest_topic": matched_interest_topic or {},
        "interest_topic_hypothesis": interest_topic_hypothesis,
        "search_intent_matches": matched_search_combinations[:3],
        "context_mixture": {"alignment": round(context_alignment, 3), "penalty": round(context_mixture_penalty, 2), "gate": float(context_gate.get("gate") or 0)},
        "exposure_fatigue": exposure_fatigue.get(code) or {},
        "neighbor_score": round(neighbor_score, 3),
        "neighbor_confidence": round(float(item.get("neighbor_confidence") or 0), 3),
        "neighbor_evidence": list(item.get("neighbor_evidence") or [])[:5],
        "neighbor_hop_count": int(item.get("neighbor_hop_count") or 1),
        "neighbor_negative_score": round(neighbor_negative_score, 3),
        "neighbor_negative_evidence": list(item.get("neighbor_negative_evidence") or [])[:3],
        "recall_sources": recall_sources,
        "portrait_completeness": dict(item.get("completeness") or item.get("portrait_completeness") or {}),
        "portrait_sources": dict(item.get("field_sources") or item.get("portrait_sources") or {}),
        "score_breakdown": {
            "preference": round(personalized_score, 1),
            "actor_preference": round(actor_preference_score, 1),
            "category_preference": round(category_preference_score, 1),
            "relationship_preference": round(relationship_preference_score, 1),
            "semantic_preference": round(semantic_preference_score, 1),
            "feedback": round(feedback_score, 1),
            "trend": round(trend_preference_score, 1),
            "interest_topic": round(interest_topic_score, 1),
            "session_intent": round(session_intent_score, 1),
            "search_intent": round(search_intent_score, 1),
            "context_mixture": round(-context_mixture_penalty, 1),
            "outcomes": round(outcome_calibration_score, 1),
            "recall_route": round(route_calibration_score, 1),
            "resources": round(actionability_score, 1),
            "quality": round(quality_score, 1),
            "penalty": round(penalty_score, 1),
        },
        "type": "recommendation",
        "in_library": bool(in_library),
        "magnets_count": magnets_count,
        "has_cnsub": bool(item.get("has_cnsub")),
        "is_cracked": bool(item.get("is_cracked")),
        "is_uncensored": bool(item.get("is_uncensored") or item.get("uncensored")),
        "best_resource_size_mb": size_mb,
        "reasons": reasons[:5],
        "source_tags": list(item.get("source_tags") or []),
        "is_today_increment": bool(item.get("is_today_increment")),
        "source": "javdb",
        "source_label": "JavDB",
        "route": f"/plugins/javdb?code={code}",
        "raw": {"id": item.get("id"), "library": item.get("library") or {}},
    }


def _resource_features(resource: dict[str, Any]) -> dict[str, bool]:
    features = resource.get("features") if isinstance(resource.get("features"), dict) else {}
    requirements = resource.get("requirements") if isinstance(resource.get("requirements"), dict) else {}
    tags = " ".join(str(x) for x in (resource.get("tags") or []))
    text = "\n".join([
        str(resource.get("title") or ""),
        str(resource.get("subtitle") or ""),
        str(resource.get("provider_label") or ""),
        tags,
        _feature_value_text({key: value for key, value in features.items() if value}),
    ])
    return {
        "has_subtitle": bool(features.get("has_subtitle") or re.search(r"中字|中文字幕|中文|字幕|sub", text, re.I)),
        "is_cracked": bool(features.get("is_cracked") or features.get("new_model_uncensored_crack") or re.search(r"破解|uncensored\s*(?:crack|leak)|crack|leak|流出", text, re.I)),
        "is_uncensored": bool(features.get("is_uncensored") or re.search(r"无码|無碼|無修正|uncensored", text, re.I)),
        "is_private_tracker": bool(features.get("is_private_tracker") or requirements.get("accepts_private_tracker")),
    }


async def _enrich_recommendation_resources(
    config: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> list[str]:
    warnings: list[str] = []
    if not items:
        return warnings
    try:
        from app.plugins.runtime import runtime
    except Exception as exc:
        return [f"资源插件运行时不可用：{exc}"]
    configured_limit = int(config.get("resource_enrich_limit") or 32)
    enrich_limit = max(0, min(configured_limit if limit is None else int(limit), 100))
    if enrich_limit <= 0:
        return warnings
    targets = items[:enrich_limit]
    concurrency = int(_config_number(config, "resource_enrich_concurrency", 4, 1, 12))
    budget_seconds = _config_number(config, "resource_enrich_budget_seconds", 6, 1, 20)
    semaphore = asyncio.Semaphore(concurrency)

    async def enrich_one(item: dict[str, Any]) -> None:
        if isinstance(item.get("resource_summary"), dict):
            return
        code = str(item.get("code") or "").strip()
        if not code:
            return
        async with semaphore:
            try:
                result = await runtime.search_resources(
                    {"keyword": code, "provider_timeout_seconds": 5, "intelligence_cache": "prefer"},
                    limit_per_plugin=8,
                )
            except Exception as exc:
                warnings.append(f"{code} 资源确认失败：{exc}")
                return
        resources: list[dict[str, Any]] = []
        groups = result if isinstance(result, list) else [result]
        for group in groups:
            if not isinstance(group, dict):
                continue
            provider = str(group.get("provider") or "")
            provider_name = str(group.get("provider_name") or provider or "资源")
            for resource in group.get("items") or []:
                if not isinstance(resource, dict):
                    continue
                row = dict(resource)
                row.setdefault("provider", provider)
                row.setdefault("provider_label", provider_name)
                resources.append(row)
        if not resources:
            item["resource_summary"] = {"total": 0, "providers": []}
            return
        provider_counts: Counter = Counter()
        total_size = 0
        best_size = 0
        has_subtitle = bool(item.get("has_cnsub"))
        is_cracked = bool(item.get("is_cracked"))
        has_uncensored = bool(item.get("is_uncensored") or item.get("uncensored"))
        has_private = False
        has_public = False
        compatible_downloaders: set[str] = set()
        for res in resources:
            provider = str(res.get("provider_label") or res.get("provider") or "资源").strip()
            provider_counts[provider] += 1
            size = int(res.get("size_bytes") or 0)
            total_size += max(0, size)
            best_size = max(best_size, size)
            feats = _resource_features(res)
            has_subtitle = has_subtitle or feats["has_subtitle"]
            is_cracked = is_cracked or feats["is_cracked"]
            has_uncensored = has_uncensored or feats["is_uncensored"]
            has_private = has_private or feats["is_private_tracker"]
            has_public = has_public or not feats["is_private_tracker"]
            for downloader_id in res.get("compatible_downloaders") or []:
                if downloader_id:
                    compatible_downloaders.add(str(downloader_id))
        providers = [{"name": name, "count": count} for name, count in provider_counts.most_common()]
        item["resource_summary"] = {
            "total": len(resources),
            "providers": providers,
            "best_size_bytes": best_size,
            "total_size_bytes": total_size,
            "has_private": has_private,
            "has_public": has_public,
            "has_uncensored": has_uncensored,
            "compatible_downloaders": sorted(compatible_downloaders),
        }
        # Resource availability is actionability, not taste. Keep it a small
        # secondary boost so it cannot dominate actor/category preference.
        score_boost = min(6, len(resources)) + min(3, best_size / 1024 / 1024 / 1024 * 0.45)
        if has_subtitle and not item.get("has_cnsub"):
            score_boost += 4
            item["has_cnsub"] = True
            item.setdefault("reasons", []).append("资源确认：中字")
        if is_cracked and not item.get("is_cracked"):
            score_boost += 5
            item["is_cracked"] = True
            item.setdefault("reasons", []).append("资源确认：破解")
        if has_uncensored and not item.get("is_uncensored"):
            score_boost += 1
            item["is_uncensored"] = True
            item.setdefault("reasons", []).append("资源确认：无码")
        if providers:
            item.setdefault("reasons", []).append("资源来源：" + " / ".join(f"{p['name']}×{p['count']}" for p in providers[:3]))
        if best_size > 0:
            item["best_resource_size_mb"] = max(float(item.get("best_resource_size_mb") or 0), best_size / 1024 / 1024)
        item["resource_summary"]["quality_score"] = round(score_boost, 1)
        breakdown = item.get("score_breakdown") if isinstance(item.get("score_breakdown"), dict) else {}
        breakdown["resources"] = round(float(breakdown.get("resources") or 0) + score_boost, 1)
        item["score_breakdown"] = breakdown
        item["actionability_score"] = round(float(item.get("actionability_score") or 0) + score_boost, 1)
        previous_confidence = float(item.get("confidence") or 0)
        item["confidence"] = max(0, min(100, round(previous_confidence + score_boost * 0.45)))
        confidence_delta = float(item["confidence"]) - previous_confidence
        interval = item.get("confidence_interval") if isinstance(item.get("confidence_interval"), dict) else {}
        if interval:
            interval["lower"] = max(0, min(100, round(float(interval.get("lower") or 0) + confidence_delta)))
            interval["upper"] = max(interval["lower"], min(100, round(float(interval.get("upper") or 0) + confidence_delta)))
            item["confidence_interval"] = interval
        explanation = item.get("recommendation_explanation") if isinstance(item.get("recommendation_explanation"), dict) else {}
        if explanation:
            explanation["confidence"] = {"value": item["confidence"], **interval}
            factors = explanation.get("factors") if isinstance(explanation.get("factors"), list) else []
            resource_factor = next((factor for factor in factors if isinstance(factor, dict) and factor.get("type") == "resource"), None)
            if resource_factor is None:
                factors.append({"type": "resource", "label": "资源可用性", "score": round(float(breakdown.get("resources") or 0), 1)})
            else:
                resource_factor["score"] = round(float(breakdown.get("resources") or 0), 1)
            explanation["factors"] = sorted(factors, key=lambda row: abs(float(row.get("score") or 0)), reverse=True)
        cap = 100 if float(item.get("personalized_score") or 0) >= 22 else 82
        item["score"] = max(0, min(cap, int(round(float(item.get("score") or 0) + score_boost))))
        if isinstance(item.get("ranking_scores"), dict):
            item["ranking_scores"] = {
                version: max(0, min(cap, int(round(float(value or 0) + score_boost))))
                for version, value in item["ranking_scores"].items()
            }
        item["reasons"] = list(dict.fromkeys(item.get("reasons") or []))[:6]

    tasks = [asyncio.create_task(enrich_one(item)) for item in targets]
    _done, pending = await asyncio.wait(tasks, timeout=budget_seconds)
    if pending:
        pending_codes = [str(targets[index].get("code") or "") for index, task in enumerate(tasks) if task in pending]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        try:
            from app.knowledge.intelligence import enqueue_resource_refresh
            queued = await enqueue_resource_refresh(pending_codes, priority=20)
        except Exception:
            queued = 0
        warnings.append(f"正在后台补全 {len(pending)} 部作品的资源情报" if queued else f"有 {len(pending)} 部作品的资源情报将在稍后重试")
    return warnings[:8]


def _dedupe_recommendations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop stale candidate-pool duplicates that resolve to the same normalized code."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        code = _norm_code(item.get("code"))
        if not code:
            deduped.append(item)
            continue
        if code in seen:
            continue
        seen.add(code)
        deduped.append(item)
    return deduped


async def _merge_cached_resource_intelligence(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items") if isinstance(result.get("items"), list) else []
    if not items:
        return result
    try:
        from app.knowledge.intelligence import cached_resource_summary_map
        summaries = await asyncio.wait_for(
            cached_resource_summary_map([str(item.get("code") or "") for item in items if isinstance(item, dict)]),
            timeout=0.25,
        )
    except Exception:
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        code = _norm_code(item.get("code"))
        summary = summaries.get(code)
        if not summary:
            continue
        item["resource_summary"] = {
            "total": summary["total"],
            "providers": summary["providers"],
            "has_private": summary["has_private"],
            "has_public": summary["has_public"],
            "has_uncensored": summary["has_uncensored"],
            "from_intelligence_core": True,
        }
        item["has_cnsub"] = bool(item.get("has_cnsub") or summary["has_subtitle"])
        item["is_cracked"] = bool(item.get("is_cracked") or summary["has_cracked"])
        item["is_uncensored"] = bool(item.get("is_uncensored") or summary["has_uncensored"])
    return result


def _diversify_recommendations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MMR-style reranking across stable actor, studio, series and topic identities."""
    remaining = list(items)
    selected: list[dict[str, Any]] = []
    actor_seen: Counter = Counter()
    category_seen: Counter = Counter()
    maker_seen: Counter = Counter()
    series_seen: Counter = Counter()
    while remaining:
        best_index = 0
        best_value = -9999.0
        best_adjustment: dict[str, Any] = {}
        for index, item in enumerate(remaining):
            # The public score is intentionally capped, so retain a small part
            # of the uncapped affinity to distinguish several 100-point rows.
            value = float(item.get("score") or 0) + min(5.0, max(0.0, float(item.get("personalized_score") or 0)) * 0.06)
            actors = list(dict.fromkeys(actor_identity_key(x) for x in item.get("actors") or [] if actor_identity_key(x)))
            categories = [str(x) for x in item.get("categories") or [] if _generic_category_factor(x) >= 0.5]
            maker = _norm_key(_name_one(item.get("maker")))
            series = _norm_key(_name_one(item.get("series")))
            actor_penalty = sum(4.5 * math.sqrt(actor_seen.get(actor, 0)) for actor in actors[:4] if actor_seen.get(actor, 0))
            category_penalty = sum(min(3, category_seen.get(category, 0)) * 1.15 for category in categories[:3])
            maker_penalty = 2.75 * (maker_seen.get(maker, 0) ** 0.8) if maker else 0.0
            series_penalty = 5.0 * series_seen.get(series, 0) if series else 0.0
            penalty = actor_penalty + category_penalty + maker_penalty + series_penalty
            adjusted = value - penalty
            if adjusted > best_value:
                best_value = adjusted
                best_index = index
                best_adjustment = {
                    "penalty": round(penalty, 2),
                    "actor": round(actor_penalty, 2),
                    "category": round(category_penalty, 2),
                    "maker": round(maker_penalty, 2),
                    "series": round(series_penalty, 2),
                    "utility": round(adjusted, 2),
                }
        picked = remaining.pop(best_index)
        picked["diversity_rank"] = len(selected) + 1
        picked["diversity_adjustment"] = best_adjustment
        selected.append(picked)
        for actor in (picked.get("actors") or [])[:3]:
            actor_seen[actor_identity_key(actor)] += 1
        for category in (picked.get("categories") or [])[:4]:
            if _generic_category_factor(category) >= 0.5:
                category_seen[str(category)] += 1
        maker = _norm_key(_name_one(picked.get("maker")))
        series = _norm_key(_name_one(picked.get("series")))
        if maker:
            maker_seen[maker] += 1
        if series:
            series_seen[series] += 1
    return selected


def _shadow_rank_map(items: list[dict[str, Any]], versions: tuple[str, ...] = (PERSONALIZED_MODEL_VERSION, STABLE_MODEL_VERSION)) -> dict[str, dict[str, int]]:
    """Rank the same scored pool under both policies without extra provider work."""
    result: dict[str, dict[str, int]] = {}
    for version in versions:
        rows: list[dict[str, Any]] = []
        for item in items:
            ranking_scores = item.get("ranking_scores") if isinstance(item.get("ranking_scores"), dict) else {}
            shadow = dict(item)
            shadow["score"] = float(ranking_scores.get(version) if ranking_scores.get(version) is not None else item.get("score") or 0)
            shadow["personalized_score"] = shadow["score"]
            rows.append(shadow)
        rows.sort(key=lambda row: (float(row.get("score") or 0), row.get("magnets_count") or 0, row.get("release_date") or ""), reverse=True)
        window = min(len(rows), max(160, 4 * 60))
        ranked = _diversify_recommendations(rows[:window]) + rows[window:]
        for rank, item in enumerate(ranked, 1):
            code = _norm_code(item.get("code"))
            if code:
                result.setdefault(code, {})[version] = rank
    return result


def _recommendation_diversity_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    actor_counts: Counter = Counter()
    maker_counts: Counter = Counter()
    for item in items:
        actor_counts.update(set(actor_identity_key(name) for name in item.get("actors") or [] if actor_identity_key(name)))
        maker = _norm_key(_name_one(item.get("maker")))
        if maker:
            maker_counts[maker] += 1

    def metrics(counter: Counter) -> dict[str, Any]:
        total = sum(counter.values())
        probabilities = [count / total for count in counter.values()] if total else []
        effective = math.exp(-sum(value * math.log(value) for value in probabilities)) if probabilities else 0.0
        maximum = max(counter.values(), default=0)
        return {
            "unique": len(counter),
            "effective": round(effective, 1),
            "max_count": maximum,
            "max_share": round(maximum / max(len(items), 1), 3),
        }

    return {"actors": metrics(actor_counts), "makers": metrics(maker_counts)}


def _shortlist_candidates(items: list[dict[str, Any]], profile: dict[str, Any], *, limit: int = 420) -> list[dict[str, Any]]:
    """Cheap recall-stage ranking before expensive semantic and outcome scoring."""
    if len(items) <= limit:
        return items
    actor_counter: Counter = profile.get("actor_identities") or Counter()
    genre_counter: Counter = profile.get("genres") or Counter()
    tag_counter: Counter = profile.get("tags") or Counter()
    studio_counter: Counter = profile.get("studios") or Counter()
    series_counter: Counter = profile.get("series") or Counter()
    director_counter: Counter = profile.get("directors") or Counter()
    current_year = dt.date.today().year

    def recall_value(item: dict[str, Any]) -> float:
        actors = [actor_identity_key(name) for name in (item.get("actors") or [])[:8]]
        categories = [str(name) for name in (item.get("categories") or [])[:12]]
        value = sum(math.log2(float(actor_counter.get(actor) or 0) + 1) * 4 for actor in actors)
        value += sum(math.log2(float(genre_counter.get(name) or tag_counter.get(name) or 0) + 1) * _generic_category_factor(name) for name in categories)
        value += math.log2(float(studio_counter.get(_name_one(item.get("maker"))) or 0) + 1) * 2
        value += math.log2(float(series_counter.get(_name_one(item.get("series"))) or 0) + 1) * 3
        value += math.log2(float(director_counter.get(_name_one(item.get("director"))) or 0) + 1) * 1.5
        value += math.log2(float(item.get("neighbor_score") or 0) + 1) * 3
        release = str(item.get("release_date") or item.get("date") or "")
        value += 2.5 if release.startswith(str(current_year)) else 1 if release.startswith(str(current_year - 1)) else 0
        value += min(2.0, float(item.get("magnets_count") or 0) * 0.25)
        completeness = item.get("completeness") if isinstance(item.get("completeness"), dict) else {}
        value += sum(bool(completeness.get(key)) for key in ("title", "cover", "actors", "categories")) * 0.35
        return value

    ranked = sorted(items, key=lambda item: (recall_value(item), str(item.get("release_date") or "")), reverse=True)
    exploit_count = max(1, int(limit * 0.82))
    selected = ranked[:exploit_count]
    selected_codes = {_candidate_code(item) for item in selected}
    discovery = [item for item in ranked[exploit_count:] if _candidate_code(item) not in selected_codes]
    seed = dt.date.today().isoformat()
    discovery.sort(key=lambda item: hashlib.sha256(f"{seed}:{_candidate_code(item)}".encode("utf-8")).digest())
    selected.extend(discovery[:limit - len(selected)])
    return selected


def _apply_recommendation_controls(items: list[dict[str, Any]], config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Reserve deterministic, uncertainty-aware exploration without displacing the strong majority."""
    threshold = int(_config_number(config, "minimum_confidence_threshold", 0, 0, 80))
    if threshold:
        items = [item for item in items if float(item.get("confidence") or 0) >= threshold]

    limit = max(1, int(limit))
    if not items:
        return []
    if len(items) <= limit:
        return items[:limit]

    ratio = _config_number(config, "exploration_ratio", 0, 0, 0.5)
    if ratio <= 0:
        return items[:limit]

    exploration_count = max(1, min(len(items) - limit, int(round(limit * ratio))))
    if exploration_count <= 0:
        return items[:limit]

    seed = dt.date.today().isoformat()
    top = items[:limit]
    exposed_actors: Counter = Counter()
    exposed_categories: Counter = Counter()
    exposed_makers: Counter = Counter()
    topic_metrics = ((config.get("_topic_evaluation") or {}).get("topics") or {}) if isinstance(config.get("_topic_evaluation"), dict) else {}
    for item in top:
        exposed_actors.update(set(actor_identity_key(name) for name in item.get("actors") or [] if actor_identity_key(name)))
        exposed_categories.update(set(str(name) for name in item.get("categories") or [] if _generic_category_factor(name) >= 0.5))
        maker = _norm_key(_name_one(item.get("maker")))
        if maker:
            exposed_makers[maker] += 1

    def exploration_value(item: dict[str, Any]) -> float:
        personalized = float(item.get("personalized_score") or 0)
        actionable = float(item.get("actionability_score") or 0)
        breakdown = item.get("score_breakdown") if isinstance(item.get("score_breakdown"), dict) else {}
        interval = item.get("confidence_interval") if isinstance(item.get("confidence_interval"), dict) else {}
        uncertainty = max(0.0, float(interval.get("upper") or 0) - float(interval.get("lower") or 0)) / 100
        actor_novelty = 1.0 if float(breakdown.get("actor_preference") or 0) <= 0 else 0.0
        relation_confidence = float(item.get("neighbor_confidence") or 0)
        release = str(item.get("release_date") or "")
        freshness = 1.0 if release.startswith(str(dt.date.today().year)) else 0.55 if release.startswith(str(dt.date.today().year - 1)) else 0.0
        completeness = item.get("portrait_completeness") if isinstance(item.get("portrait_completeness"), dict) else {}
        portrait_quality = sum(bool(completeness.get(key)) for key in ("title", "cover", "actors", "categories")) / 4 if completeness else 0.5
        digest = hashlib.sha256(f"{seed}:{item.get('code') or item.get('title') or ''}".encode("utf-8")).digest()
        jitter = int.from_bytes(digest[:2], "big") / 65535 * 2
        actor_overlap = sum(exposed_actors.get(actor_identity_key(name), 0) for name in set(item.get("actors") or []))
        category_overlap = sum(min(4, exposed_categories.get(str(name), 0)) for name in set(item.get("categories") or []) if _generic_category_factor(name) >= 0.5)
        maker = _norm_key(_name_one(item.get("maker")))
        exposure_penalty = actor_overlap * 2.5 + category_overlap * 0.35 + (exposed_makers.get(maker, 0) * 1.8 if maker else 0)
        hypothesis = item.get("interest_topic_hypothesis") if isinstance(item.get("interest_topic_hypothesis"), dict) else {}
        topic_id = str(hypothesis.get("id") or "")
        topic_confidence = float(hypothesis.get("confidence") or 0)
        topic_support = int(hypothesis.get("support") or 0)
        topic_metric = topic_metrics.get(topic_id) if isinstance(topic_metrics.get(topic_id), dict) else {}
        topic_bonus = 0.0
        if topic_id and topic_confidence >= 0.35 and topic_support >= 2:
            topic_bonus = topic_confidence * (2 + float(topic_metric.get("underexposure") or 1) * 3 + float(topic_metric.get("ucb") or 0.35) * 2)
        return personalized * 0.4 + actionable * 0.18 + actor_novelty * 4 + relation_confidence * 5 + uncertainty * 3 + freshness * 3 + portrait_quality * 2 + topic_bonus + jitter - exposure_penalty

    eligible = [
        item for item in items[limit:]
        if float(item.get("personalized_score") or 0) >= 6
        and (float(item.get("neighbor_confidence") or 0) >= 0.35 or float(item.get("actionability_score") or 0) >= 5)
    ]
    pool = list(eligible or items[limit:])
    picks: list[dict[str, Any]] = []
    picked_topics: Counter = Counter()
    while pool and len(picks) < exploration_count:
        def selection_value(item: dict[str, Any]) -> float:
            hypothesis = item.get("interest_topic_hypothesis") if isinstance(item.get("interest_topic_hypothesis"), dict) else {}
            topic_id = str(hypothesis.get("id") or "")
            return exploration_value(item) - picked_topics.get(topic_id, 0) * 4 if topic_id else exploration_value(item)
        pick = max(pool, key=selection_value)
        pool.remove(pick)
        picks.append(pick)
        hypothesis = pick.get("interest_topic_hypothesis") if isinstance(pick.get("interest_topic_hypothesis"), dict) else {}
        topic_id = str(hypothesis.get("id") or "")
        if topic_id and float(hypothesis.get("confidence") or 0) >= 0.35 and int(hypothesis.get("support") or 0) >= 2:
            picked_topics[topic_id] += 1
    step = max(1, round(limit / (len(picks) + 1)))
    position = step
    for pick in picks:
        pick["is_exploration"] = True
        hypothesis = pick.get("interest_topic_hypothesis") if isinstance(pick.get("interest_topic_hypothesis"), dict) else {}
        topic_eligible = bool(hypothesis.get("id")) and float(hypothesis.get("confidence") or 0) >= 0.35 and int(hypothesis.get("support") or 0) >= 2
        pick["exploration_kind"] = "topic-bandit" if topic_eligible else "uncertainty-aware"
        pick["exploration_topic_id"] = str(hypothesis.get("id") or "") if topic_eligible else ""
        reasons = pick.get("reasons") if isinstance(pick.get("reasons"), list) else []
        exploration_reason = f"探索位：验证主题 {hypothesis.get('label')}" if topic_eligible else "探索位：可信的新方向"
        if exploration_reason not in reasons:
            reasons.append(exploration_reason)
        pick["reasons"] = reasons
        if position < len(top):
            top[position] = pick
        else:
            top.append(pick)
        position += step
    return top[:limit]


async def _recommendations_unlocked(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    source_mode = str(payload.get("source_mode") or "latest").strip().lower()
    if source_mode not in {"latest", "full"}:
        source_mode = "latest"
    store = _ensure_store()
    behavior = await preference_behavior_summary()
    if _sync_core_conversion_stages(store, behavior.get("code_stages")):
        _save_store(store)
    model_selection = _select_ranking_model(config, store)
    route_evaluation = _route_evaluation(store)
    exploration_evaluation = _exploration_evaluation(store)
    topic_evaluation = _topic_evaluation(store)
    session_intent = _session_intent_summary(store)
    core_search_intent = search_intent_summary()
    similarity_status = work_similarity_status()
    context_gate = _contextual_intent_gate(session_intent, core_search_intent)
    search_evaluation = core_search_intent.get("evaluation") or {}
    search_evaluation_summary = {
        "eligible_events": int(search_evaluation.get("eligible_events") or 0),
        "qualified_events": int(search_evaluation.get("qualified_events") or 0),
        "verified_events": int(search_evaluation.get("verified_events") or 0),
        "adaptive_signals": int(search_evaluation.get("adaptive_signals") or 0),
        "signal_count": len(search_evaluation.get("signals") or {}),
    }
    exposure_fatigue = _exposure_fatigue(store)
    feedback = {
        "ignored_codes": _feedback_codes(store.get("ignored")),
        "liked_codes": _feedback_codes(store.get("liked")),
        "disliked_codes": _feedback_codes(store.get("disliked")),
        "liked_actors": _feedback_counter(store.get("liked"), "actors"),
        "liked_categories": _feedback_counter(store.get("liked"), "categories"),
        "disliked_actors": _feedback_counter(store.get("disliked"), "actors"),
        "disliked_categories": _feedback_counter(store.get("disliked"), "categories"),
        "liked_topics": _feedback_topic_counter(store.get("liked")),
        "disliked_topics": _feedback_topic_counter(store.get("disliked")),
        "behavior_codes": Counter(behavior.get("codes") or {}),
        "behavior_actors": Counter(behavior.get("actor_identities") or behavior.get("actors") or {}),
        "behavior_categories": Counter(behavior.get("categories") or {}),
        "trend_actors": dict((((behavior.get("trends") or {}).get("actors") or {}).get("deltas") or {})),
        "trend_categories": dict((((behavior.get("trends") or {}).get("categories") or {}).get("deltas") or {})),
        "outcome_model": behavior.get("outcomes") or {},
        "interest_topics": list((behavior.get("interest_topics") or {}).get("topics") or []),
        "topic_weights": {topic_id: float(metric.get("weight") or 1) for topic_id, metric in (topic_evaluation.get("topics") or {}).items()},
        "session_intent": session_intent,
        "search_intent": core_search_intent,
        "context_gate": context_gate,
        "route_weights": {route: float(metric.get("weight") or 1) for route, metric in (route_evaluation.get("routes") or {}).items()},
        "exposure_penalties": _exposure_penalties(store),
        "exposure_fatigue": exposure_fatigue,
    }
    requested_limit = max(1, min(int(payload.get("limit") or 48), 100))
    cache_ttl = int(_config_number(config, "recommendation_cache_minutes", 30, 5, 1440) * 60)
    # This key intentionally avoids library/profile work. Cache invalidation
    # handles feedback and candidate-pool changes, while the TTL bounds how
    # long a newly imported library item can remain visible in recommendations.
    # Looking this up first is what makes a normal page visit a real cache hit.
    fast_cache_key = json.dumps({
        "kind": "request-v3",
        "algorithm_version": RECOMMENDATION_ALGORITHM_VERSION,
        "ranking_model": model_selection["version"],
        "actor_alias_revision": actor_alias_revision(),
        "work_similarity_revision": similarity_status.get("revision"),
        "behavior_revision": behavior.get("revision"),
        "interest_topic_revision": (behavior.get("interest_topics") or {}).get("revision"),
        "config": config,
        "ignored": sorted(feedback["ignored_codes"]),
        "liked": sorted(feedback["liked_codes"]),
        "disliked": sorted(feedback["disliked_codes"]),
        "liked_actors": dict(feedback["liked_actors"]),
        "liked_categories": dict(feedback["liked_categories"]),
        "disliked_actors": dict(feedback["disliked_actors"]),
        "disliked_categories": dict(feedback["disliked_categories"]),
        "exposure_penalties": feedback["exposure_penalties"],
        "route_weights": feedback["route_weights"],
        "topic_weights": feedback["topic_weights"],
        "liked_topics": dict(feedback["liked_topics"]),
        "disliked_topics": dict(feedback["disliked_topics"]),
        "session_intent_revision": session_intent["revision"],
        "search_intent_revision": core_search_intent["revision"],
        "source_mode": source_mode,
        "requested_limit": requested_limit,
    }, sort_keys=True, ensure_ascii=False)
    if not payload.get("refresh"):
        cached = _recommendation_cache_get(fast_cache_key, cache_ttl, annotate=True)
        if cached is not None:
            return await _merge_cached_resource_intelligence(cached)
        stale = _recommendation_cache_get(fast_cache_key, cache_ttl, allow_stale=True, annotate=True)
        if stale is not None:
            _schedule_recommendation_refresh(config, payload, fast_cache_key)
            stale["cache_status"]["refreshing"] = True
            return await _merge_cached_resource_intelligence(stale)

    profile = await _library_profile()
    cold_start_threshold = max(5, min(int(config.get("cold_start_min_library_size") or 20), 100))
    media_count = int(profile.get("media_count") or 0)
    cold_start_strength = max(0.0, min(1.0, 1 - media_count / cold_start_threshold)) if config.get("adaptive_cold_start_enabled", True) else 0.0
    scoring_config = {**config, "_cold_start_strength": cold_start_strength, "_ranking_policy": model_selection["policy"]}
    live_codes, live_warning = await _live_library_codes(config, force=bool(payload.get("refresh")))
    cache_key = json.dumps({
        "algorithm_version": RECOMMENDATION_ALGORITHM_VERSION,
        "ranking_model": model_selection["version"],
        "actor_alias_revision": actor_alias_revision(),
        "work_similarity_revision": similarity_status.get("revision"),
        "behavior_revision": behavior.get("revision"),
        "interest_topic_revision": (behavior.get("interest_topics") or {}).get("revision"),
        "config": config,
        "ignored": sorted(feedback["ignored_codes"]),
        "liked": sorted(feedback["liked_codes"]),
        "disliked": sorted(feedback["disliked_codes"]),
        "liked_actors": dict(feedback["liked_actors"]),
        "liked_categories": dict(feedback["liked_categories"]),
        "disliked_actors": dict(feedback["disliked_actors"]),
        "disliked_categories": dict(feedback["disliked_categories"]),
        "exposure_penalties": feedback["exposure_penalties"],
        "route_weights": feedback["route_weights"],
        "topic_weights": feedback["topic_weights"],
        "liked_topics": dict(feedback["liked_topics"]),
        "disliked_topics": dict(feedback["disliked_topics"]),
        "session_intent_revision": session_intent["revision"],
        "search_intent_revision": core_search_intent["revision"],
        "library_codes": sorted(live_codes),
        "library_code_count": len(profile.get("codes") or []),
        "library_code_fingerprint": _code_fingerprint(profile.get("codes") or set()),
        "source_mode": source_mode,
        "requested_limit": requested_limit,
    }, sort_keys=True, ensure_ascii=False)
    if not payload.get("refresh"):
        cached = _recommendation_cache_get(cache_key, cache_ttl, annotate=True)
        if cached is not None:
            return await _merge_cached_resource_intelligence(cached)
        stale = _recommendation_cache_get(cache_key, cache_ttl, allow_stale=True, annotate=True)
        if stale is not None:
            _schedule_recommendation_refresh(config, payload, cache_key)
            stale["cache_status"]["refreshing"] = True
            return await _merge_cached_resource_intelligence(stale)

    pool = _pool()
    if source_mode == "full" and _backfill_candidate_pool_title_profiles(pool):
        _save_pool(pool)
    pool_items = pool.get("items") if isinstance(pool.get("items"), dict) else {}
    similarity_meta: dict[str, Any] = {}
    similarity_evaluation: dict[str, Any] = {}
    if source_mode == "full":
        candidates = [dict(item) for item in pool_items.values() if isinstance(item, dict)]
        warnings: list[str] = []
        if not candidates:
            warnings.append("完整候选池尚未建立，请先执行候选池扫描。")
    else:
        candidates, warnings = await _javdb_candidates(config)
        # Reuse persisted source and first-seen metadata without replacing the
        # fresher fields returned by the latest feed.
        for item in candidates:
            code = _candidate_code(item)
            persisted = pool_items.get(code) if code else None
            if isinstance(persisted, dict):
                if not item.get("source_tags"):
                    item["source_tags"] = list(persisted.get("source_tags") or [])
                item["is_today_increment"] = bool(persisted.get("is_today_increment"))

    base_recall_source = "candidate-pool" if source_mode == "full" else "javdb-feed"
    for item in candidates:
        item["recall_sources"] = list(dict.fromkeys([*(item.get("recall_sources") or []), base_recall_source]))

    try:
        from app.knowledge.intelligence import work_similarity_candidates
        graph_seed_weights, graph_seed_sources = _positive_neighbor_seed_weights(profile, behavior, store)
        try:
            similarity_evaluation = await work_similarity_recall_evaluation(
                set(profile.get("codes") or []),
                graph_seed_weights,
            )
        except Exception as exc:
            similarity_evaluation = {"error": str(exc), "evaluated": 0}
        relation_weights = dict(((similarity_evaluation.get("relation_counterfactual") or {}).get("recommended_weights") or {}))
        similarity_meta = await work_similarity_candidates(
            graph_seed_weights or {code: 1.0 for code in profile.get("codes") or set()},
            negative_seed_weights=_negative_neighbor_seed_weights(store),
            relation_weights=relation_weights,
            limit=160,
        )
        similarity_meta["seed_sources"] = graph_seed_sources
        similarity_meta["relation_weights"] = relation_weights
        coverage_repairs = [
            row for row in (similarity_evaluation.get("sample_misses") or [])
            if isinstance(row, dict)
            and row.get("reason") == "no_neighbor_path"
            and {"actors", "categories", "title"} & set(row.get("profile_gaps") or [])
        ]
        similarity_meta["coverage_repair_candidates"] = len(coverage_repairs)
        similarity_meta["coverage_repair_queued"] = _queue_profile_enrichment(
            config,
            coverage_repairs,
            max_accept=12,
            reason="offline_no_path",
        )
        by_code = {_candidate_code(item): item for item in candidates if _candidate_code(item)}
        for neighbor in similarity_meta.get("items") or []:
            code = _candidate_code(neighbor)
            if not code:
                continue
            existing = by_code.get(code)
            if existing is None:
                existing = dict(neighbor)
                graph_recall = int(neighbor.get("neighbor_hop_count") or 1) > 1
                existing["source_tags"] = [{"id": "intelligence-graph" if graph_recall else "intelligence-neighbor", "label": "Core 图传播" if graph_recall else "Core 邻域"}]
                existing["recall_sources"] = ["core-graph" if graph_recall else "core-neighbor"]
                candidates.append(existing)
                by_code[code] = existing
            else:
                existing["neighbor_score"] = neighbor.get("neighbor_score")
                existing["neighbor_confidence"] = neighbor.get("neighbor_confidence")
                existing["neighbor_evidence"] = neighbor.get("neighbor_evidence") or []
                existing["neighbor_hop_count"] = neighbor.get("neighbor_hop_count") or 1
                existing["neighbor_negative_score"] = neighbor.get("neighbor_negative_score")
                existing["neighbor_negative_evidence"] = neighbor.get("neighbor_negative_evidence") or []
                graph_recall = int(neighbor.get("neighbor_hop_count") or 1) > 1
                route_id = "core-graph" if graph_recall else "core-neighbor"
                tag_id = "intelligence-graph" if graph_recall else "intelligence-neighbor"
                tag_label = "Core 图传播" if graph_recall else "Core 邻域"
                existing["recall_sources"] = list(dict.fromkeys([*(existing.get("recall_sources") or []), route_id]))
                for key in ("actors", "categories", "maker", "series", "director", "cover_url", "image_candidates", "release_date", "field_sources", "completeness"):
                    if not existing.get(key) and neighbor.get(key):
                        existing[key] = neighbor[key]
                tags = list(existing.get("source_tags") or [])
                if not any(str(tag.get("id") or "") == tag_id for tag in tags if isinstance(tag, dict)):
                    tags.append({"id": tag_id, "label": tag_label})
                existing["source_tags"] = tags
        similarity_meta["profile_gaps"] = Counter(gap for neighbor in (similarity_meta.get("items") or []) for gap in _candidate_profile_gaps(neighbor))
        similarity_meta["profile_enrichment_queued"] = _queue_profile_enrichment(config, [neighbor for neighbor in (similarity_meta.get("items") or []) if _candidate_profile_gaps(neighbor)])
    except Exception as exc:
        warnings.append(f"Core 邻域召回暂不可用：{exc}")

    excluded_codes = set(profile.get("codes") or set())
    excluded_codes.update(live_codes)
    excluded_codes.update(_subscription_codes())
    candidates = [
        item for item in candidates
        if _candidate_code(item) not in excluded_codes
        and not bool((item.get("library") or {}).get("in_library") if isinstance(item.get("library"), dict) else False)
    ]
    if live_warning:
        warnings.append(live_warning)
    recalled_candidate_count = len(candidates)
    if source_mode == "full":
        candidates = await asyncio.to_thread(_shortlist_candidates, candidates, profile, limit=max(180, requested_limit * 3))

    def score_candidates() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        diagnostics: list[dict[str, Any]] = []
        rows = [rec for item in candidates if (rec := _candidate_score(item, profile, scoring_config, feedback, diagnostics))]
        rows = _dedupe_recommendations(rows)
        rows.sort(key=lambda x: (x["score"], x.get("magnets_count") or 0, x.get("release_date") or ""), reverse=True)
        return rows, diagnostics

    scored, filtered_diagnostics = await asyncio.to_thread(score_candidates)
    # A small first pass lets resource actionability influence ranking without
    # making the initial recommendation request excessively expensive.
    initial_resource_config = {**config, "resource_enrich_limit": 16, "resource_enrich_budget_seconds": 4}
    resource_warnings = await _enrich_recommendation_resources(initial_resource_config, scored)
    if resource_warnings:
        warnings.extend(resource_warnings)
    scored.sort(key=lambda x: (x["score"], (x.get("resource_summary") or {}).get("total") or 0, x.get("magnets_count") or 0, x.get("release_date") or ""), reverse=True)
    shadow_ranks = await asyncio.to_thread(_shadow_rank_map, scored)
    # Full mode can contain thousands of candidates. Diversifying all of them
    # is O(n²), while only a bounded head can reach this response or its
    # exploration pool. Preserve the scored tail without blocking the loop.
    diversify_window = min(len(scored), max(160, requested_limit * 4))
    diversified_head = await asyncio.to_thread(_diversify_recommendations, scored[:diversify_window])
    scored = diversified_head + scored[diversify_window:]
    controls_config = dict(config)
    controls_config["_topic_evaluation"] = topic_evaluation
    if config.get("adaptive_exploration_enabled", True):
        mature_route_samples = int(route_evaluation.get("eligible") or 0)
        adaptive_ratio = 0.08 if media_count >= 100 and mature_route_samples >= 30 else 0.1 if media_count >= 30 else 0.14
        exploration_metric = (exploration_evaluation.get("cohorts") or {}).get("exploration") or {}
        ranking_metric = (exploration_evaluation.get("cohorts") or {}).get("ranking") or {}
        if int(exploration_metric.get("exposed") or 0) >= 20:
            delta = float(exploration_metric.get("posterior_rate") or 0) - float(ranking_metric.get("posterior_rate") or 0)
            adaptive_ratio = max(0.06, min(0.14, adaptive_ratio + max(-0.02, min(0.02, delta * 0.25))))
        controls_config["exploration_ratio"] = max(float(config.get("exploration_ratio") or 0), adaptive_ratio)
    if cold_start_strength > 0:
        controls_config["exploration_ratio"] = max(float(config.get("exploration_ratio") or 0), 0.1 + cold_start_strength * 0.1)
    scored = _apply_recommendation_controls(scored, controls_config, requested_limit)
    for rank, item in enumerate(scored, 1):
        item["recommendation_rank"] = rank
        item["model_version"] = model_selection["version"]
        item["shadow_ranks"] = dict(shadow_ranks.get(_norm_code(item.get("code"))) or {})
        item["shadow_ranks"][model_selection["version"]] = rank
    # Diversification can promote candidates that were outside the first-pass
    # window. Confirm the cards that will actually be displayed as a second
    # pass, skipping rows already enriched above.
    display_resource_warnings = await _enrich_recommendation_resources(config, scored)
    if display_resource_warnings:
        warnings.extend(display_resource_warnings)
    enrichment_public_state = _profile_enrichment_public_state()
    result = {
        "ok": True,
        "generated_at": _now_ms(),
        "algorithm_version": RECOMMENDATION_ALGORITHM_VERSION,
        "requested_limit": requested_limit,
        "source_mode": source_mode,
        "source_label": "完整推荐" if source_mode == "full" else "最新推荐",
        "model": model_selection,
        "items": scored,
        "total": len(scored),
        "profile": {
            "media_count": profile.get("media_count") or 0,
            "code_count": len(profile.get("codes") or []),
            "top_actors": _top(profile.get("actors") or Counter(), 10),
            "top_genres": _top(profile.get("genres") or Counter(), 10),
            "top_tags": _top(profile.get("tags") or Counter(), 10),
            "top_title_traits": _top(profile.get("title_traits") or Counter(), 10),
            "top_title_terms": _top(profile.get("title_terms") or Counter(), 12),
            "top_studios": _top(profile.get("studios") or Counter(), 8),
            "top_series": _top(profile.get("series") or Counter(), 8),
            "top_directors": _top(profile.get("directors") or Counter(), 8),
            "top_interest_topics": list((behavior.get("interest_topics") or {}).get("topics") or [])[:8],
            "current_intent": {
                "interaction": session_intent,
                "search": {**{key: value for key, value in core_search_intent.items() if key != "evaluation"}, "evaluation": search_evaluation_summary},
            },
        },
        "stats": {
            "candidates": len(candidates),
            "recalled_candidates": recalled_candidate_count,
            "shortlisted_candidates": len(candidates),
            "candidate_pool_total": _candidate_pool_stats(pool)["total"],
            "candidate_pool_today": _candidate_pool_stats(pool)["today_increment"],
            "today_increment": sum(1 for item in candidates if item.get("is_today_increment")),
            "ignored": len([x for x in feedback["ignored_codes"] if x]),
            "disliked": len([x for x in feedback["disliked_codes"] if x]),
            "cold_start": {"active": cold_start_strength > 0, "strength": round(cold_start_strength, 3), "threshold": cold_start_threshold},
            "model_evaluation": _model_evaluation(store),
            "shadow_evaluation": _shadow_model_evaluation(store),
            "route_evaluation": route_evaluation,
            "exploration_evaluation": exploration_evaluation,
            "topic_evaluation": topic_evaluation,
            "search_evaluation": search_evaluation_summary,
            "context_mixture": context_gate,
            "exposure_fatigue": {
                "active": len(exposure_fatigue),
                "short": sum(1 for row in exposure_fatigue.values() if float(row.get("short") or 0) >= 0.1),
                "daily": sum(1 for row in exposure_fatigue.values() if float(row.get("daily") or 0) >= 0.1),
                "long": sum(1 for row in exposure_fatigue.values() if float(row.get("long") or 0) >= 0.1),
                "max_penalty": max((float(row.get("total") or 0) for row in exposure_fatigue.values()), default=0.0),
            },
            "session_intent": {
                "event_count": session_intent["event_count"] + core_search_intent["event_count"],
                "interaction_events": session_intent["event_count"],
                "search_events": core_search_intent["event_count"],
                "revision": f"{session_intent['revision']}:{core_search_intent['revision']}",
            },
            "exploration": {
                "adaptive": bool(config.get("adaptive_exploration_enabled", True)),
                "ratio": round(float(controls_config.get("exploration_ratio") or 0), 3),
                "selected": sum(1 for item in scored if item.get("is_exploration")),
            },
            "diversity": _recommendation_diversity_metrics(scored),
            "neighbor_recall": {
                "seeds": int(similarity_meta.get("seed_count") or 0),
                "negative_seeds": int(similarity_meta.get("negative_seed_count") or 0),
                "candidates": len(similarity_meta.get("items") or []),
                "linked_works": int(similarity_meta.get("linked_work_count") or 0),
                "scored": sum(1 for item in scored if float(item.get("neighbor_score") or 0) > 0),
                "selected": sum(1 for item in scored if {"core-neighbor", "core-graph"} & set(item.get("recall_sources") or [])),
                "direct_selected": sum(1 for item in scored if "core-neighbor" in (item.get("recall_sources") or [])),
                "multi_hop_selected": sum(1 for item in scored if "core-graph" in (item.get("recall_sources") or [])),
                "core_only_selected": sum(1 for item in scored if set(item.get("recall_sources") or []) in ({"core-neighbor"}, {"core-graph"})),
                "feed_overlap_selected": sum(1 for item in scored if {"core-neighbor", "core-graph"} & set(item.get("recall_sources") or []) and base_recall_source in (item.get("recall_sources") or [])),
                "propagation": dict(similarity_meta.get("propagation") or {}),
                "relation_weights": dict(similarity_meta.get("relation_weights") or {}),
                "seed_sources": dict(similarity_meta.get("seed_sources") or {}),
                "average_confidence": round(sum(float(item.get("neighbor_confidence") or 0) for item in scored if float(item.get("neighbor_score") or 0) > 0) / max(1, sum(1 for item in scored if float(item.get("neighbor_score") or 0) > 0)), 3),
                "profile_gaps": dict(similarity_meta.get("profile_gaps") or {}),
                "profile_enrichment_queued": int(similarity_meta.get("profile_enrichment_queued") or 0),
                "coverage_repair_candidates": int(similarity_meta.get("coverage_repair_candidates") or 0),
                "coverage_repair_queued": int(similarity_meta.get("coverage_repair_queued") or 0),
                "coverage_repair_state": {
                    "queued": int(enrichment_public_state.get("coverage_queued") or 0),
                    "enriched": int(enrichment_public_state.get("coverage_enriched") or 0),
                    "failed": int(enrichment_public_state.get("coverage_failed") or 0),
                },
                "feature_quality": dict(similarity_meta.get("feature_quality") or {}),
                "offline_evaluation": similarity_evaluation,
            },
        },
        "candidate_meta": {"pool": _candidate_pool_stats(pool)},
        "filtered": _filtered_summary(filtered_diagnostics),
        "warnings": warnings,
        "cache_status": {"status": "generated", "age_seconds": 0, "refreshing": False},
    }
    _recommendation_cache_put(cache_key, result, source_mode=source_mode)
    _recommendation_cache_put(fast_cache_key, result, source_mode=source_mode)
    return result


def _schedule_recommendation_refresh(config: dict[str, Any], payload: dict[str, Any], cache_key: str) -> None:
    task_key = _recommendation_cache_id(cache_key)
    current = _recommendation_refresh_tasks.get(task_key)
    if current and not current.done():
        return

    async def refresh() -> None:
        try:
            await _recommendations(dict(config), {**dict(payload), "refresh": True})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            _recommendation_refresh_tasks.pop(task_key, None)

    _recommendation_refresh_tasks[task_key] = asyncio.create_task(refresh())


async def _recommendations(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Single-flight each mode without letting full maintenance block latest."""
    source_mode = str(payload.get("source_mode") or "latest").strip().lower()
    lock = _recommendation_generation_locks["full" if source_mode == "full" else "latest"]
    if not payload.get("refresh"):
        requested_limit = max(1, min(int(payload.get("limit") or 48), 100))
        ttl_seconds = int(_config_number(config, "recommendation_cache_minutes", 30, 5, 1440) * 60)
        snapshot = _latest_mode_snapshot(source_mode, requested_limit, ttl_seconds=ttl_seconds)
        if snapshot is not None:
            if snapshot.get("cache_status", {}).get("status") == "stale":
                _schedule_recommendation_refresh(config, payload, f"mode-snapshot:{source_mode}:{requested_limit}")
            return snapshot
    async with lock:
        return await _recommendations_unlocked(config, payload)


async def _prewarm_recommendations(config: dict[str, Any], *, force: bool = False, include_full: bool = False) -> None:
    """Keep the normal page variants warm before their persistent cache expires."""
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    _prewarm_state.update({"status": "running", "last_started_at": started_at, "last_error": "", "modes": []})
    modes = ["latest"]
    if include_full and _candidate_pool_stats(_pool()).get("total"):
        modes.append("full")
    try:
        for source_mode in modes:
            await _recommendations(config, {"source_mode": source_mode, "limit": 60, "refresh": force})
            _prewarm_state["modes"] = [*_prewarm_state.get("modes", []), source_mode]
        _prewarm_state.update({"status": "idle", "last_finished_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _prewarm_state.update({"status": "failed", "last_finished_at": dt.datetime.now(dt.timezone.utc).isoformat(), "last_error": str(exc)[:1000]})


async def test(config: dict[str, Any]) -> PluginTestResult:
    try:
        from app.plugins.runtime import runtime
        if not runtime.is_enabled("javdb"):
            return PluginTestResult(ok=False, message="JavDB 插件未启用")
        profile = await _library_profile()
        return PluginTestResult(ok=True, message="recommendation ready", details={"media_count": profile.get("media_count", 0)})
    except Exception as exc:
        return PluginTestResult(ok=False, message=f"recommendation failed: {exc}")


async def handle_action(action: str, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if action in {"recommendations", "overview"}:
        return await _recommendations(config, payload or {})
    if action == "profile":
        profile = await _library_profile()
        return {
            "ok": True,
            "profile": {
                "media_count": profile.get("media_count") or 0,
                "code_count": len(profile.get("codes") or []),
                "top_actors": _top(profile.get("actors") or Counter(), 20),
                "top_genres": _top(profile.get("genres") or Counter(), 20),
                "top_tags": _top(profile.get("tags") or Counter(), 20),
                "top_title_traits": _top(profile.get("title_traits") or Counter(), 20),
                "top_title_terms": _top(profile.get("title_terms") or Counter(), 30),
                "top_studios": _top(profile.get("studios") or Counter(), 12),
                "top_series": _top(profile.get("series") or Counter(), 12),
                "top_directors": _top(profile.get("directors") or Counter(), 12),
            },
        }
    if action == "scan_candidate_pool":
        return await _scan_candidate_pool(config, force=bool(payload.get("force")))
    if action == "refresh_cover":
        return await _refresh_candidate_cover(payload.get("code") or payload.get("number"))
    if action == "candidate_pool":
        return {"ok": True, "pool": _candidate_pool_stats(_pool()), "profile_enrichment": _profile_enrichment_public_state()}
    if action == "feedback":
        code = _norm_code(payload.get("code"))
        kind = str(payload.get("kind") or "ignore").strip()
        if not code:
            raise ValueError("缺少番号")
        data = _ensure_store()
        key = "ignored" if kind == "ignore" else "liked" if kind == "like" else "disliked"
        row = {
            "code": code,
            "created_at": _now_ms(),
            "reason": str(payload.get("reason") or ""),
            "actors": [str(x).strip() for x in (payload.get("actors") or []) if str(x or "").strip()][:8],
            "categories": [str(x).strip() for x in (payload.get("categories") or []) if str(x or "").strip()][:12],
            "recall_sources": [str(x).strip() for x in (payload.get("recall_sources") or []) if str(x or "").strip()][:8],
            "is_exploration": bool(payload.get("is_exploration")),
            "exploration_kind": str(payload.get("exploration_kind") or "")[:64],
            "interest_topic": payload.get("interest_topic") if isinstance(payload.get("interest_topic"), dict) else {},
            "interest_topic_hypothesis": payload.get("interest_topic_hypothesis") if isinstance(payload.get("interest_topic_hypothesis"), dict) else {},
        }
        data[key] = [x for x in data.get(key, []) if _norm_code(x.get("code") if isinstance(x, dict) else x) != code]
        data[key].insert(0, row)
        if kind == "like":
            _mark_exposure_converted(data, code, "feedback:like")
            _record_session_intent(data, row, "feedback:like")
        _save_store(data)
        _invalidate_recommendation_cache()
        return {"ok": True, "code": code, "kind": kind}
    if action == "behavior":
        created = await record_preference_event(
            str(payload.get("code") or ""),
            str(payload.get("event_type") or ""),
            source="av-recommend",
            actors=list(payload.get("actors") or []),
            categories=list(payload.get("categories") or []),
            data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
        )
        data = _ensure_store()
        converted = _mark_exposure_converted(data, payload.get("code"), str(payload.get("event_type") or "interaction"))
        intent_recorded = _record_session_intent(data, payload, str(payload.get("event_type") or "interaction"))
        if converted or intent_recorded:
            _save_store(data)
        if created or converted or intent_recorded:
            _invalidate_recommendation_cache(hard=False, reason="session-intent")
        return {"ok": True, "created": created, "exposure_converted": converted, "session_intent_recorded": intent_recorded}
    if action == "exposure":
        data = _ensure_store()
        before = _exposure_penalties(data)
        recorded = _record_exposure_batch(data, str(payload.get("batch_id") or ""), list(payload.get("items") or []))
        if recorded:
            _save_store(data)
            if _exposure_penalties(data) != before:
                _invalidate_recommendation_cache(hard=False, reason="exposure-fatigue")
        return {"ok": True, "recorded": recorded}
    if action == "model_evaluation":
        data = _ensure_store()
        return {"ok": True, "selection": _select_ranking_model(config, data), "evaluation": _model_evaluation(data), "shadow_evaluation": _shadow_model_evaluation(data), "route_evaluation": _route_evaluation(data), "exploration_evaluation": _exploration_evaluation(data)}
    if action == "model_policy":
        policy = str(payload.get("policy") or "auto").strip().lower()
        if policy not in {"auto", "personal", "stable"}:
            raise ValueError("模型策略仅支持 auto、personal 或 stable")
        data = _ensure_store()
        if policy == "auto":
            data.pop("ranking_model_override", None)
        else:
            data["ranking_model_override"] = policy
        _save_store(data)
        _invalidate_recommendation_cache()
        asyncio.create_task(_prewarm_recommendations(config, force=True, include_full=False))
        return {"ok": True, "selection": _select_ranking_model(config, data), "evaluation": _model_evaluation(data), "shadow_evaluation": _shadow_model_evaluation(data)}
    if action == "reset_feedback":
        data = _ensure_store()
        data["ignored"] = []
        data["liked"] = []
        data["disliked"] = []
        data["exposures"] = {}
        data["exposure_batches"] = []
        _save_store(data)
        await clear_preference_events(source="av-recommend")
        _invalidate_recommendation_cache()
        return {"ok": True}
    raise ValueError(f"unsupported action: {action}")


def background_tasks(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or {}
    pool = _pool()
    stats = _candidate_pool_stats(pool)
    background = stats.get("background") or {}
    last_scan = stats.get("last_full_scan") or {}
    interval = _scan_interval_minutes(config)
    running = bool(background.get("running")) and not _candidate_pool_background_stale(pool, background)
    failed = bool(background.get("last_error"))
    status = "failed" if failed else ("running" if running else "idle")
    store = _ensure_store()
    selection = _select_ranking_model(config, store)
    evaluation = _model_evaluation(store)
    active_metrics = (evaluation.get("models") or {}).get(selection["version"]) or {}
    return [{
        "id": "av-recommend.candidate-pool",
        "title": "完整推荐候选池",
        "status": status,
        "last_run_at": background.get("started_at") or last_scan.get("at") or None,
        "last_finished_at": background.get("finished_at") or last_scan.get("at") or None,
        "summary": f"{stats['total']} 个候选 · 今日新增 {stats['today_increment']} · 每 {interval} 分钟更新",
        "detail": background.get("last_error") or (f"最近扫描 {last_scan.get('scanned', 0)} 项" if last_scan else "等待首次扫描"),
        "metrics": {
            "candidate_pool_total": stats["total"],
            "today_increment": stats["today_increment"],
            "interval_minutes": interval,
        },
    }, {
        "id": "av-recommend.snapshot-prewarm",
        "title": "推荐快照预热",
        "status": str(_prewarm_state.get("status") or "idle"),
        "last_run_at": _prewarm_state.get("last_started_at"),
        "last_finished_at": _prewarm_state.get("last_finished_at"),
        "summary": f"提前生成最新推荐与完整推荐 · 缓存 {_config_number(config, 'recommendation_cache_minutes', 30, 5, 1440):g} 分钟",
        "detail": _prewarm_state.get("last_error") or ("已预热：" + "、".join(_prewarm_state.get("modes") or []) if _prewarm_state.get("modes") else "等待首次预热"),
        "metrics": {"modes": list(_prewarm_state.get("modes") or [])},
    }, {
        "id": "av-recommend.model-safety",
        "title": "推荐模型安全评估",
        "status": "idle",
        "last_run_at": evaluation.get("generated_at"),
        "last_finished_at": evaluation.get("generated_at"),
        "summary": f"{selection['version']} · {int(active_metrics.get('exposed') or 0)} 个曝光样本",
        "detail": selection["reason"],
        "metrics": {"selection": selection, "evaluation": evaluation},
    }]
