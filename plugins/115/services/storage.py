from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, JSON, String, Text, create_engine, select
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


_db_path = plugin_data_path("115", "state.db")
_db_path.parent.mkdir(parents=True, exist_ok=True)
_engine = create_engine(f"sqlite:///{_db_path}", connect_args={"timeout": 15}, pool_pre_ping=True)
Session = sessionmaker(bind=_engine, expire_on_commit=False)


def init_storage() -> None:
    Base.metadata.create_all(_engine)


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
        "created_at": task.created_at.isoformat() if task.created_at else "",
        "updated_at": task.updated_at.isoformat() if task.updated_at else "",
        "completed_at": task.completed_at.isoformat() if task.completed_at else "",
    }
