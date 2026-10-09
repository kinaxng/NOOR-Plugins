from __future__ import annotations

import hashlib
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
    create_engine,
    delete,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.core.runtime_paths import plugin_data_path


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class OfflineTask(Base):
    __tablename__ = "cloud115_offline_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    info_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_kind: Mapped[str] = mapped_column(String(16))
    source_hint: Mapped[str] = mapped_column(String(256), default="")
    name: Mapped[str] = mapped_column(String(512), default="")
    target_directory_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    noor_job_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    detected_file_ids: Mapped[list] = mapped_column(JSON, default=list)
    result_file_id: Mapped[str] = mapped_column(String(64), default="")
    pipeline_status: Mapped[str] = mapped_column(
        String(32), default="pending", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MediaFile(Base):
    __tablename__ = "cloud115_media_files"

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha1: Mapped[str] = mapped_column(String(64), default="", index=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(512))
    parent_id: Mapped[str] = mapped_column(String(64), default="")
    display_path: Mapped[str] = mapped_column(Text, default="")
    pick_code: Mapped[str] = mapped_column(String(128), default="")
    updated_at_remote: Mapped[int] = mapped_column(Integer, default=0)
    source_task_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    organized_path: Mapped[str] = mapped_column(Text, default="")
    organization_status: Mapped[str] = mapped_column(
        String(32), default="pending", index=True
    )
    emby_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    strm_assistant_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    strm_assistant_error: Mapped[str] = mapped_column(Text, default="")
    strm_assistant_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StrmRecord(Base):
    __tablename__ = "cloud115_strm_records"

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    local_path: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(String(32), default="created")
    sha1: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    config_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StrmReference(Base):
    __tablename__ = "cloud115_strm_references"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_type: Mapped[str] = mapped_column(String(32), default="offline_task")
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SubtitleRecord(Base):
    __tablename__ = "cloud115_subtitle_records"

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_file_id: Mapped[str] = mapped_column(String(64), index=True)
    sha1: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    local_path: Mapped[str] = mapped_column(Text, unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ServiceToken(Base):
    __tablename__ = "cloud115_service_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), default="stream")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MediaInfoRecord(Base):
    __tablename__ = "cloud115_mediainfo_records"

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha1: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    error_message: Mapped[str] = mapped_column(Text, default="")
    json_path: Mapped[str] = mapped_column(Text, default="")
    media: Mapped[dict] = mapped_column(JSON, default=dict)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PipelineEvent(Base):
    __tablename__ = "cloud115_pipeline_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    file_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FolderWatchState(Base):
    __tablename__ = "cloud115_folder_watch_states"

    folder_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor_updated_at: Mapped[int] = mapped_column(Integer, default=0)
    cursor_file_ids: Mapped[list] = mapped_column(JSON, default=list)
    initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


_db_path = plugin_data_path("115", "state.db")
_db_path.parent.mkdir(parents=True, exist_ok=True)
_engine = create_engine(
    f"sqlite:///{_db_path}", connect_args={"timeout": 15}, pool_pre_ping=True
)
Session = sessionmaker(bind=_engine, expire_on_commit=False)
_mediainfo_claim_lock = threading.Lock()


def init_storage() -> None:
    Base.metadata.create_all(_engine)
    with _engine.begin() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("cloud115_offline_tasks")
        }
        if "result_file_id" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE cloud115_offline_tasks ADD COLUMN result_file_id VARCHAR(64) DEFAULT ''"
                )
            )
        if "pipeline_status" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE cloud115_offline_tasks ADD COLUMN pipeline_status VARCHAR(32) DEFAULT 'pending'"
                )
            )
        media_columns = {
            column["name"]
            for column in inspect(connection).get_columns("cloud115_media_files")
        }
        for name, definition in {
            "organized_path": "TEXT DEFAULT ''",
            "organization_status": "VARCHAR(32) DEFAULT 'pending'",
            "emby_status": "VARCHAR(32) DEFAULT 'pending'",
            "strm_assistant_status": "VARCHAR(32) DEFAULT 'pending'",
            "strm_assistant_error": "TEXT DEFAULT ''",
            "strm_assistant_synced_at": "DATETIME",
        }.items():
            if name not in media_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE cloud115_media_files ADD COLUMN {name} {definition}"
                    )
                )
        strm_columns = {
            column["name"]
            for column in inspect(connection).get_columns("cloud115_strm_records")
        }
        for name, definition in {
            "sha1": "VARCHAR(64) DEFAULT ''",
            "size": "INTEGER DEFAULT 0",
            "content_hash": "VARCHAR(64) DEFAULT ''",
            "config_fingerprint": "VARCHAR(64) DEFAULT ''",
            "last_error": "TEXT DEFAULT ''",
        }.items():
            if name not in strm_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE cloud115_strm_records ADD COLUMN {name} {definition}"
                    )
                )
        event_columns = {
            column["name"]
            for column in inspect(connection).get_columns("cloud115_pipeline_events")
        }
        if "attempts" not in event_columns:
            connection.execute(
                text(
                    "ALTER TABLE cloud115_pipeline_events ADD COLUMN attempts INTEGER DEFAULT 0"
                )
            )
        if "last_error" not in event_columns:
            connection.execute(
                text(
                    "ALTER TABLE cloud115_pipeline_events ADD COLUMN last_error TEXT DEFAULT ''"
                )
            )
        if "next_attempt_at" not in event_columns:
            connection.execute(
                text(
                    "ALTER TABLE cloud115_pipeline_events ADD COLUMN next_attempt_at DATETIME"
                )
            )


