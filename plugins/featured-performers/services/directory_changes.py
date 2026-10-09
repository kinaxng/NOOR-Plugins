"""Read-only directory cache notifications, independent of the STRM pipeline.

Consumers have separate revision cursors; nobody acknowledges another reader's
events. External changes are found by bounded, low-frequency directory paging,
NOT a claimed 115 push API. Only explicitly subscribed directories are checked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import contextmanager

from app.core.runtime_paths import plugin_data_path

log = logging.getLogger("noor.plugin.115.directory_changes")
KEYS = ("file_id", "parent_id", "name", "is_directory", "size", "sha1", "updated_at", "pick_code")


@contextmanager
def database():
    path = plugin_data_path("featured-performers", "directory_changes.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS directories (
          id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          snapshot TEXT, dirty INTEGER NOT NULL DEFAULT 0,
          checked REAL NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
          cursor TEXT, failures INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS subscriptions (
          consumer TEXT, folder TEXT, minutes INTEGER, expires REAL,
          PRIMARY KEY(consumer, folder));
        CREATE TABLE IF NOT EXISTS members (id TEXT, parent TEXT, PRIMARY KEY(id,parent));
        CREATE TABLE IF NOT EXISTS directory_paths (folder TEXT PRIMARY KEY, path TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value REAL);
        """)
        with db:
            yield db
    finally:
        db.close()


def subscribe(consumer, folder_ids, minutes=60, *, event_only_folder_ids=(), folder_paths=None):
    minutes = max(15, min(int(minutes), 10080)) if minutes else 0
    polling = {str(f) for f in folder_ids if str(f) not in {"", "0"}}
    event_only = {
        str(f) for f in event_only_folder_ids
        if str(f) not in {"", "0"} and str(f) not in polling
    }
    wanted = {**{folder: minutes for folder in polling}, **{folder: 0 for folder in event_only}}
    now = time.time()
    with database() as db:
        leases = list(db.execute("SELECT folder,minutes,expires FROM subscriptions WHERE consumer=?", (consumer,)))
        previous = {r["folder"]: r["minutes"] for r in leases}
        for folder, path in (folder_paths or {}).items():
            folder, path = str(folder), _normalize_path(path)
            if folder in wanted and path:
                db.execute("INSERT OR REPLACE INTO directory_paths VALUES (?,?)", (folder, path))
        if previous == wanted and all(r["expires"] > now + 6 * 86400 for r in leases):
            return  # Artwork/list reads do not rewrite identical subscriptions.
        db.execute("DELETE FROM subscriptions WHERE consumer=?", (consumer,))
        for folder, interval_minutes in wanted.items():
            due = now + max(interval_minutes, 60) * 60
            db.execute("INSERT OR IGNORE INTO directories(id,due) VALUES (?,?)", (folder, due))
            db.execute("INSERT INTO subscriptions VALUES (?,?,?,?)", (consumer, folder, interval_minutes, now + 7 * 86400))
            # Renewing a lease must not override debounce, pagination or backoff.
            if folder in previous and previous[folder] != interval_minutes and interval_minutes > 0:
                interval = db.execute("SELECT MIN(minutes) FROM subscriptions WHERE folder=? AND minutes>0 AND expires>?", (folder, now)).fetchone()[0] or 60
                db.execute("UPDATE directories SET due=? WHERE id=? AND failures=0 AND dirty=0 AND cursor IS NULL", (now + interval * 60, folder))

def snapshot(folder):
    with database() as db:
        row = db.execute("SELECT * FROM directories WHERE id=?", (str(folder),)).fetchone()
        if not row:
            return None
        return {**dict(row), "items": json.loads(row["snapshot"]) if row["snapshot"] is not None else None}


def revisions(consumer):
    with database() as db:
        return [dict(r) for r in db.execute("SELECT d.id,d.revision,d.dirty,d.checked,d.error FROM directories d JOIN subscriptions s ON s.folder=d.id WHERE s.consumer=?", (consumer,))]


def clear_authentication_errors():
    """Release stale auth backoff after a verified successful 115 request."""
    now = time.time()
    with database() as db:
        db.execute(
            """UPDATE directories
               SET failures=0,error='',due=CASE WHEN due>? THEN ? ELSE due END
               WHERE error LIKE '%access_token%' OR error LIKE '%access token%'
                  OR error LIKE '%token expired%' OR error LIKE '%token 已失效%'""",
            (now, now),
        )


def observe(folder, items, *, only_if_missing=False):
    rows = sorted(({k: item[k] for k in KEYS if k in item} for item in items), key=lambda r: str(r.get("file_id")))
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    now = time.time()
    with database() as db:
        old = db.execute("SELECT * FROM directories WHERE id=?", (str(folder),)).fetchone()
        if not old or (only_if_missing and (old["snapshot"] is not None or old["dirty"])):
            return
        interval = db.execute("SELECT MIN(minutes) FROM subscriptions WHERE folder=? AND minutes>0 AND expires>?", (str(folder), now)).fetchone()[0] or 60
        changed = old["snapshot"] != encoded or bool(old["dirty"])
        db.execute("UPDATE directories SET snapshot=?,revision=revision+?,dirty=0,checked=?,due=?,cursor=NULL,failures=0,error='' WHERE id=?", (encoded, int(changed), now, now + interval * 60, str(folder)))
        db.execute("DELETE FROM members WHERE parent=?", (str(folder),))
        db.executemany("INSERT OR IGNORE INTO members VALUES (?,?)", [(str(r["file_id"]), str(folder)) for r in rows if r.get("file_id")])


