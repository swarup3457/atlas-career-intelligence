"""Phase 1A: source framework ↔ existing LangGraph governor integration,
including crash/resume. No LLM, no network."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory, TaskStatus
from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.orchestration.graph import build_graph, run_to_completion
from atlas.orchestration.state import RUN_STATUS_COMPLETE, initial_queue_state
from atlas.persistence.sqlite import StateStore
from atlas.sources.coverage import CoverageManifest, CoverageTask, coverage_status_for
from atlas.sources.models import SearchRequest, ZeroResultKind
from atlas.sources.provenance import ingest_discovery_results
from atlas.sources.testing.fake import FakeAdapter, FakeScenario, make_fake_instance
from atlas.sources.worker import SourceSearchWorker, SourceTask

pytestmark = pytest.mark.integration

_SEARCH = SearchRequest(query="engineer", limit=10)
_SENTINEL = SearchRequest(query="*", limit=10, extra_filters={"sentinel": True})


def _adapters():
    return {
        "r": FakeAdapter(make_fake_instance("r"), FakeScenario(kind="results", result_count=3)),
        "z": FakeAdapter(make_fake_instance("z"), FakeScenario(kind="zero")),
        "u": FakeAdapter(make_fake_instance("u"), FakeScenario(kind="untrusted_zero", sentinel="healthy")),
        "d": FakeAdapter(make_fake_instance("d"), FakeScenario(kind="untrusted_zero", sentinel="drift")),
        "t": FakeAdapter(make_fake_instance("t"),
                         FakeScenario(kind="results", result_count=2, fail_first_n=1,
                                      transient_category=ErrorCategory.TIMEOUT)),
        "rl": FakeAdapter(make_fake_instance("rl"), FakeScenario(kind="error", error_category=ErrorCategory.HTTP_429)),
    }


def _plan():
    return {
        "results-1": SourceTask("results-1", "r", _SEARCH, lane="A"),
        "trusted-zero": SourceTask("trusted-zero", "z", _SEARCH, lane="A"),
        "untrusted-ok": SourceTask("untrusted-ok", "u", _SEARCH, lane="B", sentinel_request=_SENTINEL),
        "drift": SourceTask("drift", "d", _SEARCH, lane="B", sentinel_request=_SENTINEL),
        "transient": SourceTask("transient", "t", _SEARCH, lane="C"),
        "ratelimited": SourceTask("ratelimited", "rl", _SEARCH, lane="C"),
    }


def test_governor_runs_mixed_source_outcomes(tmp_path):
    plan = _plan()
    worker = SourceSearchWorker(_adapters(), plan)
    with open_checkpointer(tmp_path / "cp.sqlite") as cp:
        graph = build_graph(worker, retry_budget=2).compile(checkpointer=cp)
        cfg = thread_config("run-1")
        final = run_to_completion(graph, cfg, initial_queue_state(list(plan)))

    statuses = {item: final["item_results"][item]["status"] for item in plan}
    assert statuses["results-1"] == TaskStatus.SUCCESS.value
    assert statuses["trusted-zero"] == TaskStatus.NO_RELEVANT_RESULTS.value
    assert statuses["untrusted-ok"] == TaskStatus.NO_RELEVANT_RESULTS.value  # sentinel → healthy
    assert statuses["drift"] == TaskStatus.EXTRACTION_UNRESOLVED.value       # sentinel → drift
    assert statuses["transient"] == TaskStatus.SUCCESS.value                 # retried then ok
    assert statuses["ratelimited"] == TaskStatus.RATE_LIMITED.value          # budget exhausted
    # Exactly one bounded sentinel probe per untrusted zero (2 total).
    assert len(worker.sentinel_log) == 2

    # Coverage: PLANNED == TERMINAL.
    manifest = CoverageManifest("run-1")
    for item, task in plan.items():
        manifest.plan(CoverageTask(coverage_id=item, source_instance=task.instance_id, lane=task.lane))
    for item in plan:
        res = final["item_results"][item]
        zk = res["data"].get("zero_result_kind")
        manifest.mark(
            item,
            coverage_status_for(TaskStatus(res["status"]), result_count=res["data"].get("result_count", 0),
                                zero_kind=ZeroResultKind(zk) if zk else None),
        )
    assert manifest.is_complete()
    assert manifest.summary()["_terminal"] == len(plan)


def test_crash_and_resume_no_repeat_no_duplicate_observations(tmp_path):
    # Deterministic (single-attempt) plan so adapter-state resets on
    # "restart" cannot change the outcome.
    plan = {
        "r1": SourceTask("r1", "r", _SEARCH),
        "z1": SourceTask("z1", "z", _SEARCH),
        "u1": SourceTask("u1", "u", _SEARCH, sentinel_request=_SENTINEL),
        "d1": SourceTask("d1", "d", _SEARCH, sentinel_request=_SENTINEL),
        "r2": SourceTask("r2", "r", _SEARCH),
        "z2": SourceTask("z2", "z", _SEARCH),
    }
    cp_db = tmp_path / "cp.sqlite"
    cfg = thread_config("run-crash")
    items = list(plan)

    # --- Session 1: process a couple of items then "crash". ---
    with open_checkpointer(cp_db) as cp:
        graph = build_graph(SourceSearchWorker(_adapters(), plan), retry_budget=2).compile(checkpointer=cp)
        state = graph.invoke(initial_queue_state(items), cfg)
        while len(state.get("completed_items", [])) < 2 and state.get("run_status") != RUN_STATUS_COMPLETE:
            state = graph.invoke({}, cfg)
        partial_completed = list(state["completed_items"])
    assert 0 < len(partial_completed) < len(items)

    # --- Session 2: fresh worker/adapters resume from checkpoint. ---
    with open_checkpointer(cp_db) as cp:
        graph = build_graph(SourceSearchWorker(_adapters(), plan), retry_budget=2).compile(checkpointer=cp)
        existing = graph.get_state(cfg).values
        seed = existing if existing else initial_queue_state(items)
        final = run_to_completion(graph, cfg, seed)

    assert final["run_status"] == RUN_STATUS_COMPLETE
    # No task repeated: every item completed exactly once.
    assert sorted(final["completed_items"]) == sorted(items)
    assert len(final["completed_items"]) == len(set(final["completed_items"]))

    # Observations from result-bearing items are idempotent across a re-ingest.
    with StateStore(tmp_path / "state.sqlite") as store:
        all_results = []
        for item in items:
            data = final["item_results"][item]["data"]
            if data.get("results"):
                from atlas.sources.models import DiscoveryResult, SourceType, WorkMode, ActiveState, VerificationLevel
                for rd in data["results"]:
                    all_results.append(DiscoveryResult(
                        source_type=SourceType(rd["source_type"]), source_instance=rd["source_instance"],
                        source_job_id=rd["source_job_id"], company=rd["company"], title=rd["title"],
                        location=rd["location"], adapter_version=rd["adapter_version"],
                        parser_version=rd["parser_version"], source_url=rd["source_url"],
                    ))
        ingest_discovery_results(store, all_results)
        canonical_after_first = store.count_canonical_jobs()
        obs_after_first = store.count_observations()
        ingest_discovery_results(store, all_results)  # resume-safe re-ingest
        assert store.count_canonical_jobs() == canonical_after_first
        assert store.count_observations() == obs_after_first
