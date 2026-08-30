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


def test_search_intent_scores_canonical_actor_category_and_title_term() -> None:
    backend = _backend()
    identity = backend.actor_identity_key("吉沢明歩")
    profile = {
        "codes": set(), "actor_identities": Counter(), "actors": Counter(),
        "genres": Counter({"人妻": 2}), "tags": Counter(), "studios": Counter(),
        "series": Counter(), "directors": Counter(), "title_traits": Counter(),
        "semantic_terms": Counter(), "actor_category": Counter(), "category_pairs": Counter(),
        "media_count": 20,
    }
    item = {
        "code": "AAA-002", "title": "秘密の人妻ドラマ", "actors": ["吉泽明步"],
        "categories": ["已婚妇女"], "magnets_count": 1, "release_date": "2026-01-01",
    }
    baseline = backend._candidate_score(item, profile, {}, {})
    combination_id = f"combo:actor:{identity}|category:人妻"
    searched = backend._candidate_score(item, profile, {}, {
        "search_intent": {
            "actors": {identity: 1.0}, "categories": {"人妻": 1.0}, "terms": {"秘密": 1.0},
            "combinations": {combination_id: 1.0}, "combination_labels": {combination_id: "吉沢明歩 × 人妻"},
        },
    })

    assert baseline is not None and searched is not None
    assert baseline["categories"][:2] == ["人妻", "剧情"]
    assert searched["score"] > baseline["score"]
    assert searched["score_breakdown"]["search_intent"] >= 1.7
    assert "当前组合搜索方向" in searched["reasons"]
    assert searched["search_intent_matches"] == [{"id": combination_id, "label": "吉沢明歩 × 人妻", "strength": 1.0}]


def test_v29_context_gate_is_reliable_bounded_and_favors_current_alignment() -> None:
    backend = _backend()
    weak = backend._contextual_intent_gate({"event_count": 1, "actors": {"actor:a": 0.35}}, {"event_count": 0})
    strong = backend._contextual_intent_gate(
        {"event_count": 3, "actors": {"actor:a": 1.0}, "categories": {"人妻": 0.8}},
        {"event_count": 2, "actors": {"actor:a": 1.2}, "categories": {"人妻": 1.5}},
    )
    assert 0 < weak["gate"] < strong["gate"] <= 0.24
    assert strong["agreement"] == 1.0

    identity = backend.actor_identity_key("吉沢明歩")
    profile = {
        "codes": set(), "actor_identities": Counter({identity: 5}), "actors": Counter(),
        "genres": Counter({"人妻": 4, "巨乳": 4}), "tags": Counter(), "studios": Counter(),
        "series": Counter(), "directors": Counter(), "title_traits": Counter(),
        "semantic_terms": Counter(), "actor_category": Counter(), "category_pairs": Counter(),
        "media_count": 30,
    }
    common = {"actors": ["吉泽明步"], "magnets_count": 1, "release_date": "2026-01-01"}
    feedback = {"context_gate": strong, "search_intent": {"categories": {"人妻": 1.5}}}
    unaligned = backend._candidate_score({**common, "code": "AAA-001", "title": "巨乳作品", "categories": ["巨乳"]}, profile, {}, feedback)
    aligned = backend._candidate_score({**common, "code": "AAA-002", "title": "人妻作品", "categories": ["人妻"]}, profile, {}, feedback)
    assert unaligned is not None and aligned is not None
    assert unaligned["context_mixture"]["penalty"] > aligned["context_mixture"]["penalty"]
    assert aligned["score"] > unaligned["score"]


def test_v32_exposure_fatigue_rotates_recovers_and_respects_engagement(monkeypatch) -> None:
    backend = _backend()
    assert backend.RECOMMENDATION_ALGORITHM_VERSION == 34
    assert backend.PERSONALIZED_MODEL_VERSION == "personal-v32"
    hour = 3_600_000
    day = 24 * hour
    now = 1_800_000_000_000
    store = {"exposures": {
        "AAA-001": {"batch_count": 2, "first_seen_at": now - hour, "last_seen_at": now, "last_rank": 5, "impression_history": [{"at": now - hour, "rank": 5}, {"at": now, "rank": 5}]},
        "AAA-002": {"batch_count": 5, "first_seen_at": now - 8 * day, "last_seen_at": now - 4 * day, "last_rank": 8, "impression_history": [{"at": now - 8 * day, "rank": 8}, {"at": now - 4 * day, "rank": 8}]},
        "AAA-003": {"batch_count": 2, "first_seen_at": now - hour, "last_seen_at": now, "last_rank": 5, "conversion_value": 0.75, "impression_history": [{"at": now, "rank": 5}]},
        "AAA-004": {"batch_count": 2, "first_seen_at": now - hour, "last_seen_at": now, "last_rank": 5, "conversion_value": 0.15, "impression_history": [{"at": now - hour, "rank": 5}, {"at": now, "rank": 5}]},
    }}
    fresh = backend._exposure_fatigue(store, now_ms=now)
    recovered = backend._exposure_fatigue(store, now_ms=now + 40 * day)
    assert fresh["AAA-001"]["short"] > 0 and fresh["AAA-001"]["daily"] > 0
    assert fresh["AAA-002"]["long"] > 0
    assert "AAA-003" not in fresh
    assert fresh["AAA-004"]["total"] < fresh["AAA-001"]["total"]
    assert recovered["AAA-002"]["long"] < fresh["AAA-002"]["long"] / 4

    monkeypatch.setattr(backend, "_now_ms", lambda: now)
    recorded = {"exposures": {}, "exposure_batches": []}
    item = {"code": "AAA-010", "rank": 3, "model_version": "v32", "score": 10}
    assert backend._record_exposure_batch(recorded, "batch:1", [item]) == 1
    assert backend._record_exposure_batch(recorded, "batch:1", [item]) == 0
    assert recorded["exposures"]["AAA-010"]["impression_history"] == [{"at": now, "rank": 3, "batch_id": "batch:1"}]


