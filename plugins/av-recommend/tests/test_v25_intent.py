from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path


def _backend():
    path = Path(__file__).parents[1] / "backend.py"
    spec = importlib.util.spec_from_file_location("av_recommend_v25", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_session_intent_uses_identity_and_half_life(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(backend, "actor_identity_key", lambda value: "actor:one" if value else "")
    monkeypatch.setattr(backend, "canonical_preference_category", lambda value: "人妻" if value else "")
    now = 1_800_000_000_000
    store = {"session_intents": []}

    assert backend._record_session_intent(
        store,
        {"code": "AAA-001", "actors": ["别名"], "categories": ["已婚妇女"], "interest_topic": {"id": "topic:a"}},
        "detail_view",
        now_ms=now,
    )
    assert not backend._record_session_intent(
        store,
        {"code": "AAA-001", "actors": ["正式名"], "categories": ["人妻"]},
        "detail_view",
        now_ms=now + 30_000,
    )

    fresh = backend._session_intent_summary(store, now_ms=now)
    aged = backend._session_intent_summary(store, now_ms=now + backend.SESSION_INTENT_HALF_LIFE_MS)
    assert fresh["actors"] == {"actor:one": 0.35}
    assert fresh["categories"] == {"人妻": 0.35}
    assert fresh["topics"] == {"topic:a": 0.35}
    assert aged["actors"]["actor:one"] == fresh["actors"]["actor:one"] / 2


def test_topic_feedback_is_counted_without_hard_exclusion() -> None:
    backend = _backend()
    assert backend._feedback_topic_counter([
        {"interest_topic": {"id": "topic:a"}},
        {"interest_topic_hypothesis": {"id": "topic:a"}},
        {"interest_topic": {}},
    ]) == Counter({"topic:a": 2})

