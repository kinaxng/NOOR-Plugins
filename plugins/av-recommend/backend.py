from __future__ import annotations

import ast
import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import async_session_maker
from app.core.models import EmbyItemCache
from app.core.runtime_paths import plugin_data_path
from app.knowledge.models import KnowledgeActionState, KnowledgeEdge, KnowledgeEntity
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
DEFAULT_CACHE_TTL = 1800
_CACHE: dict[str, Any] = {"entries": {}}
_LIVE_LIBRARY_CODES_CACHE: dict[str, Any] = {"ts": 0.0, "key": "", "codes": set(), "warning": ""}
_pool_lock = asyncio.Lock()
_scheduler_task: asyncio.Task[None] | None = None
_scheduler_stop: asyncio.Event | None = None


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


def _recommendation_cache_get(cache_key: str, ttl_seconds: int = DEFAULT_CACHE_TTL) -> dict[str, Any] | None:
    cache_id = _recommendation_cache_id(cache_key)
    entries = _CACHE.setdefault("entries", {})
    entry = entries.get(cache_id)
    if not isinstance(entry, dict):
        entry = _load_recommendation_cache().get("entries", {}).get(cache_id)
        if isinstance(entry, dict):
            entries[cache_id] = entry
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("ts") or 0) >= ttl_seconds:
        entries.pop(cache_id, None)
        return None
    value = entry.get("value")
    return value if isinstance(value, dict) else None


def _recommendation_cache_put(cache_key: str, value: dict[str, Any]) -> None:
    cache_id = _recommendation_cache_id(cache_key)
    data = _load_recommendation_cache()
    entries = data.setdefault("entries", {})
    entries[cache_id] = {"ts": time.time(), "value": value}
    data["entries"] = dict(
        sorted(entries.items(), key=lambda item: float((item[1] or {}).get("ts") or 0), reverse=True)[:8]
    )
    path = _recommendation_cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    _CACHE["entries"] = dict(data["entries"])


def _invalidate_recommendation_cache() -> None:
    _CACHE["entries"] = {}
    with contextlib.suppress(OSError):
        _recommendation_cache_file().unlink()

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
    keys = ("fanart_url", "cover_url", "thumb_url", "image", "poster_url", "jacket_url", "preview_url")

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
                counter[text] += 1
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
        data = {"version": 1, "ignored": [], "liked": [], "disliked": []}
        data_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    try:
        data = json.loads(data_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid feedback store")
        data.setdefault("ignored", [])
        data.setdefault("liked", [])
        data.setdefault("disliked", [])
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


def _is_actor_name_term(term: str, actor_names: set[str]) -> bool:
    normalized = _norm_key(term)
    return any(
        normalized and actor_key and (normalized == actor_key or normalized in actor_key or actor_key in normalized)
        for actor_key in (_norm_key(actor) for actor in actor_names)
    )


def _prune_title_term_counter(counter: Counter) -> Counter:
    trait_labels = {str(trait.get("name") or "") for trait in TITLE_TRAIT_PATTERNS}
    items = [(str(term), float(count)) for term, count in counter.items() if str(term).strip() and float(count or 0) > 0]
    kept: Counter = Counter()
    for term, count in sorted(items, key=lambda row: (len(row[0]), -row[1])):
        if term in trait_labels:
            continue
        if len(term) <= 2 and any(term != longer and term in longer and len(longer) > len(term) and longer_count >= count * 0.5 for longer, longer_count in items):
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
    observed_at = entity.updated_at or entity.created_at
    if not observed_at:
        return 1.0
    try:
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=dt.timezone.utc)
        age_days = max(0.0, (dt.datetime.now(dt.timezone.utc) - observed_at.astimezone(dt.timezone.utc)).total_seconds() / 86400)
    except Exception:
        return 1.0
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