def test_v32_shadow_ranking_compares_same_pool_and_records_both_models(monkeypatch) -> None:
    backend = _backend()
    items = [
        {"code": "AAA-001", "score": 20, "ranking_scores": {backend.PERSONALIZED_MODEL_VERSION: 30, backend.STABLE_MODEL_VERSION: 10}},
        {"code": "AAA-002", "score": 20, "ranking_scores": {backend.PERSONALIZED_MODEL_VERSION: 10, backend.STABLE_MODEL_VERSION: 30}},
    ]
    ranks = backend._shadow_rank_map(items)
    assert ranks["AAA-001"][backend.PERSONALIZED_MODEL_VERSION] == 1
    assert ranks["AAA-001"][backend.STABLE_MODEL_VERSION] == 2
    assert ranks["AAA-002"][backend.PERSONALIZED_MODEL_VERSION] == 2
    assert ranks["AAA-002"][backend.STABLE_MODEL_VERSION] == 1

    monkeypatch.setattr(backend, "_now_ms", lambda: 1_800_000_000_000)
    store = {"exposures": {}, "exposure_batches": []}
    assert backend._record_exposure_batch(store, "shadow:1", [{
        "code": "AAA-001", "rank": 1, "model_version": backend.PERSONALIZED_MODEL_VERSION,
        "score": 30, "shadow_ranks": ranks["AAA-001"],
    }]) == 1
    shadow_models = store["exposures"]["AAA-001"]["shadow_models"]
    assert shadow_models[backend.PERSONALIZED_MODEL_VERSION]["last_rank"] == 1
    assert shadow_models[backend.STABLE_MODEL_VERSION]["last_rank"] == 2


def test_v32_shadow_evaluation_promotes_only_with_confident_shared_outcomes() -> None:
    backend = _backend()

    def store(personal_rank: int, stable_rank: int, count: int) -> dict:
        return {"exposures": {
            f"AAA-{index:03d}": {
                "conversion_value": 1.0,
                "shadow_models": {
                    backend.PERSONALIZED_MODEL_VERSION: {"last_rank": personal_rank},
                    backend.STABLE_MODEL_VERSION: {"last_rank": stable_rank},
                },
            }
            for index in range(count)
        }}

    assert backend._shadow_model_evaluation(store(1, 10, 10))["recommended_policy"] == "collecting"
    personal = backend._shadow_model_evaluation(store(1, 10, 20))
    stable = backend._shadow_model_evaluation(store(10, 1, 20))
    assert personal["recommended_policy"] == "personal"
    assert stable["recommended_policy"] == "stable"
    assert backend._select_ranking_model({}, store(10, 1, 20))["version"] == backend.STABLE_MODEL_VERSION


def test_mdc_ng_actor_aliases_share_one_recommendation_identity() -> None:
    backend = _backend()
    assert backend.actor_identity_key("吉泽明步") == backend.actor_identity_key("吉沢明歩")


def test_coverage_repair_queue_is_bounded_actionable_and_cooled_down(monkeypatch) -> None:
    backend = _backend()
    backend._profile_enrichment_pending = {}
    backend._profile_enrichment_attempts = {}
    backend._profile_enrichment_task = None
    backend._profile_enrichment_state = {
        "status": "idle", "queued": 0, "coverage_queued": 0,
        "coverage_enriched": 0, "coverage_failed": 0, "coverage_last_queued_at": 0.0,
    }
    monkeypatch.setattr(backend, "_pool", lambda: {"items": {}})

    class DoneTask:
        def done(self):
            return False

    def fake_create_task(coro):
        coro.close()
        return DoneTask()

    monkeypatch.setattr(backend.asyncio, "create_task", fake_create_task)
    rows = [
        {"code": "AAA-001", "profile_gaps": ["actors"]},
        {"code": "AAA-002", "profile_gaps": ["categories"]},
        {"code": "AAA-003", "profile_gaps": ["title"]},
        {"code": "AAA-004", "profile_gaps": ["maker"]},
    ]

    assert backend._queue_profile_enrichment({}, rows, max_accept=2, reason="offline_no_path") == 2
    assert list(backend._profile_enrichment_pending) == ["AAA-001", "AAA-002"]
    assert backend._profile_enrichment_state["coverage_queued"] == 2
    assert backend._queue_profile_enrichment({}, rows, max_accept=2, reason="offline_no_path") == 0