def source_identity(url: str) -> tuple[str, str, str]:
    normalized = str(url or "").strip()
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    lower = normalized.lower()
    if lower.startswith("magnet:?"):
        kind = "magnet"
        marker = next(
            (
                part[3:]
                for part in normalized.split("&")
                if part.lower().startswith("xt=") and "btih:" in part.lower()
            ),
            "",
        )
        hint = marker.rsplit(":", 1)[-1][:16] if marker else digest[:16]
    elif lower.startswith("ed2k://"):
        kind, hint = "ed2k", digest[:16]
    elif lower.startswith(("http://", "https://")):
        kind, hint = "http", normalized.split("?", 1)[0].rsplit("/", 1)[-1][:80]
    else:
        raise ValueError("115 离线下载仅支持 magnet、ed2k、http/https")
    return digest, kind, hint


def find_duplicate(source_digest: str) -> OfflineTask | None:
    init_storage()
    with Session() as session:
        return session.scalar(
            select(OfflineTask).where(OfflineTask.source_digest == source_digest)
        )


def get_task(task_id: str) -> OfflineTask | None:
    init_storage()
    with Session() as session:
        return session.scalar(
            select(OfflineTask).where(
                (OfflineTask.id == str(task_id))
                | (OfflineTask.info_hash == str(task_id))
            )
        )


def create_task(
    *,
    info_hash: str,
    source_digest: str,
    source_kind: str,
    source_hint: str,
    name: str,
    target_directory_id: str,
    context: dict[str, Any],
) -> OfflineTask:
    init_storage()
    task = OfflineTask(
        id=info_hash or source_digest,
        info_hash=info_hash or source_digest,
        source_digest=source_digest,
        source_kind=source_kind,
        source_hint=source_hint,
        name=name,
        target_directory_id=target_directory_id,
        context=context,
    )
    with Session.begin() as session:
        session.add(task)
    return task


def update_task(info_hash: str, **values: Any) -> OfflineTask | None:
    init_storage()
    with Session.begin() as session:
        task = session.scalar(
            select(OfflineTask).where(OfflineTask.info_hash == info_hash)
        )
        if not task:
            return None
        for key, value in values.items():
            if hasattr(task, key):
                setattr(task, key, value)
        task.updated_at = utcnow()
        return task


def list_tasks(*, active_only: bool = False, limit: int = 100) -> list[OfflineTask]:
    init_storage()
    with Session() as session:
        statement = select(OfflineTask)
        if active_only:
            statement = statement.where(
                OfflineTask.status.in_(("queued", "downloading"))
            )
        return list(
            session.scalars(
                statement.order_by(OfflineTask.created_at.desc()).limit(
                    max(1, min(limit, 500))
                )
            ).all()
        )


