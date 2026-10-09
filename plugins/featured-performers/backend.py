"""精选女优：通过 CloudDrive2 管理远端媒体目录中的 Emby 旁车文件。

目录读取、文件 ID、上传、复制、改名和删除都通过插件内置的
CD2Storage 完成；本文件只保存精选女优的绑定关系和 NFO/图片业务规则。
本插件不创建 STRM，也不依赖本地挂载目录。
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import html as html_lib
import io
import json
import logging
import math
import os
import posixpath
import re
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from PIL import Image, ImageOps

from app.core.config import get_settings
from app.core.runtime_paths import plugin_data_path
from app.plugins.secrets import plugin_secret_store
from app.plugins.contracts import PluginTestResult
from .services.cd2 import CD2Client
from .services import directory_changes

PLUGIN_ID = "featured-performers"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".flv", ".webm"}
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_NFO_BYTES = 4 * 1024 * 1024
REMOTE_CACHE_SECONDS = 300.0
REMOTE_STALE_SECONDS = 1800.0
DEFAULT_CHECK_MINUTES = 0
XCHINA_MAX_PAGES = 100
XCHINA_MAX_PAGE_BYTES = 8 * 1024 * 1024
XCHINA_MAX_TOTAL_BYTES = 80 * 1024 * 1024
XCHINA_PAGE_CONCURRENCY = 6
XCHINA_PAGE_TIMEOUT_SECONDS = 12.0
DEFAULT_XCHINA_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
XCHINA_HOST_SUFFIX = ".xchina.co"
TANGXIN_HOST_SUFFIX = ".tangxinweb.com"
TANGXIN_MAX_PAGES = 20
MADOUQU_HOST_SUFFIX = ".madouqu.com"
MADOUQU_MAX_PAGES = 100
_state_lock = threading.RLock()
_mutation_lock = asyncio.Lock()
_folder_cache: dict[str, dict[str, Any]] = {}
_remote_bytes_cache: dict[str, dict[str, Any]] = {}
_cleanup_jobs: dict[str, dict[str, Any]] = {}
_cleanup_workers: dict[str, asyncio.Task] = {}
_layout_jobs: dict[str, dict[str, Any]] = {}
_layout_workers: dict[str, asyncio.Task] = {}
_library_index_lock = asyncio.Lock()
_artwork_locks: dict[str, asyncio.Lock] = {}
_auto_match_lock = threading.RLock()
_auto_match_worker: asyncio.Task | None = None
_auto_discovery_worker: asyncio.Task | None = None
_cd2_event_worker: asyncio.Task | None = None
_cd2_cache_worker: asyncio.Task | None = None
_auto_match_wakeup: asyncio.Event | None = None
_auto_match_stopping = False

AUTO_MATCH_RETRY_SECONDS = (300, 1800, 7200)
log = logging.getLogger("noor.plugin.featured_performers")


def _directory_changes():
    return directory_changes


def _cache_consumer(author):
    return f"featured-performers:{author['id']}"


def _subscribe_cache(author, index, state):
    service = _directory_changes()
    if service:
        polling = _watch_folder_ids(author, index)
        queued = {str(item.get("id")): item for item in index.get("queue", []) if item.get("id")}
        cached_folders = {**index.get("fallback_folders", {}), **index.get("folders", {})}
        all_folders = {
            str(author.get("root_folder_id") or ""),
            *map(str, cached_folders),
            *queued,
        }
        folder_paths = {
            str(author.get("root_folder_id") or ""): str(author.get("remote_path") or ""),
            **{str(folder_id): str(node.get("path") or "") for folder_id, node in cached_folders.items()},
            **{folder_id: str(node.get("path") or "") for folder_id, node in queued.items()},
        }
        service.subscribe(
            _cache_consumer(author),
            polling,
            state.get("library_settings", {}).get("check_minutes", DEFAULT_CHECK_MINUTES),
            event_only_folder_ids=all_folders - polling,
            folder_paths=folder_paths,
        )
    return service


def _watch_folder_ids(author, index):
    """Watch only author and season/year boundaries, not every work folder."""
    root = str(author.get("root_folder_id") or "")
    folders = {root} if root and root != "0" else set()
    folders.update(str(year.get("folder_id")) for year in author.get("years", []) if year.get("folder_id"))
    for folder_id, node in index.get("folders", {}).items():
        branch = node.get("branch") or []
        if len(branch) == 1 and _year_number(str(branch[0].get("name") or "")):
            folders.add(str(folder_id))
    return folders


def _install_index_folder(index, current, items):
    """Refresh one directory including deleted/moved/renamed child branches."""
    depth = len(current["branch"])
    children = {str(r["file_id"]): r["name"] for r in items if r.get("is_directory") and not _text(r.get("name")).startswith(".")}
    prefix = [r["id"] for r in current["branch"]]
    def update_branch(node):
        branch = node["branch"]
        if len(branch) <= depth or [r["id"] for r in branch[:depth]] != prefix:
            return True
        fid = branch[depth]["id"]
        if fid not in children:
            return False
        branch[depth] = {"id": fid, "name": children[fid]}
        node["path"] = _remote_path(current["path"], "/".join(r["name"] for r in branch[depth:]))
        return True
    index["folders"] = {fid: node for fid, node in index["folders"].items() if update_branch(node)}
    index["queue"] = [q for q in index["queue"] if q["id"] != current["id"] and update_branch(q)]
    index["folders"][current["id"]] = {"path": current["path"], "branch": current["branch"], "items": items}
    known = set(index["folders"]) | {q["id"] for q in index["queue"]}
    for fid, name in children.items():
        if fid in known:
            continue
        if depth >= 16:
            raise ValueError("目录层级超过 16 层，请绑定更具体的作者目录")
        index["queue"].append({"id": fid, "path": _remote_path(current["path"], name), "branch": [*current["branch"], {"id": fid, "name": name}], "offset": 0, "items": []})


def _library_min_mb(state):
    return state.get("library_settings", {}).get("min_video_mb", 100)


def _library_check_minutes(state):
    return state.get("library_settings", {}).get("check_minutes", DEFAULT_CHECK_MINUTES)


def _library_index_path(author):
    # Opaque stable author identity, never a user-supplied filesystem path.
    return plugin_data_path(PLUGIN_ID, "library-" + hashlib.sha256(author["id"].encode()).hexdigest() + ".json")


def _read_library_index(author):
    path = _library_index_path(author)
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("root_id") == str(author.get("root_folder_id")):
                return value
        except (ValueError, OSError):
            pass  # A disposable index is rebuilt, never the binding/NFO state.
    return None


def _write_library_index(author, index):
    path = _library_index_path(author)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _auto_match_path():
    path = plugin_data_path(PLUGIN_ID, "auto-match.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _empty_auto_match_state() -> dict[str, Any]:
    return {"version": 1, "updated_at": _now(), "items": {}}


def _load_auto_match_state() -> dict[str, Any]:
    with _auto_match_lock:
        path = _auto_match_path()
        if not path.exists():
            return _empty_auto_match_state()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("items", {}), dict):
                raise ValueError("根节点格式无效")
        except Exception as exc:
            raise ValueError(f"自动匹配任务文件无法读取：{exc}") from exc
        value.setdefault("version", 1)
        value.setdefault("items", {})
        return value


def _save_auto_match_state(value: dict[str, Any]) -> None:
    with _auto_match_lock:
        value["updated_at"] = _now()
        _write_cache_json(_auto_match_path(), value)


def _auto_match_key(author_id: str, file_id: str) -> str:
    return f"{author_id}:{file_id}"


def _auto_match_summary(author_id: str | None = None) -> dict[str, Any]:
    items = list(_load_auto_match_state().get("items", {}).values())
    if author_id:
        items = [item for item in items if item.get("author_id") == author_id]
    counts = {name: 0 for name in ("queued", "matching", "writing", "completed", "manual", "failed", "stale")}
    for item in items:
        status = str(item.get("status") or "queued")
        counts[status] = counts.get(status, 0) + 1
    active = counts["queued"] + counts["matching"] + counts["writing"]
    return {"counts": counts, "active": active, "total": len(items), "running": bool(_auto_match_worker and not _auto_match_worker.done())}


def _wake_auto_match_worker() -> None:
    if _auto_match_wakeup:
        _auto_match_wakeup.set()


def _discover_auto_match_jobs(author: dict[str, Any], works: list[dict[str, Any]]) -> int:
    """Persist missing-NFO candidates after a complete cached library scan."""
    queue = _load_auto_match_state()
    items = queue.setdefault("items", {})
    source_version = _text((author.get("external_source") or {}).get("fetched_at"))
    seen: set[str] = set()
    added = 0
    for work in works:
        file_id = _text(work.get("file_id"))
        if not file_id:
            continue
        key = _auto_match_key(author["id"], file_id)
        seen.add(key)
        current = items.get(key)
        branch = work.get("branch") or []
        already_in_season = bool(branch and re.fullmatch(r"Season\s+\d+", _text(branch[-1].get("name")), re.I))
        if work.get("nfo_exists") and already_in_season:
            if current and current.get("status") not in {"completed", "stale"}:
                current.update(status="completed", result="NFO 已存在", error="", updated_at=_now())
            continue
        payload = {
            "author_id": author["id"], "file_id": file_id,
            "parent_id": _text(work.get("parent_id")), "name": _text(work.get("name")),
            "path": _text(work.get("path")), "branch": branch,
            "nfo_exists": bool(work.get("nfo_exists")),
            "source_version": source_version,
        }
        if current is None:
            items[key] = {**payload, "status": "queued", "attempts": 0, "error": "", "created_at": _now(), "updated_at": _now(), "next_attempt_at": 0}
            added += 1
        else:
            previous_source_version = current.get("source_version")
            current.update(payload, updated_at=_now())
            if current.get("status") in {"manual", "failed"} and previous_source_version != source_version:
                current.update(status="queued", attempts=0, error="", next_attempt_at=0)
                added += 1
    for key, item in items.items():
        if item.get("author_id") == author["id"] and key not in seen and item.get("status") in {"queued", "matching", "writing", "failed"}:
            item.update(status="stale", error="作品已不在当前索引中", updated_at=_now())
    _save_auto_match_state(queue)
    if added:
        _wake_auto_match_worker()
    return added


def _update_auto_match_job(author_id: str, file_id: str, **changes: Any) -> dict[str, Any] | None:
    queue = _load_auto_match_state()
    item = queue.get("items", {}).get(_auto_match_key(author_id, file_id))
    if not item:
        return None
    item.update(changes, updated_at=_now())
    _save_auto_match_state(queue)
    return item


def _discover_from_complete_indexes() -> int:
    state = _load_state()
    added = 0
    for author in state.get("authors", []):
        index = _read_library_index(author)
        if not index or index.get("queue") or index.get("root_id") != str(author.get("root_folder_id")):
            continue
        bound = [work for year in author.get("years", []) for work in year.get("works", [])]
        works: dict[str, dict[str, Any]] = {}
        for parent, node in index.get("folders", {}).items():
            context = {"folder_id": parent, "remote_path": node["path"], "works": bound, "author": author}
            for work in _candidates_from_items(node.get("items") or [], context, min_bytes=_library_min_mb(state) * 1024 * 1024):
                work["branch"] = node.get("branch") or []
                works[str(work["file_id"])] = work
        added += _discover_auto_match_jobs(author, list(works.values()))
    return added


async def _invalidate_library_parent(author, parent):
    async with _library_index_lock:
        index = _read_library_index(author)
        if index and str(parent) in index["folders"]:
            node = index["folders"][str(parent)]
            index["queue"] = [q for q in index["queue"] if q["id"] != str(parent)]
            index["queue"].insert(0, {"id": str(parent), "path": node["path"], "branch": node["branch"], "offset": 0, "items": [], "force": True})
            _write_library_index(author, index)


async def _forget_library_files(author: dict[str, Any], parent_id: str, file_ids: set[str]) -> None:
    """Remove confirmed remote deletions from the persistent index immediately."""
    async with _library_index_lock:
        index = _read_library_index(author)
        if not index:
            return
        for collection in (index.get("folders", {}), index.get("fallback_folders", {})):
            node = collection.get(str(parent_id))
            if node:
                node["items"] = [
                    item for item in node.get("items", [])
                    if str(item.get("file_id")) not in file_ids
                ]
        for queued in index.get("queue", []):
            if str(queued.get("id")) == str(parent_id):
                queued["items"] = [
                    item for item in queued.get("items", [])
                    if str(item.get("file_id")) not in file_ids
                ]
        index["updated_at"] = _now()
        _write_library_index(author, index)
        cached = _folder_cache.get(str(parent_id))
        if cached:
            cached["items"] = [
                item for item in cached.get("items", [])
                if str(item.get("file_id")) not in file_ids
            ]


async def _library_index(client, author, state, payload):
    """One cached directory/page at a time; never download media or bind it."""
    async with _library_index_lock:
        root = str(author.get("root_folder_id") or "")
        if not root or root == "0":
            raise ValueError("请先绑定具体作者目录")
        previous = _read_library_index(author)
        index = None if payload.get("force") else previous
        if index is None:
            index = {"root_id": root, "folders": {}, "queue": [{"id": root, "path": author.get("remote_path") or "/", "branch": [], "offset": 0, "items": []}], "force": bool(payload.get("force")), "updated_at": ""}
            if previous:
                index["fallback_folders"] = previous.get("fallback_folders") or previous["folders"]
        service = _subscribe_cache(author, index, state)
        if service and (not index.get("force") or payload.get("cache_only")):
            refreshed = []
            for parent in list(index["folders"]):
                if parent not in index["folders"]:
                    continue
                node = index["folders"][parent]
                service.observe(parent, node["items"], only_if_missing=True)
                remote = service.snapshot(parent)
                if not remote or remote["dirty"] or remote["revision"] == node.get("revision") or remote["items"] is None:
                    continue
                refreshed.append(({"id": parent, **node}, remote))
            for queued in list(index["queue"]):
                remote = service.snapshot(queued["id"])
                if remote and not remote["dirty"] and remote["items"] is not None:
                    refreshed.append((queued, remote))
            added = set()
            for current, remote in refreshed:
                before = {str(item.get("id")) for item in index["queue"]}
                _install_index_folder(index, current, remote["items"])
                index["folders"][current["id"]]["revision"] = remote["revision"]
                _folder_cache[current["id"]] = {"at": time.monotonic(), "items": remote["items"]}
                added.update(str(item.get("id")) for item in index["queue"] if str(item.get("id")) not in before)
                index["updated_at"] = _now()
            if refreshed:
                _subscribe_cache(author, index, state)
            missing = [folder_id for folder_id in added if not (service.snapshot(folder_id) or {}).get("items")]
            if missing:
                service.invalidate(missing)
        cooling_down = bool(service and service.cooldown_remaining() > 0 and not payload.get("force"))
        if index["queue"] and not payload.get("cache_only") and not cooling_down:
            current = index["queue"][0]
            cached = _folder_cache.get(current["id"])
            if current["offset"] == 0 and not (index["force"] or current.get("force")) and cached and time.monotonic() - cached["at"] < REMOTE_CACHE_SECONDS:
                rows = cached["items"]
                count = len(rows)
            else:
                page = await client.list_folder(current["id"], offset=current["offset"], limit=500)
                rows = page.get("items") or []
                count = int(page.get("count") or current["offset"] + len(rows))
            # Index only metadata; no expiring links, cookies or file contents.
            keys = ("file_id", "parent_id", "name", "is_directory", "size", "sha1", "updated_at", "pick_code", "full_path")
            current["items"].extend({k: row[k] for k in keys if k in row} for row in rows)
            current["offset"] += len(rows)
            if current["offset"] > 200000 or len(index["folders"]) + len(index["queue"]) > 10000:
                raise ValueError("作者目录条目过多，请绑定更具体的作者目录")
            if not rows or current["offset"] >= count:
                _install_index_folder(index, current, current["items"])
                _folder_cache[current["id"]] = {"at": time.monotonic(), "items": current["items"]}
                if service:
                    _subscribe_cache(author, index, state)
                    service.observe(current["id"], current["items"])
                    index["folders"][current["id"]]["revision"] = service.snapshot(current["id"])["revision"]
            index["updated_at"] = _now()
        if not index["queue"]:
            index.pop("fallback_folders", None)
            index["force"] = False
        _write_library_index(author, index)
        bound = [w for y in author.get("years", []) for w in y.get("works", [])]
        works = {}
        for parent, node in {**index.get("fallback_folders", {}), **index["folders"]}.items():
            context = {"folder_id": parent, "remote_path": node["path"], "works": bound, "author": author}
            for work in _candidates_from_items(node["items"], context, min_bytes=_library_min_mb(state) * 1024 * 1024):
                work["branch"] = node["branch"]
                works[str(work["file_id"])] = work
        result_works = list(works.values())
        if not index["queue"]:
            _discover_auto_match_jobs(author, result_works)
        return {"works": result_works, "complete": not index["queue"], "scanned": len(index["folders"]), "pending": len(index["queue"]), "updated_at": index["updated_at"], "min_video_mb": _library_min_mb(state), "auto_match": _auto_match_summary(author["id"])}


def _application_proxy() -> str | None:
    """Use NOOR Settings -> 网络 -> HTTP 代理 for external HTTP only.

    The long-running service may also receive the same proxy through its
    systemd environment.  Keep that as a compatibility fallback when the
    persisted settings field is empty, otherwise plugins would silently fall
    back to a direct connection and time out.
    """
    try:
        value = _text(get_settings().http_proxy)
    except Exception:
        value = ""
    if not value:
        value = _text(os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    return value or None


def _xchina_cookie(value: Any) -> str:
    cookie = _text(value)
    if "\r" in cookie or "\n" in cookie:
        raise ValueError("xChina Cookie 不能包含换行")
    if len(cookie) > 16 * 1024:
        raise ValueError("xChina Cookie 过长")
    return cookie


def _xchina_user_agent(value: Any) -> str:
    user_agent = _text(value)
    if not user_agent:
        return DEFAULT_XCHINA_USER_AGENT
    if "\r" in user_agent or "\n" in user_agent:
        raise ValueError("xChina User-Agent 不能包含换行")
    return user_agent[:512]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _state_path():
    path = plugin_data_path(PLUGIN_ID, "state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _empty_state() -> dict[str, Any]:
    return {"version": 2, "authors": [], "updated_at": _now()}


def _load_state() -> dict[str, Any]:
    with _state_lock:
        path = _state_path()
        if not path.exists():
            return _empty_state()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"精选女优状态文件无法读取：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("精选女优状态文件不是对象")
        value.setdefault("authors", [])
        value["version"] = max(2, int(value.get("version") or 1))
        return value


def _save_state(state: dict[str, Any]) -> None:
    with _state_lock:
        path = _state_path()
        state["updated_at"] = _now()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def _find_author(state: dict[str, Any], author_id: str) -> dict[str, Any]:
    author = next((item for item in state.get("authors", []) if item.get("id") == author_id), None)
    if not author:
        raise ValueError("作者不存在")
    author.setdefault("years", [])
    return author


def _find_year(author: dict[str, Any], year_id: str) -> dict[str, Any]:
    year = next((item for item in author.get("years", []) if item.get("id") == year_id), None)
    if not year:
        raise ValueError("年份不存在")
    year.setdefault("works", [])
    return year


def _find_work(author: dict[str, Any], work_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for year in author.get("years", []):
        for work in year.get("works", []):
            if work.get("id") == work_id:
                return year, work
    raise ValueError("作品不存在")


def _remote_path(parent: str, name: str) -> str:
    parent = _text(parent) or "/"
    if not parent.startswith("/"):
        parent = f"/{parent}"
    return posixpath.join(parent, _text(name))


def _file_ext(item_or_name: dict[str, Any] | str) -> str:
    name = item_or_name if isinstance(item_or_name, str) else item_or_name.get("name") or ""
    return PurePosixPath(str(name)).suffix.lower()


def _stem(name: str) -> str:
    return PurePosixPath(str(name)).stem


def _is_video(item: dict[str, Any]) -> bool:
    return not bool(item.get("is_directory")) and _file_ext(item) in VIDEO_EXTENSIONS


def _year_number(name: str) -> int | None:
    match = re.fullmatch(r"(?:Season\s*)?(19\d{2}|20\d{2})", _text(name), re.I)
    return int(match.group(1)) if match else None


def _item_version(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    return ":".join(str(item.get(key) or "") for key in ("sha1", "size", "updated_at", "file_id"))


_XCHINA_MODEL_RE = re.compile(r"/videos/model-([a-z0-9]+)(?:/|\.html|$)", re.I)
_XCHINA_PAGE_RE = re.compile(r"/videos/model-[^/]+(?:/sort-[^/]+)?/(\d+)\.html$", re.I)
_XCHINA_VIDEO_RE = re.compile(r"/(?:video|videos?)[/-](?!model-)[^/?#]+", re.I)
_XCHINA_CODE_RE = re.compile(r"\b([A-Z]{2,12})\s*[-_ ]\s*(\d{2,6})\b", re.I)
_TANGXIN_ARTIST_RE = re.compile(r"/artists/series/[^/?#]+", re.I)
_TANGXIN_VIDEO_RE = re.compile(r"/videoDetail/(\d+)", re.I)
_MADOUQU_TAG_RE = re.compile(r"/video/tag/[^/?#]+(?:/page/\d+)?/?$", re.I)
_MADOUQU_PAGE_RE = re.compile(r"/video/tag/[^/?#]+/page/(\d+)/?$", re.I)
_MADOUQU_VIDEO_RE = re.compile(r"/video/[^/?#]+/?$", re.I)
_BRACKET_RE = re.compile(r"[\[【〔（(]([^\]】〕）)]+)[\]】〕）)]")


class _XChinaHTMLParser(HTMLParser):
    """Small dependency-free parser for xChina's card and pagination links.

    xChina has changed its card markup a few times.  We intentionally collect
    links, text, image lazy-loading attributes and title attributes instead of
    relying on one CSS class.  This also keeps the plugin usable on a minimal
    NOOR installation without adding BeautifulSoup as a runtime dependency.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = self._attrs(attrs)
        if tag == "a" and values.get("href"):
            self._stack.append({
                "href": values["href"],
                "title": values.get("title") or values.get("aria-label") or "",
                "text": [],
                "image": "",
            })
        elif self._stack and tag in {"img", "source"}:
            current = self._stack[-1]
            for key in ("data-src", "data-original", "data-lazy-src", "lazy-src", "src", "srcset"):
                if values.get(key):
                    current["image"] = values[key].split(",", 1)[0].strip().split(" ", 1)[0] if key == "srcset" else values[key].strip()
                    break
            if not current.get("title"):
                current["title"] = values.get("alt") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._stack:
            self.anchors.append(self._stack.pop())

    def handle_data(self, data: str) -> None:
        if self._stack and data.strip():
            self._stack[-1]["text"].append(data.strip())


