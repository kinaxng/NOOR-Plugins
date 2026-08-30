from __future__ import annotations

import asyncio
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
    actor_factor = next(row for row in aligned["recommendation_explanation"]["factors"] if row["type"] == "actor")
    assert actor_factor["evidence"][0]["identity"] == identity
    assert actor_factor["evidence"][0]["library_count"] == 5
    assert actor_factor["evidence"][0]["name"] == backend.canonical_actor_name("吉泽明步")


def test_v47_missing_structured_actor_uses_conservative_mdc_title_mention(monkeypatch) -> None:
    backend = _backend()
    identity = "mdc-ng:actor-1"
    monkeypatch.setattr(backend, "actor_mentions", lambda value, limit=4: [{
        "name": "吉泽明步", "identity": identity, "alias": "吉沢明歩", "source": "mdc-ng-title",
    }] if "吉沢明歩" in value else [])
    monkeypatch.setattr(backend, "canonical_actor_name", lambda value: "吉泽明步" if value else "")
    monkeypatch.setattr(backend, "actor_identity_key", lambda value: identity if value else "")
    profile = {
        "codes": set(), "actor_identities": Counter({identity: 6}), "actors": Counter(),
        "genres": Counter(), "tags": Counter(), "studios": Counter(), "series": Counter(),
        "directors": Counter(), "title_traits": Counter(), "semantic_terms": Counter(),
        "actor_category": Counter(), "category_pairs": Counter(), "media_count": 20,
    }
    scored = backend._candidate_score({
        "code": "AAA-047", "title": "吉沢明歩 最新作品", "actors": [],
        "magnets_count": 1, "release_date": "2026-01-01",
    }, profile, {}, {})
    assert scored is not None
    assert scored["actors"] == ["吉泽明步"]
    assert scored["actor_inference"]["source"] == "mdc-ng-title"
    assert "MDC-NG 标题识别演员" in scored["reasons"]
    assert scored["score_breakdown"]["actor_preference"] > 0


def test_topic_match_requires_the_labeled_anchor_and_relation() -> None:
    backend = _backend()
    profile = {
        "codes": set(), "actor_identities": Counter(), "actors": Counter(),
        "genres": Counter({"人妻": 3, "出轨": 2}), "tags": Counter(), "studios": Counter(),
        "series": Counter(), "directors": Counter(), "title_traits": Counter(),
        "semantic_terms": Counter(), "actor_category": Counter(), "category_pairs": Counter(),
        "media_count": 3,
    }
    scored = backend._candidate_score(
        {"code": "TOPIC-002", "title": "人妻出轨作品", "categories": ["人妻", "出轨"], "magnets_count": 1},
        profile,
        {},
        {"interest_topics": [{
            "id": "topic-2", "label": "剧情 · 中出", "anchor": "剧情",
            "relation_type": "category_pair", "relation_category": "中出",
            "categories": ["剧情", "中出", "人妻", "出轨"], "actors": [], "support": 20,
            "strength": 0.4, "recent_strength": 0.4, "momentum": 0.0, "confidence": 0.8,
        }]},
    )

    assert scored is not None
    assert scored["score_breakdown"]["interest_topic"] == 0
    assert not scored["interest_topic"]


def test_v32_exposure_fatigue_rotates_recovers_and_respects_engagement(monkeypatch) -> None:
    backend = _backend()
    assert backend.RECOMMENDATION_ALGORITHM_VERSION == 68
    assert backend.RANKING_POLICY_VERSION == 57
    assert backend.PERSONALIZED_MODEL_VERSION == "personal-v57"
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


def test_v41_local_diversity_separates_mdc_ng_aliases_and_reports_adjacency() -> None:
    backend = _backend()
    rows = [
        {"code": "AAA-001", "score": 90, "personalized_score": 40, "actors": ["吉泽明步"], "categories": ["人妻"]},
        {"code": "AAA-002", "score": 89, "personalized_score": 40, "actors": ["吉沢明歩"], "categories": ["人妻"]},
        {"code": "AAA-003", "score": 87, "personalized_score": 36, "actors": ["葵つかさ"], "categories": ["ドラマ"]},
    ]
    ranked = backend._diversify_recommendations(rows)
    assert [row["code"] for row in ranked[:2]] == ["AAA-001", "AAA-003"]
    metrics = backend._recommendation_diversity_metrics(ranked)
    assert metrics["adjacent"]["actor_repeats"]["count"] == 0
    assert metrics["adjacent"]["actor_identity_source"] == "mdc-ng"
    assert ranked[1]["diversity_adjustment"]["adjacent"] == 0