def list_pending_pipeline_tasks(*, limit: int = 500) -> list[OfflineTask]:
    """Oldest-first pipeline queue; completed rows cannot starve older imports."""
    init_storage()
    with Session() as session:
        return list(
            session.scalars(
                select(OfflineTask)
                .where(OfflineTask.status == "completed")
                .where(OfflineTask.pipeline_status != "completed")
                .order_by(OfflineTask.created_at.asc())
                .limit(max(1, min(int(limit), 500)))
            ).all()
        )


def get_folder_watch_state(folder_id: str) -> FolderWatchState | None:
    init_storage()
    with Session() as session:
        return session.get(FolderWatchState, str(folder_id))


def save_folder_watch_state(
    folder_id: str, *, cursor_updated_at: int, cursor_file_ids: list[str]
) -> FolderWatchState:
    init_storage()
    with Session.begin() as session:
        state = session.get(FolderWatchState, str(folder_id))
        if state is None:
            state = FolderWatchState(folder_id=str(folder_id))
            session.add(state)
        state.cursor_updated_at = max(0, int(cursor_updated_at or 0))
        state.cursor_file_ids = list(
            dict.fromkeys(str(value) for value in cursor_file_ids if value)
        )
        state.initialized = True
        state.updated_at = utcnow()
        return state


def task_dict(task: OfflineTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "info_hash": task.info_hash,
        "source_kind": task.source_kind,
        "source_hint": task.source_hint,
        "name": task.name,
        "target_directory_id": task.target_directory_id,
        "status": task.status,
        "progress": task.progress,
        "error": task.error_message,
        "noor_job_id": task.noor_job_id,
        "detected_file_ids": list(task.detected_file_ids or []),
        "result_file_id": task.result_file_id,
        "pipeline_status": task.pipeline_status,
        "created_at": task.created_at.isoformat() if task.created_at else "",
        "updated_at": task.updated_at.isoformat() if task.updated_at else "",
        "completed_at": task.completed_at.isoformat() if task.completed_at else "",
    }


def upsert_media(
    item: dict[str, Any], *, task_id: str, display_path: str = ""
) -> tuple[MediaFile, bool]:
    init_storage()
    file_id = str(item.get("file_id") or "")
    if not file_id:
        raise ValueError("115 media is missing file_id")
    with Session.begin() as session:
        media = session.get(MediaFile, file_id)
        created = media is None
        if media is None:
            media = MediaFile(file_id=file_id, name=str(item.get("name") or ""))
            session.add(media)
        media.sha1 = str(item.get("sha1") or "").upper()
        media.size = int(item.get("size") or 0)
        media.name = str(item.get("name") or media.name)
        media.parent_id = str(item.get("parent_id") or "")
        media.display_path = display_path or media.display_path
        media.pick_code = str(item.get("pick_code") or media.pick_code)
        media.updated_at_remote = int(item.get("updated_at") or 0)
        media.source_task_id = task_id or media.source_task_id
        media.updated_at = utcnow()
        session.flush()
        return media, created


def get_media(file_id: str) -> MediaFile | None:
    init_storage()
    with Session() as session:
        return session.get(MediaFile, str(file_id))


def list_media(limit: int = 200) -> list[MediaFile]:
    init_storage()
    with Session() as session:
        return list(
            session.scalars(
                select(MediaFile)
                .order_by(MediaFile.created_at.desc())
                .limit(max(1, min(limit, 1000)))
            ).all()
        )


def mark_media_organized(
    file_id: str, *, local_path: str = "", server_path: str = "", emby_ok: bool = False
) -> MediaFile | None:
    init_storage()
    with Session.begin() as session:
        media = session.get(MediaFile, str(file_id))
        if media is None:
            return None
        media.organized_path = str(local_path or server_path or "")
        media.organization_status = "completed"
        media.emby_status = "notified" if emby_ok else "failed"
        media.updated_at = utcnow()
        return media


def mark_strm_assistant(
    file_id: str, *, status: str, error: str = ""
) -> MediaFile | None:
    init_storage()
    with Session.begin() as session:
        media = session.get(MediaFile, str(file_id))
        if media is None:
            return None
        media.strm_assistant_status = str(status or "pending")
        media.strm_assistant_error = str(error or "")[:1000]
        media.strm_assistant_synced_at = utcnow() if status == "ready" else None
        media.updated_at = utcnow()
        return media