def _canonical_url(value: str, *, base: str = "") -> str:
    url = html_lib.unescape(_text(value))
    if base:
        from urllib.parse import urljoin

        url = urljoin(base, url)
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def _validate_xchina_url(value: str) -> str:
    from urllib.parse import urlsplit

    url = _canonical_url(value)
    host = urlsplit(url).hostname or ""
    if not url or not (host == "xchina.co" or host.endswith(XCHINA_HOST_SUFFIX)):
        raise ValueError("来源页面必须是 xchina.co 站点的 http(s) 地址")
    if not _XCHINA_MODEL_RE.search(url):
        raise ValueError("来源页面应为 xChina 的 model 视频列表地址")
    return url


def _validate_madouqu_url(value: str) -> str:
    url = _canonical_url(value)
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    if not url or not (host == "madouqu.com" or host.endswith(MADOUQU_HOST_SUFFIX)):
        raise ValueError("来源页面必须是 madouqu.com 的 http(s) 标签页地址")
    if not _MADOUQU_TAG_RE.search(url):
        raise ValueError("来源页面应为麻豆区具体标签页，例如 /video/tag/nana/")
    return url


def _validate_source_url(value: str) -> tuple[str, str]:
    return "madouqu_tag", _validate_madouqu_url(value)


def _source_title_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", _text(value)).casefold()
    return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", normalized)


def _author_aliases(author: dict[str, Any] | None = None) -> list[str]:
    values = ["OnlyFans", "Only Fans"]
    if author and any(value in (_text(author.get("name")) + _text(author.get("aliases"))).casefold() for value in ("hongkongdoll", "hong kong doll", "玩偶姐姐")):
        values.extend(["HongKongdoll", "Hong Kong Doll", "玩偶姐姐"])
    if author:
        values.extend(re.split(r"[,，、;；/|\n]+", _text(author.get("aliases"))))
        values.append(_text(author.get("name")))
        if re.search(r"台[北灣湾].*娜娜|nana", " ".join(values), re.I):
            values.extend(["台北娜娜", "臺北娜娜", "娜娜", "Nana Taipei", "Nana_taipei", "娜娜Nana_taipei", "Nana_taipei娜娜"])
    result: list[str] = []
    for value in values:
        value = _text(value)
        if value and value.casefold() not in {item.casefold() for item in result}:
            result.append(value)
    return sorted(result, key=len, reverse=True)


_TITLE_TAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AI增强", re.compile(r"(?:AI\s*(?:增强|enhance(?:d)?|修复)|人工智能增强)", re.I)),
    ("4K60帧", re.compile(r"4K\s*60\s*(?:帧(?:率)?|fps)(?:增强版|修复版|版)?", re.I)),
    ("4K", re.compile(r"(?<![a-z0-9])4K(?:高清|增强版|修复版|版)?(?![a-z0-9])", re.I)),
    ("8K", re.compile(r"(?<![a-z0-9])8K(?:高清|增强版|修复版|版)?(?![a-z0-9])", re.I)),
    ("1080P", re.compile(r"(?<![a-z0-9])1080p(?![a-z0-9])", re.I)),
    ("60帧", re.compile(r"(?<![a-z0-9])60\s*(?:帧(?:率)?|fps)(?:增强版|修复版|版)?", re.I)),
    ("120帧", re.compile(r"(?<![a-z0-9])120\s*(?:帧(?:率)?|fps)(?:增强版|修复版|版)?", re.I)),
    ("HDR", re.compile(r"\bHDR(?:10\+?)?\b", re.I)),
    ("中字", re.compile(r"(?:中文字幕|中字)", re.I)),
)


def _strip_title_prefix(value: str) -> str:
    # Strip distributor URLs only at a title boundary, and dates only as a
    # leading release stamp. Never infer a release date or strip episode digits.
    previous = None
    while previous != value:
        previous = value
        value = value.lstrip(" \t~·•|/_-,:：")
        value = re.sub(r"^(?:(?:https?://)?(?:[a-z0-9-]+\.)+(?:com|net|org|cc|co|tv|xyz|top|vip|cn)(?:/[^\s@]*)?\s*[@|_\-:：]*\s*)+", "", value, flags=re.I)
        value = re.sub(r"^(?:S\d{1,4}E\d{1,5})\s*[-_ .]+", "", value, flags=re.I)
        value = re.sub(r"^(?:19|20)\d{2}[.年/_-](?:0?[1-9]|1[0-2])[.月/_-](?:0?[1-9]|[12]\d|3[01])日?(?=[\s_\-~·]|$)[\s_\-~·]*", "", value)
        value = re.sub(r"^2\d[./_-](?:0?[1-9]|1[0-2])[./_-](?:0?[1-9]|[12]\d|3[01])(?=[\s_\-~·]|$)[\s_\-~·]*", "", value)
        compact_date = re.match(r"^((?:19|20)\d{6})(?![a-z0-9])", value, re.I)
        if compact_date:
            try:
                datetime.strptime(compact_date[1], "%Y%m%d")
                value = value[8:]
            except ValueError:
                pass
    return value


def parse_external_title(value: str, author: dict[str, Any] | None = None) -> dict[str, Any]:
    """Split a source title into the usable title and non-title quality tags."""
    original = unicodedata.normalize("NFKC", _text(value))
    if _file_ext(original) in VIDEO_EXTENSIONS:
        original = _stem(original)
    original = _strip_title_prefix(original)
    tags: list[str] = []

    def add_tag(tag: str) -> None:
        if tag not in tags:
            tags.append(tag)

    # Bracketed quality markers are metadata.  Unknown bracketed text is kept,
    # because it may be a meaningful part of a work title.
    for match in list(_BRACKET_RE.finditer(original)):
        inside = match.group(1)
        recognized = False
        for tag, pattern in _TITLE_TAG_PATTERNS:
            if pattern.search(inside):
                add_tag(tag)
                recognized = True
        if recognized:
            remainder = inside
            for _, pattern in _TITLE_TAG_PATTERNS:
                remainder = pattern.sub(" ", remainder)
            original = original.replace(match.group(0), remainder.strip() or " ")

    # Inline quality markers, including the common "4K60帧增强版" suffix.
    for tag, pattern in _TITLE_TAG_PATTERNS:
        if pattern.search(original):
            add_tag(tag)
            original = pattern.sub(" ", original)

    if "AI增强" in tags:
        # Known processing suffixes observed in the existing library, not an
        # arbitrary trailing word or episode code.
        original = re.sub(r"(?:_(?:chf|apo|iris)\d+)+\s*$", "", original, flags=re.I)
    original = _strip_title_prefix(original)
    original = re.sub(r"^(?:糖心\s*vlog|麻豆传媒)[\s_\-]*", "", original, flags=re.I)
    for alias in _author_aliases(author):
        # Short names can be part of a meaningful story title. Remove them only
        # as an explicit prefix/suffix; long brand names are common inline noise.
        original = re.sub(r"^\s*[【\[(（]\s*" + re.escape(alias) + r"\s*[】\])）]\s*", "", original, flags=re.I)
        if len(alias) <= 4 and alias != "玩偶姐姐":
            original = re.sub(r"^\s*" + re.escape(alias) + r"[\s~·•|/_\-,:：]+|[\s~·•|/_\-,:：]+" + re.escape(alias) + r"\s*$", " ", original, flags=re.I)
        else:
            original = re.sub(re.escape(alias), " ", original, flags=re.I)
        original = _strip_title_prefix(original)

    # These are source/platform words, not the work title.  Keep this list
    # deliberately narrow so manual titles are not over-cleaned.
    original = re.sub(r"\b(?:only\s*fans|of|exclusive)\b", " ", original, flags=re.I)
    original = re.sub(r"(?:增强版|修复版|高清版|完整版)$", " ", original, flags=re.I)
    original = re.sub(r"^[\s~·•|/_\-,:：]+|[\s~·•|/_\-,:：]+$", "", original)
    original = re.sub(r"\s+", " ", original).strip()
    if not original:
        original = unicodedata.normalize("NFKC", _text(value))
    return {"raw_title": _text(value), "title": original, "tags": tags}