def test_v41_transition_model_collapses_funnel_stages_and_waits_for_support() -> None:
    backend = _backend()
    now = 1_800_000_000_000
    actor_a = backend.actor_identity_key("吉泽明步")
    actor_b = backend.actor_identity_key("葵つかさ")

    def row(code: str, offset: int, actor: str, category: str, event_type: str = "detail_view") -> dict:
        return {"code": code, "created_at": now - (70 - offset) * 60_000, "weight": 1, "event_type": event_type, "actors": [actor], "categories": [category]}

    sparse = {"session_intents": [row("A-1", 0, actor_a, "人妻"), row("B-1", 10, actor_b, "剧情"), row("A-2", 20, actor_a, "人妻")]}
    assert backend._session_transition_model(sparse, now_ms=now)["status"] == "collecting"

    store = {"session_intents": [
        row("A-1", 0, actor_a, "人妻"),
        row("A-1", 1, actor_a, "人妻", "download_intent"),
        row("B-1", 10, actor_b, "剧情"),
        row("A-2", 20, actor_a, "人妻"),
        row("B-2", 30, actor_b, "剧情"),
        row("A-3", 40, actor_a, "人妻"),
        row("C-1", 50, backend.actor_identity_key("波多野结衣"), "巨乳"),
        row("A-4", 60, actor_a, "人妻"),
    ]}
    model = backend._session_transition_model(store, now_ms=now)
    assert model["status"] == "active"
    assert model["event_count"] == 7
    assert model["evidence"][f"actor:{actor_b}"]["support"] == 2
    assert model["predictions"][f"actor:{actor_b}"] > model["predictions"].get("category:巨乳", 0)


def test_v42_javdb_candidate_sources_run_in_parallel(monkeypatch) -> None:
    backend = _backend()
    from app.plugins.runtime import runtime

    active = 0
    maximum_active = 0
    source_calls: list[str] = []

    async def fake_handle_action(plugin_id: str, action: str, payload: dict) -> dict:
        nonlocal active, maximum_active
        assert plugin_id == "javdb"
        if action == "video":
            return {"data": {}}
        source_calls.append(f"{action}:{payload.get('period') or 'latest'}")
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"items": []}

    monkeypatch.setattr(runtime, "is_enabled", lambda plugin_id: plugin_id == "javdb")
    monkeypatch.setattr(runtime, "handle_action", fake_handle_action)
    items, warnings = asyncio.run(backend._javdb_candidates({"candidate_limit": 12, "detail_limit": 0}))
    assert items == [] and warnings == []
    assert len(source_calls) == 4
    assert maximum_active == 4


