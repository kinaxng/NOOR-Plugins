from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path


def _backend():
    path = Path(__file__).parents[1] / "backend.py"
    spec = importlib.util.spec_from_file_location("subscription_core_preference_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_create_subscription_records_idempotent_core_stage(monkeypatch) -> None:
    backend = _backend()
    store = {"version": 1, "subscriptions": [], "events": []}
    recorded: list[tuple[str, str]] = []

    async def no_media(*_args, **_kwargs):
        return None

    async def enqueue(*_args, **_kwargs):
        return 1

    async def record(sub: dict, event_type: str, payload: dict | None = None):
        recorded.append((event_type, str((payload or {}).get("evidence_id") or "")))

    monkeypatch.setattr(backend, "_ensure_store", lambda: store)
    monkeypatch.setattr(backend, "_save", lambda _data: None)
    monkeypatch.setattr(backend, "_find_media", no_media)
    monkeypatch.setattr(backend, "_schedule_immediate_check", lambda *_args: None)
    monkeypatch.setattr(backend, "enqueue_resource_refresh", enqueue)
    monkeypatch.setattr(backend, "_record_core_outcome", record)

    first = asyncio.run(backend._create_subscription({}, {"code": "PRED-878", "title": "PRED-878"}))
    second = asyncio.run(backend._create_subscription({}, {"code": "PRED-878", "title": "PRED-878"}))

    assert first["created"] is True and second["created"] is False
    assert recorded[0][0] == "subscription"
    assert recorded[0][1].endswith(":subscription")
    assert recorded[1] == recorded[0]