def _filename_metadata(value: str) -> dict[str, Any]:
    """Extract only explicit release metadata; never use filesystem dates."""
    normalized = unicodedata.normalize("NFKC", _stem(_text(value)))
    premiered = ""
    for pattern in (
        r"(?<!\d)((?:19|20)\d{2})[.年/_-](0?[1-9]|1[0-2])[.月/_-](0?[1-9]|[12]\d|3[01])日?(?!\d)",
        r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)",
        r"(?<!\d)(2\d)[./_-](0?[1-9]|1[0-2])[./_-](0?[1-9]|[12]\d|3[01])(?!\d)",
    ):
        match = re.search(pattern, normalized)
        if not match:
            continue
        try:
            year_value = int(match[1]) + (2000 if len(match[1]) == 2 else 0)
            premiered = datetime(year_value, int(match[2]), int(match[3])).strftime("%Y-%m-%d")
            break
        except ValueError:
            continue
    studio = ""
    studio_patterns = (
        ("麻豆传媒", r"麻豆(?:传媒)?"),
        ("糖心", r"糖心(?:Vlog)?"),
        ("果冻传媒", r"果冻传媒"),
        ("精东影业", r"精东影业"),
        ("天美传媒", r"天美传媒"),
    )
    for label, pattern in studio_patterns:
        if re.search(pattern, normalized, re.I):
            studio = label
            break
    year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?=$|[\s._\-~【\[(（]|年)", normalized)
    year_value = int(premiered[:4]) if premiered else (int(year_match[1]) if year_match else 0)
    return {"premiered": premiered, "year": year_value, "studio": studio}


def _extract_code(value: str) -> str:
    match = _XCHINA_CODE_RE.search(unicodedata.normalize("NFKC", _text(value)))
    return f"{match.group(1).upper()}-{match.group(2)}" if match else ""


def _parse_xchina_html(raw: str, base_url: str, author: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    parser = _XChinaHTMLParser()
    parser.feed(raw)
    parser.close()
    items: list[dict[str, Any]] = []
    page_links: list[str] = []
    base = _canonical_url(base_url)
    for anchor in parser.anchors:
        url = _canonical_url(anchor.get("href") or "", base=base)
        if not url:
            continue
        if _XCHINA_PAGE_RE.search(url) or (_XCHINA_MODEL_RE.search(url) and "/videos/model-" in url):
            page_links.append(url)
        if not _XCHINA_VIDEO_RE.search(url) or "/videos/model-" in url:
            continue
        cover = _canonical_url(anchor.get("image") or "", base=url)
        title = _text(anchor.get("title")) or _text(" ".join(anchor.get("text") or []))
        if not title and cover:
            title = _text(anchor.get("image") or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if not title:
            continue
        parsed = parse_external_title(title, author)
        key = url.casefold()
        if any(item.get("url", "").casefold() == key for item in items):
            continue
        items.append({
            "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:20],
            "url": url,
            "cover_url": cover,
            "code": _extract_code(title),
            **parsed,
        })
    return items, list(dict.fromkeys(page_links))


def _parse_madouqu_html(raw: str, base_url: str, author: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Read only Madouqu's tag cards; no video/detail page is requested.

    The site renders one image link and one title link per card.  Both links
    point to the same /video/<slug>/ URL, so collect them together before
    producing one source item.
    """
    parser = _XChinaHTMLParser()
    parser.feed(raw)
    parser.close()
    base = _canonical_url(base_url)
    studio = ""
    for anchor in parser.anchors:
        link = _canonical_url(anchor.get("href") or "", base=base)
        label = _text(" ".join(anchor.get("text") or []))
        if link and re.search(r"/gccm/[^/]+/", link) and label:
            studio = label
            break
    cards: dict[str, dict[str, str]] = {}
    page_links: list[str] = []
    for anchor in parser.anchors:
        link = _canonical_url(anchor.get("href") or "", base=base)
        if not link:
            continue
        if _MADOUQU_TAG_RE.search(link):
            page_links.append(link)
        if not _MADOUQU_VIDEO_RE.search(link):
            continue
        card = cards.setdefault(link, {"title": "", "cover": ""})
        title = _text(anchor.get("title")) or _text(" ".join(anchor.get("text") or []))
        cover = _canonical_url(anchor.get("image") or "", base=link)
        if title and not card["title"]:
            card["title"] = title
        if cover and not card["cover"]:
            card["cover"] = cover
    items: list[dict[str, Any]] = []
    for link, card in cards.items():
        title = card["title"]
        if not title:
            continue
        parsed = parse_external_title(title, author)
        items.append({
            "id": hashlib.sha1(link.encode("utf-8")).hexdigest()[:20],
            "url": link,
            "cover_url": card["cover"],
            "code": _extract_code(title),
            "studio": studio,
            **parsed,
        })
    return items, list(dict.fromkeys(page_links))


async def _fetch_xchina_page(client: httpx.AsyncClient, url: str, *, cookie: str = "", user_agent: str = DEFAULT_XCHINA_USER_AGENT) -> tuple[str, int]:
    try:
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
        if cookie:
            headers["Cookie"] = cookie
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ValueError(f"xChina 页面读取失败：{exc}") from exc
    content = response.content
    if len(content) > XCHINA_MAX_PAGE_BYTES:
        raise ValueError("xChina 页面超过 8 MiB，已停止抓取")
    text = content.decode(response.encoding or "utf-8", errors="replace")
    lowered = text.casefold()
    if response.status_code in {403, 429, 503} or "just a moment" in lowered or "cf-chl-" in lowered or "challenge-platform" in lowered:
        raise ValueError("xChina 被 Cloudflare/访问验证拦截；请检查 NOOR 设置中的 HTTP 代理是否可访问该站点")
    if response.status_code >= 400:
        raise ValueError(f"xChina 页面返回 HTTP {response.status_code}")
    return text, len(content)


async def _fetch_tangxin_page(client: httpx.AsyncClient, url: str, *, cookie: str = "", user_agent: str = DEFAULT_XCHINA_USER_AGENT) -> tuple[str, int]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    if cookie:
        headers["Cookie"] = cookie
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ValueError(f"糖心页面读取失败：{exc}") from exc
    content = response.content
    if len(content) > XCHINA_MAX_PAGE_BYTES:
        raise ValueError("糖心页面超过 8 MiB，已停止抓取")
    text = content.decode(response.encoding or "utf-8", errors="replace")
    lowered = text.casefold()
    if response.status_code in {403, 429, 503} or "challenge-platform" in lowered or "cloudflare" in lowered and response.status_code >= 400:
        raise ValueError("糖心页面被访问验证或代理拦截；请检查 NOOR 设置中的 HTTP 代理")
    if response.status_code >= 400:
        raise ValueError(f"糖心页面返回 HTTP {response.status_code}")
    return text, len(content)


async def _fetch_madouqu_page(client: httpx.AsyncClient, url: str, *, cookie: str = "", user_agent: str = DEFAULT_XCHINA_USER_AGENT) -> tuple[str, int]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    if cookie:
        headers["Cookie"] = cookie
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ValueError(f"麻豆区页面读取失败：{type(exc).__name__}: {exc!r}") from exc
    content = response.content
    if len(content) > XCHINA_MAX_PAGE_BYTES:
        raise ValueError("麻豆区页面超过 8 MiB，已停止抓取")
    text = content.decode(response.encoding or "utf-8", errors="replace")
    lowered = text.casefold()
    # Madouqu's normal WordPress pages include Cloudflare telemetry scripts
    # (including the string `challenge-platform`).  Treat only an actual
    # challenge page/status as blocked; otherwise every valid page would be
    # rejected before its cards can be parsed.
    if response.status_code in {403, 429, 503} or "just a moment" in lowered or "cf-chl-" in lowered:
        raise ValueError("麻豆区页面被访问验证或代理拦截；请检查 NOOR 设置中的 HTTP 代理")
    if response.status_code >= 400:
        raise ValueError(f"麻豆区页面返回 HTTP {response.status_code}")
    return text, len(content)


def _meta_content(raw: str, key: str) -> str:
    escaped = re.escape(key)
    patterns = (
        rf"<meta[^>]+(?:property|name)=[\"']{escaped}[\"'][^>]+content=[\"']([^\"']+)",
        rf"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']{escaped}[\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match:
            return html_lib.unescape(match.group(1)).strip()
    return ""


def _tangxin_title(value: str, author: dict[str, Any] | None = None) -> dict[str, Any]:
    title = re.sub(r"\s*[-|｜]\s*糖心官网\s*$", "", _text(value), flags=re.I)
    title = re.sub(r"^糖心(?:福利姬|原创|网黄|视频|Vlog)\s*[-|｜:：]*\s*", "", title, flags=re.I)
    return parse_external_title(title, author)


async def _scrape_tangxin_artist_source(url: str, author: dict[str, Any] | None = None, *, cookie: str = "", user_agent: str = DEFAULT_XCHINA_USER_AGENT) -> dict[str, Any]:
    canonical = _canonical_url(url)
    pending = [canonical]
    seen_pages: set[str] = set()
    video_urls: list[str] = []
    total_bytes = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(XCHINA_PAGE_TIMEOUT_SECONDS, connect=5.0),
        follow_redirects=True,
        proxy=_application_proxy(),
        trust_env=False,
        limits=httpx.Limits(max_connections=XCHINA_PAGE_CONCURRENCY, max_keepalive_connections=XCHINA_PAGE_CONCURRENCY),
    ) as client:
        while pending and len(seen_pages) < TANGXIN_MAX_PAGES:
            current = pending.pop(0)
            if current in seen_pages:
                continue
            seen_pages.add(current)
            raw, size = await _fetch_tangxin_page(client, current, cookie=cookie, user_agent=user_agent)
            total_bytes += size
            if total_bytes > XCHINA_MAX_TOTAL_BYTES:
                raise ValueError("糖心分页总大小超过 80 MiB，已停止抓取")
            parser = _XChinaHTMLParser()
            parser.feed(raw)
            parser.close()
            for anchor in parser.anchors:
                link = _canonical_url(anchor.get("href") or "", base=current)
                if not link:
                    continue
                if _TANGXIN_VIDEO_RE.search(link) and link not in video_urls:
                    video_urls.append(link)
                elif _TANGXIN_ARTIST_RE.search(link) and "page=" in link and link not in seen_pages and link not in pending:
                    pending.append(link)
        if not video_urls:
            raise ValueError("糖心艺人页面已读取，但没有识别到作品列表")

        async def fetch_work(video_url: str) -> dict[str, Any] | None:
            raw, _size = await _fetch_tangxin_page(client, video_url, cookie=cookie, user_agent=user_agent)
            title = _meta_content(raw, "og:title") or _meta_content(raw, "twitter:title")
            cover = _meta_content(raw, "og:image") or _meta_content(raw, "twitter:image")
            if not title:
                title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
                title = html_lib.unescape(title_match.group(1)).strip() if title_match else ""
            parsed = _tangxin_title(title, author)
            video_id = _TANGXIN_VIDEO_RE.search(video_url)
            return {
                "id": hashlib.sha1(video_url.encode("utf-8")).hexdigest()[:20],
                "url": video_url,
                "cover_url": _canonical_url(cover, base=video_url),
                "code": "",
                "source_id": video_id.group(1) if video_id else "",
                **parsed,
            } if title or cover else None

        works = await asyncio.gather(*(fetch_work(video_url) for video_url in video_urls), return_exceptions=True)
        items: list[dict[str, Any]] = []
        for work in works:
            if isinstance(work, Exception):
                raise work
            if work:
                items.append(work)
    return {"url": canonical, "source_type": "tangxin_artist", "pages": list(seen_pages), "page_count": len(seen_pages), "items": items, "fetched_at": _now(), "status": "ok", "error": ""}


async def _scrape_madouqu_tag_source(url: str, author: dict[str, Any] | None = None, *, cookie: str = "", user_agent: str = DEFAULT_XCHINA_USER_AGENT) -> dict[str, Any]:
    canonical = _validate_madouqu_url(url)
    pending = [canonical]
    seen: set[str] = set()
    pages: list[str] = []
    items: list[dict[str, Any]] = []
    total_bytes = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(XCHINA_PAGE_TIMEOUT_SECONDS, connect=5.0),
        follow_redirects=True,
        proxy=_application_proxy(),
        trust_env=False,
        limits=httpx.Limits(max_connections=XCHINA_PAGE_CONCURRENCY, max_keepalive_connections=XCHINA_PAGE_CONCURRENCY),
    ) as client:
        while pending and len(pages) < MADOUQU_MAX_PAGES:
            batch: list[str] = []
            while pending and len(batch) < XCHINA_PAGE_CONCURRENCY and len(pages) + len(batch) < MADOUQU_MAX_PAGES:
                current = pending.pop(0)
                if current in seen:
                    continue
                seen.add(current)
                batch.append(current)
            if not batch:
                continue
            responses = await asyncio.gather(*(_fetch_madouqu_page(client, current, cookie=cookie, user_agent=user_agent) for current in batch), return_exceptions=True)
            for current, response in zip(batch, responses):
                if isinstance(response, Exception):
                    raise response
                raw, size = response
                total_bytes += size
                if total_bytes > XCHINA_MAX_TOTAL_BYTES:
                    raise ValueError("麻豆区分页总大小超过 80 MiB，已停止抓取")
                page_items, links = _parse_madouqu_html(raw, current, author)
                pages.append(current)
                known = {item.get("url") for item in items}
                items.extend(item for item in page_items if item.get("url") not in known)
                current_host = current.split("/", 3)[2].casefold().split(":", 1)[0]
                for link in links:
                    link_host = link.split("/", 3)[2].casefold().split(":", 1)[0]
                    if link_host != current_host or not _MADOUQU_TAG_RE.search(link):
                        continue
                    if link not in seen and link not in pending:
                        pending.append(link)
            pending.sort(key=lambda value: int((_MADOUQU_PAGE_RE.search(value) or [0, 1])[1]))
    pages.sort(key=lambda value: int((_MADOUQU_PAGE_RE.search(value) or [0, 1])[1]))
    if not items:
        raise ValueError("麻豆区标签页已读取，但没有识别到作品卡片；页面结构可能已变化")
    return {
        "url": canonical,
        "source_type": "madouqu_tag",
        "pages": pages,
        "page_count": len(pages),
        "items": items,
        "fetched_at": _now(),
        "status": "ok",
        "error": "",
    }


async def _scrape_xchina_source(url: str, author: dict[str, Any] | None = None, *, cookie: str = "", user_agent: str = DEFAULT_XCHINA_USER_AGENT) -> dict[str, Any]:
    canonical = _validate_xchina_url(url)
    model_match = _XCHINA_MODEL_RE.search(canonical)
    model_id = model_match.group(1).casefold() if model_match else ""
    pending = [canonical]
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    pages: list[str] = []
    total_bytes = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(XCHINA_PAGE_TIMEOUT_SECONDS, connect=5.0),
        follow_redirects=True,
        proxy=_application_proxy(),
        trust_env=False,
        limits=httpx.Limits(max_connections=XCHINA_PAGE_CONCURRENCY, max_keepalive_connections=XCHINA_PAGE_CONCURRENCY),
    ) as client:
        while pending and len(pages) < XCHINA_MAX_PAGES:
            batch: list[str] = []
            while pending and len(batch) < XCHINA_PAGE_CONCURRENCY and len(pages) + len(batch) < XCHINA_MAX_PAGES:
                current = pending.pop(0)
                current_host = current.split("/", 3)[2].casefold()
                if current in seen:
                    continue
                seen.add(current)
                host = current_host.split(":", 1)[0]
                if host == "xchina.co" or host.endswith(XCHINA_HOST_SUFFIX):
                    batch.append(current)
            if not batch:
                continue
            responses = await asyncio.gather(*(_fetch_xchina_page(client, current, cookie=cookie, user_agent=user_agent) for current in batch), return_exceptions=True)
            for current, response in zip(batch, responses):
                if isinstance(response, Exception):
                    raise response
                raw, size = response
                total_bytes += size
                if total_bytes > XCHINA_MAX_TOTAL_BYTES:
                    raise ValueError("xChina 分页总大小超过 80 MiB，已停止抓取")
                page_items, links = _parse_xchina_html(raw, current, author)
                pages.append(current)
                known = {item.get("url") for item in items}
                items.extend(item for item in page_items if item.get("url") not in known)
                for link in links:
                    link_match = _XCHINA_MODEL_RE.search(link)
                    if not link_match or link_match.group(1).casefold() != model_id:
                        continue
                    if link not in seen and link not in pending:
                        pending.append(link)
            pending.sort(key=lambda value: int((_XCHINA_PAGE_RE.search(value) or [0, 1])[1]))
    pages.sort(key=lambda value: int((_XCHINA_PAGE_RE.search(value) or [0, 1])[1]))
    if not items:
        raise ValueError("xChina 页面已读取，但没有识别到作品卡片；页面结构可能已变化")
    return {
        "url": canonical,
        "pages": pages,
        "page_count": len(pages),
        "items": items,
        "fetched_at": _now(),
        "status": "ok",
        "error": "",
    }


def _match_text(value: str, author: dict[str, Any] | None = None) -> str:
    parsed = parse_external_title(value, author)
    return "".join(_source_title_tokens(parsed["title"]))


def _title_similarity(left: str, right: str, author: dict[str, Any] | None = None) -> float:
    a = _match_text(left, author)
    b = _match_text(right, author)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    a_tokens, b_tokens = set(_source_title_tokens(a)), set(_source_title_tokens(b))
    overlap = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return max(ratio, overlap)


def _external_work_score(author: dict[str, Any], item: dict[str, Any], work: dict[str, Any]) -> tuple[float, str]:
    if _text(work.get("nfo_title")):
        return _title_similarity(item.get("raw_title") or item.get("title") or "", work["nfo_title"], author), "nfo_title"
    item_code = _text(item.get("code")) or _extract_code(item.get("raw_title") or item.get("title") or "")
    work_code = _extract_code(work.get("name") or "") or _extract_code(work.get("title") or "")
    if item_code and work_code and item_code.casefold() == work_code.casefold():
        return 1.0, "code"
    score = max(
        _title_similarity(item.get("title") or item.get("raw_title") or "", work.get("title") or "", author),
        _title_similarity(item.get("title") or item.get("raw_title") or "", work.get("name") or "", author),
    )
    return score, "title"


def _rank_cached_source(author, title):
    source = author.get("external_source") or {}
    items = source.get("items") or []
    identity = json.dumps([2, author["id"], author.get("name"), author.get("aliases"), title, source.get("fetched_at"), items], ensure_ascii=False, sort_keys=True)
    path = plugin_data_path(PLUGIN_ID, "matches", hashlib.sha256(identity.encode()).hexdigest() + ".json")
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    result = [{"id": item["id"], "title": parse_external_title(item.get("raw_title") or item.get("title") or "", author)["title"], "cover_url": item.get("cover_url"), "score": round(_external_work_score(author, item, {"nfo_title": title})[0], 3)} for item in items]
    result.sort(key=lambda row: row["score"], reverse=True)
    _write_cache_json(path, result)
    return result


def _automatic_source_match(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Select only an unambiguous cached match for editor or queued writes.

    Low-confidence results remain available to the manual selector and are
    never written by the background worker.
    """
    if not matches:
        return {"status": "missing", "candidate": None, "score": 0.0, "margin": 0.0}
    best = matches[0]
    score = float(best.get("score") or 0)
    second = float(matches[1].get("score") or 0) if len(matches) > 1 else 0.0
    margin = max(0.0, score - second)
    confident = score >= 0.88 or (score >= 0.68 and margin >= 0.06) or (score >= 0.55 and margin >= 0.12)
    return {
        "status": "matched" if confident else ("ambiguous" if score >= 0.42 else "low_confidence"),
        "candidate": best if confident else None,
        "best": best,
        "score": round(score, 3),
        "margin": round(margin, 3),
    }


def _write_cache_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _title_cache_path(author, work):
    key = json.dumps([author["id"], author.get("root_folder_id"), work["file_id"]])
    return plugin_data_path(PLUGIN_ID, "nfo-titles", hashlib.sha256(key.encode()).hexdigest() + ".json")


def _read_title_cache(author, work):
    path = _title_cache_path(author, work)
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            pass
    return None


def _cached_title(author, work, nfo):
    cached = _read_title_cache(author, work)
    return cached if nfo and cached and cached.get("version") == _item_version(nfo) else None


async def _work_title(client, author, work, nfo, *, cache_only=False):
    path = _title_cache_path(author, work)
    async with _artwork_locks.setdefault("title:" + str(path), asyncio.Lock()):
        cached = _cached_title(author, work, nfo)
        if cached:
            return {**cached, "cached": True}
        stale = _read_title_cache(author, work)
        if cache_only:
            if stale:
                return {**stale, "cached": True, "stale": True}
            title = _stem(work["name"])
            return {"version": _item_version(nfo), "title": title, "nfo_title": "", "nfo_error": "", "cached": True, "uncached": True}
        info = await _parse_remote_nfo(client, nfo, "episodedetails", work.get("path") or work.get("remote_path") or work["name"])
        result = {"version": _item_version(nfo), "title": _text(info.get("fields", {}).get("title")) or _stem(work["name"]), "nfo_title": _text(info.get("fields", {}).get("title")), "nfo_error": info.get("error", "")}
        if nfo:
            _write_cache_json(path, result)
        return result


async def _source_cover(author, item):
    url = _text(item.get("cover_url"))
    key = hashlib.sha256(json.dumps([url, (author.get("external_source") or {}).get("fetched_at")]).encode()).hexdigest()
    path = plugin_data_path(PLUGIN_ID, "source-covers", key + ".jpg")
    async with _artwork_locks.setdefault("source:" + key, asyncio.Lock()):
        if path.exists():
            return path.read_bytes()
        raw = _convert_image(await _image_source({"image_url": url}), png=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
        return raw


def _match_external_work(author: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for year in author.get("years", []):
        for work in year.get("works", []):
            score, _matched_by = _external_work_score(author, item, work)
            candidates.append((score, year, work))
    if not candidates:
        return None
    score, year, work = max(candidates, key=lambda value: value[0])
    if score < 0.42:
        return None
    _score, matched_by = _external_work_score(author, item, work)
    return {"score": round(score, 4), "year": year, "work": work, "matched_by": matched_by}


def _storage_paths():
    paths = {}
    state = _load_state()
    for author in state.get("authors", []):
        if author.get("root_folder_id") and author.get("remote_path"):
            paths[str(author["root_folder_id"])] = str(author["remote_path"])
        for year in author.get("years", []):
            if year.get("folder_id") and year.get("remote_path"):
                paths[str(year["folder_id"])] = str(year["remote_path"])
            for work in year.get("works", []):
                if work.get("file_id") and work.get("remote_path"):
                    paths[str(work["file_id"])] = str(work["remote_path"])
        index = _read_library_index(author) or {}
        for node in {**index.get("fallback_folders", {}), **index.get("folders", {})}.values():
            parent = str(node.get("id") or "")
            for item in node.get("items", []):
                file_id = str(item.get("file_id") or "")
                if file_id:
                    paths[file_id] = _remote_path(node.get("path") or "/", item.get("name") or "")
        for folder_id, node in {**index.get("fallback_folders", {}), **index.get("folders", {})}.items():
            paths[str(folder_id)] = str(node.get("path") or "")
        for node in index.get("queue", []):
            if node.get("id") and node.get("path"):
                paths[str(node["id"])] = str(node["path"])
    return paths


def _get_115_client():
    """Compatibility name: production storage is CloudDrive2, never 115 Open API."""
    from app.plugins.runtime import runtime
    config = runtime.get_config(PLUGIN_ID)
    client = CD2Client(config)
    client.seed_paths(_storage_paths())
    return client


async def _list_folder(client: Any, folder_id: str, *, force: bool = False) -> list[dict[str, Any]]:
    key = str(folder_id or "0")
    cached = _folder_cache.get(key)
    if not force and cached and time.monotonic() - float(cached.get("at") or 0) < REMOTE_CACHE_SECONDS:
        return list(cached.get("items") or [])
    try:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            kwargs = {"offset": offset, "limit": 500}
            if getattr(client, "supports_force_refresh", False):
                kwargs["force"] = force
            page = await client.list_folder(key, **kwargs)
            rows = [item for item in (page.get("items") or []) if isinstance(item, dict)]
            items.extend(rows)
            count = int(page.get("count") or len(items))
            if not rows or len(items) >= count:
                break
            offset += len(rows)
        _folder_cache[key] = {"at": time.monotonic(), "items": items}
        return list(items)
    except Exception:
        # A recent/stale snapshot is safer than making a read-only page fail
        # after a 30-second API timeout. Explicit force refresh still reports
        # the error when there is no cached snapshot to use.
        if not force and cached and time.monotonic() - float(cached.get("at") or 0) < REMOTE_STALE_SECONDS:
            return list(cached.get("items") or [])
        raise


async def _download_remote_bytes(client: Any, item: dict[str, Any], *, max_bytes: int, force: bool = False) -> bytes:
    key = f"{item.get('file_id') or ''}:{_item_version(item)}:{max_bytes}"
    cached = _remote_bytes_cache.get(key)
    if not force and cached and time.monotonic() - float(cached.get("at") or 0) < REMOTE_CACHE_SECONDS:
        return bytes(cached.get("raw") or b"")
    raw = await client.download_bytes(item, max_bytes=max_bytes)
    _remote_bytes_cache[key] = {"at": time.monotonic(), "raw": raw}
    return raw


def _child(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    wanted = _text(name).casefold()
    return next((item for item in items if _text(item.get("name")).casefold() == wanted), None)


async def _refresh_child(client: Any, parent_id: str, name: str) -> dict[str, Any] | None:
    return _child(await _list_folder(client, parent_id, force=True), name)


async def _wait_child(client: Any, parent_id: str, name: str, *, attempts: int = 4) -> dict[str, Any] | None:
    for index in range(attempts):
        item = await _refresh_child(client, parent_id, name)
        if item:
            return item
        if index + 1 < attempts:
            await asyncio.sleep(0.25)
    return None


async def _ensure_folder(client: Any, parent_id: str, name: str) -> dict[str, Any]:
    existing = _child(await _list_folder(client, parent_id, force=True), name)
    if existing and existing.get("is_directory"):
        return existing
    if existing:
        raise ValueError(f"CD2 中已存在同名文件：{name}")
    created = await client.create_folder(parent_id, name)
    item = await _wait_child(client, parent_id, name)
    if not item:
        raise ValueError(f"CD2 创建目录后无法确认：{name}")
    return {**item, **created, "is_directory": True, "parent_id": str(parent_id)}


async def _backup_remote(client: Any, item: dict[str, Any]) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup_root = await _ensure_folder(client, str(item.get("parent_id") or "0"), ".noor-backups")
    backup_dir = await _ensure_folder(client, str(backup_root["file_id"]), stamp)
    await client.copy_file(str(item["file_id"]), str(backup_dir["file_id"]), no_duplicate=True)
    copied = await _wait_child(client, str(backup_dir["file_id"]), str(item.get("name") or ""))
    if not copied:
        raise ValueError(f"CD2 备份未确认：{item.get('name')}")
    return {"file_id": str(copied.get("file_id") or ""), "folder_id": str(backup_dir["file_id"]), "path": _remote_path(_remote_path(str(item.get("parent_id") or "0"), ".noor-backups"), stamp)}


async def _remote_replace(client: Any, parent_id: str, name: str, content: bytes, *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Upload temp -> verify -> backup old -> remove old -> rename temp."""
    if not content:
        raise ValueError("不能写入空文件")
    existing = existing or await _refresh_child(client, parent_id, name)
    content_sha1 = hashlib.sha1(content).hexdigest().upper()
    if existing and _text(existing.get("sha1")).upper() == content_sha1:
        actual = await _download_remote_bytes(
            client, existing,
            max_bytes=max(MAX_IMAGE_BYTES, len(content)), force=True,
        )
        if actual == content:
            return {
                "status": "saved", "name": name,
                "file_id": str(existing.get("file_id") or ""), "backup": None,
                "uploaded": False, "reused": True, "unchanged": True,
            }
    temporary_name = f".noor-write-{uuid.uuid4().hex}-{name}"
    uploaded = await client.upload_bytes(parent_id, temporary_name, content)
    temporary = await _wait_child(client, parent_id, temporary_name)
    if not temporary:
        raise ValueError(f"CD2 上传后无法确认临时文件：{temporary_name}")
    backup = None
    try:
        if existing:
            backup = await _backup_remote(client, existing)
            await client.delete_file(str(existing["file_id"]), parent_id)
        await client.rename_file(str(temporary["file_id"]), name)
        final = await _wait_child(client, parent_id, name)
        if not final:
            raise ValueError(f"CD2 改名后无法确认文件：{name}")
    except Exception:
        try:
            temporary_left = await _refresh_child(client, parent_id, temporary_name)
            if temporary_left:
                await client.delete_file(str(temporary_left["file_id"]), parent_id)
        except Exception:
            pass
        if backup:
            try:
                backup_items = await _list_folder(client, backup["folder_id"], force=True)
                old_copy = _child(backup_items, str(existing.get("name") or name))
                if old_copy:
                    current = _child(await _list_folder(client, parent_id, force=True), name)
                    if current:
                        await client.delete_file(str(current["file_id"]), parent_id)
                    await client.copy_file(str(old_copy["file_id"]), parent_id, no_duplicate=True)
            except Exception:
                pass
        raise
    return {"status": "saved", "name": name, "file_id": str(final.get("file_id") or ""), "backup": backup, "uploaded": uploaded}


async def _remove_variants(client: Any, parent_id: str, names: list[str], *, keep: str) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    names_lower = {value.casefold() for value in names}
    for item in await _list_folder(client, parent_id, force=True):
        name = _text(item.get("name"))
        if name.casefold() == keep.casefold() or name.casefold() not in names_lower:
            continue
        backup = await _backup_remote(client, item)
        await client.delete_file(str(item["file_id"]), parent_id)
        removed.append({"name": name, "backup": backup})
    return removed


def _nfo_fields(root: ET.Element, expected_root: str) -> dict[str, Any]:
    fields = {
        key: root.findtext(tag) or ""
        for key, tag in {
            "title": "title", "original_title": "originaltitle", "sort_title": "sorttitle",
            "plot": "plot", "outline": "outline", "show_title": "showtitle", "studio": "studio",
            "premiered": "premiered", "season": "season", "episode": "episode",
            "date_added": "dateadded", "aliases": "aliases",
        }.items()
    }
    if expected_root == "season":
        fields["season"] = root.findtext("seasonnumber") or root.findtext("season") or ""
    fields["actors"] = [node.findtext("name") or "" for node in root.findall("actor") if node.findtext("name")]
    fields["genres"] = [node.text or "" for node in root.findall("genre") if node.text]
    fields["tags"] = [node.text or "" for node in root.findall("tag") if node.text]
    return fields


async def _parse_remote_nfo(client: Any, item: dict[str, Any] | None, expected_root: str, path: str, *, force: bool = False, raw: bytes | None = None) -> dict[str, Any]:
    if not item:
        return {"status": "missing", "path": path, "hash": "", "version": "", "root": expected_root, "fields": {}}
    if raw is None:
        raw = await _download_remote_bytes(client, item, max_bytes=MAX_NFO_BYTES, force=force)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        root = ET.fromstring(raw)
    except Exception as exc:
        return {"status": "error", "path": path, "hash": digest, "version": _item_version(item), "root": "", "fields": {}, "error": f"XML 解析失败：{exc}"}
    if root.tag != expected_root:
        return {"status": "error", "path": path, "hash": digest, "version": _item_version(item), "root": root.tag, "fields": {}, "error": f"根节点应为 {expected_root}，实际为 {root.tag}"}
    return {"status": "ok", "path": path, "hash": digest, "version": _item_version(item), "root": root.tag, "fields": _nfo_fields(root, expected_root)}


def _set_xml(root: ET.Element, tag: str, value: Any) -> None:
    node = root.find(tag)
    if node is None:
        node = ET.SubElement(root, tag)
    node.text = _text(value)


def _render_nfo(raw: bytes | None, root_name: str, fields: dict[str, Any]) -> bytes:
    if raw:
        if len(raw) > MAX_NFO_BYTES:
            raise ValueError("NFO 超过 4 MiB 限制")
        try:
            root = ET.fromstring(raw)
        except Exception as exc:
            raise ValueError(f"XML 解析失败，未覆盖原文件：{exc}") from exc
        if root.tag != root_name:
            raise ValueError(f"NFO 根节点应为 {root_name}，实际为 {root.tag}")
    else:
        root = ET.Element(root_name)
    mapping = {
        "title": "title", "original_title": "originaltitle", "sort_title": "sorttitle",
        "plot": "plot", "outline": "outline", "show_title": "showtitle", "studio": "studio",
        "premiered": "premiered", "season": "seasonnumber" if root_name == "season" else "season",
        "episode": "episode", "date_added": "dateadded", "aliases": "aliases",
    }
    for key, tag in mapping.items():
        if key in fields:
            _set_xml(root, tag, fields.get(key))
    for tag_name, field_name in (("actor", "actors"), ("genre", "genres"), ("tag", "tags")):
        if field_name not in fields:
            continue
        root[:] = [node for node in root if node.tag != tag_name]
        values = fields.get(field_name) or []
        if isinstance(values, str):
            values = [part.strip() for part in re.split(r"[,，、\n]", values) if part.strip()]
        for value in values:
            node = ET.SubElement(root, tag_name)
            if tag_name == "actor":
                ET.SubElement(node, "name").text = _text(value)
            else:
                node.text = _text(value)
    content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if len(content) > MAX_NFO_BYTES:
        raise ValueError("生成的 NFO 超过 4 MiB 限制")
    return content


async def _entity_target(client: Any, author: dict[str, Any], entity: str, entity_id: str, *, force: bool = False):
    if entity == "author":
        parent_id, path, root_name, target_name = str(author.get("root_folder_id") or ""), _remote_path(author.get("remote_path") or "/", "tvshow.nfo"), "tvshow", "tvshow.nfo"
    elif entity == "year":
        year = _find_year(author, entity_id)
        parent_id, path, root_name, target_name = str(year.get("folder_id") or ""), _remote_path(year.get("remote_path") or "/", "season.nfo"), "season", "season.nfo"
    elif entity == "work":
        year, work = _find_work(author, entity_id)
        target_name = f"{_stem(work.get('name') or 'work')}.nfo"
        parent_id, path, root_name = str(work.get("parent_id") or year.get("folder_id") or ""), _remote_path(work.get("remote_path") or "/", target_name), "episodedetails"
    else:
        raise ValueError("不支持的编辑对象")
    if not parent_id:
        raise ValueError("请先绑定 CD2 目录")
    existing = _child(await _list_folder(client, parent_id, force=force), target_name)
    raw = await _download_remote_bytes(client, existing, max_bytes=MAX_NFO_BYTES, force=force) if existing else None
    return {"file_id": parent_id, "name": target_name}, path, root_name, target_name, existing, raw


async def _entity_info(client: Any, author: dict[str, Any], entity: str, entity_id: str, *, force: bool = False) -> dict[str, Any]:
    if entity == "author":
        model = author
        if not author.get("root_folder_id"):
            return {"entity": entity, "id": entity_id, "path": "", "nfo": {"status": "unbound", "root": "tvshow", "fields": {}, "hash": ""}, "model": model}
    elif entity == "year":
        model = _find_year(author, entity_id)
    elif entity == "work":
        _year, model = _find_work(author, entity_id)
    else:
        raise ValueError("不支持的编辑对象")
    _target, path, root_name, _name, existing, _raw = await _entity_target(client, author, entity, entity_id, force=force)
    return {"entity": entity, "id": entity_id, "path": path, "nfo": await _parse_remote_nfo(client, existing, root_name, path, force=force, raw=_raw), "model": model}


def _image_spec(author: dict[str, Any], role: str, *, year: dict[str, Any] | None = None, work: dict[str, Any] | None = None):
    if work:
        stem = _stem(work.get("name") or "work")
        return str(work.get("parent_id") or ""), f"{stem}-thumb.jpg", [f"{stem}-thumb.jpg", f"{stem}-thumb.jpeg", f"{stem}-thumb.png"]
    if role not in {"poster", "fanart", "clearlogo"}:
        raise ValueError("图片角色无效")
    parent_id = str(year.get("folder_id") if year else author.get("root_folder_id") or "")
    canonical = f"{role}.jpg" if role in {"poster", "fanart"} else "clearlogo.png"
    names = [canonical]
    names.extend(name for name in (f"{role}.jpeg", f"{role}.png", f"{role}.jpg") if name not in names)
    return parent_id, canonical, names


async def _image_info(client: Any, author: dict[str, Any], role: str, *, year: dict[str, Any] | None = None, work: dict[str, Any] | None = None, force=False, cache_only=False, refresh=False) -> dict[str, Any]:
    parent, _, _ = _image_spec(author, role, year=year, work=work)
    async with _artwork_locks.setdefault(str(parent), asyncio.Lock()):
        return await _cached_image_info(client, author, role, year=year, work=work, force=force, cache_only=cache_only, refresh=refresh)


async def _cached_image_info(client: Any, author: dict[str, Any], role: str, *, year=None, work=None, force=False, cache_only=False, refresh=False):
    parent_id, canonical, names = _image_spec(author, role, year=year, work=work)
    if not parent_id:
        return {"status": "unbound", "name": canonical, "candidates": names}
    service = _subscribe_cache(author, _read_library_index(author) or {}, _load_state())
    remote = service.snapshot(parent_id) if service else None
    revision = remote["revision"] if remote else 0
    key = hashlib.sha256(json.dumps([author["id"], author.get("root_folder_id"), parent_id, canonical]).encode()).hexdigest()
    cache_path = plugin_data_path(PLUGIN_ID, "artwork", key + ".json")
    cached = None
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if cached and cache_only:
        return {**cached["image"], "cached": True, "stale": cached.get("revision") != revision}
    if cache_only:
        return {"status": "uncached", "name": canonical, "candidates": names, "cached": True}
    if cached and not force and not refresh and cached["revision"] == revision:
        return {**cached["image"], "cached": True}
    try:
        if refresh:
            if not remote or remote["dirty"] or remote["items"] is None:
                return {**cached["image"], "cached": True, "stale": True} if cached else {"status": "uncached", "name": canonical, "candidates": names, "cached": True}
            items = remote["items"]
        else:
            items = remote["items"] if not force and remote and not remote["dirty"] and remote["items"] is not None else await _list_folder(client, parent_id, force=force or bool(remote and remote["dirty"]))
        if service and (force or not remote or remote["dirty"]):
            service.observe(parent_id, items)
            remote = service.snapshot(parent_id)
            revision = remote["revision"] if remote else revision
        item = next((candidate for name in names if (candidate := _child(items, name))), None)
        if item and cached and not force and cached["image"].get("file_id") == item.get("file_id") and cached["image"].get("version") == _item_version(item):
            info = cached["image"]  # Another file changed in this folder, not this image.
        elif item:
            raw = await _download_remote_bytes(client, item, max_bytes=MAX_IMAGE_BYTES, force=force)
            mime = "image/png" if _file_ext(item) == ".png" else "image/jpeg"
            info = {"status": "ok", "name": str(item.get("name") or canonical), "candidates": names, "file_id": item.get("file_id"), "hash": hashlib.sha256(raw).hexdigest(), "version": _item_version(item), "data_url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"}
        else:
            info = {"status": "missing", "name": canonical, "candidates": names}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps({"revision": revision, "image": info}, ensure_ascii=False), encoding="utf-8")
        temp.replace(cache_path)
        return info
    except Exception as exc:
        if cached and not force:
            return {**cached["image"], "cached": True, "stale": True, "warning": str(exc)}
        raise


def _decode_data_url(value: str) -> bytes:
    match = re.fullmatch(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)", _text(value), re.I)
    if not match:
        raise ValueError("图片数据格式无效")
    try:
        raw = base64.b64decode(match.group(1), validate=True)
    except Exception as exc:
        raise ValueError("图片 Base64 数据无效") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 25 MiB")
    return raw


async def _image_source(payload: dict[str, Any]) -> bytes:
    if _text(payload.get("data_url")):
        return _decode_data_url(_text(payload.get("data_url")))
    url = _text(payload.get("image_url"))
    if not url or not re.match(r"^https?://", url, re.I):
        raise ValueError("请上传图片文件或输入 http(s) 图片外链")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, proxy=_application_proxy(), trust_env=False) as http:
        async with http.stream("GET", url) as response:
            response.raise_for_status()
            if int(response.headers.get("content-length") or 0) > MAX_IMAGE_BYTES:
                raise ValueError("外链图片超过 25 MiB")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes(1024 * 1024):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ValueError("外链图片超过 25 MiB")
                chunks.append(chunk)
    return b"".join(chunks)


def _convert_image(raw: bytes, *, png: bool) -> bytes:
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            if png:
                image = image.convert("RGBA")
                output = io.BytesIO()
                image.save(output, format="PNG", optimize=True)
            else:
                if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=92, optimize=True)
            result = output.getvalue()
    except Exception as exc:
        raise ValueError(f"图片格式无法解析或转换：{exc}") from exc
    if not result or len(result) > MAX_IMAGE_BYTES:
        raise ValueError("转换后的图片为空或超过 25 MiB")
    return result


def _jpeg_storage_variant(raw: bytes, identity: str) -> bytes:
    """Keep pixels unchanged while avoiding 115 duplicate-upload challenges.

    A deterministic JPEG COM segment makes the same source cover distinct for
    each target sidecar. Media servers ignore the comment and still receive a
    standards-compliant JPEG.
    """
    if not raw.startswith(b"\xff\xd8"):
        raise ValueError("封面转码结果不是 JPEG")
    comment = b"NOOR:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24].encode("ascii")
    return raw[:2] + b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment + raw[2:]


def _compatibility(author: dict[str, Any], year: dict[str, Any], work: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    year_name = PurePosixPath(str(year.get("remote_path") or "")).name
    if not re.fullmatch(r"Season\s+\d+", year_name, re.I):
        warnings.append(f"年份目录“{year_name or '未命名'}”不是标准 Season N 命名，Emby 可能无法自动归季")
    if not re.search(r"(?i)(?:^|[-_. ])S\d{1,3}E\d{1,3}(?:[-_. ]|$)", _stem(work.get("name") or "")):
        warnings.append("作品文件名没有标准 SxxEyy 片段，将依赖 NFO 中的季号/集号")
    return warnings


def _organized_name(year: dict[str, Any], work: dict[str, Any]) -> tuple[str, str]:
    season = max(1, int(year.get("season_number") or 1))
    title = re.sub(r"[\\/:*?\"<>|]+", "_", _text(work.get("title")) or _stem(work.get("name") or "work")).strip() or "作品"
    suffix = _file_ext(work.get("name") or ".mp4")
    return f"Season {season:02d}", f"S{season:02d}E{max(1, int(work.get('episode_number') or 1)):02d} - {title}{suffix}"


def _scraped_media_name(year: dict[str, Any], work: dict[str, Any], title: str) -> str:
    safe_title = re.sub(r"[\\/:*?\"<>|]+", "_", _text(title)).strip() or "作品"
    safe_title = safe_title[:180].rstrip(" .") or "作品"
    suffix = _file_ext(work.get("name") or ".mp4")
    season = max(1, int(year.get("season_number") or 1))
    episode = max(1, int(work.get("episode_number") or 1))
    return f"S{season:02d}E{episode:02d} - {safe_title}{suffix}"


def _unique_text(values: Any) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,，、;；\n]+", values)
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text.casefold() not in {item.casefold() for item in result}:
            result.append(text)
    return result


def _external_work_fields(author: dict[str, Any], year: dict[str, Any], work: dict[str, Any], item: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_external_title(item.get("raw_title") or item.get("title") or "", author)
    old = current or {}
    local_tags = parse_external_title(_stem(work.get("name") or ""), author)["tags"]
    tags = _unique_text([*(_unique_text(old.get("tags"))), *(parsed.get("tags") or []), *local_tags])
    actors = _unique_text(old.get("actors"))
    actor_name = (_unique_text(author.get("aliases")) or [_text(author.get("name"))])[0]
    if actor_name and actor_name not in actors:
        actors.append(actor_name)
    genres = _unique_text(old.get("genres"))
    studio = _text(old.get("studio"))
    if not studio:
        studio = next((name for name in ("OnlyFans", "糖心VLOG", "麻豆传媒") if name.casefold() in _text(work.get("name")).casefold()), "")
    return {
        "title": parsed["title"],
        "original_title": _text(old.get("original_title")) or _text(item.get("raw_title")),
        "plot": _text(old.get("plot")),
        "show_title": _text(old.get("show_title")) or _text(author.get("name")),
        "premiered": _text(old.get("premiered")),
        "season": _text(old.get("season")) or str(year.get("season_number") or ""),
        "episode": _text(old.get("episode")) or str(work.get("episode_number") or ""),
        "studio": studio,
        "actors": actors,
        "genres": genres,
        "tags": tags,
        "filename": work.get("name"),
    }


async def _move_and_rename(client: Any, file_id: str, source_parent: str, target_parent: str, old_name: str, new_name: str) -> dict[str, Any]:
    if source_parent != target_parent:
        await client.move_file(file_id, target_parent)
    current = await _wait_child(client, target_parent, old_name)
    if not current:
        raise ValueError(f"CD2 移动后无法确认文件：{old_name}")
    if old_name != new_name:
        await client.rename_file(str(current["file_id"]), new_name)
        current = await _wait_child(client, target_parent, new_name)
        if not current:
            raise ValueError(f"CD2 改名后无法确认文件：{new_name}")
    return current


async def _rename_remote_item(client: Any, parent_id: str, old_name: str, new_name: str) -> dict[str, Any]:
    if old_name.casefold() == new_name.casefold():
        current = await _refresh_child(client, parent_id, old_name)
        if not current:
            raise ValueError(f"CD2 中找不到文件：{old_name}")
        return current
    current = await _refresh_child(client, parent_id, old_name)
    if not current:
        raise ValueError(f"CD2 中找不到文件：{old_name}")
    conflict = await _refresh_child(client, parent_id, new_name)
    if conflict and str(conflict.get("file_id")) != str(current.get("file_id")):
        raise ValueError(f"CD2 中已存在目标文件：{new_name}")
    await client.rename_file(str(current["file_id"]), new_name)
    final = await _wait_child(client, parent_id, new_name)
    if not final:
        raise ValueError(f"CD2 改名后无法确认文件：{new_name}")
    return final


async def _organize_remote_work(client: Any, author: dict[str, Any], year: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    target_folder_name, target_media_name = _organized_name(year, work)
    source_parent = str(work.get("parent_id") or year.get("folder_id") or "")
    source_items = await _list_folder(client, source_parent, force=True)
    source_media = _child(source_items, str(work.get("name") or ""))
    if not source_media:
        raise ValueError("CD2 中找不到绑定的作品文件，未执行整理")
    current_folder_name = PurePosixPath(str(year.get("remote_path") or "")).name
    target_folder = None
    created_target = False
    if current_folder_name.casefold() == target_folder_name.casefold():
        target_folder = {"file_id": source_parent, "name": current_folder_name, "parent_id": str(year.get("parent_folder_id") or author.get("root_folder_id") or "0")}
    else:
        other_bound = [item for item in year.get("works", []) if item.get("id") != work.get("id")]
        other_videos = [item for item in source_items if _is_video(item) and str(item.get("file_id")) != str(work.get("file_id"))]
        if other_bound or other_videos:
            raise ValueError("源年份目录还有其他作品，不能只移动当前作品；请先逐项确认")
        target_folder = _child(await _list_folder(client, str(author.get("root_folder_id") or ""), force=True), target_folder_name)
        if target_folder and not target_folder.get("is_directory"):
            raise ValueError(f"CD2 中已存在同名文件：{target_folder_name}")
        if not target_folder:
            target_folder = await _ensure_folder(client, str(author.get("root_folder_id") or "0"), target_folder_name)
            created_target = True
    target_parent = str(target_folder["file_id"])
    target_items = await _list_folder(client, target_parent, force=True)
    for item in target_items:
        if str(item.get("file_id")) != str(work.get("file_id")) and _text(item.get("name")) == target_media_name:
            raise ValueError(f"目标作品已存在：{target_media_name}")
    old_stem = _stem(work.get("name") or "work")
    new_stem = _stem(target_media_name)
    sidecars = []
    for item in source_items:
        name = _text(item.get("name"))
        if name.casefold() == f"{old_stem}.nfo".casefold():
            sidecars.append((item, f"{new_stem}.nfo"))
        elif name.casefold() in {f"{old_stem}-thumb.jpg".casefold(), f"{old_stem}-thumb.jpeg".casefold(), f"{old_stem}-thumb.png".casefold()}:
            sidecars.append((item, f"{new_stem}-thumb{_file_ext(name)}"))
    target_names = {str(item.get("name")).casefold() for item in target_items if str(item.get("file_id")) != str(work.get("file_id"))}
    if any(new_name.casefold() in target_names for _item, new_name in sidecars):
        raise ValueError("目标目录已有作品 NFO 或图片，未执行整理")
    moved: list[tuple[str, str, str, str]] = []
    try:
        media = await _move_and_rename(client, str(source_media["file_id"]), source_parent, target_parent, str(source_media["name"]), target_media_name)
        moved.append((str(media["file_id"]), target_parent, target_media_name, str(source_media["name"])))
        for item, new_name in sidecars:
            moved_item = await _move_and_rename(client, str(item["file_id"]), source_parent, target_parent, str(item["name"]), new_name)
            moved.append((str(moved_item["file_id"]), target_parent, new_name, str(item["name"])))
    except Exception:
        for file_id, parent, current_name, old_name in reversed(moved):
            try:
                await _move_and_rename(client, file_id, parent, source_parent, current_name, old_name)
            except Exception:
                pass
        raise
    work.update({"parent_id": target_parent, "name": target_media_name, "remote_path": _remote_path(_remote_path(author.get("remote_path") or "/", target_folder_name), target_media_name), "updated_at": _now()})
    year.update({"folder_id": target_parent, "remote_path": _remote_path(author.get("remote_path") or "/", target_folder_name), "updated_at": _now()})
    return {"work": work, "year": year, "target": work["remote_path"], "created_target": created_target, "message": "已按确认的目标路径在 CD2 整理作品及旁车文件。"}


async def _restore_remote_replace(client: Any, parent_id: str, name: str, backup: dict[str, Any] | None) -> None:
    """Best-effort rollback for a sidecar replacement.

    CD2 does not expose a local-filesystem atomic rename.  The replacement
    helper already leaves a backup in .noor-backups; use it if a later file in
    a multi-file scrape fails.
    """
    current = await _refresh_child(client, parent_id, name)
    if current:
        await client.delete_file(str(current["file_id"]), parent_id)
    if not backup:
        return
    backup_items = await _list_folder(client, backup["folder_id"], force=True)
    original = _child(backup_items, name)
    if original:
        await client.copy_file(str(original["file_id"]), parent_id, no_duplicate=False)
        if not await _wait_child(client, parent_id, name):
            raise ValueError(f"CD2 回滚后无法确认文件：{name}")


async def _scrape_external_work(client: Any, author: dict[str, Any], item: dict[str, Any], *, work_id: str = "", fields_override: dict[str, Any] | None = None, expected_hash: str | None = None) -> dict[str, Any]:
    if work_id:
        year, work = _find_work(author, work_id)
        score, matched_by = _external_work_score(author, item, work)
        match = {"score": round(score, 4), "matched_by": matched_by, "year": year, "work": work}
    else:
        match = _match_external_work(author, item)
    if not match:
        raise ValueError("没有找到足够相似的已绑定作品；请先绑定作品，或修改来源标题后重试")
    year, work = match["year"], match["work"]
    cover_url = _text(item.get("cover_url"))
    # Fetch and validate everything before touching CD2.
    image_source = item.get("image_source")
    cover = _convert_image(await _image_source(image_source), png=False) if image_source else (await _source_cover(author, item) if cover_url else None)
    target, path, root_name, target_name, existing_nfo, nfo_raw = await _entity_target(client, author, "work", work["id"], force=True)
    current_nfo = await _parse_remote_nfo(client, existing_nfo, root_name, path, force=True, raw=nfo_raw)
    if current_nfo.get("status") == "error":
        raise ValueError(current_nfo.get("error") or "作品 NFO 无法解析，未写入来源封面")
    if expected_hash is None or current_nfo.get("hash", "") != expected_hash:
        raise ValueError("CD2 上的 NFO 已变化或缺少读取版本，请重新刮削后保存")
    fields = _external_work_fields(author, year, work, item, current_nfo.get("fields") or {})
    if fields_override:
        for key in ("title", "original_title", "plot", "show_title", "premiered", "season", "episode", "studio", "actors", "genres", "tags"):
            if key in fields_override:
                fields[key] = fields_override[key]
    fields["title"] = _text(fields.get("title")) or _text(item.get("title")) or _stem(work.get("name") or "作品")
    fields["tags"] = _unique_text(fields.get("tags"))
    fields["actors"] = _unique_text(fields.get("actors"))
    fields["genres"] = _unique_text(fields.get("genres"))
    fields["season"] = str(year["season_number"])
    fields["episode"] = str(work["episode_number"])
    old_media_name = _text(work.get("name"))
    new_media_name = old_media_name
    fields["filename"] = old_media_name
    new_stem = _stem(new_media_name)
    old_nfo_name = target_name
    thumb_parent, thumb_name, thumb_variants = _image_spec(author, "thumb", work=work)
    new_thumb_name = f"{new_stem}-thumb.jpg"
    if cover is not None:
        cover = _jpeg_storage_variant(cover, f"{thumb_parent}:{new_thumb_name}")
    existing_items = await _list_folder(client, str(work.get("parent_id") or year.get("folder_id") or ""), force=True)
    # Keeping a cover must keep its actual format and existing basename.
    kept_thumb = next((entry for name in thumb_variants if (entry := _child(existing_items, name))), None)
    if cover is None and kept_thumb:
        thumb_name = kept_thumb["name"]
        new_thumb_name = f"{new_stem}-thumb{_file_ext(thumb_name)}"
    media = next((entry for entry in existing_items if str(entry.get("file_id")) == str(work["file_id"])), None)
    if not media or media.get("name") != old_media_name:
        raise ValueError("媒体文件已被外部改名或移动，请重新读取目录")
    nfo_content = _render_nfo(nfo_raw, root_name, {key: value for key, value in fields.items() if key != "filename"})
    existing_thumb = await _refresh_child(client, thumb_parent, thumb_name)
    image_saved: dict[str, Any] | None = None
    nfo_saved: dict[str, Any] | None = None
    try:
        if cover is not None:
            try:
                if existing_thumb and _text(existing_thumb.get("sha1")).upper() == hashlib.sha1(cover).hexdigest().upper():
                    image_saved = {"status": "saved", "name": thumb_name, "file_id": str(existing_thumb.get("file_id") or ""), "backup": None, "uploaded": False, "reused": True}
                else:
                    image_saved = await _remote_replace(client, thumb_parent, thumb_name, cover, existing=existing_thumb)
            except Exception as exc:
                raise ValueError(f"封面写入失败：{exc}") from exc
        try:
            nfo_saved = await _remote_replace(client, target["file_id"], old_nfo_name, nfo_content, existing=existing_nfo)
        except Exception as exc:
            raise ValueError(f"NFO 写入失败：{exc}") from exc
        removed = []
        # Move old image alternatives to recoverable backups only after the
        # replacement has succeeded.
        if cover is not None:
            removed = await _remove_variants(client, thumb_parent, thumb_variants, keep=new_thumb_name)
    except Exception:
        if image_saved:
            try:
                await _restore_remote_replace(client, thumb_parent, thumb_name, image_saved.get("backup"))
            except Exception:
                pass
        if nfo_saved:
            try:
                await _restore_remote_replace(client, target["file_id"], target_name, nfo_saved.get("backup"))
            except Exception:
                pass
        raise
    parsed = parse_external_title(item.get("raw_title") or item.get("title") or "", author)
    work.update({
        "name": new_media_name,
        "title": _text(fields["title"]),
        "tags": fields["tags"],
        "actors": fields["actors"],
        "genres": fields["genres"],
        "studio": _text(fields.get("studio")),
        "remote_path": _remote_path(posixpath.dirname(work.get("remote_path") or "") or year.get("remote_path") or "/", new_media_name),
        "external_source_url": _text(item.get("url")),
        "external_source_title": _text(item.get("raw_title") or item.get("title")),
        "external_scraped_at": _now(),
        "updated_at": _now(),
    })
    author["updated_at"] = _now()
    return {
        "match": {"score": match["score"], "matched_by": match["matched_by"], "year_id": year["id"], "work_id": work["id"], "work_title": _text(fields["title"])},
        "work": work,
        "parsed": parsed,
        "fields": fields,
        "filename": new_media_name,
        "image": {**(image_saved or {}), "removed_variants": removed, "name": new_thumb_name},
        "nfo": nfo_saved,
        "message": "NFO 和所选封面已保存到 CD2；文件名、目录保持不变，Emby 尚未刷新。",
    }


def _public_author(author: dict[str, Any]) -> dict[str, Any]:
    years = []
    for year in sorted(author.get("years", []), key=lambda item: (int(item.get("year") or 0), int(item.get("season_number") or 0))):
        works = sorted(year.get("works", []), key=lambda item: (int(item.get("episode_number") or 0), _text(item.get("title") or item.get("name")).casefold()))
        years.append({**year, "display_title": year.get("title") or f"{year.get('year')} 年", "works": works})
    return {**author, "years": years, "root_path": author.get("remote_path", "")}


def _ensure_auto_bound_work(author: dict[str, Any], candidate: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    for year in author.get("years", []):
        for work in year.get("works", []):
            if str(work.get("file_id")) == str(candidate["file_id"]):
                return year, work
    metadata = _filename_metadata(candidate.get("name") or "")
    year_value = int(metadata.get("year") or 0)
    branch_ids = [str(part.get("id")) for part in candidate.get("branch") or []]
    year = next((group for group in author.get("years", []) if year_value and int(group.get("year") or 0) == year_value), None)
    year = year or next((group for group in author.get("years", []) if str(group.get("folder_id")) in branch_ids), None)
    if not year:
        year = next((group for group in author.get("years", []) if group.get("virtual_unknown_year")), None)
    if not year:
        season_number = year_value or 1
        year = {
            "id": _new_id("year"), "year": year_value,
            "title": f"{year_value} 年" if year_value else "未指定年份", "virtual_unknown_year": not bool(year_value),
            "season_number": season_number,
            "folder_id": "", "remote_path": author.get("remote_path"), "works": [],
        }
        author.setdefault("years", []).append(year)
    work = {
        "id": _new_id("work"), "file_id": str(candidate["file_id"]),
        "parent_id": str(candidate["parent_id"]), "name": candidate["name"],
        "remote_path": candidate.get("path") or _remote_path(author.get("remote_path") or "/", candidate["name"]),
        "kind": "video", "title": _stem(candidate["name"]),
        "episode_number": max([int(entry.get("episode_number") or 0) for entry in year.get("works", [])] or [0]) + 1,
        "created_at": _now(), "updated_at": _now(),
    }
    year.setdefault("works", []).append(work)
    return year, work


async def _purge_auto_source_folder(
    client: Any, author: dict[str, Any], source_id: str, branch: list[dict[str, Any]],
) -> list[str]:
    """Permanently remove every leftover in an automatically organized work folder."""
    root_id = str(author.get("root_folder_id") or "")
    if not source_id or source_id == root_id or not branch:
        raise ValueError("自动入库来源必须是作者目录下的独立作品文件夹")
    source_name = _text(branch[-1].get("name"))
    if re.fullmatch(r"Season\s+\d+", source_name, re.I):
        raise ValueError("自动入库不会删除 Season 目录")
    removed: list[str] = []

    async def purge(folder_id: str, prefix: str = "") -> None:
        for item in await _list_folder(client, folder_id, force=True):
            name = _text(item.get("name"))
            display = f"{prefix}/{name}".strip("/")
            if item.get("is_directory"):
                await purge(str(item["file_id"]), display)
            await client.delete_file(str(item["file_id"]), folder_id)
            removed.append(display)
        if await _list_folder(client, folder_id, force=True):
            raise ValueError(f"CD2 尚未确认原作品目录内容删除结果：{source_name}")

    await purge(source_id)
    parent_id = str(branch[-2].get("id")) if len(branch) > 1 else root_id
    parent_items = await _list_folder(client, parent_id, force=True)
    folder = next((item for item in parent_items if str(item.get("file_id")) == source_id and item.get("is_directory")), None)
    if folder:
        if _text(folder.get("name")) != source_name:
            raise ValueError("原作品目录已发生变化，拒绝删除")
        await client.delete_file(source_id, parent_id)
    if any(str(item.get("file_id")) == source_id for item in await _list_folder(client, parent_id, force=True)):
        raise ValueError("CD2 尚未确认原作品目录删除结果")
    return removed


async def _delete_confirmed_empty_source_folder(
    client: Any, author: dict[str, Any], source_id: str, branch: list[dict[str, Any]],
) -> bool:
    """Delete only a confirmed-empty former work folder, never an author/season folder."""
    root_id = str(author.get("root_folder_id") or "")
    if not source_id or source_id == root_id or not branch:
        return False
    source_name = _text(branch[-1].get("name"))
    if re.fullmatch(r"Season\s+\d+", source_name, re.I):
        return False
    remaining = await _list_folder(client, source_id, force=True)
    if remaining:
        return False
    parent_id = str(branch[-2].get("id")) if len(branch) > 1 else root_id
    parent_before = await _list_folder(client, parent_id, force=True)
    folder = next((item for item in parent_before if str(item.get("file_id")) == source_id and item.get("is_directory")), None)
    if not folder:
        return True  # Idempotent recovery after a confirmed delete but before state persistence.
    if _text(folder.get("name")) != source_name:
        raise ValueError("原作品目录已发生变化，拒绝删除")
    await client.delete_file(source_id, parent_id)
    if any(str(item.get("file_id")) == source_id for item in await _list_folder(client, parent_id, force=True)):
        raise ValueError("CD2 尚未确认原作品目录删除结果")
    return True


async def _auto_organize_work(
    client: Any, state: dict[str, Any], author: dict[str, Any], work: dict[str, Any], candidate: dict[str, Any],
) -> dict[str, Any]:
    """Complete one matched work as an Emby episode and clean its source folder."""
    source_id = str(candidate.get("source_parent_id") or candidate.get("parent_id") or work.get("parent_id") or "")
    source_items = await _list_folder(client, source_id, force=True)
    media_name = _text(work.get("name") or candidate.get("name"))
    spec = _layout_group_spec(author, work, media_name)
    root_id = str(author.get("root_folder_id") or "")
    target_folder = await _ensure_folder(client, root_id, spec["folder_name"])
    target_id = str(target_folder["file_id"])
    target_items = source_items if source_id == target_id else await _list_folder(client, target_id, force=True)
    media = next((item for item in [*source_items, *target_items] if str(item.get("file_id")) == str(work.get("file_id")) and _is_video(item)), None)
    if not media or media.get("name") != media_name:
        raise ValueError("媒体文件已被外部改名、替换或移出目标季目录")
    stem = _stem(media["name"])
    companion_names = [f"{stem}.nfo", f"{stem}-thumb.jpg", f"{stem}-thumb.jpeg", f"{stem}-thumb.png"]
    companion_set = {name.casefold() for name in companion_names}
    companions_by_id = {
        str(item["file_id"]): item for item in [*source_items, *target_items]
        if _text(item.get("name")).casefold() in companion_set
    }
    companions = list(companions_by_id.values())
    expected_ids = {str(media["file_id"]), *[str(item["file_id"]) for item in companions]}
    for name in [media["name"], *[item["name"] for item in companions]]:
        collision = _child(target_items, name)
        if collision and str(collision.get("file_id")) not in expected_ids:
            raise ValueError(f"目标季目录已有同名文件：{name}")
    if str(media.get("parent_id") or source_id) != target_id:
        await client.move_file(str(media["file_id"]), target_id)
    for item in companions:
        if str(item.get("parent_id") or source_id) != target_id:
            await client.move_file(str(item["file_id"]), target_id)
    if source_id != target_id:
        if not await _wait_child(client, target_id, media["name"]):
            raise ValueError("CD2 尚未确认正片移动结果")
    group = _layout_year(author, spec, target_id)
    old_group = next((entry for entry in author.get("years", []) if work in entry.get("works", [])), None)
    if old_group is not group:
        if old_group:
            old_group["works"] = [item for item in old_group.get("works", []) if item is not work]
        group.setdefault("works", []).append(work)
    work.update(
        parent_id=target_id, name=media["name"],
        remote_path=_remote_path(group["remote_path"], media["name"]), updated_at=_now(),
    )
    nfo_name = f"{stem}.nfo"
    final_items = await _list_folder(client, target_id, force=True)
    nfo_item = _child(final_items, nfo_name)
    nfo_raw = await _download_remote_bytes(client, nfo_item, max_bytes=MAX_NFO_BYTES, force=True) if nfo_item else None
    parsed = await _parse_remote_nfo(client, nfo_item, "episodedetails", nfo_name, force=True, raw=nfo_raw) if nfo_item else {"status": "missing", "fields": {}}
    if parsed.get("status") == "error":
        raise ValueError(parsed["error"])
    old = parsed.get("fields") or {}
    extracted = _filename_metadata(media["name"])
    fields = {
        **old, "title": _text(old.get("title")) or _text(work.get("title")) or parse_external_title(media["name"], author)["title"],
        "show_title": author.get("name"), "season": str(spec["season_number"]),
        "episode": str(work["episode_number"]), "premiered": _text(old.get("premiered")) or extracted["premiered"],
        "studio": _text(old.get("studio")) or extracted["studio"], "actors": old.get("actors") or [author.get("name")],
    }
    await _remote_replace(client, target_id, nfo_name, _render_nfo(nfo_raw, "episodedetails", fields), existing=nfo_item)
    await _write_layout_nfo(client, root_id, "tvshow.nfo", "tvshow", {
        "title": author.get("name"), "plot": author.get("bio", ""), "aliases": author.get("aliases", ""),
    })
    await _write_layout_nfo(client, target_id, "season.nfo", "season", {
        "title": spec["title"], "season": str(spec["season_number"]),
    })
    deleted_source_items: list[str] = []
    if source_id != target_id:
        # Target media and sidecars are already confirmed above. Everything
        # still under the former work folder is obsolete and is deleted without backup.
        deleted_source_items = await _purge_auto_source_folder(
            client, author, source_id, list(candidate.get("branch") or []),
        )
    removed_folder = source_id != target_id
    work.update(title=fields["title"], studio=fields["studio"], updated_at=_now())
    author["years"] = [entry for entry in author.get("years", []) if entry.get("works")]
    author["updated_at"] = _now()
    return {
        "season": spec["title"], "season_number": spec["season_number"],
        "target_parent_id": target_id, "deleted_ads": deleted_source_items, "deleted_source_items": deleted_source_items, "removed_source_folder": removed_folder,
    }


async def _process_auto_match_job(job: dict[str, Any]) -> None:
    author_id, file_id = str(job["author_id"]), str(job["file_id"])
    _update_auto_match_job(author_id, file_id, status="matching", error="")
    async with _mutation_lock:
        state = _load_state()
        try:
            author = _find_author(state, author_id)
        except ValueError:
            _update_auto_match_job(author_id, file_id, status="stale", error="作者已解绑")
            return
        index = _read_library_index(author) or {}
        node = (index.get("folders") or {}).get(str(job.get("parent_id") or ""))
        indexed = next((item for item in (node or {}).get("items", []) if str(item.get("file_id")) == file_id and _is_video(item)), None)
        if not indexed:
            _update_auto_match_job(author_id, file_id, status="stale", error="作品已不在当前索引中")
            return
        source_parent_id = str(indexed.get("parent_id") or job.get("parent_id") or "")
        parent_id = source_parent_id
        client = _get_115_client()
        entries = await _list_folder(client, parent_id, force=True)
        media = next((item for item in entries if str(item.get("file_id")) == file_id and _is_video(item)), None)
        if not media:
            # Recover an interrupted run after the video was moved but before
            # state persistence. The stable file_id proves it is the same work.
            spec = _layout_group_spec(author, None, job.get("name") or indexed.get("name") or "")
            root_items = await _list_folder(client, str(author.get("root_folder_id") or ""), force=True)
            season_folder = _child(root_items, spec["folder_name"])
            if season_folder and season_folder.get("is_directory"):
                parent_id = str(season_folder["file_id"])
                entries = await _list_folder(client, parent_id, force=True)
                media = next((item for item in entries if str(item.get("file_id")) == file_id and _is_video(item)), None)
        if not media:
            _update_auto_match_job(author_id, file_id, status="stale", error="CD2 中已找不到作品")
            return
        nfo_name = f"{_stem(media.get('name') or '')}.nfo"
        if _child(entries, nfo_name):
            candidate = {
                **job, **media, "file_id": file_id, "parent_id": parent_id, "source_parent_id": source_parent_id,
                "name": media["name"], "branch": (node or {}).get("branch") or job.get("branch") or [],
                "path": _remote_path((node or {}).get("path") or posixpath.dirname(job.get("path") or "/"), media["name"]),
            }
            _year, work = _ensure_auto_bound_work(author, candidate)
            organized = await _auto_organize_work(_get_115_client(), state, author, work, candidate)
            _save_state(state)
            _update_auto_match_job(
                author_id, file_id, status="completed",
                result=f"已有 NFO；已整理到 {organized['season']}", error="", completed_at=_now(), next_attempt_at=0,
                organize=organized,
            )
            return
        candidate = {
            **job, **media, "file_id": file_id, "parent_id": parent_id, "source_parent_id": source_parent_id,
            "name": media["name"], "branch": (node or {}).get("branch") or job.get("branch") or [],
            "path": _remote_path((node or {}).get("path") or posixpath.dirname(job.get("path") or "/"), media["name"]),
        }
        _year, work = _ensure_auto_bound_work(author, candidate)
        cleaned_title = parse_external_title(_stem(media["name"]), author)["title"]
        ranked = _rank_cached_source(author, cleaned_title)
        automatic = _automatic_source_match(ranked)
        if automatic["status"] != "matched":
            _save_state(state)  # Keep the stable work/episode binding for manual editing.
            _update_auto_match_job(
                author_id, file_id, status="manual", match_status=automatic["status"],
                score=automatic.get("score", 0), margin=automatic.get("margin", 0),
                candidate=(automatic.get("best") or {}).get("id", ""), error="需要手动匹配",
            )
            return
        selected_id = str(automatic["candidate"]["id"])
        selected = next((item for item in (author.get("external_source") or {}).get("items", []) if str(item.get("id")) == selected_id), None)
        if not selected or not _text(selected.get("cover_url")):
            _save_state(state)
            _update_auto_match_job(author_id, file_id, status="manual", candidate=selected_id, score=automatic["score"], error="匹配来源缺少封面")
            return
        _update_auto_match_job(author_id, file_id, status="writing", candidate=selected_id, score=automatic["score"], margin=automatic["margin"])
        await _scrape_external_work(client, author, selected, work_id=work["id"], expected_hash="")
        organized = await _auto_organize_work(client, state, author, work, candidate)
        _save_state(state)
        _update_auto_match_job(
            author_id, file_id, status="completed", attempts=int(job.get("attempts") or 0),
            result=f"NFO 和封面已保存；已整理到 {organized['season']}", error="", completed_at=_now(), next_attempt_at=0,
            organize=organized,
        )


def _next_auto_match_job() -> dict[str, Any] | None:
    queue = _load_auto_match_state()
    now = time.time()
    candidates = []
    for item in queue.get("items", {}).values():
        status = item.get("status")
        if status == "queued" or (status == "failed" and int(item.get("attempts") or 0) < len(AUTO_MATCH_RETRY_SECONDS) and float(item.get("next_attempt_at") or 0) <= now):
            candidates.append(item)
    return min(candidates, key=lambda item: item.get("created_at") or "") if candidates else None


async def _auto_match_loop() -> None:
    global _auto_match_wakeup
    _auto_match_wakeup = asyncio.Event()
    while not _auto_match_stopping:
        job = _next_auto_match_job()
        if not job:
            try:
                await asyncio.wait_for(_auto_match_wakeup.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            _auto_match_wakeup.clear()
            continue
        try:
            await _process_auto_match_job(dict(job))
        except asyncio.CancelledError:
            _update_auto_match_job(str(job["author_id"]), str(job["file_id"]), status="queued", error="服务重载，等待恢复")
            raise
        except Exception as exc:
            attempts = int(job.get("attempts") or 0) + 1
            delay = AUTO_MATCH_RETRY_SECONDS[min(attempts - 1, len(AUTO_MATCH_RETRY_SECONDS) - 1)]
            _update_auto_match_job(
                str(job["author_id"]), str(job["file_id"]), status="failed", attempts=attempts,
                error=str(exc) or type(exc).__name__, next_attempt_at=time.time() + delay,
            )


async def _auto_discovery_loop() -> None:
    """Reconcile persistent indexes in the background without downloading media."""
    while not _auto_match_stopping:
        await asyncio.sleep(30)
        if _auto_match_stopping:
            break
        state = _load_state()
        for author in state.get("authors", []):
            if _auto_match_stopping:
                break
            if not author.get("root_folder_id"):
                continue
            try:
                # A completed unchanged index is served entirely from disk.
                # 115 is called only for a new/incomplete/invalidated directory.
                await _library_index(_get_115_client(), author, state, {"cache_only": True})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Featured performers background discovery failed for %s: %s", author.get("id"), exc)


def _resume_auto_match_jobs() -> None:
    queue = _load_auto_match_state()
    changed = False
    for item in queue.get("items", {}).values():
        if item.get("status") in {"matching", "writing"}:
            item.update(status="queued", error="服务重启后恢复", next_attempt_at=0, updated_at=_now())
            changed = True
    if changed:
        _save_auto_match_state(queue)


async def test(config: dict[str, Any]) -> PluginTestResult:
    try:
        client = CD2Client(config)
        client.seed_paths(_storage_paths())
        root_id = next((str(author.get("root_folder_id")) for author in _load_state().get("authors", []) if author.get("root_folder_id")), "")
        if root_id:
            await client.list_folder(root_id, offset=0, limit=1)
        return PluginTestResult(ok=True, message="精选女优已连接 CloudDrive2", details={"authors": len(_load_state().get("authors", [])), "storage": "clouddrive2", "endpoint": client.endpoint, "strm": False})
    except Exception as exc:
        return PluginTestResult(ok=False, message=f"精选女优 CD2 不可用：{exc}")


async def _library_location(client: Any, author: dict[str, Any], folder_ids: list[str], *, force: bool = False):
    """Resolve only the requested branch, proving every folder is under the author."""
    parent = str(author.get("root_folder_id") or "")
    if not parent:
        raise ValueError("请先绑定 CD2 作者目录")
    path = author.get("remote_path") or "/"
    year = None
    if len(folder_ids) > 16:
        raise ValueError("目录层级过深")
    for folder_id in folder_ids:
        entries = await _list_folder(client, parent, force=force)
        folder = next((entry for entry in entries if str(entry.get("file_id")) == str(folder_id) and entry.get("is_directory") and entry.get("name") != ".noor-backups"), None)
        if not folder:
            raise ValueError("目录不在已绑定的作者路径中，请刷新目录")
        parent = str(folder_id)
        path = _remote_path(path, folder["name"])
        year = next((group for group in author.get("years", []) if str(group.get("folder_id")) == parent), year)
    return parent, path, year


def _cleanup_reason(item: dict[str, Any], rules: dict[str, Any]) -> str:
    """A size limit is never itself evidence that a file is disposable."""
    if item.get("is_directory") or item.get("size") is None:
        return ""
    name = _text(item.get("name"))
    if name.startswith(".noor-") or not 0 <= int(item["size"]) <= rules["max_bytes"]:
        return ""
    if name.casefold() in {".ds_store", "thumbs.db", "desktop.ini"}:
        return "系统目录杂项"
    ext = _file_ext(name)
    if ext in {".url", ".lnk", ".webloc"}:
        return "网页或系统快捷方式"
    if ext in {".txt", ".html", ".htm"}:
        if re.search(r"广告|推广|加群|扫码|网址|最新地址|最新域名|发布地址|下载地址", name):
            return "文件名含广告／地址字样，请核对"
        if rules.get("include_text"):
            return "其他小文本／网页，可能含有用说明，请核对"
    return ""


def _layout_group_spec(author: dict[str, Any], work: dict[str, Any] | None, name: str) -> dict[str, Any]:
    metadata = _filename_metadata(name)
    year_value = int(metadata["year"] or 0)
    if not year_value and work:
        for group in author.get("years", []):
            if any(candidate is work for candidate in group.get("works", [])) and 1900 <= int(group.get("year") or 0) <= 2100:
                year_value = int(group["year"])
                break
    season_number = year_value or 1
    return {
        **metadata,
        "year": year_value,
        "season_number": season_number,
        "title": f"{year_value} 年" if year_value else "未指定年份",
        "folder_name": f"Season {season_number:02d}",
    }


def _layout_preview(state: dict[str, Any], author: dict[str, Any]) -> dict[str, Any]:
    index = _read_library_index(author)
    if not index or index.get("queue"):
        raise ValueError("作者目录索引尚未完成，请先等待作品读取完成再生成整理预览")
    if index.get("root_id") != str(author.get("root_folder_id") or ""):
        raise ValueError("作者目录绑定已变化，请先刷新作品")
    threshold = int(float(_library_min_mb(state)) * 1024 * 1024)
    bound = {
        str(work.get("file_id")): work
        for group in author.get("years", [])
        for work in group.get("works", [])
    }
    episode = max(
        [int(work.get("episode_number") or 0) for work in bound.values()] or [0]
    )
    root_id = str(author["root_folder_id"])
    root_node = index.get("folders", {}).get(root_id) or {"items": []}
    season_folders = {
        _text(item.get("name")).casefold(): item
        for item in root_node.get("items", [])
        if item.get("is_directory") and re.fullmatch(r"Season\s+\d+", _text(item.get("name")), re.I)
    }
    works: list[dict[str, Any]] = []
    deletes: list[dict[str, Any]] = []
    conflicts: list[str] = []
    planned_names: dict[str, set[str]] = {}
    seasons: dict[str, dict[str, Any]] = {}
    for parent_id, node in index.get("folders", {}).items():
        if any(_text(part.get("name")).startswith(".") for part in node.get("branch", [])):
            continue
        items = node.get("items") or []
        large = [item for item in items if _is_video(item) and int(item.get("size") or 0) >= threshold]
        if not large:
            continue
        item_by_name = {_text(item.get("name")).casefold(): item for item in items if not item.get("is_directory")}
        for media in large:
            file_id = str(media.get("file_id") or "")
            existing = bound.get(file_id)
            if not existing:
                episode += 1
            spec = _layout_group_spec(author, existing, media["name"])
            season = seasons.setdefault(spec["folder_name"], {
                "folder_name": spec["folder_name"], "folder_id": str((season_folders.get(spec["folder_name"].casefold()) or {}).get("file_id") or ""),
                "year": spec["year"], "season_number": spec["season_number"], "title": spec["title"],
            })
            stem = _stem(media["name"])
            companion_names = [f"{stem}.nfo", f"{stem}-thumb.jpg", f"{stem}-thumb.jpeg", f"{stem}-thumb.png"]
            companions = [item_by_name[name.casefold()] for name in companion_names if name.casefold() in item_by_name]
            target_names = [media["name"], *[item["name"] for item in companions]]
            key = spec["folder_name"].casefold()
            seen_names = planned_names.setdefault(key, set())
            target_node = index.get("folders", {}).get(season["folder_id"], {}) if season["folder_id"] else {}
            target_items = {_text(item.get("name")).casefold(): item for item in target_node.get("items", [])}
            for target_name in target_names:
                lower = target_name.casefold()
                target_item = target_items.get(lower)
                source_item = media if target_name == media["name"] else next(item for item in companions if item["name"] == target_name)
                if lower in seen_names and str(parent_id) != season["folder_id"]:
                    conflicts.append(f"{spec['folder_name']}/{target_name}：多个来源同名")
                elif target_item and str(target_item.get("file_id")) != str(source_item.get("file_id")):
                    conflicts.append(f"{spec['folder_name']}/{target_name}：目标已存在同名文件")
                seen_names.add(lower)
            works.append({
                "file_id": file_id, "parent_id": str(parent_id), "source_path": _remote_path(node["path"], media["name"]),
                "source_branch": list(node.get("branch") or []),
                "name": media["name"], "size": int(media.get("size") or 0), "version": _item_version(media),
                "companions": [{"file_id": str(item["file_id"]), "name": item["name"], "version": _item_version(item)} for item in companions],
                "season": spec, "episode": int((existing or {}).get("episode_number") or episode),
                "work_id": _text((existing or {}).get("id")), "status": "pending", "error": "",
            })
        for item in items:
            reason = ""
            if _is_video(item) and int(item.get("size") or 0) < threshold:
                reason = f"同目录有正片，视频小于 {float(_library_min_mb(state)):g} MB"
            elif not item.get("is_directory"):
                reason = _cleanup_reason(item, {"max_bytes": threshold, "include_text": False})
            if reason:
                deletes.append({
                    "file_id": str(item["file_id"]), "parent_id": str(parent_id), "name": item["name"],
                    "path": _remote_path(node["path"], item["name"]), "size": int(item.get("size") or 0),
                    "version": _item_version(item), "reason": reason, "status": "pending", "error": "",
                })
    plan_id = _new_id("emby-layout")
    plan = {
        "id": plan_id, "author_id": author["id"], "root_id": root_id, "created_at": _now(),
        "status": "ready" if not conflicts else "blocked", "threshold_mb": _library_min_mb(state),
        "seasons": list(seasons.values()), "works": works, "deletes": deletes,
        "source_folders": [
            {"folder_id": folder_id, "branch": branch, "status": "pending", "error": ""}
            for folder_id, branch in {
                str(item["parent_id"]): list(item.get("source_branch") or []) for item in works
                if str(item["parent_id"]) != root_id
                and item.get("source_branch")
                and not re.fullmatch(r"Season\s+\d+", _text(item["source_branch"][-1].get("name")), re.I)
            }.items()
        ],
        "conflicts": list(dict.fromkeys(conflicts)), "completed_at": "", "error": "",
    }
    state.setdefault("layout_plans", {})[plan_id] = plan
    _save_state(state)
    return plan


def _layout_year(author: dict[str, Any], spec: dict[str, Any], folder_id: str) -> dict[str, Any]:
    year_value = int(spec.get("year") or 0)
    group = next((item for item in author.get("years", []) if int(item.get("year") or 0) == year_value), None)
    if not group:
        group = {
            "id": _new_id("year"), "year": year_value, "title": spec["title"],
            "virtual_unknown_year": not bool(year_value), "season_number": int(spec["season_number"]),
            "folder_id": folder_id, "parent_folder_id": str(author["root_folder_id"]),
            "remote_path": _remote_path(author.get("remote_path") or "/", spec["folder_name"]),
            "works": [], "created_at": _now(), "updated_at": _now(),
        }
        author.setdefault("years", []).append(group)
    else:
        group.update(
            title=spec["title"], season_number=int(spec["season_number"]), folder_id=folder_id,
            parent_folder_id=str(author["root_folder_id"]),
            remote_path=_remote_path(author.get("remote_path") or "/", spec["folder_name"]), updated_at=_now(),
        )
    return group


async def _write_layout_nfo(client: Any, parent_id: str, name: str, root_name: str, fields: dict[str, Any]) -> None:
    existing = _child(await _list_folder(client, parent_id, force=True), name)
    raw = await _download_remote_bytes(client, existing, max_bytes=MAX_NFO_BYTES, force=True) if existing else None
    if raw:
        parsed = await _parse_remote_nfo(client, existing, root_name, name, force=True, raw=raw)
        if parsed.get("status") == "error":
            raise ValueError(parsed["error"])
    await _remote_replace(client, parent_id, name, _render_nfo(raw, root_name, fields), existing=existing)


async def _execute_layout_plan(config: dict[str, Any], author_id: str, plan_id: str, job: dict[str, Any]) -> dict[str, Any]:
    client = _get_115_client()
    state = _load_state()
    author = _find_author(state, author_id)
    plan = (state.get("layout_plans") or {}).get(plan_id)
    if not plan or plan.get("root_id") != str(author.get("root_folder_id") or ""):
        raise ValueError("整理预览无效或作者绑定已变化")
    if plan.get("conflicts"):
        raise ValueError("存在同名冲突，请处理后重新预览")
    plan.update(status="running", error="")
    _save_state(state)
    root_id = str(author["root_folder_id"])
    job.update(phase="创建并确认季目录", updated_at=_now())
    folders: dict[str, str] = {}
    for season in plan["seasons"]:
        folder = await _ensure_folder(client, root_id, season["folder_name"])
        season["folder_id"] = folders[season["folder_name"]] = str(folder["file_id"])
    await _write_layout_nfo(client, root_id, "tvshow.nfo", "tvshow", {
        "title": author.get("name"), "plot": author.get("bio", ""), "aliases": author.get("aliases", ""),
    })
    for index, entry in enumerate(plan["works"], 1):
        if entry.get("status") == "completed":
            continue
        target_id = folders[entry["season"]["folder_name"]]
        job.update(phase=f"整理作品 {index}/{len(plan['works'])}：{entry['name']}", updated_at=_now())
        try:
            source_items = await _list_folder(client, entry["parent_id"], force=True)
            target_items = source_items if entry["parent_id"] == target_id else await _list_folder(client, target_id, force=True)
            media = next((item for item in source_items if str(item.get("file_id")) == entry["file_id"]), None)
            if not media:
                media = next((item for item in target_items if str(item.get("file_id")) == entry["file_id"]), None)
            if not media or media.get("name") != entry["name"] or _item_version(media) != entry["version"]:
                raise ValueError("正片已被移动、改名或替换")
            nfo_name = f"{_stem(entry['name'])}.nfo"
            source_nfo = _child(source_items, nfo_name) or _child(target_items, nfo_name)
            nfo_raw = await _download_remote_bytes(client, source_nfo, max_bytes=MAX_NFO_BYTES, force=True) if source_nfo else None
            parsed = await _parse_remote_nfo(client, source_nfo, "episodedetails", nfo_name, force=True, raw=nfo_raw) if source_nfo else {"status": "missing", "fields": {}}
            if parsed.get("status") == "error":
                raise ValueError(parsed["error"])
            old = parsed.get("fields") or {}
            for name in [entry["name"], *[item["name"] for item in entry["companions"]]]:
                collision = _child(target_items, name)
                expected_ids = {entry["file_id"], *[item["file_id"] for item in entry["companions"]]}
                if collision and str(collision.get("file_id")) not in expected_ids:
                    raise ValueError(f"目标目录已有同名文件：{name}")
            if str(media.get("parent_id") or entry["parent_id"]) != target_id:
                await client.move_file(entry["file_id"], target_id)
            for companion in entry["companions"]:
                current = next((item for item in [*source_items, *target_items] if str(item.get("file_id")) == companion["file_id"]), None)
                if not current or current.get("name") != companion["name"] or _item_version(current) != companion["version"]:
                    raise ValueError(f"旁车文件已变化：{companion['name']}")
                if str(current.get("parent_id") or entry["parent_id"]) != target_id:
                    await client.move_file(companion["file_id"], target_id)
            if not await _wait_child(client, target_id, entry["name"]):
                raise ValueError("CD2 尚未确认正片移动结果")
            spec = entry["season"]
            group = _layout_year(author, spec, target_id)
            old_group = None
            work = None
            for candidate_group in author.get("years", []):
                candidate = next((item for item in candidate_group.get("works", []) if str(item.get("file_id")) == entry["file_id"]), None)
                if candidate:
                    old_group, work = candidate_group, candidate
                    break
            if not work:
                work = {"id": _new_id("work"), "file_id": entry["file_id"], "created_at": _now()}
            if old_group is not group:
                if old_group:
                    old_group["works"] = [item for item in old_group.get("works", []) if item is not work]
                group.setdefault("works", []).append(work)
            work.update(
                parent_id=target_id, name=entry["name"], episode_number=int(entry["episode"]),
                remote_path=_remote_path(group["remote_path"], entry["name"]), updated_at=_now(),
            )
            nfo_item = _child(await _list_folder(client, target_id, force=True), nfo_name)
            if source_nfo and (not nfo_item or str(nfo_item.get("file_id")) != str(source_nfo.get("file_id")) or _item_version(nfo_item) != _item_version(source_nfo)):
                raise ValueError("NFO 在移动期间发生变化")
            extracted = _filename_metadata(entry["name"])
            title = _text(old.get("title")) or parse_external_title(entry["name"], author)["title"]
            fields = {
                **old, "title": title, "show_title": author.get("name"), "season": str(spec["season_number"]),
                "episode": str(entry["episode"]), "premiered": _text(old.get("premiered")) or extracted["premiered"],
                "studio": _text(old.get("studio")) or extracted["studio"],
                "actors": old.get("actors") or [author.get("name")],
            }
            await _remote_replace(client, target_id, nfo_name, _render_nfo(nfo_raw, "episodedetails", fields), existing=nfo_item)
            work.update(title=title, studio=fields["studio"], updated_at=_now())
            entry.update(status="completed", target_parent_id=target_id, target_path=_remote_path(group["remote_path"], entry["name"]), error="")
            _folder_cache.pop(entry["parent_id"], None)
            _folder_cache.pop(target_id, None)
            _save_state(state)
        except Exception as exc:
            entry.update(status="failed", error=str(exc))
            plan.update(status="failed", error=f"{entry['name']}：{exc}")
            _save_state(state)
            raise
    for season in plan["seasons"]:
        job.update(phase=f"写入 {season['title']} 季资料", updated_at=_now())
        await _write_layout_nfo(client, folders[season["folder_name"]], "season.nfo", "season", {
            "title": season["title"], "season": str(season["season_number"]),
        })
    for index, entry in enumerate(plan["deletes"], 1):
        if entry.get("status") == "deleted":
            continue
        job.update(phase=f"删除小广告 {index}/{len(plan['deletes'])}：{entry['name']}", updated_at=_now())
        try:
            current = next((item for item in await _list_folder(client, entry["parent_id"], force=True) if str(item.get("file_id")) == entry["file_id"]), None)
            if not current:
                entry.update(status="deleted", error="")
            elif current.get("name") != entry["name"] or _item_version(current) != entry["version"]:
                raise ValueError("文件已变化，拒绝删除")
            else:
                await client.delete_file(entry["file_id"], entry["parent_id"])
                if any(str(item.get("file_id")) == entry["file_id"] for item in await _list_folder(client, entry["parent_id"], force=True)):
                    raise ValueError("CD2 尚未确认删除结果")
                entry.update(status="deleted", error="")
            _folder_cache.pop(entry["parent_id"], None)
            _save_state(state)
        except Exception as exc:
            entry.update(status="failed", error=str(exc))
            plan.update(status="failed", error=f"{entry['name']}：{exc}")
            _save_state(state)
            raise
    for index, entry in enumerate(plan.get("source_folders") or [], 1):
        if entry.get("status") == "deleted":
            continue
        job.update(phase=f"清理原作品目录 {index}/{len(plan['source_folders'])}", updated_at=_now())
        try:
            deleted = await _delete_confirmed_empty_source_folder(
                client, author, str(entry.get("folder_id") or ""), list(entry.get("branch") or []),
            )
            entry.update(status="deleted" if deleted else "retained", error="" if deleted else "目录仍有未识别文件，已保留")
            _save_state(state)
        except Exception as exc:
            entry.update(status="failed", error=str(exc))
            plan.update(status="failed", error=f"原作品目录：{exc}")
            _save_state(state)
            raise
    author["years"] = [group for group in author.get("years", []) if group.get("works")]
    plan.update(status="completed", completed_at=_now(), error="")
    author["updated_at"] = _now()
    _save_state(state)
    index_path = _library_index_path(author)
    if index_path.exists():
        index_path.unlink()
    return {"plan": plan, "author": _public_author(author), "message": "CD2 整理完成；请更新挂载缓存并重新扫描 Emby 媒体库。"}


async def _layout_job_action(action: str, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    state = _load_state()
    author = _find_author(state, _text(payload.get("author_id")))
    if action == "emby_layout_status":
        job = _layout_jobs.get(_text(payload.get("job_id")))
        if not job or job.get("author_id") != author["id"]:
            plan = (state.get("layout_plans") or {}).get(_text(payload.get("plan_id")))
            return {"job": {"status": "unknown", "phase": "任务状态已失效，请按预览记录核对", "plan": plan}}
        return {"job": dict(job)}
    if payload.get("confirm") is not True:
        raise ValueError("需要明确确认移动正片并永久删除小广告")
    plan = (state.get("layout_plans") or {}).get(_text(payload.get("plan_id")))
    if not plan or plan.get("author_id") != author["id"]:
        raise ValueError("整理预览不存在，请重新生成")
    if plan.get("conflicts"):
        raise ValueError("整理预览存在同名冲突，未执行任何移动或删除")
    for active in _layout_jobs.values():
        if active.get("plan_id") == plan["id"] and active.get("status") in {"queued", "running"}:
            return {"job": dict(active)}
    job_id = _new_id("emby-layout-job")
    job = {"id": job_id, "author_id": author["id"], "plan_id": plan["id"], "status": "queued", "phase": "等待开始", "updated_at": _now()}
    _layout_jobs[job_id] = job

    async def run():
        try:
            async with _mutation_lock:
                job.update(status="running", phase="校验整理预览", updated_at=_now())
                result = await _execute_layout_plan(config, author["id"], plan["id"], job)
            job.update(status="completed", phase="整理完成", result=result, updated_at=_now())
        except asyncio.CancelledError:
            job.update(status="unknown", phase="任务被中断，可重新执行同一预览继续", updated_at=_now())
            raise
        except Exception as exc:
            try:
                latest = _load_state()
                failed_plan = (latest.get("layout_plans") or {}).get(plan["id"])
                if failed_plan:
                    failed_plan.update(status="failed", error=str(exc) or type(exc).__name__)
                    _save_state(latest)
            except Exception:
                pass
            job.update(status="failed", phase="整理已停止", error=str(exc) or type(exc).__name__, updated_at=_now())
        finally:
            _layout_workers.pop(job_id, None)

    _layout_workers[job_id] = asyncio.create_task(run())
    return {"job": dict(job)}


def _cleanup_phase(payload: dict[str, Any], phase: str):
    job = _cleanup_jobs.get(payload.get("_job_id", ""))
    if job:
        job.update(phase=phase, updated_at=_now())


async def _cleanup_job_action(action: str, config: dict[str, Any], payload: dict[str, Any]):
    state = _load_state()
    author = _find_author(state, _text(payload.get("author_id")))
    if action == "cleanup_job_status":
        job = _cleanup_jobs.get(_text(payload.get("job_id")))
        if not job or job["author_id"] != author["id"]:
            return {"job": {"status": "unknown", "phase": "任务状态已失效，请从清理记录核对结果；未自动重试"}}
        return {"job": dict(job)}
    operation = payload.get("operation")
    if operation not in {"cleanup_execute", "cleanup_restore"} or payload.get("confirm") is not True:
        raise ValueError("需要明确确认清理或恢复")
    plan = (state.get("cleanup_plans") or {}).get(_text(payload.get("plan_id")))
    if not plan or plan["author_id"] != author["id"] or plan["root_id"] != str(author.get("root_folder_id")):
        raise ValueError("清理清单无效或绑定已变化")
    entry = next((row for row in plan["items"] if row["file_id"] == _text(payload.get("file_id"))), None)
    if not entry:
        raise ValueError("文件不在已预览的清单中")
    for job in _cleanup_jobs.values():
        if (job["author_id"], job["plan_id"], job["file_id"]) == (author["id"], plan["id"], entry["file_id"]) and job["status"] in {"queued", "running"}:
            if job["operation"] != operation:
                raise ValueError("此文件正在处理中，请等待结果后再执行其他操作")
            return {"job": dict(job)}
    # Cap retained in-memory results, never discard active workers.
    for key in list(_cleanup_jobs):
        if len(_cleanup_jobs) < 100:
            break
        if _cleanup_jobs[key]["status"] not in {"queued", "running"}:
            _cleanup_jobs.pop(key)
    job_id = _new_id("cleanup-job")
    job = {"id": job_id, "author_id": author["id"], "plan_id": plan["id"], "file_id": entry["file_id"], "operation": operation, "status": "queued", "phase": "等待前一项操作完成", "updated_at": _now()}
    _cleanup_jobs[job_id] = job

    async def run():
        try:
            async with _mutation_lock:
                job.update(status="running", phase="校验清理清单", updated_at=_now())
                result = await _handle_action(operation, config, {**payload, "_job_id": job_id})
            job.update(status="completed", phase="已确认远端结果", result=result, updated_at=_now())
        except asyncio.CancelledError:
            job.update(status="unknown", phase="处理被中断，请从清理记录核对，未自动重试", updated_at=_now())
            raise
        except Exception as exc:
            job.update(status="failed", phase="处理已停止", error=str(exc) or type(exc).__name__, updated_at=_now())
        finally:
            _cleanup_workers.pop(job_id, None)

    _cleanup_workers[job_id] = asyncio.create_task(run())
    return {"job": dict(job)}


async def _cd2_event_loop():
    while not _auto_match_stopping:
        try:
            client = _get_115_client()
            async for event in client.watch_changes():
                paths = [event.get("path"), event.get("new_path")]
                matched = directory_changes.invalidate_paths(path for path in paths if path)
                if matched:
                    log.info("CD2 change queued type=%s folders=%s", event.get("type"), matched)
                if _auto_match_stopping:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("CD2 PushMessage disconnected: %s", exc)
            await asyncio.sleep(10)


async def _restart_cd2_workers():
    global _cd2_event_worker, _cd2_cache_worker
    workers=[worker for worker in (_cd2_event_worker,_cd2_cache_worker) if worker]
    for worker in workers:worker.cancel()
    if workers:await asyncio.gather(*workers,return_exceptions=True)
    _cd2_event_worker=None;_cd2_cache_worker=None
    from app.plugins.runtime import runtime
    config=runtime.get_config(PLUGIN_ID)
    if config.get("cd2_token"):
        _cd2_event_worker=asyncio.create_task(_cd2_event_loop(),name="featured-performers-cd2-events")
        _cd2_cache_worker=asyncio.create_task(directory_changes.run(config,lambda _config:_get_115_client()),name="featured-performers-cd2-cache")


async def start_background(config):
    global _auto_match_worker, _auto_discovery_worker, _cd2_event_worker, _cd2_cache_worker, _auto_match_stopping
    _auto_match_stopping = False
    _resume_auto_match_jobs()
    # A completed persistent index is already validated discovery state. This
    # queues existing missing-NFO works without traversing 115 at startup.
    _discover_from_complete_indexes()
    if not _auto_match_worker or _auto_match_worker.done():
        _auto_match_worker = asyncio.create_task(_auto_match_loop(), name="featured-performers-auto-match")
    if not _auto_discovery_worker or _auto_discovery_worker.done():
        _auto_discovery_worker = asyncio.create_task(_auto_discovery_loop(), name="featured-performers-auto-discovery")
    if config.get("cd2_token") and (not _cd2_event_worker or _cd2_event_worker.done()):
        _cd2_event_worker = asyncio.create_task(_cd2_event_loop(), name="featured-performers-cd2-events")
    if config.get("cd2_token") and (not _cd2_cache_worker or _cd2_cache_worker.done()):
        _cd2_cache_worker = asyncio.create_task(directory_changes.run(config, lambda _config: _get_115_client()), name="featured-performers-cd2-cache")


async def stop_background():
    # Allow in-flight file operations to settle before a plugin reload. If the
    # grace period expires, recovery uses the persisted backup/cleanup journal.
    global _auto_match_stopping, _auto_match_worker, _auto_discovery_worker, _cd2_event_worker, _cd2_cache_worker
    _auto_match_stopping = True
    if _cd2_cache_worker:
        _cd2_cache_worker.cancel()
    _wake_auto_match_worker()
    workers = [
        *list(_cleanup_workers.values()), *list(_layout_workers.values()),
        *([_auto_match_worker] if _auto_match_worker else []),
        *([_auto_discovery_worker] if _auto_discovery_worker else []),
        *([_cd2_event_worker] if _cd2_event_worker else []),
        *([_cd2_cache_worker] if _cd2_cache_worker else []),
    ]
    if workers:
        try:
            await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), timeout=60)
        except asyncio.TimeoutError:
            pass
    if _auto_match_worker and _auto_match_worker.done():
        _auto_match_worker = None
    if _auto_discovery_worker and _auto_discovery_worker.done():
        _auto_discovery_worker = None
    if _cd2_event_worker and _cd2_event_worker.done():
        _cd2_event_worker = None
    if _cd2_cache_worker and _cd2_cache_worker.done():
        _cd2_cache_worker = None


async def _cleanup_action(action: str, client: Any, state: dict[str, Any], author: dict[str, Any], payload: dict[str, Any]):
    plans = state.setdefault("cleanup_plans", {})
    if action == "cleanup_history":
        return {"plans": [plan for plan in plans.values() if plan["author_id"] == author["id"]][-20:], "jobs": [dict(job) for job in _cleanup_jobs.values() if job["author_id"] == author["id"] and job["status"] in {"queued", "running"}]}
    if not author.get("root_folder_id") or str(author["root_folder_id"]) == "0":
        raise ValueError("清理仅支持已绑定作者目录，不能清理 CD2 根目录")
    plan_id = _text(payload.get("plan_id"))
    if action == "cleanup_scan" and not plan_id:
        branch = payload.get("folder_ids") or []
        if not isinstance(branch, list):
            raise ValueError("目录路径格式无效")
        parent, path, _ = await _library_location(client, author, branch)
        limit_mb = float(payload.get("max_mb", 2))
        if not 0.01 <= limit_mb <= 100:
            raise ValueError("大小上限须为 0.01 至 100 MB")
        plan_id = _new_id("cleanup")
        plans[plan_id] = {"id": plan_id, "author_id": author["id"], "root_id": str(author["root_folder_id"]), "scope": path, "created_at": _now(), "expires_at": time.time() + 3600, "rules": {"max_bytes": int(limit_mb * 1024 * 1024), "include_text": bool(payload.get("include_text"))}, "recursive": bool(payload.get("recursive", True)), "queue": [{"id": parent, "path": path, "branch": branch}], "seen": [], "items": [], "complete": False, "errors": [], "scanned": 0}
    plan = plans.get(plan_id)
    if not plan or plan["author_id"] != author["id"] or plan["root_id"] != str(author["root_folder_id"]):
        raise ValueError("清理清单无效或作者绑定已变化，请重新扫描")
    if action == "cleanup_scan":
        # Two directories per request: the page can show progress and stop
        # without leaving an unbounded recursive crawler on the 115 account.
        for _ in range(2):
            if not plan["queue"] or plan["complete"]:
                break
            current = plan["queue"][0]
            if current["id"] in plan["seen"]:
                plan["queue"].pop(0)
                continue
            if len(plan["seen"]) >= 1000 or plan["scanned"] >= 20000:
                raise ValueError("扫描范围过大，请进入更小的作品目录再清理；尚未删除任何文件")
            entries = await asyncio.wait_for(_list_folder(client, current["id"]), timeout=12)
            plan["queue"].pop(0)
            plan["seen"].append(current["id"])
            for item in entries:
                plan["scanned"] += 1
                if item.get("is_directory"):
                    if plan["recursive"] and not _text(item.get("name")).startswith(".") and _text(item.get("name")).casefold() not in {"$recycle.bin", "recycle"}:
                        branch = [*current["branch"], str(item["file_id"])]
                        if len(branch) <= 16:
                            plan["queue"].append({"id": str(item["file_id"]), "path": _remote_path(current["path"], item["name"]), "branch": branch})
                        else:
                            plan["errors"].append(f"跳过过深目录：{current['path']}/{item['name']}")
                    continue
                reason = _cleanup_reason(item, plan["rules"])
                if reason:
                    plan["items"].append({"file_id": str(item["file_id"]), "parent_id": current["id"], "branch": current["branch"], "name": item["name"], "path": _remote_path(current["path"], item["name"]), "size": item["size"], "version": _item_version(item), "sha1": item.get("sha1"), "reason": reason, "status": "pending"})
            _save_state(state)
        plan["complete"] = not plan["queue"]
        _save_state(state)
        return {"plan": plan}
    if action not in {"cleanup_execute", "cleanup_restore"}:
        raise ValueError("不支持的清理操作")
    if payload.get("confirm") is not True:
        raise ValueError("需要明确确认清理或恢复")
    entry = next((entry for entry in plan["items"] if entry["file_id"] == _text(payload.get("file_id"))), None)
    if not entry:
        raise ValueError("文件不在已预览的清理清单中")
    if action == "cleanup_execute" and entry["status"] == "cleaned":
        return {"item": entry}
    if action == "cleanup_execute" and entry["status"] == "restored":
        raise ValueError("文件已恢复，请重新扫描后再清理")
    if action == "cleanup_restore" and entry["status"] == "restored":
        return {"item": entry}
    if action == "cleanup_execute" and (not plan["complete"] or time.time() > plan["expires_at"]):
        raise ValueError("请完成扫描，或重新扫描已过期清单后再清理")
    _cleanup_phase(payload, "校验目录范围及原文件")
    parent, _, _ = await _library_location(client, author, entry["branch"], force=True)
    if str(parent) != str(entry["parent_id"]):
        raise ValueError("文件位置已变化，拒绝清理")
    try:
        current = await _list_folder(client, parent, force=True)
        if action == "cleanup_restore":
            _cleanup_phase(payload, "校验备份与同名冲突")
            backup = entry.get("backup")
            if not backup:
                raise ValueError("没有可恢复备份")
            if any(str(row.get("file_id")) == entry["file_id"] for row in current):
                raise ValueError("原文件仍在，无需恢复")
            if _child(current, entry["name"]):
                raise ValueError("原目录已有同名文件，拒绝覆盖；备份仍保留")
            copies = await _list_folder(client, backup["folder_id"], force=True)
            source = next((row for row in copies if str(row.get("file_id")) == backup["file_id"]), None)
            if not source or source.get("name") != entry["name"] or int(source.get("size", -1)) != entry["size"] or (entry.get("sha1") and source.get("sha1") != entry["sha1"]):
                raise ValueError("备份不存在或内容已变化，无法恢复")
            _cleanup_phase(payload, "恢复文件到原目录")
            await client.copy_file(backup["file_id"], parent, no_duplicate=True)
            _cleanup_phase(payload, "确认远端恢复结果")
            restored = await _wait_child(client, parent, entry["name"])
            if not restored or int(restored.get("size", -1)) != entry["size"]:
                raise ValueError("恢复结果待核实，请刷新记录；未覆盖其他文件")
            entry.update(status="restored", restored_file_id=restored["file_id"], error="")
        else:
            media = next((row for row in current if str(row.get("file_id")) == entry["file_id"]), None)
            if not media:
                if entry.get("backup"):
                    entry.update(status="cleaned", error="")
                else:
                    raise ValueError("原文件已不存在，未执行删除")
            else:
                if media.get("name") != entry["name"] or _item_version(media) != entry["version"] or not _cleanup_reason(media, plan["rules"]):
                    raise ValueError("文件名称、大小或内容已变化，请重新扫描；未删除")
                _cleanup_phase(payload, "创建并确认远端备份（受 CD2 上传队列影响，可能超过 30 秒）")
                backup = entry.get("backup") or await _backup_remote(client, {**media, "parent_id": parent})
                entry.update(backup=backup, status="backed_up", error="")
                _save_state(state)  # recovery evidence survives a request timeout
                _cleanup_phase(payload, "复核备份内容与原文件")
                copies = await _list_folder(client, backup["folder_id"], force=True)
                copied = next((row for row in copies if str(row.get("file_id")) == backup["file_id"]), None)
                if not copied or int(copied.get("size", -1)) != entry["size"] or (entry.get("sha1") and copied.get("sha1") != entry["sha1"]):
                    raise ValueError("备份内容未确认，拒绝删除")
                latest = next((row for row in await _list_folder(client, parent, force=True) if str(row.get("file_id")) == entry["file_id"]), None)
                if not latest or latest.get("name") != entry["name"] or _item_version(latest) != entry["version"]:
                    raise ValueError("备份期间原文件发生变化，拒绝删除")
                _cleanup_phase(payload, "备份已确认，删除原目录杂项")
                await client.delete_file(entry["file_id"], parent)
                _cleanup_phase(payload, "确认 115 删除结果")
                if any(str(row.get("file_id")) == entry["file_id"] for row in await _list_folder(client, parent, force=True)):
                    raise ValueError("CD2 尚未确认删除，备份已保留，可稍后重试")
                entry.update(status="cleaned", error="")
        _save_state(state)
    except Exception as exc:
        entry["error"] = str(exc)
        _save_state(state)
        raise
    return {"item": entry}


async def handle_action(action: str, config: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    if action == "storage_settings":
        return {"ok": True, "endpoint": config.get("cd2_endpoint") or "192.168.31.10:19798", "root": config.get("cd2_root") or "/dbonline/国产精选", "token_set": bool(config.get("cd2_token")), "storage": "clouddrive2"}
    if action in {"test_storage", "save_storage_settings"}:
        endpoint = _text(payload.get("endpoint")) or _text(config.get("cd2_endpoint")) or "192.168.31.10:19798"
        root = "/" + (_text(payload.get("root")) or _text(config.get("cd2_root")) or "/dbonline/国产精选").strip("/")
        token = _text(payload.get("token")) or _text(config.get("cd2_token"))
        candidate = {**config, "cd2_endpoint": endpoint, "cd2_root": root, "cd2_token": token}
        client = CD2Client(candidate)
        rows = await asyncio.to_thread(client._list, "/", False)
        if action == "save_storage_settings":
            from app.plugins.runtime import runtime
            values = {"cd2_endpoint": endpoint, "cd2_root": root}
            if _text(payload.get("token")):
                values["cd2_token"] = _text(payload.get("token"))
            public = await runtime.update_config(PLUGIN_ID, values)
            await _restart_cd2_workers()
            return {"ok": True, "endpoint": endpoint, "root": root, "token_set": bool(runtime.get_config(PLUGIN_ID).get("cd2_token")), "root_items": len(rows), "config": public, "message": "CloudDrive2 设置已保存并连接成功"}
        return {"ok": True, "endpoint": endpoint, "root": root, "token_set": bool(token), "root_items": len(rows), "message": "CloudDrive2 连接成功"}
    if action == "storage_event":
        paths = (payload or {}).get("paths") if isinstance((payload or {}).get("paths"), list) else []
        paths = [_text(path) for path in paths[:200] if _text(path)]
        if not paths:
            raise ValueError("CD2 目录事件缺少 paths")
        matched = directory_changes.invalidate_paths(paths)
        return {"ok": True, "paths": len(set(paths)), "matched_folders": matched, "message": "CD2 目录事件已接收"}
    if action == "library_cache_status":
        state = _load_state()
        author = _find_author(state, _text((payload or {}).get("author_id")))
        service = _directory_changes()
        rows = service.revisions(_cache_consumer(author)) if service else []
        errors = [str(row.get("error") or "") for row in rows if row.get("error") and row.get("dirty")]
        token = hashlib.sha256(json.dumps([(r["id"], r["revision"]) for r in sorted(rows, key=lambda r: r["id"])]).encode()).hexdigest()
        return {"token": token, "errors": [r["error"] for r in rows if r.get("error") and r.get("dirty")], "mode": "events", "check_minutes": _library_check_minutes(state), "pending": sum(1 for r in rows if r.get("dirty"))}
    if action in {"cleanup_start", "cleanup_job_status"}:
        return await _cleanup_job_action(action, config, payload or {})
    if action in {"emby_layout_start", "emby_layout_status"}:
        return await _layout_job_action(action, config, payload or {})
    read_actions = {"overview", "authors", "auto_match_status", "browse", "match_source_title", "library_title", "library_index", "library_folder", "library_image", "library_folder_cover", "source_image", "prepare_work", "prepare_external_work", "scan_author", "scan_year", "open_work_folder", "candidate_image", "read_entity", "entity", "images", "image", "compatibility", "preview_organize"}
    if action in read_actions or action == "cleanup_history":
        return await _handle_action(action, config, payload)
    # Serialize complete read/modify/write transactions, including remote awaits.
    # Otherwise saving one work can overwrite another request's author state.
    async with _mutation_lock:
        return await _handle_action(action, config, payload)


async def _handle_action(action: str, config: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    state = _load_state()
    if action == "match_source_title":
        author = _find_author(state, _text(payload.get("author_id")))
        title = _text(payload.get("title"))[:2000]
        return {"matches": _rank_cached_source(author, title), "match_title": title, "cached": True}
    if action in {"preview_organize", "organize_work"}:
        raise ValueError("当前模式仅编辑 NFO 与图片，不再移动或重命名文件及目录")
    if action in {"overview", "authors"}:
        connected = bool(config.get("cd2_token"))
        return {"ok": True, "library_editor_version": 1, "library_index_version": 1, "persistent_cache_version": 1, "auto_match_version": 2, "emby_layout_version": 1, "check_minutes": _library_check_minutes(state), "min_video_mb": _library_min_mb(state), "cleanup_version": 2, "authors": [_public_author(item) for item in state.get("authors", [])], "storage": {"type": "clouddrive2", "enabled": connected, "endpoint": config.get("cd2_endpoint") or "192.168.31.10:19798", "strm": False}}
    if action == "save_library_settings":
        try:
            value = float(payload.get("min_video_mb"))
        except (ValueError, TypeError):
            raise ValueError("最小视频体积必须是 1～100000 MB")
        if not math.isfinite(value) or not 1 <= value <= 100000:
            raise ValueError("最小视频体积必须是 1～100000 MB")
        state.setdefault("library_settings", {})["min_video_mb"] = value
        try:
            minutes = float(payload.get("check_minutes", _library_check_minutes(state)))
        except (ValueError, TypeError):
            raise ValueError("目录检查间隔必须是 15～10080 分钟，或 0（仅手动刷新）")
        if not math.isfinite(minutes) or (minutes != 0 and not 15 <= minutes <= 10080) or minutes != int(minutes):
            raise ValueError("目录检查间隔必须是 15～10080 分钟，或 0（仅手动刷新）")
        state["library_settings"]["check_minutes"] = int(minutes)
        _save_state(state)
        for item in state["authors"]:
            _subscribe_cache(item, _read_library_index(item) or {}, state)
        return {"ok": True, "min_video_mb": value, "check_minutes": int(minutes)}
    if action == "create_author":
        name = _text(payload.get("name"))
        if not name:
            raise ValueError("作者名称不能为空")
        if any(_text(item.get("name")).casefold() == name.casefold() for item in state["authors"]):
            raise ValueError("作者名称已存在")
        author = {"id": _new_id("author"), "name": name, "aliases": _text(payload.get("aliases")), "bio": _text(payload.get("bio")), "root_folder_id": "", "remote_path": "", "years": [], "created_at": _now(), "updated_at": _now()}
        state["authors"].append(author)
        _save_state(state)
        return {"ok": True, "author": _public_author(author)}
    client = _get_115_client()
    if action == "browse":
        folder_id = _text(payload.get("folder_id")) or "0"
        path = _text(payload.get("path")) or "/"
        items = await _list_folder(client, folder_id, force=bool(payload.get("force")))
        return {"ok": True, "folder_id": folder_id, "path": path, "parent_id": _text(payload.get("parent_id")), "items": [{**item, "path": _remote_path(path, item.get("name") or "")} for item in items]}
    author = _find_author(state, _text(payload.get("author_id")))
    if action == "auto_match_status":
        return {"ok": True, "auto_match": _auto_match_summary(author["id"])}
    if action == "library_index":
        return await _library_index(client, author, state, payload)
    if action == "emby_layout_preview":
        return {"ok": True, "plan": _layout_preview(state, author)}
    if action in {"cleanup_scan", "cleanup_execute", "cleanup_restore", "cleanup_history"}:
        return await _cleanup_action(action, client, state, author, payload)
    if action == "delete_work":
        if payload.get("confirm") is not True:
            raise ValueError("删除作品需要明确确认")
        file_id = _text(payload.get("file_id"))
        expected_name = _text(payload.get("name"))
        folder_ids = payload.get("folder_ids") or []
        if not file_id or not expected_name or not isinstance(folder_ids, list):
            raise ValueError("删除目标信息不完整，请刷新作品列表后重试")
        parent_id, _path, _group = await _library_location(client, author, folder_ids, force=True)
        if _text(payload.get("parent_id")) and _text(payload.get("parent_id")) != parent_id:
            raise ValueError("作品所在目录已变化，拒绝删除；请刷新后重试")
        entries = await _list_folder(client, parent_id, force=True)
        media = next((item for item in entries if str(item.get("file_id")) == file_id), None)
        if not media or media.get("is_directory") or not _is_video(media):
            raise ValueError("CD2 中已找不到该作品，或目标已不再是视频；请刷新后重试")
        if _text(media.get("name")) != expected_name:
            raise ValueError("作品文件名已变化，拒绝删除；请刷新后重试")
        work_id = _text(payload.get("work_id"))
        bound: tuple[dict[str, Any], dict[str, Any]] | None = None
        if work_id:
            bound = _find_work(author, work_id)
            if str(bound[1].get("file_id")) != file_id:
                raise ValueError("作品绑定与 CD2 文件不一致，拒绝删除")
        stem = _stem(expected_name)
        sidecar_names = {
            f"{stem}.nfo".casefold(),
            f"{stem}-thumb.jpg".casefold(),
            f"{stem}-thumb.jpeg".casefold(),
            f"{stem}-thumb.png".casefold(),
        }
        sidecars = [
            item for item in entries
            if not item.get("is_directory") and _text(item.get("name")).casefold() in sidecar_names
        ]
        targets = [*sidecars, media]  # Keep the video until every sidecar deletion succeeds.
        deleted: list[dict[str, str]] = []
        try:
            for item in targets:
                await client.delete_file(str(item["file_id"]), parent_id)
                deleted.append({"file_id": str(item["file_id"]), "name": _text(item.get("name"))})
        except Exception as exc:
            remaining = await _list_folder(client, parent_id, force=True)
            remaining_ids = {str(item.get("file_id")) for item in remaining}
            confirmed = [item for item in deleted if item["file_id"] not in remaining_ids]
            names = "、".join(item["name"] for item in confirmed) or "无"
            raise ValueError(f"CD2 删除未完成：已确认删除 {names}；失败于 {_text(item.get('name'))}：{exc}") from exc
        remaining = await _list_folder(client, parent_id, force=True)
        remaining_ids = {str(item.get("file_id")) for item in remaining}
        not_deleted = [item["name"] for item in deleted if item["file_id"] in remaining_ids]
        if not_deleted:
            raise ValueError(f"CD2 尚未确认删除：{'、'.join(not_deleted)}；作品卡片已保留，请重试")
        if bound:
            bound[0]["works"] = [item for item in bound[0].get("works", []) if item.get("id") != bound[1].get("id")]
        else:
            for year in author.get("years", []):
                year["works"] = [item for item in year.get("works", []) if str(item.get("file_id")) != file_id]
        queue = _load_auto_match_state()
        queue.get("items", {}).pop(_auto_match_key(author["id"], file_id), None)
        _save_auto_match_state(queue)
        _save_state(state)
        await _forget_library_files(author, parent_id, {item["file_id"] for item in deleted})
        return {
            "ok": True,
            "deleted": deleted,
            "author": _public_author(author),
            "message": "CD2 已确认删除作品及同名 NFO、封面；Emby 尚未刷新。",
        }
    if action == "source_image":
        item = next((item for item in (author.get("external_source") or {}).get("items", []) if item.get("id") == payload.get("item_id")), None)
        if not item or not item.get("cover_url"):
            return {"image": {"status": "missing"}}
        raw = await _source_cover(author, item)
        return {"image": {"data_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")}}
    if action in {"library_folder", "library_bind", "library_image", "library_title", "library_folder_cover"}:
        folder_ids = payload.get("folder_ids") or []
        if not isinstance(folder_ids, list):
            raise ValueError("目录路径格式无效")
        if action in {"library_image", "library_title"}:
            index = _read_library_index(author)
            parent_id = str(folder_ids[-1] if folder_ids else author.get("root_folder_id") or "")
            nodes = {**(index or {}).get("fallback_folders", {}), **(index or {}).get("folders", {})}
            node = nodes.get(parent_id)
            if node and [r["id"] for r in node["branch"]] == [str(fid) for fid in folder_ids]:
                media = next((r for r in node["items"] if str(r.get("file_id")) == str(payload.get("file_id")) and _is_video(r)), None)
                if media:
                    if action == "library_title":
                        return await _work_title(client, author, media, _child(node["items"], _stem(media["name"]) + ".nfo"), cache_only=not bool(payload.get("refresh")))
                    return {"image": await _image_info(client, author, "thumb", work={"name": media["name"], "parent_id": parent_id}, cache_only=not bool(payload.get("refresh")), refresh=bool(payload.get("refresh")))}
            if not payload.get("refresh") and not payload.get("force"):
                if action == "library_title":
                    return {"title": "", "nfo_title": "", "nfo_error": "", "cached": True, "uncached": True}
                return {"image": {"status": "uncached", "cached": True}}
        parent, path, group = await _library_location(client, author, folder_ids, force=bool(payload.get("force")))
        entries = await _list_folder(client, parent, force=bool(payload.get("force")))
        works = [work for year in author.get("years", []) for work in year.get("works", [])]
        if action == "library_folder_cover":
            image = next((entry for name in ("poster.jpg", "poster.png", "fanart.jpg", "fanart.png") if (entry := _child(entries, name))), None)
            image = image or next((entry for entry in entries if not entry.get("is_directory") and re.search(r"-thumb\.(?:jpg|jpeg|png)$", str(entry.get("name")), re.I)), None)
            if not image:
                return {"image": {"status": "missing"}}
            raw = await _download_remote_bytes(client, image, max_bytes=MAX_IMAGE_BYTES)
            mime = "image/png" if _file_ext(image) == ".png" else "image/jpeg"
            return {"image": {"data_url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"}}
        if action == "library_folder":
            context = {"folder_id": parent, "remote_path": path, "works": works, "author": author}
            return {"ok": True, "path": path, "folders": [entry for entry in entries if entry.get("is_directory") and entry.get("name") != ".noor-backups"], "works": await _work_candidates(client, context)}
        media = next((entry for entry in entries if str(entry.get("file_id")) == str(payload.get("file_id")) and _is_video(entry)), None)
        if not media:
            raise ValueError("作品不在所选目录中")
        if action == "library_title":
            return await _work_title(client, author, media, _child(entries, _stem(media["name"]) + ".nfo"), cache_only=not bool(payload.get("refresh")))
        if action == "library_image":
            return {"image": await _image_info(client, author, "thumb", work={"name": media["name"], "parent_id": parent}, cache_only=not bool(payload.get("refresh")), refresh=bool(payload.get("refresh")))}
        work = next((work for work in works if str(work.get("file_id")) == str(media["file_id"])), None)
        if not work:
            if not group:
                group = next((year for year in author.get("years", []) if year.get("virtual_unknown_year")), None)
            if not group:
                group = {"id": _new_id("year"), "year": 0, "title": "未指定年份", "virtual_unknown_year": True, "season_number": max([int(year.get("season_number") or 0) for year in author.get("years", [])] or [0]) + 1, "folder_id": "", "remote_path": author.get("remote_path"), "works": []}
                author["years"].append(group)
            work = {"id": _new_id("work"), "file_id": str(media["file_id"]), "parent_id": parent, "name": media["name"], "remote_path": _remote_path(path, media["name"]), "title": _stem(media["name"]), "episode_number": max([int(entry.get("episode_number") or 0) for entry in group["works"]] or [0]) + 1}
            group["works"].append(work)
            _save_state(state)
        return {"ok": True, "work": work, "author": _public_author(author)}
    if action == "prepare_work":
        year, work = _find_work(author, _text(payload.get("work_id")))
        refresh = bool(payload.get("refresh"))
        try:
            info = await asyncio.wait_for(_entity_info(client, author, "work", work["id"], force=refresh), timeout=30)
        except asyncio.TimeoutError as exc:
            raise ValueError("CD2 作品资料读取超过 30 秒；请稍后重试或使用本地缓存") from exc
        if info["nfo"].get("status") == "error":
            raise ValueError(info["nfo"]["error"])
        old = info["nfo"].get("fields") or {}
        parsed = parse_external_title(_stem(work["name"]), author)
        fields = _external_work_fields(author, year, work, {"raw_title": _stem(work["name"]), "title": parsed["title"]}, old)
        extracted = _filename_metadata(work["name"])
        fields.update({"title": old.get("title") or parsed["title"], "filename": work["name"], "studio": old.get("studio") or extracted["studio"], "premiered": old.get("premiered") or extracted["premiered"], "actors": old.get("actors") or [_text(author.get("name"))]})
        _write_cache_json(_title_cache_path(author, work), {"version": info["nfo"].get("version", ""), "title": _text(old.get("title")) or _stem(work["name"]), "nfo_title": _text(old.get("title")), "nfo_error": ""})
        matches = _rank_cached_source(author, fields["title"])
        return {"ok": True, "work": work, "fields": fields, "hash": info["nfo"]["hash"], "clean_title": parsed["title"], "suggested_filename": work["name"], "match_title": fields["title"], "matches": matches, "auto_match": _automatic_source_match(matches), "image": await _image_info(client, author, "thumb", work=work, cache_only=not refresh, refresh=refresh)}
    if action == "save_work":
        _year, work = _find_work(author, _text(payload.get("work_id")))
        item = {"title": work.get("title") or _stem(work["name"])}
        if not isinstance(payload.get("fields"), dict) or not _text(payload["fields"].get("title")):
            raise ValueError("作品标题不能为空")
        date_value = _text(payload["fields"].get("premiered"))
        if date_value:
            try:
                datetime.strptime(date_value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("发布日期请使用 YYYY-MM-DD，未知可留空") from exc
        if payload.get("item_id"):
            item = next((entry for entry in (author.get("external_source") or {}).get("items", []) if entry.get("id") == payload["item_id"]), None)
            if not item:
                raise ValueError("来源候选已变化，请重新匹配")
            item = dict(item)
        if payload.get("data_url") or payload.get("image_url"):
            item["image_source"] = {key: payload[key] for key in ("data_url", "image_url") if payload.get(key)}
        result = await _scrape_external_work(client, author, item, work_id=work["id"], fields_override=payload.get("fields"), expected_hash=payload.get("hash"))
        _save_state(state)
        _update_auto_match_job(author["id"], str(work["file_id"]), status="completed", result="已手动保存", error="", next_attempt_at=0, completed_at=_now())
        entries = await _list_folder(client, work["parent_id"])
        nfo = _child(entries, _stem(work["name"]) + ".nfo")
        if nfo:
            _write_cache_json(_title_cache_path(author, work), {"version": _item_version(nfo), "title": work["title"], "nfo_title": work["title"], "nfo_error": ""})
        await _invalidate_library_parent(author, work["parent_id"])
        return {"ok": True, "author": _public_author(author), **result}
    if action == "update_author":
        for key in ("name", "aliases", "bio"):
            if key in payload:
                value = _text(payload.get(key))
                if key == "name" and not value:
                    raise ValueError("作者名称不能为空")
                if key == "name" and any(item.get("id") != author.get("id") and _text(item.get("name")).casefold() == value.casefold() for item in state["authors"]):
                    raise ValueError("作者名称已存在")
                author[key] = value
        author["updated_at"] = _now()
        _save_state(state)
        return {"ok": True, "author": _public_author(author)}
    if action in {"bind_external_source", "refresh_external_source"}:
        source_url = _text(payload.get("url")) or _text((author.get("external_source") or {}).get("url"))
        if not source_url:
            raise ValueError("请填写麻豆区标签页地址，例如 https://madouqu.com/video/tag/nana/")
        supplied_cookie = _xchina_cookie(payload.get("cookie")) if "cookie" in payload else ""
        supplied_user_agent = _xchina_user_agent(payload.get("user_agent")) if "user_agent" in payload else ""
        if supplied_cookie:
            plugin_secret_store.set(PLUGIN_ID, "madouqu_cookie", supplied_cookie)
        if supplied_user_agent:
            plugin_secret_store.set(PLUGIN_ID, "madouqu_user_agent", supplied_user_agent)
        cookie = supplied_cookie or _xchina_cookie(config.get("madouqu_cookie") or config.get("xchina_cookie"))
        user_agent = supplied_user_agent or _xchina_user_agent(config.get("madouqu_user_agent") or config.get("xchina_user_agent"))
        source_type, source_url = _validate_source_url(source_url)
        previous_source = dict(author.get("external_source") or {})
        source = {"url": source_url, "source_type": source_type, "status": "scraping", "items": [], "pages": [], "page_count": 0, "fetched_at": "", "error": ""}
        author["external_source"] = source
        _save_state(state)
        try:
            source = await _scrape_madouqu_tag_source(source["url"], author, cookie=cookie, user_agent=user_agent)
        except Exception as exc:
            source.update({"status": "error", "error": str(exc), "fetched_at": _now()})
            author["external_source"] = {**previous_source, "status": "error", "error": str(exc)} if previous_source.get("items") else source
            _save_state(state)
            raise
        author["external_source"] = source
        author["updated_at"] = _now()
        _save_state(state)
        _discover_from_complete_indexes()
        return {"ok": True, "author": _public_author(author), "source": source}
    if action == "scrape_external_work":
        source = author.get("external_source") or {}
        item_id = _text(payload.get("item_id"))
        item = next((value for value in source.get("items", []) if str(value.get("id")) == item_id), None)
        if not item and isinstance(payload.get("item"), dict):
            item = dict(payload["item"])
        if not item:
            raise ValueError("来源作品不存在，请先重新抓取麻豆区标签页")
        if not payload.get("work_id"):
            raise ValueError("请从 CD2 作品卡片发起刮削")
        result = await _scrape_external_work(client, author, item, work_id=_text(payload.get("work_id")), fields_override=payload.get("fields"), expected_hash=payload.get("hash"))
        _save_state(state)
        _update_auto_match_job(author["id"], str(result["work"]["file_id"]), status="completed", result="已手动刮削", error="", next_attempt_at=0, completed_at=_now())
        await _invalidate_library_parent(author, result["work"]["parent_id"])
        return {"ok": True, "author": _public_author(author), **result}
    if action == "bind_author_folder":
        folder_id = _text(payload.get("folder_id"))
        if not folder_id:
            raise ValueError("请选择 CD2 作者目录")
        for item in state["authors"]:
            if item.get("id") != author.get("id") and str(item.get("root_folder_id") or "") == folder_id:
                raise ValueError("该 CD2 目录已绑定到其他作者")
        author.update({"root_folder_id": folder_id, "remote_path": _text(payload.get("path")) or "/", "root_name": _text(payload.get("name")) or PurePosixPath(_text(payload.get("path")) or "/").name, "updated_at": _now()})
        _save_state(state)
        return {"ok": True, "author": _public_author(author), "years": await _year_candidates(client, author)}
    if action == "scan_author":
        if not author.get("root_folder_id"):
            raise ValueError("请先绑定 CD2 作者目录")
        return {"ok": True, "author": _public_author(author), "years": await _year_candidates(client, author, force=bool(payload.get("force")))}
    if action == "bind_year":
        folder_id = _text(payload.get("folder_id"))
        if not folder_id or folder_id == str(author.get("root_folder_id")):
            raise ValueError("请选择作者目录下的年份文件夹")
        children = await _list_folder(client, str(author["root_folder_id"]))
        folder = next((item for item in children if item.get("file_id") == folder_id and item.get("is_directory")), None)
        if not folder:
            raise ValueError("年份目录必须是已绑定作者目录的直接子目录")
        existing = next((item for item in author["years"] if str(item.get("folder_id")) == folder_id), None)
        if existing:
            return {"ok": True, "year": existing, "duplicate": True, "candidates": await _work_candidates(client, existing)}
        year_value = int(payload.get("year") or _year_number(folder.get("name") or "") or 0)
        if not 1900 <= year_value <= 2100:
            raise ValueError("年份必须是 1900 至 2100")
        used = {int(item.get("season_number") or 0) for item in author["years"]}
        season_number = max(used or {0}) + 1
        year = {"id": _new_id("year"), "year": year_value, "title": _text(payload.get("title")) or f"{year_value} 年", "season_number": season_number, "folder_id": folder_id, "parent_folder_id": str(author["root_folder_id"]), "remote_path": _text(folder.get("path")) or _remote_path(author.get("remote_path") or "/", folder.get("name") or ""), "works": [], "created_at": _now(), "updated_at": _now()}
        author["years"].append(year)
        _save_state(state)
        return {"ok": True, "year": year, "candidates": await _work_candidates(client, year)}
    if action in {"open_work_folder", "bind_folder_work"}:
        folder_id = _text(payload.get("folder_id"))
        children = await _list_folder(client, str(author.get("root_folder_id") or ""))
        folder = next((entry for entry in children if str(entry.get("file_id")) == folder_id and entry.get("is_directory")), None)
        if not folder:
            raise ValueError("请选择已绑定作者目录中的作品文件夹")
        context = {"folder_id": folder_id, "remote_path": _remote_path(author.get("remote_path") or "/", folder["name"]), "works": [work for year in author.get("years", []) for work in year.get("works", []) if str(work.get("parent_id")) == folder_id]}
        if action == "open_work_folder":
            return {"ok": True, "folder": context, "candidates": await _work_candidates(client, context)}
        entries = await _list_folder(client, folder_id)
        media = next((entry for entry in entries if str(entry.get("file_id")) == _text(payload.get("file_id")) and _is_video(entry)), None)
        if not media:
            raise ValueError("视频不属于所选作品目录")
        for existing_year in author.get("years", []):
            for existing_work in existing_year.get("works", []):
                if str(existing_work.get("file_id")) == str(media["file_id"]):
                    return {"ok": True, "author": _public_author(author), "work": existing_work, "year": existing_year}
        year = next((entry for entry in author["years"] if entry.get("virtual_unknown_year")), None)
        if not year:
            year = {"id": _new_id("year"), "year": 0, "title": "未指定年份", "virtual_unknown_year": True, "season_number": max([int(entry.get("season_number") or 0) for entry in author["years"]] or [0]) + 1, "folder_id": "", "remote_path": author.get("remote_path"), "works": []}
            author["years"].append(year)
        work = {"id": _new_id("work"), "file_id": str(media["file_id"]), "parent_id": folder_id, "name": media["name"], "remote_path": _remote_path(context["remote_path"], media["name"]), "title": _stem(media["name"]), "episode_number": max([int(entry.get("episode_number") or 0) for entry in year["works"]] or [0]) + 1}
        year["works"].append(work)
        _save_state(state)
        return {"ok": True, "author": _public_author(author), "work": work, "year": year}
    if action == "scan_year":
        year = _find_year(author, _text(payload.get("year_id")))
        return {"ok": True, "year": year, "candidates": await _work_candidates(client, year, force=bool(payload.get("force")))}
    if action == "candidate_image":
        year = _find_year(author, _text(payload.get("year_id")))
        entries = await _list_folder(client, str(year["folder_id"]))
        media = next((entry for entry in entries if str(entry.get("file_id")) == _text(payload.get("file_id")) and _is_video(entry)), None)
        if not media:
            raise ValueError("作品不在已绑定目录中")
        return {"ok": True, "image": await _image_info(client, author, "thumb", work={"name": media["name"], "parent_id": str(year["folder_id"])})}
    if action == "prepare_external_work":
        year, work = _find_work(author, _text(payload.get("work_id")))
        items = (author.get("external_source") or {}).get("items") or []
        info = await _entity_info(client, author, "work", work["id"], force=True)
        if info["nfo"].get("status") == "error":
            raise ValueError(info["nfo"].get("error"))
        current = info["nfo"].get("fields") or {}
        title = current.get("title") or parse_external_title(work["name"], author)["title"]
        ranked = _rank_cached_source(author, title)
        selected_id = payload.get("item_id") or (ranked[0]["id"] if ranked else "")
        selected = next((item for item in items if item.get("id") == selected_id), None)
        if not selected:
            raise ValueError("请先绑定并抓取麻豆区来源")
        score = next(row["score"] for row in ranked if row["id"] == selected_id)
        return {"ok": True, "work": work, "item": selected, "matches": ranked, "auto_match": _automatic_source_match(ranked), "match_title": title, "score": score, "hash": info["nfo"]["hash"], "fields": _external_work_fields(author, year, work, selected, current)}
    if action == "bind_work":
        year = _find_year(author, _text(payload.get("year_id")))
        file_id = _text(payload.get("file_id"))
        year_items = await _list_folder(client, str(year.get("folder_id") or ""))
        item = next((entry for entry in year_items if entry.get("file_id") == file_id), None)
        if not item:
            item = next((entry for entry in await _list_folder(client, str(year.get("folder_id") or ""), force=True) if entry.get("file_id") == file_id), None)
        if not item or not _is_video(item):
            raise ValueError("作品必须是年份目录中的视频文件；本插件不使用 STRM")
        duplicate = next((work for work in year["works"] if str(work.get("file_id")) == file_id), None)
        if duplicate:
            return {"ok": True, "work": duplicate, "duplicate": True, "info": await _entity_info(client, author, "work", duplicate["id"])}
        episode = max([int(work.get("episode_number") or 0) for work in year["works"]] or [0]) + 1
        work = {"id": _new_id("work"), "file_id": file_id, "parent_id": str(year["folder_id"]), "name": _text(item.get("name")), "remote_path": _text(item.get("path")) or _remote_path(year.get("remote_path") or "/", item.get("name") or ""), "kind": "video", "title": _stem(item.get("name") or "作品"), "episode_number": episode, "remote_version": _item_version(item), "created_at": _now(), "updated_at": _now()}
        year["works"].append(work)
        _save_state(state)
        return {"ok": True, "work": work, "info": await _entity_info(client, author, "work", work["id"]), "compatibility": _compatibility(author, year, work)}
    if action == "unbind":
        entity, entity_id = _text(payload.get("entity")), _text(payload.get("entity_id"))
        if entity == "author":
            service = _directory_changes()
            if service:
                service.subscribe(_cache_consumer(author), [])
            state["authors"] = [item for item in state["authors"] if item.get("id") != author.get("id")]
        elif entity == "year":
            author["years"] = [item for item in author.get("years", []) if item.get("id") != entity_id]
        elif entity == "work":
            year, _work = _find_work(author, entity_id)
            year["works"] = [item for item in year.get("works", []) if item.get("id") != entity_id]
        else:
            raise ValueError("不支持的解绑对象")
        _save_state(state)
        return {"ok": True}
    if action in {"read_entity", "entity"}:
        return {"ok": True, "info": await _entity_info(client, author, _text(payload.get("entity")), _text(payload.get("entity_id")), force=True)}
    if action == "save_entity":
        entity, entity_id = _text(payload.get("entity")), _text(payload.get("entity_id"))
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
        target, path, root_name, target_name, existing, raw = await _entity_target(client, author, entity, entity_id, force=True)
        current = await _parse_remote_nfo(client, existing, root_name, path, force=True)
        expected_hash = _text(payload.get("hash"))
        if current.get("hash", "") != expected_hash:
            raise ValueError("CD2 上的 NFO 已被其他工具修改，请重新读取后再保存")
        if entity == "year":
            fields = {**fields, "season": _find_year(author, entity_id)["season_number"]}
        if entity == "work" and "episode" in fields and _text(fields["episode"]):
            group, target_work = _find_work(author, entity_id)
            episode = int(fields["episode"])
            if episode < 1 or any(int(entry.get("episode_number") or 0) == episode and entry.get("id") != entity_id for entry in group["works"]):
                raise ValueError("集号必须是未占用的正整数")
        content = _render_nfo(raw, root_name, fields)
        saved = await _remote_replace(client, target["file_id"], target_name, content, existing=existing)
        if entity == "author":
            if "title" in fields and _text(fields.get("title")):
                author["name"] = _text(fields.get("title"))
            if "plot" in fields:
                author["bio"] = _text(fields.get("plot"))
            if "aliases" in fields:
                author["aliases"] = _text(fields.get("aliases"))
        elif entity == "year":
            year = _find_year(author, entity_id)
            if "title" in fields and _text(fields.get("title")):
                year["title"] = _text(fields.get("title"))
        elif entity == "work":
            year, work = _find_work(author, entity_id)
            if "title" in fields and _text(fields.get("title")):
                work["title"] = _text(fields.get("title"))
            if "episode" in fields and _text(fields.get("episode")):
                episode = int(fields["episode"])
                if episode < 1 or any(int(item.get("episode_number") or 0) == episode and item.get("id") != work.get("id") for item in year["works"]):
                    raise ValueError("集号必须是未占用的正整数")
                work["episode_number"] = episode
            _update_auto_match_job(author["id"], str(work["file_id"]), status="completed", result="已手动保存", error="", next_attempt_at=0, completed_at=_now())
        author["updated_at"] = _now()
        _save_state(state)
        return {"ok": True, "saved": {**saved, "remote": True, "emby": "not_refreshed"}, "info": await _entity_info(client, author, entity, entity_id), "message": "CD2 保存成功；Emby 尚未刷新。若 Emby 读取 115 挂载目录，请更新挂载缓存并扫描媒体库。"}
    if action in {"images", "image"}:
        year = _find_year(author, _text(payload.get("year_id"))) if payload.get("year_id") else None
        work = _find_work(author, _text(payload.get("work_id")))[1] if payload.get("work_id") else None
        role = _text(payload.get("role")) or ("thumb" if work else "poster")
        return {"ok": True, "role": role, "image": await _image_info(client, author, role, year=year, work=work, cache_only=not bool(payload.get("refresh")), refresh=bool(payload.get("refresh")))}
    if action == "upload_image":
        year = _find_year(author, _text(payload.get("year_id"))) if payload.get("year_id") else None
        work = _find_work(author, _text(payload.get("work_id")))[1] if payload.get("work_id") else None
        role = _text(payload.get("role")) or ("thumb" if work else "poster")
        parent_id, canonical, variants = _image_spec(author, role, year=year, work=work)
        if not parent_id:
            raise ValueError("请先绑定对应的 CD2 目录")
        raw = await _image_source(payload)
        converted = _convert_image(raw, png=canonical.endswith(".png"))
        existing = await _refresh_child(client, parent_id, canonical)
        saved = await _remote_replace(client, parent_id, canonical, converted, existing=existing)
        removed = await _remove_variants(client, parent_id, variants, keep=canonical)
        info = await _image_info(client, author, role, year=year, work=work, force=True)
        return {"ok": True, "saved": {**saved, "removed_variants": removed, "remote": True, "emby": "not_refreshed"}, "image": info, "message": "CD2 图片保存成功；Emby 尚未刷新。"}
    if action == "compatibility":
        year, work = _find_work(author, _text(payload.get("work_id")))
        return {"ok": True, "warnings": _compatibility(author, year, work)}
    raise ValueError(f"不支持的动作：{action}")


async def _year_candidates(client: Any, author: dict[str, Any], *, force: bool = False) -> list[dict[str, Any]]:
    bound = {str(item.get("folder_id")) for item in author.get("years", [])}
    bound_by_folder = {str(item.get("folder_id")): item for item in author.get("years", [])}
    result = []
    for item in await _list_folder(client, str(author.get("root_folder_id") or ""), force=force):
        if not item.get("is_directory") or _text(item.get("name")) == ".noor-backups":
            continue
        folder_id = str(item.get("file_id") or "")
        known = bound_by_folder.get(folder_id)
        source_items = (author.get("external_source") or {}).get("items") or []
        best = max(source_items, key=lambda entry: _external_work_score(author, entry, {"name": item.get("name")})[0], default=None)
        score = _external_work_score(author, best, {"name": item.get("name")})[0] if best else 0
        cached_children = _folder_cache.get(folder_id)
        children = list(cached_children.get("items") or []) if cached_children and time.monotonic() - float(cached_children.get("at") or 0) < REMOTE_CACHE_SECONDS else []
        result.append({
            "folder_id": item.get("file_id"),
            "cover_url": best.get("cover_url", "") if best and score >= 0.42 else "",
            "cover_hint": "来源匹配预览，待确认" if best and score >= 0.42 else "",
            "parent_folder_id": item.get("parent_id"),
            "name": item.get("name"),
            "path": _remote_path(author.get("remote_path") or "/", item.get("name") or ""),
            "year": _year_number(item.get("name") or ""),
            "bound": folder_id in bound,
            "work_count": sum(1 for child in children if _is_video(child)) if children else (len(known.get("works") or []) if known else None),
            "nfo_exists": bool(_child(children, "season.nfo")) if children else None,
            "image_names": [child.get("name") for child in children if _text(child.get("name")).casefold() in {"poster.jpg", "poster.jpeg", "poster.png"}],
            "children_cached": bool(children),
        })
    return result


async def _work_candidates(client: Any, year: dict[str, Any], *, force: bool = False) -> list[dict[str, Any]]:
    children = await _list_folder(client, str(year.get("folder_id") or ""), force=force)
    return _candidates_from_items(children, year)


def _candidates_from_items(children, year, *, min_bytes=0):
    bound = {str(item.get("file_id")): item for item in year.get("works", [])}
    result = []
    for item in children:
        if not _is_video(item) or (min_bytes and (item.get("size") is None or int(item.get("size") or 0) < min_bytes)):
            continue
        work = bound.get(str(item.get("file_id")))
        nfo_name = f"{_stem(item.get('name') or '')}.nfo"
        thumb_prefix = f"{_stem(item.get('name') or '')}-thumb."
        nfo = _child(children, nfo_name)
        cached = _cached_title(year["author"], item, nfo) if year.get("author") else None
        result.append({"file_id": item.get("file_id"), "parent_id": item.get("parent_id") or year.get("folder_id"), "name": item.get("name"), "path": _remote_path(year.get("remote_path") or "/", item.get("name") or ""), "kind": "video", "size": item.get("size"), "updated_at": item.get("updated_at"), "nfo_exists": bool(nfo), "nfo_version": _item_version(nfo), "nfo_title": (cached or {}).get("nfo_title", ""), "nfo_error": (cached or {}).get("nfo_error", ""), "title_loaded": bool(cached) or not nfo, "image_names": [child.get("name") for child in children if _text(child.get("name")).startswith(thumb_prefix)], "bound": work is not None, "work_id": work.get("id") if work else "", "title": cached["title"] if cached else _stem(item.get("name") or "作品"), "episode_number": work.get("episode_number") if work else None})
    return result