def test_resource_timeout_schedules_queue_write_without_waiting(monkeypatch) -> None:
    backend = _backend()
    from app.knowledge import intelligence
    from app.plugins.runtime import runtime
    enqueue_started = asyncio.Event()
    release_enqueue = asyncio.Event()

    async def slow_search(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Simulate a database/driver call that takes time to unwind after
            # cancellation. The HTTP budget must not wait for this cleanup.
            await asyncio.sleep(0.4)
            raise

    async def slow_enqueue(_codes, *, priority):
        assert priority == 20
        enqueue_started.set()
        await release_enqueue.wait()
        return 1

    monkeypatch.setattr(runtime, "search_resources", slow_search)
    monkeypatch.setattr(intelligence, "enqueue_resource_refresh", slow_enqueue)

    async def scenario() -> None:
        started = asyncio.get_running_loop().time()
        warnings = await backend._enrich_recommendation_resources(
            {"resource_enrich_limit": 1, "resource_enrich_budget_seconds": 1},
            [{"code": "AAA-001"}],
        )
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.wait_for(enqueue_started.wait(), timeout=0.1)
        assert warnings == ["正在后台补全 1 部作品的资源情报"]
        assert elapsed < 1.25
        assert backend._resource_enqueue_tasks
        release_enqueue.set()
        await asyncio.gather(*list(backend._resource_enqueue_tasks))

    asyncio.run(scenario())


def test_conversion_uses_the_acted_card_attribution_snapshot() -> None:
    backend = _backend()
    store = {"exposures": {"AAA-001": {
        "last_routes": ["javdb-feed"], "last_rank": 22, "last_model": "old-model",
        "last_strategy": "ranking", "last_topic_ids": ["old-topic"],
    }}}

    converted = backend._mark_exposure_converted(store, "AAA-001", "subscription", {
        "recall_sources": ["core-neighbor", "core-graph"],
        "recommendation_rank": 4,
        "model_version": "personal-current",
        "is_exploration": True,
        "interest_topic": {"id": "topic-current"},
    })

    row = store["exposures"]["AAA-001"]
    assert converted is True
    assert row["converted_routes"] == ["core-neighbor", "core-graph"]
    assert row["converted_rank"] == 4
    assert row["converted_model"] == "personal-current"
    assert row["converted_strategy"] == "exploration"
    assert row["converted_topic_ids"] == ["topic-current"]
    assert row["conversion_history"][-1]["routes"] == row["converted_routes"]
    assert backend._mark_exposure_converted(store, "AAA-001", "library_imported") is True
    assert row["converted_routes"] == ["core-neighbor", "core-graph"]
    assert row["converted_rank"] == 4
    assert row["converted_topic_ids"] == ["topic-current"]


def test_route_evaluation_reports_funnel_stages_without_calling_views_mature() -> None:
    backend = _backend()
    now = 1_800_000_000_000
    route = {"core-neighbor": {"batch_count": 1}}
    store = {"exposures": {
        "AAA-001": {"first_seen_at": now, "conversion_value": 0.15, "conversion_stage": "detail_view", "routes": route, "converted_routes": ["core-neighbor"]},
        "AAA-002": {"first_seen_at": now, "conversion_value": 0.60, "conversion_stage": "subscription", "routes": route, "converted_routes": ["core-neighbor"]},
        "AAA-003": {"first_seen_at": now, "conversion_value": 1.0, "conversion_stage": "library_imported", "routes": route, "converted_routes": ["core-neighbor"]},
        "AAA-004": {"first_seen_at": now - 8 * 86400 * 1000, "routes": route},
        "AAA-005": {"first_seen_at": now, "routes": route},
    }}

    result = backend._route_evaluation(store, now_ms=now)

    assert result["eligible"] == 4
    assert result["engaged"] == 3
    assert result["converted"] == 2
    assert result["verified"] == 1
    assert result["mature_no_action"] == 1


def test_v42_explicit_refresh_coalesces_with_inflight_generation(monkeypatch) -> None:
    backend = _backend()
    calls = 0
    snapshot: dict = {}
    backend._recommendation_generation_locks = {"latest": asyncio.Lock(), "full": asyncio.Lock()}

    async def fake_unlocked(_config: dict, _payload: dict) -> dict:
        nonlocal calls, snapshot
        calls += 1
        await asyncio.sleep(0.03)
        snapshot = {
            "ok": True,
            "algorithm_version": backend.RECOMMENDATION_ALGORITHM_VERSION,
            "model": {"version": backend.PERSONALIZED_MODEL_VERSION},
            "items": [{"code": "AAA-001"}],
            "requested_limit": 1,
        }
        return dict(snapshot)

    monkeypatch.setattr(backend, "_recommendations_unlocked", fake_unlocked)
    monkeypatch.setattr(backend, "_latest_mode_snapshot", lambda *_args, **_kwargs: dict(snapshot) if snapshot else None)

    async def run_pair() -> tuple[dict, dict]:
        first = asyncio.create_task(backend._recommendations({}, {"limit": 1, "refresh": True}))
        await asyncio.sleep(0.005)
        second = asyncio.create_task(backend._recommendations({}, {"limit": 1, "refresh": True}))
        return await first, await second

    first, second = asyncio.run(run_pair())
    assert calls == 1
    assert first["algorithm_version"] == backend.RECOMMENDATION_ALGORITHM_VERSION
    assert second["cache_status"]["status"] == "coalesced"


def test_normal_request_rejects_snapshot_from_old_algorithm(monkeypatch) -> None:
    backend = _backend()
    calls = 0
    backend._recommendation_generation_locks = {"latest": asyncio.Lock(), "full": asyncio.Lock()}

    async def fake_unlocked(_config: dict, _payload: dict) -> dict:
        nonlocal calls
        calls += 1
        return {
            "algorithm_version": backend.RECOMMENDATION_ALGORITHM_VERSION,
            "model": {"version": backend.PERSONALIZED_MODEL_VERSION},
            "items": [],
        }

    monkeypatch.setattr(backend, "_recommendations_unlocked", fake_unlocked)
    monkeypatch.setattr(backend, "_latest_mode_snapshot", lambda *_args, **_kwargs: {
        "algorithm_version": backend.RECOMMENDATION_ALGORITHM_VERSION - 1,
        "model": {"version": backend.PERSONALIZED_MODEL_VERSION},
        "items": [{"code": "STALE-001"}],
        "cache_status": {"status": "stale"},
    })

    result = asyncio.run(backend._recommendations({}, {"limit": 1, "refresh": False}))

    assert calls == 1
    assert result["algorithm_version"] == backend.RECOMMENDATION_ALGORITHM_VERSION
    assert result["items"] == []


def test_normal_request_coalesces_with_startup_prewarm(monkeypatch) -> None:
    backend = _backend()
    calls = 0
    snapshot: dict = {}
    backend._recommendation_generation_locks = {"latest": asyncio.Lock(), "full": asyncio.Lock()}

    async def fake_unlocked(_config: dict, _payload: dict) -> dict:
        nonlocal calls, snapshot
        calls += 1
        await asyncio.sleep(0.03)
        snapshot = {
            "algorithm_version": backend.RECOMMENDATION_ALGORITHM_VERSION,
            "model": {"version": backend.PERSONALIZED_MODEL_VERSION},
            "items": [{"code": "AAA-001"}],
        }
        return dict(snapshot)

    monkeypatch.setattr(backend, "_recommendations_unlocked", fake_unlocked)
    monkeypatch.setattr(backend, "_latest_mode_snapshot", lambda *_args, **_kwargs: dict(snapshot) if snapshot else None)

    async def scenario() -> tuple[dict, dict]:
        prewarm = asyncio.create_task(backend._recommendations({}, {"limit": 1, "refresh": True}))
        await asyncio.sleep(0.005)
        page = asyncio.create_task(backend._recommendations({}, {"limit": 1, "refresh": False}))
        return await prewarm, await page

    _prewarm, page = asyncio.run(scenario())
    assert calls == 1
    assert page["cache_status"]["status"] == "coalesced"
    assert page["items"] == [{"code": "AAA-001"}]


def test_v42_library_profile_reuses_matching_core_revision(monkeypatch) -> None:
    backend = _backend()
    expected = {"media_count": 7, "codes": {"AAA-001"}}
    backend._library_profile_cache = {"temporal": {"revision": "revision-a", "value": expected}}

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    async def fake_revision(_db, _policy):
        return "revision-a"

    async def should_not_scan(_db):
        raise AssertionError("matching profile revision must skip the expensive Emby scan")

    monkeypatch.setattr(backend, "async_session_maker", lambda: SessionContext())
    monkeypatch.setattr(backend, "_library_profile_revision", fake_revision)
    monkeypatch.setattr(backend, "_emby_cache_codes", should_not_scan)
    assert asyncio.run(backend._library_profile()) is expected

    backend._library_profile_cache["temporal"]["expires_at"] = backend.time.monotonic() + 60
    monkeypatch.setattr(backend, "async_session_maker", lambda: (_ for _ in ()).throw(AssertionError("hot profile must not open the database")))
    assert asyncio.run(backend._library_profile()) is expected


def test_v44_resource_outcomes_collect_snapshots_and_adapt_only_with_controls(monkeypatch) -> None:
    backend = _backend()
    now = 1_800_000_000_000
    monkeypatch.setattr(backend, "_now_ms", lambda: now)
    store = {"exposures": {}, "exposure_batches": []}
    assert backend._record_exposure_batch(store, "resource:1", [{
        "code": "AAA-001", "rank": 5, "model_version": backend.PERSONALIZED_MODEL_VERSION,
        "resource_shadow_ranks": {
            backend.RESOURCE_LEARNED_MODEL_VERSION: 2,
            backend.RESOURCE_FIXED_MODEL_VERSION: 7,
        },
        "resource_summary": {"total": 5, "providers": [{"name": "AVDB"}], "has_public": True},
        "is_cracked": True, "has_cnsub": True, "best_resource_size_mb": 4096,
    }]) == 1
    snapshot = store["exposures"]["AAA-001"]["resource_snapshot"]
    assert snapshot == {
        "total": 5, "providers": ["AVDB"], "has_subtitle": True, "has_cracked": True,
        "has_uncensored": False, "has_private": False, "has_public": True,
        "best_size_mb": 4096.0,
    }
    resource_models = store["exposures"]["AAA-001"]["resource_shadow_models"]
    assert resource_models[backend.RESOURCE_LEARNED_MODEL_VERSION]["last_rank"] == 2
    assert resource_models[backend.RESOURCE_FIXED_MODEL_VERSION]["last_rank"] == 7

    sparse = backend._resource_outcome_evaluation(store, now_ms=now)
    assert sparse["status"] == "collecting"
    assert all(weight == 1.0 for weight in sparse["weights"].values())

    exposures = {}
    for index in range(40):
        cracked = index < 20
        exposures[f"AAA-{index:03d}"] = {
            "first_seen_at": now - 8 * 86_400_000,
            "last_rank": 5 + index % 4,
            "last_strategy": "ranking",
            "conversion_value": 1.0 if cracked else 0.0,
            "resource_snapshot": {"total": 5 if cracked else 1, "providers": ["AVDB"] if cracked else ["JavDB"], "has_cracked": cracked, "has_public": True},
        }
    mature = backend._resource_outcome_evaluation({"exposures": exposures}, now_ms=now)
    assert mature["status"] == "active"
    assert mature["features"]["cracked"]["adaptation_status"] == "active"
    assert mature["weights"]["cracked"] > 1.0
    assert mature["weights"]["availability_4plus"] > 1.0


def test_v45_resource_shadow_policy_requires_paired_confident_outcomes() -> None:
    backend = _backend()

    def store(count: int) -> dict:
        return {"exposures": {
            f"AAA-{index:03d}": {
                "conversion_value": 1.0,
                "resource_shadow_models": {
                    backend.RESOURCE_LEARNED_MODEL_VERSION: {"last_rank": 1},
                    backend.RESOURCE_FIXED_MODEL_VERSION: {"last_rank": 10},
                },
            }
            for index in range(count)
        }}

    assert backend._resource_shadow_evaluation(store(10))["recommended_policy"] == "collecting"
    mature = backend._resource_shadow_evaluation(store(20))
    assert mature["recommended_policy"] == "learned"
    assert mature["wins"]["learned"] == 20
    assert mature["paired_qualified"] == 20

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

    backend._profile_enrichment_pending = {}
    backend._profile_enrichment_state["coverage_last_queued_at"] = 0.0
    recent = backend.dt.datetime.now(backend.dt.timezone.utc).isoformat()
    monkeypatch.setattr(backend, "_pool", lambda: {"items": {"OLD-001": {"profile_enrichment_at": recent}}})
    assert backend._queue_profile_enrichment({}, rows, max_accept=2, reason="offline_no_path") == 0

    backend._profile_enrichment_state.update({"coverage_queued": 0, "coverage_enriched": 0, "coverage_failed": 0})
    monkeypatch.setattr(backend, "_pool", lambda: {"items": {
        "AAA-001": {"profile_enrichment_reason": "offline_no_path", "profile_enrichment_error": ""},
        "AAA-002": {"profile_enrichment_reason": "offline_no_path", "profile_enrichment_error": "timeout"},
        "AAA-003": {"profile_enrichment_reason": "candidate_gap", "profile_enrichment_error": ""},
    }})
    public = backend._profile_enrichment_public_state()
    assert (public["coverage_queued"], public["coverage_enriched"], public["coverage_failed"]) == (2, 1, 1)


def test_release_freshness_uses_trusted_full_dates_not_calendar_year_shortcuts() -> None:
    backend = _backend()
    today = backend.dt.date(2026, 8, 30)
    recent = backend._release_freshness({"release_date": "2026-08-01", "field_sources": {"release_date": "javdb"}}, today=today)
    prior_year_but_recent = backend._release_freshness({"release_date": "2025-12-31", "field_sources": {"release_date": "avdb"}}, today=today)
    stale = backend._release_freshness({"release_date": "2023-08-01"}, today=today)
    future = backend._release_freshness({"release_date": "2027-12-01"}, today=today)
    partial = backend._release_freshness({"release_date": "2026"}, today=today)

    assert recent["score"] == 4.0 and recent["reliable"]
    assert 2.0 < prior_year_but_recent["score"] < 4.0
    assert stale["score"] == 0
    assert not future["reliable"]
    assert not partial["reliable"]


def test_library_time_weight_keeps_durable_floor_and_ignores_reindex_timestamp() -> None:
    backend = _backend()
    now = backend.dt.datetime.now(backend.dt.timezone.utc)
    old = type("Entity", (), {
        "data": {"date_created": (now - backend.dt.timedelta(days=630)).isoformat()},
        "updated_at": now,
        "created_at": now,
    })()
    reindexed_without_library_date = type("Entity", (), {"data": {}, "updated_at": now, "created_at": now})()

    assert 0.75 <= backend._media_preference_weight(old) < 0.8
    assert backend._media_preference_weight(reindexed_without_library_date) == 0.75