def save_strm(
    file_id: str,
    local_path: str,
    *,
    sha1: str = "",
    size: int = 0,
    content_hash: str = "",
    config_fingerprint: str = "",
    owner_id: str = "",
) -> StrmRecord:
    init_storage()
    with Session.begin() as session:
        record = session.get(StrmRecord, file_id)
        if record is None:
            record = StrmRecord(file_id=file_id, local_path=local_path)
            session.add(record)
        else:
            record.local_path = local_path
        record.status = "ready"
        record.sha1 = str(sha1 or "").upper()
        record.size = int(size or 0)
        record.content_hash = str(content_hash or "")
        record.config_fingerprint = str(config_fingerprint or "")
        record.last_error = ""
        record.updated_at = utcnow()
        if owner_id:
            reference_id = hashlib.sha256(
                f"offline_task:{owner_id}:{file_id}".encode()
            ).hexdigest()[:36]
            if session.get(StrmReference, reference_id) is None:
                session.add(
                    StrmReference(
                        id=reference_id, file_id=file_id, owner_id=str(owner_id)
                    )
                )
        return record


def get_strm(file_id: str) -> StrmRecord | None:
    init_storage()
    with Session() as session:
        return session.get(StrmRecord, str(file_id))


def get_strm_by_local_path(local_path: str) -> StrmRecord | None:
    init_storage()
    with Session() as session:
        return session.scalar(
            select(StrmRecord).where(StrmRecord.local_path == str(local_path)).limit(1)
        )


def save_subtitle(
    file_id: str,
    media_file_id: str,
    local_path: str,
    *,
    sha1: str = "",
    size: int = 0,
    content_hash: str = "",
) -> SubtitleRecord:
    init_storage()
    with Session.begin() as session:
        record = session.get(SubtitleRecord, str(file_id))
        if record is None:
            record = SubtitleRecord(
                file_id=str(file_id),
                media_file_id=str(media_file_id),
                local_path=str(local_path),
            )
            session.add(record)
        record.media_file_id = str(media_file_id)
        record.local_path = str(local_path)
        record.sha1 = str(sha1 or "").upper()
        record.size = int(size or 0)
        record.content_hash = str(content_hash or "")
        record.status = "ready"
        record.updated_at = utcnow()
        return record


def get_subtitle(file_id: str) -> SubtitleRecord | None:
    init_storage()
    with Session() as session:
        return session.get(SubtitleRecord, str(file_id))