def activate_cooldown(seconds=3600):
    """Pause periodic directory API reads after provider risk/access limits."""
    until = time.time() + max(300, int(seconds or 0))
    with database() as db:
        current = db.execute("SELECT value FROM control WHERE key='cooldown'").fetchone()
        if not current or float(current[0] or 0) < until:
            db.execute("INSERT OR REPLACE INTO control VALUES ('cooldown',?)", (until,))


def cooldown_remaining():
    with database() as db:
        current = db.execute("SELECT value FROM control WHERE key='cooldown'").fetchone()
    return max(0.0, float(current[0] or 0) - time.time()) if current else 0.0


def _normalize_path(path):
    value = str(path or "").replace("\\", "/").strip()
    while "//" in value:
        value = value.replace("//", "/")
    return "/" + value.strip("/") if value.strip("/") else ""


def invalidate(folder_ids=(), file_ids=()):
    """Hints after successful NOOR writes; debounced and recoverable on disk."""
    with database() as db:
        folders = {str(f) for f in folder_ids if f}
        for fid in file_ids:
            folders.update(r[0] for r in db.execute("SELECT parent FROM members WHERE id=?", (str(fid),)))
        for folder in folders:
            db.execute("UPDATE directories SET revision=revision+1,dirty=1,due=?,cursor=NULL WHERE id=?", (time.time() + 10, folder))


def invalidate_paths(paths):
    """Map mounted/CloudDrive paths to the longest registered 115 directory."""
    changed_paths = [_normalize_path(path) for path in paths]
    changed_paths = [path for path in changed_paths if path]
    if not changed_paths:
        return []
    with database() as db:
        registered = [(str(row[0]), _normalize_path(row[1])) for row in db.execute("SELECT folder,path FROM directory_paths")]
    matched = set()
    for changed in changed_paths:
        candidates = []
        for folder, path in registered:
            if not path:
                continue
            marker = path.rstrip("/") + "/"
            if changed == path or changed.startswith(marker) or marker in changed:
                candidates.append((len(path), folder))
        if candidates:
            matched.add(max(candidates)[1])
    if matched:
        invalidate(matched)
    return sorted(matched)


def notify_write(path, data):
    # Never turn a successful cloud write into a failure just because a local
    # cache hint could not be persisted. Periodic reconciliation remains active.
    try:
        parents, files = [], []
        if path in {"/open/ufile/copy", "/open/folder/add"}:
            parents = [data.get("pid")]
        elif path == "/open/ufile/delete":
            parents = [data.get("parent_id")]
            files = str(data.get("file_ids") or "").split(",")
        elif path == "/open/ufile/move":
            parents = [data.get("to_cid")]
            files = str(data.get("file_ids") or "").split(",")
        elif path == "/open/ufile/update":
            files = [data.get("file_id")]
        elif path == "upload_completed":
            parents = [data.get("folder_id")]
        if parents or files:
            invalidate(parents, files)
    except Exception:
        log.exception("Could not persist directory change hint")


async def poll_once(client):
    """At most ONE list API call per tick across all consumers/directories."""
    now = time.time()
    with database() as db:
        cooldown = db.execute("SELECT value FROM control WHERE key='cooldown'").fetchone()
        if cooldown and cooldown[0] > now:
            return
        row = db.execute("SELECT d.* FROM directories d WHERE d.due<=? AND EXISTS(SELECT 1 FROM subscriptions s WHERE s.folder=d.id AND s.expires>? AND (s.minutes>0 OR d.dirty=1)) ORDER BY d.due,d.id LIMIT 1", (now, now)).fetchone()
        if not row:
            return
        row = dict(row)
    cursor = json.loads(row["cursor"]) if row["cursor"] else {"offset": 0, "items": [], "revision": row["revision"]}
    try:
        page = await client.list_folder(row["id"], offset=cursor["offset"], limit=500)
        items = page.get("items") or []
        cursor["items"].extend({k: r[k] for k in KEYS if k in r} for r in items)
        cursor["offset"] += len(items)
        if cursor["offset"] > 200000:
            raise ValueError("目录过大，自动校验已退避；请手动刷新")
        with database() as db:
            latest = db.execute("SELECT revision FROM directories WHERE id=?", (row["id"],)).fetchone()
            if not latest or latest[0] != cursor["revision"]:
                db.execute("UPDATE directories SET cursor=NULL,due=? WHERE id=?", (time.time() + 30, row["id"]))
                return  # A concurrent local write invalidates this in-flight scan.
        if not items or cursor["offset"] >= int(page.get("count") or cursor["offset"]):
            observe(row["id"], cursor["items"])
        else:
            with database() as db:
                db.execute("UPDATE directories SET cursor=?,due=? WHERE id=?", (json.dumps(cursor), time.time() + 30, row["id"]))
    except Exception as exc:
        with database() as db:
            failures = row["failures"] + 1
            delay = min(86400, 900 * 2 ** min(failures - 1, 6))
            db.execute("UPDATE directories SET failures=?,error=?,due=?,cursor=NULL WHERE id=?", (failures, str(exc)[:500], time.time() + delay, row["id"]))
            if getattr(exc, "risk_control", False):
                db.execute("INSERT OR REPLACE INTO control VALUES ('cooldown',?)", (time.time() + max(3600, delay),))


async def run(config, client_type):
    while True:
        await asyncio.sleep(30)  # no full scan, no immediate restart burst
        if not (config.get("access_token") or config.get("refresh_token")):
            continue
        try:
            await poll_once(client_type(config))
        except Exception:
            log.exception("Directory cache watcher failed; preserving old snapshots")