def _load_title_profile_cache(signature: str) -> dict[str, Counter] | None:
    title_profile_file = _title_profile_file()
    try:
        data = json.loads(title_profile_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("signature") != signature or int(data.get("version") or 0) != TITLE_PROFILE_VERSION:
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


def _build_title_profile(media: list[KnowledgeEntity], media_weights: dict[str, float], actor_names: set[str]) -> dict[str, Counter]:
    title_traits: Counter = Counter()
    title_terms: Counter = Counter()
    for item in media:
        payload = _title_profile_media_payload(item)
        weight = media_weights.get(item.id, 1.0)
        for tag in _analyze_title(payload).get("tags") or []:
            name = str(tag.get("name") or "").strip()
            if name:
                title_traits[name] += weight * float(tag.get("weight") or 1.0)
        for term in _title_mined_terms(payload, 80):
            if not _is_actor_name_term(term, actor_names):
                title_terms[term] += weight
    return {"title_traits": title_traits, "title_terms": _prune_title_term_counter(title_terms)}


async def _library_profile() -> dict[str, Any]:
    empty_profile = {
        "media_count": 0,
        "codes": set(),
        "media_by_code": {},
        "actors": Counter(),
        "genres": Counter(),
        "tags": Counter(),
        "studios": Counter(),
        "series": Counter(),
        "directors": Counter(),
        "title_traits": Counter(),
        "title_terms": Counter(),
        "actor_category": Counter(),
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
            "actors": Counter(),
            "genres": Counter(),
            "tags": Counter(),
            "studios": Counter(),
            "series": Counter(),
            "directors": Counter(),
            "title_traits": Counter(),
            "title_terms": Counter(),
            "actor_category": Counter(),
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
        for edge, target in rows.all():
            rel = edge.relation_type
            if rel == "HAS_CODE":
                code = _norm_code(target.label or target.key)
                if code:
                    profile["codes"].add(code)
                    profile["media_by_code"][code] = _entity_payload(media_by_id.get(edge.source_entity_id)) if media_by_id.get(edge.source_entity_id) else None
            elif rel == "HAS_ACTOR":
                profile["actors"][target.label] += media_weights.get(edge.source_entity_id, 1.0)
                relations_by_media[edge.source_entity_id]["actors"].add(target.label)
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
        actor_names = {str(name) for name in profile["actors"] if str(name or "").strip()}
        signature = _title_profile_signature(media, media_weights)
        title_profile = _load_title_profile_cache(signature)
        if title_profile is None:
            title_profile = _build_title_profile(media, media_weights, actor_names)
            with contextlib.suppress(Exception):
                _save_title_profile_cache(signature, title_profile["title_traits"], title_profile["title_terms"], len(media))
        profile["title_traits"] = title_profile["title_traits"]
        profile["title_terms"] = title_profile["title_terms"]
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
            _invalidate_recommendation_cache()
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
    _invalidate_recommendation_cache()
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
            if enabled and _pool_scan_due(_pool(), minutes):
                await _scan_candidate_pool(config)
        except asyncio.CancelledError:
            raise
        except Exception:
            minutes = 30
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_scheduler_stop.wait(), timeout=minutes * 60)


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
    global _scheduler_task, _scheduler_stop
    if _scheduler_stop:
        _scheduler_stop.set()
    if _scheduler_task:
        _scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _scheduler_task
    _scheduler_task = None
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
    actors = _unique_names(item.get("actors") or [], 12)
    base_categories = _unique_names(item.get("categories") or [], 16)
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
    feedback_score = 0.0
    actionability_score = 0.0
    quality_score = 0.0
    penalty_score = 0.0

    actor_counter: Counter = profile.get("actors") or Counter()
    genre_counter: Counter = profile.get("genres") or Counter()
    tag_counter: Counter = profile.get("tags") or Counter()
    studio_counter: Counter = profile.get("studios") or Counter()
    series_counter: Counter = profile.get("series") or Counter()
    director_counter: Counter = profile.get("directors") or Counter()
    title_trait_counter: Counter = profile.get("title_traits") or Counter()
    title_term_counter: Counter = profile.get("title_terms") or Counter()
    actor_category_counter: Counter = profile.get("actor_category") or Counter()
    media_count = max(int(profile.get("media_count") or 0), 1)

    actor_hits = [(name, actor_counter.get(name, 0)) for name in actors if actor_counter.get(name, 0) > 0]
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
    for actor in actors:
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

    category_hits = []
    for name in categories:
        count = _combined_category_count(name, genre_counter, tag_counter)
        if count > 0:
            category_hits.append((name, count, _generic_category_factor(name)))
    if category_hits:
        sorted_hits = sorted(category_hits, key=lambda x: x[1] * x[2], reverse=True)
        names = "/".join(name for name, _, factor in sorted_hits[:3] if factor >= 0.5)
        boost = min(24, sum(min(6.5, math.sqrt(count) * 2.9 + _preference_confidence(count, media_count) * 2) * factor for _, count, factor in sorted_hits[:6]))
        score += boost
        personalized_score += boost
        category_preference_score += boost
        if names:
            reasons.append(f"类型匹配：{names}")

    title_trait_hits = [(name, float(title_trait_counter.get(name) or 0)) for name in title_traits if float(title_trait_counter.get(name) or 0) > 0]
    if title_trait_hits:
        sorted_traits = sorted(title_trait_hits, key=lambda row: row[1], reverse=True)
        boost = min(16, sum(min(4.8, math.sqrt(count) * 2.1 + _preference_confidence(int(round(count)), media_count) * 1.6) for _, count in sorted_traits[:5]))
        score += boost
        personalized_score += boost
        category_preference_score += boost
        reasons.append("标题题材：" + "/".join(name for name, _ in sorted_traits[:3]))

    mined_term_hits = _profile_title_term_matches(item, title_term_counter, 8)
    if mined_term_hits:
        boost = min(14, sum(min(4.0, math.log2(float(hit.get("count") or 0) + 1) * 0.95) for hit in mined_term_hits[:5]))
        if boost > 0:
            score += boost
            personalized_score += boost
            category_preference_score += boost
            reasons.append("媒体库标题词：" + "/".join(str(hit.get("name") or "") for hit in mined_term_hits[:3] if hit.get("name")))

    feedback_category_boost = 0.0
    feedback_category_penalty = 0.0
    for category in categories:
        factor = _generic_category_factor(category)
        feedback_category_boost += min(6, liked_categories.get(category, 0) * 3 * factor)
        disliked_count = disliked_categories.get(category, 0)
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

    combo_hits = []
    for actor in actors:
        for category in categories:
            count = actor_category_counter.get((actor, category), 0)
            factor = _generic_category_factor(category)
            if count > 0 and factor >= 0.5:
                actor_count = max(actor_counter.get(actor, 0), 1)
                category_count = max(_combined_category_count(category, genre_counter, tag_counter), 1)
                lift = count / math.sqrt(actor_count * category_count)
                combo_hits.append((actor, category, count, factor, lift))
    if combo_hits:
        actor, category, count, factor, lift = max(combo_hits, key=lambda x: (x[2] * x[3], x[4]))
        boost = min(24, (5 if count <= 1 else 9) + math.log2(count + 1) * 5 * factor + min(5, lift * 7))
        score += boost
        personalized_score += boost
        relationship_preference_score += boost
        reasons.append(f"组合偏好：{actor} + {category} 出现 {count} 次")

    # If the candidate has no familiar actor but several strong preferred tags,
    # mark it as a controlled discovery rather than letting it look random.
    if not actor_hits and category_hits:
        strong_category_count = sum(1 for _, count, factor in category_hits if count >= 2 and factor >= 0.5)
        if strong_category_count >= 2:
            discovery_boost = min(8, strong_category_count * 2.5)
            score += discovery_boost
            personalized_score += discovery_boost
            category_preference_score += discovery_boost
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
    release = str(item.get("release_date") or item.get("date") or "")
    if release.startswith("2026"):
        score += 4
        quality_score += 4
        reasons.append("近期作品")
    elif release.startswith("2025"):
        score += 2
        quality_score += 2

    if profile.get("media_count", 0) < 5:
        score += min(12, float(item.get("score") or 0) * 2)
        if not reasons:
            reasons.append("媒体库样本较少，按资源可用性推荐")

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

    score = max(0, min(92, round(score)))
    if score <= 0:
        _record_filter(diagnostics, item, code, "score_too_low", "综合评分小于等于 0")
        return None
    match_bucket = _score_bucket(personalized_score)
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
        "personalized_score": round(personalized_score, 1),
        "actionability_score": round(actionability_score, 1),
        "quality_score": round(quality_score, 1),
        "penalty_score": round(penalty_score, 1),
        "match_level": match_bucket,
        "confidence": max(0, min(100, round(personalized_score * 1.6 + actionability_score * 0.45 - penalty_score * 0.7))),
        "score_breakdown": {
            "preference": round(personalized_score, 1),
            "actor_preference": round(actor_preference_score, 1),
            "category_preference": round(category_preference_score, 1),
            "relationship_preference": round(relationship_preference_score, 1),
            "feedback": round(feedback_score, 1),
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
                    {"keyword": code, "provider_timeout_seconds": 5},
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
        item["confidence"] = max(0, min(100, round(float(item.get("confidence") or 0) + score_boost * 0.45)))
        cap = 100 if float(item.get("personalized_score") or 0) >= 22 else 82
        item["score"] = max(0, min(cap, int(round(float(item.get("score") or 0) + score_boost))))
        item["reasons"] = list(dict.fromkeys(item.get("reasons") or []))[:6]

    tasks = [asyncio.create_task(enrich_one(item)) for item in targets]
    _done, pending = await asyncio.wait(tasks, timeout=budget_seconds)
    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        warnings.append(f"资源确认已达 {budget_seconds:g} 秒页面预算，剩余 {len(pending)} 项已跳过，不影响推荐展示")
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


def _diversify_recommendations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep strong recommendations but avoid a front page monopolized by one actor/tag."""
    remaining = list(items)
    selected: list[dict[str, Any]] = []
    actor_seen: Counter = Counter()
    category_seen: Counter = Counter()
    while remaining:
        best_index = 0
        best_value = -9999.0
        for index, item in enumerate(remaining):
            value = float(item.get("score") or 0)
            actors = [str(x) for x in item.get("actors") or []]
            categories = [str(x) for x in item.get("categories") or [] if _generic_category_factor(x) >= 0.5]
            actor_penalty = sum(actor_seen.get(actor, 0) for actor in actors[:3]) * 7
            category_penalty = sum(category_seen.get(category, 0) for category in categories[:3]) * 2
            adjusted = value - actor_penalty - category_penalty
            if adjusted > best_value:
                best_value = adjusted
                best_index = index
        picked = remaining.pop(best_index)
        picked["diversity_rank"] = len(selected) + 1
        selected.append(picked)
        for actor in (picked.get("actors") or [])[:3]:
            actor_seen[str(actor)] += 1
        for category in (picked.get("categories") or [])[:4]:
            if _generic_category_factor(category) >= 0.5:
                category_seen[str(category)] += 1
    return selected


def _apply_recommendation_controls(items: list[dict[str, Any]], config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Apply confidence filtering and a small deterministic exploration slot."""
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

    def exploration_key(item: dict[str, Any]) -> bytes:
        value = str(item.get("code") or item.get("title") or "")
        return hashlib.sha256(f"{seed}:{value}".encode("utf-8")).digest()

    top = items[:limit]
    pool = sorted(items[limit:], key=exploration_key)
    picks = pool[:exploration_count]
    step = max(1, round(limit / (len(picks) + 1)))
    position = step
    for pick in picks:
        if position < len(top):
            top[position] = pick
        else:
            top.append(pick)
        position += step
    return top[:limit]


async def _recommendations(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    source_mode = str(payload.get("source_mode") or "latest").strip().lower()
    if source_mode not in {"latest", "full"}:
        source_mode = "latest"
    store = _ensure_store()
    feedback = {
        "ignored_codes": _feedback_codes(store.get("ignored")),
        "liked_codes": _feedback_codes(store.get("liked")),
        "disliked_codes": _feedback_codes(store.get("disliked")),
        "liked_actors": _feedback_counter(store.get("liked"), "actors"),
        "liked_categories": _feedback_counter(store.get("liked"), "categories"),
        "disliked_actors": _feedback_counter(store.get("disliked"), "actors"),
        "disliked_categories": _feedback_counter(store.get("disliked"), "categories"),
    }
    profile = await _library_profile()
    live_codes, live_warning = await _live_library_codes(config, force=bool(payload.get("refresh")))
    requested_limit = max(1, min(int(payload.get("limit") or 48), 100))
    cache_key = json.dumps({
        "config": config,
        "ignored": sorted(feedback["ignored_codes"]),
        "liked": sorted(feedback["liked_codes"]),
        "disliked": sorted(feedback["disliked_codes"]),
        "liked_actors": dict(feedback["liked_actors"]),
        "liked_categories": dict(feedback["liked_categories"]),
        "disliked_actors": dict(feedback["disliked_actors"]),
        "disliked_categories": dict(feedback["disliked_categories"]),
        "library_codes": sorted(live_codes),
        "library_code_count": len(profile.get("codes") or []),
        "library_code_fingerprint": _code_fingerprint(profile.get("codes") or set()),
        "source_mode": source_mode,
        "requested_limit": requested_limit,
    }, sort_keys=True, ensure_ascii=False)
    cache_ttl = int(_config_number(config, "recommendation_cache_minutes", 30, 5, 1440) * 60)
    if not payload.get("refresh"):
        cached = _recommendation_cache_get(cache_key, cache_ttl)
        if cached is not None:
            return cached

    pool = _pool()
    if source_mode == "full" and _backfill_candidate_pool_title_profiles(pool):
        _save_pool(pool)
    pool_items = pool.get("items") if isinstance(pool.get("items"), dict) else {}
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
    scored = []
    filtered_diagnostics: list[dict[str, Any]] = []
    for item in candidates:
        rec = _candidate_score(item, profile, config, feedback, filtered_diagnostics)
        if rec:
            scored.append(rec)
    scored = _dedupe_recommendations(scored)
    scored.sort(key=lambda x: (x["score"], x.get("magnets_count") or 0, x.get("release_date") or ""), reverse=True)
    # A small first pass lets resource actionability influence ranking without
    # making the initial recommendation request excessively expensive.
    initial_resource_config = {**config, "resource_enrich_limit": 16, "resource_enrich_budget_seconds": 4}
    resource_warnings = await _enrich_recommendation_resources(initial_resource_config, scored)
    if resource_warnings:
        warnings.extend(resource_warnings)
    scored.sort(key=lambda x: (x["score"], (x.get("resource_summary") or {}).get("total") or 0, x.get("magnets_count") or 0, x.get("release_date") or ""), reverse=True)
    scored = _diversify_recommendations(scored)
    scored = _apply_recommendation_controls(scored, config, requested_limit)
    # Diversification can promote candidates that were outside the first-pass
    # window. Confirm the cards that will actually be displayed as a second
    # pass, skipping rows already enriched above.
    display_resource_warnings = await _enrich_recommendation_resources(config, scored)
    if display_resource_warnings:
        warnings.extend(display_resource_warnings)
    result = {
        "ok": True,
        "generated_at": _now_ms(),
        "source_mode": source_mode,
        "source_label": "完整推荐" if source_mode == "full" else "最新推荐",
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
        },
        "stats": {
            "candidates": len(candidates),
            "candidate_pool_total": _candidate_pool_stats(pool)["total"],
            "candidate_pool_today": _candidate_pool_stats(pool)["today_increment"],
            "today_increment": sum(1 for item in candidates if item.get("is_today_increment")),
            "ignored": len([x for x in feedback["ignored_codes"] if x]),
            "disliked": len([x for x in feedback["disliked_codes"] if x]),
        },
        "candidate_meta": {"pool": _candidate_pool_stats(pool)},
        "filtered": _filtered_summary(filtered_diagnostics),
        "warnings": warnings,
    }
    _recommendation_cache_put(cache_key, result)
    return result


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
        return {"ok": True, "pool": _candidate_pool_stats(_pool())}
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
        }
        data[key] = [x for x in data.get(key, []) if _norm_code(x.get("code") if isinstance(x, dict) else x) != code]
        data[key].insert(0, row)
        _save_store(data)
        _invalidate_recommendation_cache()
        return {"ok": True, "code": code, "kind": kind}
    if action == "reset_feedback":
        data = _ensure_store()
        data["ignored"] = []
        data["liked"] = []
        data["disliked"] = []
        _save_store(data)
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
    }]
