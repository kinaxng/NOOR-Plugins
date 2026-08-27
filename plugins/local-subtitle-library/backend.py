from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.core.runtime_paths import data_path, plugin_data_path
from app.plugins.contracts import PluginTestResult

PLUGIN_ID = "local-subtitle-library"
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sbv", ".sup"}
_index_lock = threading.Lock()


def resolve_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "library_paths": config.get("library_paths", "") or "",
        "index_enabled": bool(config.get("index_enabled", False)),
        "match_fuzzy": bool(config.get("match_fuzzy", False)),
    }


def _library_paths(config: dict[str, Any]) -> list[str]:
    raw = config.get("library_paths", "")
    values = raw if isinstance(raw, list) else str(raw or "").splitlines()
    return [str(item).strip() for item in values if str(item).strip() and os.path.isdir(str(item).strip())]


def _index_db_path() -> Path:
    path = plugin_data_path(PLUGIN_ID, "subtitle_index.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = data_path("runtime", "subtitle_library", "subtitle_index.db")
    if not path.exists() and legacy.is_file():
        shutil.copy2(legacy, path)
    return path


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS subtitle_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        base_name TEXT NOT NULL,
        full_path TEXT UNIQUE NOT NULL,
        ext TEXT NOT NULL,
        updated_at REAL NOT NULL
    )""")


def _build_index(config: dict[str, Any]) -> tuple[int, float]:
    started = time.time()
    paths = _library_paths(config)
    if not paths:
        return 0, 0.0
    with _index_lock, sqlite3.connect(_index_db_path()) as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM subtitle_index")
        now, count = time.time(), 0
        for library_path in paths:
            for root, _dirs, files in os.walk(library_path):
                for filename in files:
                    ext = Path(filename).suffix.lower()
                    if ext not in SUBTITLE_EXTS:
                        continue
                    before = conn.total_changes
                    conn.execute(
                        "INSERT OR IGNORE INTO subtitle_index (base_name, full_path, ext, updated_at) VALUES (?, ?, ?, ?)",
                        (Path(filename).stem.lower(), str(Path(root) / filename), ext, now),
                    )
                    count += int(conn.total_changes > before)
        conn.commit()
    return count, time.time() - started


def _index_stats(config: dict[str, Any]) -> dict[str, Any]:
    path = _index_db_path()
    if not path.is_file():
        return {"index_exists": False, "indexed_count": 0, "index_updated_at": None}
    try:
        with sqlite3.connect(path) as conn:
            _ensure_schema(conn)
            count = int(conn.execute("SELECT COUNT(*) FROM subtitle_index").fetchone()[0])
            updated = conn.execute("SELECT MAX(updated_at) FROM subtitle_index").fetchone()[0]
        return {"index_exists": True, "indexed_count": count, "index_updated_at": updated}
    except sqlite3.Error:
        return {"index_exists": False, "indexed_count": 0, "index_updated_at": None}


def _matches(subtitle_base: str, video_base: str, fuzzy: bool) -> bool:
    subtitle, video = subtitle_base.lower(), video_base.lower()
    return subtitle == video or bool(fuzzy and (subtitle in video or (len(subtitle) >= 8 and video.startswith(subtitle[:8]))))


def _result(filename: str, ext: str, full_path: str) -> dict[str, Any]:
    lowered = filename.lower()
    language = "zh" if re.search(r"[中港台澳]", lowered) else "ja" if re.search(r"[日韩]", lowered) else "unknown"
    return {
        "id": f"local:{filename}:{full_path}", "filename": filename, "ext": ext,
        "language": language, "source": "本地字幕库", "source_key": PLUGIN_ID,
        "source_type": "local_library", "url": full_path, "score": 1.0,
    }


def _search(config: dict[str, Any], video_code: str) -> list[dict[str, Any]]:
    cfg, query = resolve_config(config), video_code.lower()
    results: list[dict[str, Any]] = []
    paths = _library_paths(cfg)
    if cfg["index_enabled"] and paths:
        db_path = _index_db_path()
        if db_path.is_file():
            with sqlite3.connect(db_path) as conn:
                _ensure_schema(conn)
                if cfg["match_fuzzy"]:
                    rows = conn.execute("SELECT base_name, full_path, ext FROM subtitle_index").fetchall()
                    rows = [row for row in rows if _matches(str(row[0]), query, True)]
                else:
                    rows = conn.execute("SELECT base_name, full_path, ext FROM subtitle_index WHERE base_name = ?", (query,)).fetchall()
            results.extend(_result(Path(full_path).name, ext, full_path) for _base, full_path, ext in rows)
    elif paths:
        for library_path in paths:
            for root, _dirs, files in os.walk(library_path):
                for filename in files:
                    ext = Path(filename).suffix.lower()
                    if ext in SUBTITLE_EXTS and _matches(Path(filename).stem, query, cfg["match_fuzzy"]):
                        results.append(_result(filename, ext, str(Path(root) / filename)))
    return list({item["id"]: item for item in results}.values())


async def on_config_updated(config: dict[str, Any]) -> None:
    resolve_config(config)


async def test(config: dict[str, Any]) -> PluginTestResult:
    paths = await asyncio.to_thread(_library_paths, resolve_config(config))
    return PluginTestResult(ok=True, message=f"local subtitle library ready: {len(paths)} configured path(s)")


async def search_subtitles(config: dict[str, Any], video_code: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_search, config, video_code)


async def handle_action(action: str, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    cfg = resolve_config(config)
    if action == "index_status":
        stats = await asyncio.to_thread(_index_stats, cfg)
        paths = await asyncio.to_thread(_library_paths, cfg)
        return {**stats, "configured_paths": paths, "index_enabled": cfg["index_enabled"]}
    if action == "rebuild_index":
        count, elapsed = await asyncio.to_thread(_build_index, cfg)
        return {"indexed_files": count, "elapsed_seconds": round(elapsed, 2)}
    raise ValueError(f"unsupported action: {action}")
