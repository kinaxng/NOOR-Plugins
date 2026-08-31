from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, create_engine, inspect, select, text
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
    pipeline_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StrmRecord(Base):
    __tablename__ = "cloud115_strm_records"

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    local_path: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(String(32), default="created")
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
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


_db_path = plugin_data_path("115", "state.db")
_db_path.parent.mkdir(parents=True, exist_ok=True)
_engine = create_engine(f"sqlite:///{_db_path}", connect_args={"timeout": 15}, pool_pre_ping=True)
Session = sessionmaker(bind=_engine, expire_on_commit=False)
_mediainfo_claim_lock = threading.Lock()


def init_storage() -> None:
    Base.metadata.create_all(_engine)
    with _engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("cloud115_offline_tasks")}
        if "result_file_id" not in columns:
            connection.execute(text("ALTER TABLE cloud115_offline_tasks ADD COLUMN result_file_id VARCHAR(64) DEFAULT ''"))
        if "pipeline_status" not in columns:
            connection.execute(text("ALTER TABLE cloud115_offline_tasks ADD COLUMN pipeline_status VARCHAR(32) DEFAULT 'pending'"))


def source_identity(url: str) -> tuple[str, str, str]:
    normalized = str(url or "").strip()
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    lower = normalized.lower()
    if lower.startswith("magnet:?"):
        kind = "magnet"
        marker = next((part[3:] for part in normalized.split("&") if part.lower().startswith("xt=") and "btih:" in part.lower()), "")
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
        return session.scalar(select(OfflineTask).where(OfflineTask.source_digest == source_digest))


def get_task(task_id: str) -> OfflineTask | None:
    init_storage()
    with Session() as session:
        return session.scalar(select(OfflineTask).where((OfflineTask.id == str(task_id)) | (OfflineTask.info_hash == str(task_id))))


def create_task(*, info_hash: str, source_digest: str, source_kind: str, source_hint: str, name: str, target_directory_id: str, context: dict[str, Any]) -> OfflineTask:
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
        task = session.scalar(select(OfflineTask).where(OfflineTask.info_hash == info_hash))
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
            statement = statement.where(OfflineTask.status.in_(("queued", "downloading")))
        return list(session.scalars(statement.order_by(OfflineTask.created_at.desc()).limit(max(1, min(limit, 500)))).all())


def task_dict(task: OfflineTask) -> dict[str, Any]:
    return {
        "id": task.id, "info_hash": task.info_hash, "source_kind": task.source_kind,
        "source_hint": task.source_hint, "name": task.name, "target_directory_id": task.target_directory_id,
        "status": task.status, "progress": task.progress, "error": task.error_message,
        "noor_job_id": task.noor_job_id, "detected_file_ids": list(task.detected_file_ids or []),
        "result_file_id": task.result_file_id,
        "pipeline_status": task.pipeline_status,
        "created_at": task.created_at.isoformat() if task.created_at else "",
        "updated_at": task.updated_at.isoformat() if task.updated_at else "",
        "completed_at": task.completed_at.isoformat() if task.completed_at else "",
    }


def upsert_media(item: dict[str, Any], *, task_id: str, display_path: str = "") -> tuple[MediaFile, bool]:
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
        return list(session.scalars(select(MediaFile).order_by(MediaFile.created_at.desc()).limit(max(1, min(limit, 1000)))).all())


def save_strm(file_id: str, local_path: str) -> StrmRecord:
    init_storage()
    with Session.begin() as session:
        record = session.get(StrmRecord, file_id)
        if record is None:
            record = StrmRecord(file_id=file_id, local_path=local_path)
            session.add(record)
        else:
            record.local_path = local_path
            record.status = "created"
            record.updated_at = utcnow()
        return record


def get_strm(file_id: str) -> StrmRecord | None:
    init_storage()
    with Session() as session:
        return session.get(StrmRecord, str(file_id))


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
        "strm_status": record.status if record else "pending",
        "strm_path": record.local_path if record else "",
        "mediainfo_status": info.status if info else "pending",
        "emby_status": "pending",
    }


def queue_mediainfo(media: MediaFile, *, schema_version: int = 1) -> tuple[MediaInfoRecord, bool]:
    init_storage()
    with Session.begin() as session:
        record = session.get(MediaInfoRecord, media.file_id)
        identity_matches = bool(record and record.sha1 == media.sha1 and record.size == media.size and record.schema_version == schema_version)
        if record and identity_matches and record.status in {"queued", "running", "ready"}:
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


def next_mediainfo() -> MediaInfoRecord | None:
    init_storage()
    now = utcnow()
    with _mediainfo_claim_lock:
        with Session.begin() as session:
            record = session.scalar(
                select(MediaInfoRecord)
                .where(MediaInfoRecord.status.in_(("queued", "retry")))
                .where((MediaInfoRecord.next_retry_at.is_(None)) | (MediaInfoRecord.next_retry_at <= now))
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
        records = list(session.scalars(select(MediaInfoRecord).where(MediaInfoRecord.status == "running")).all())
        for record in records:
            record.status = "retry"
            record.next_retry_at = utcnow()
            record.error_message = "worker interrupted before completion"
            record.updated_at = utcnow()
        return len(records)


def emit_event(event_type: str, *, file_id: str = "", payload: dict[str, Any] | None = None, dedupe_key: str = "") -> PipelineEvent:
    init_storage()
    with Session.begin() as session:
        stable_key = str(dedupe_key or f"{event_type}:{file_id}")
        existing = session.scalar(select(PipelineEvent).where(PipelineEvent.dedupe_key == stable_key))
        if existing:
            return existing
        event = PipelineEvent(id=str(uuid.uuid4()), event_type=str(event_type), file_id=str(file_id), dedupe_key=stable_key, payload=dict(payload or {}))
        session.add(event)
        return event


def list_events(*, pending_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
    init_storage()
    with Session() as session:
        statement = select(PipelineEvent)
        if pending_only:
            statement = statement.where(PipelineEvent.status == "pending")
        rows = list(session.scalars(statement.order_by(PipelineEvent.created_at.asc()).limit(max(1, min(limit, 500)))).all())
        return [{"id": row.id, "type": row.event_type, "file_id": row.file_id, "payload": dict(row.payload or {}),
            "status": row.status, "created_at": row.created_at.isoformat() if row.created_at else ""} for row in rows]


def acknowledge_event(event_id: str) -> bool:
    init_storage()
    with Session.begin() as session:
        event = session.get(PipelineEvent, str(event_id))
        if not event:
            return False
        event.status = "acknowledged"
        event.acknowledged_at = utcnow()
        return True