def _managed_output(
    path_value: str, output_root: str, extensions: set[str]
) -> Path | None:
    candidate = Path(str(path_value or "")).resolve()
    root = Path(str(output_root or "")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate == root or candidate.suffix.lower() not in extensions:
        return None
    return candidate


def reconcile_strm_references(
    owner_id: str, active_file_ids: set[str], *, output_root: str
) -> dict[str, int]:
    """Detach stale task ownership and delete only unshared, managed outputs.

    The caller must invoke this only after an authoritative, complete discovery.
    Failed deletions retain their reference so a later complete run can retry.
    """
    init_storage()
    active = {str(value) for value in active_file_ids}
    with Session() as session:
        stale = list(
            session.scalars(
                select(StrmReference).where(
                    StrmReference.owner_type == "offline_task",
                    StrmReference.owner_id == str(owner_id),
                    StrmReference.file_id.not_in(active),
                )
            ).all()
        )
    result = {
        "stale": len(stale),
        "detached": 0,
        "shared": 0,
        "deleted": 0,
        "missing": 0,
        "failed": 0,
    }
    for reference in stale:
        with Session() as session:
            other_reference = session.scalar(
                select(StrmReference)
                .where(
                    StrmReference.file_id == reference.file_id,
                    StrmReference.id != reference.id,
                )
                .limit(1)
            )
            record = session.get(StrmRecord, reference.file_id)
            subtitles = list(
                session.scalars(
                    select(SubtitleRecord).where(
                        SubtitleRecord.media_file_id == reference.file_id
                    )
                ).all()
            )
        if other_reference:
            with Session.begin() as session:
                session.execute(
                    delete(StrmReference).where(StrmReference.id == reference.id)
                )
            result["shared"] += 1
            continue
        managed_strm = (
            _managed_output(record.local_path, output_root, {".strm"})
            if record
            else None
        )
        managed_subtitles = [
            _managed_output(
                row.local_path, output_root, {".srt", ".ass", ".ssa", ".sub", ".vtt"}
            )
            for row in subtitles
        ]
        if (record and managed_strm is None) or any(
            path is None for path in managed_subtitles
        ):
            with Session.begin() as session:
                session.execute(
                    delete(StrmReference).where(StrmReference.id == reference.id)
                )
                session.execute(
                    delete(SubtitleRecord).where(
                        SubtitleRecord.media_file_id == reference.file_id
                    )
                )
                session.execute(
                    delete(StrmRecord).where(StrmRecord.file_id == reference.file_id)
                )
            result["detached"] += 1
            continue
        failed = False
        for candidate in [*managed_subtitles, managed_strm]:
            if candidate is None:
                continue
            try:
                os.unlink(candidate)
                result["deleted"] += 1
            except FileNotFoundError:
                result["missing"] += 1
            except OSError:
                failed = True
                result["failed"] += 1
                break
        if failed:
            continue
        with Session.begin() as session:
            session.execute(
                delete(StrmReference).where(StrmReference.id == reference.id)
            )
            session.execute(
                delete(SubtitleRecord).where(
                    SubtitleRecord.media_file_id == reference.file_id
                )
            )
            session.execute(
                delete(StrmRecord).where(StrmRecord.file_id == reference.file_id)
            )
    return result


def register_service_token(token_hash: str) -> None:
    init_storage()
    with Session.begin() as session:
        if session.get(ServiceToken, token_hash) is None:
            session.add(ServiceToken(token_hash=token_hash))


def service_token_valid(token_hash: str) -> bool:
    init_storage()
    with Session() as session:
        token = session.get(ServiceToken, token_hash)
        return bool(token and token.scope == "stream" and not token.revoked)


def revoke_other_service_tokens(active_token_hash: str) -> None:
    init_storage()
    with Session.begin() as session:
        for token in session.scalars(select(ServiceToken)).all():
            token.revoked = token.token_hash != active_token_hash


def list_strm_records() -> list[StrmRecord]:
    init_storage()
    with Session() as session:
        return list(session.scalars(select(StrmRecord)).all())


def media_dict(media: MediaFile) -> dict[str, Any]:
    record = get_strm(media.file_id)
    info = get_mediainfo(media.file_id)
    return {
        "file_id": media.file_id,
        "sha1": media.sha1,
        "size": media.size,
        "name": media.name,
        "parent_id": media.parent_id,
        "path": media.display_path,
        "organized_path": media.organized_path,
        "organization_status": media.organization_status,
        "strm_status": record.status if record else "pending",
        "strm_path": record.local_path if record else "",
        "mediainfo_status": info.status if info else "pending",
        "mediainfo_error": info.error_message if info else "",
        "mediainfo_attempts": int(info.attempts or 0) if info else 0,
        "mediainfo_probed_at": (
            info.probed_at.isoformat() if info and info.probed_at else ""
        ),
        "emby_status": media.emby_status,
        "strm_assistant_status": media.strm_assistant_status,
        "strm_assistant_error": media.strm_assistant_error,
        "strm_assistant_synced_at": (
            media.strm_assistant_synced_at.isoformat()
            if media.strm_assistant_synced_at else ""
        ),
    }


def queue_mediainfo(
    media: MediaFile, *, schema_version: int = 1
) -> tuple[MediaInfoRecord, bool]:
    init_storage()
    with Session.begin() as session:
        record = session.get(MediaInfoRecord, media.file_id)
        identity_matches = bool(
            record
            and record.sha1 == media.sha1
            and record.size == media.size
            and record.schema_version == schema_version
        )
        if (
            record
            and identity_matches
            and record.status in {"queued", "running", "ready"}
        ):
            return record, False
        if record is None:
            record = MediaInfoRecord(file_id=media.file_id)
            session.add(record)
        record.sha1 = media.sha1
        record.size = media.size
        record.schema_version = schema_version
        record.status = "queued"
        record.attempts = 0
        record.next_retry_at = None
        record.error_message = ""
        record.media = {}
        record.json_path = ""
        record.updated_at = utcnow()
        return record, True


def get_mediainfo(file_id: str) -> MediaInfoRecord | None:
    init_storage()
    with Session() as session:
        return session.get(MediaInfoRecord, str(file_id))


def mediainfo_dict(record: MediaInfoRecord) -> dict[str, Any]:
    return {
        "provider": "115",
        "file_id": record.file_id,
        "sha1": record.sha1,
        "size": record.size,
        "schema_version": record.schema_version,
        "status": record.status,
        "attempts": record.attempts,
        "error": record.error_message,
        "probed_at": record.probed_at.isoformat() if record.probed_at else "",
        "media": dict(record.media or {}) if record.status == "ready" else {},
    }


def next_mediainfo() -> MediaInfoRecord | None:
    init_storage()
    now = utcnow()
    with _mediainfo_claim_lock:
        with Session.begin() as session:
            record = session.scalar(
                select(MediaInfoRecord)
                .where(MediaInfoRecord.status.in_(("queued", "retry")))
                .where(
                    (MediaInfoRecord.next_retry_at.is_(None))
                    | (MediaInfoRecord.next_retry_at <= now)
                )
                .order_by(MediaInfoRecord.created_at.asc())
                .limit(1)
            )
            if record:
                record.status = "running"
                record.updated_at = now
            return record


def update_mediainfo(file_id: str, **values: Any) -> MediaInfoRecord | None:
    init_storage()
    with Session.begin() as session:
        record = session.get(MediaInfoRecord, str(file_id))
        if not record:
            return None
        for key, value in values.items():
            if hasattr(record, key):
                setattr(record, key, value)
        record.updated_at = utcnow()
        return record


def recover_interrupted_mediainfo() -> int:
    init_storage()
    with Session.begin() as session:
        records = list(
            session.scalars(
                select(MediaInfoRecord).where(MediaInfoRecord.status == "running")
            ).all()
        )
        for record in records:
            record.status = "retry"
            record.next_retry_at = utcnow()
            record.error_message = "worker interrupted before completion"
            record.updated_at = utcnow()
        return len(records)


def emit_event(
    event_type: str,
    *,
    file_id: str = "",
    payload: dict[str, Any] | None = None,
    dedupe_key: str = "",
) -> PipelineEvent:
    init_storage()
    with Session.begin() as session:
        stable_key = str(dedupe_key or f"{event_type}:{file_id}")
        existing = session.scalar(
            select(PipelineEvent).where(PipelineEvent.dedupe_key == stable_key)
        )
        if existing:
            return existing
        event = PipelineEvent(
            id=str(uuid.uuid4()),
            event_type=str(event_type),
            file_id=str(file_id),
            dedupe_key=stable_key,
            payload=dict(payload or {}),
        )
        session.add(event)
        return event


def list_events(*, pending_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
    init_storage()
    with Session() as session:
        statement = select(PipelineEvent)
        if pending_only:
            statement = statement.where(PipelineEvent.status == "pending")
        rows = list(
            session.scalars(
                statement.order_by(PipelineEvent.created_at.asc()).limit(
                    max(1, min(limit, 500))
                )
            ).all()
        )
        return [
            {
                "id": row.id,
                "type": row.event_type,
                "file_id": row.file_id,
                "payload": dict(row.payload or {}),
                "status": row.status,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "next_attempt_at": row.next_attempt_at.isoformat()
                if row.next_attempt_at
                else "",
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
            for row in rows
        ]


def acknowledge_event(event_id: str) -> bool:
    init_storage()
    with Session.begin() as session:
        event = session.get(PipelineEvent, str(event_id))
        if not event:
            return False
        event.status = "acknowledged"
        event.acknowledged_at = utcnow()
        return True


def record_event_attempt(event_id: str, error: str, *, delay_seconds: int) -> bool:
    init_storage()
    with Session.begin() as session:
        event = session.get(PipelineEvent, str(event_id))
        if not event:
            return False
        event.attempts = int(event.attempts or 0) + 1
        event.last_error = str(error or "pipeline delivery failed")[:1000]
        event.next_attempt_at = utcnow() + timedelta(
            seconds=max(30, min(delay_seconds, 3600))
        )
        return True
