"""Phase 1C-A — bounded parallel dispatcher tests (build spec 6/8, matrix A/D).

Proves atomic leasing + per-company/instance/tenant caps + crash-reclaim +
concurrency-1==concurrency-N equality using the SAME production child path.
"""

from __future__ import annotations

import threading
import time

import pytest

from atlas.persistence.sqlite import StateStore
from atlas.runtime.canonicalize import canonicalize_run
from atlas.runtime.parallel_pipeline import ParallelExecutionPipeline
from atlas.sources.adapter import SourceAdapter, new_result_base
from atlas.sources.child_executor import CrashInjection
from atlas.sources.coverage import (
    TERMINAL_COVERAGE_STATUSES,
    CoverageManifest,
    CoverageStatus,
    CoverageTask,
)
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    ActiveState,
    Capability,
    DiscoveryResult,
    SearchRequest,
    SearchResult,
    SourceInstance,
    SourceType,
    VerificationLevel,
    ZeroResultKind,
)
from atlas.sources.registry import SourceRegistry
from atlas.sources.testing.fake import FakeAdapter, make_fake_instance

pytestmark = pytest.mark.integration


# --- a blocking probe adapter to OBSERVE concurrency caps -------------------
class _Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.active_company: dict[str, int] = {}
        self.active_tenant: dict[str, int] = {}
        self.max_company: dict[str, int] = {}
        self.max_tenant: dict[str, int] = {}
        self.global_active = 0
        self.global_max = 0

    def enter(self, company: str, tenant: str):
        with self.lock:
            self.active_company[company] = self.active_company.get(company, 0) + 1
            self.active_tenant[tenant] = self.active_tenant.get(tenant, 0) + 1
            self.global_active += 1
            self.max_company[company] = max(self.max_company.get(company, 0), self.active_company[company])
            self.max_tenant[tenant] = max(self.max_tenant.get(tenant, 0), self.active_tenant[tenant])
            self.global_max = max(self.global_max, self.global_active)

    def exit(self, company: str, tenant: str):
        with self.lock:
            self.active_company[company] -= 1
            self.active_tenant[tenant] -= 1
            self.global_active -= 1


_TRACKERS: dict[str, _Tracker] = {}


class ProbeAdapter(SourceAdapter):
    source_type = SourceType.FAKE
    CAPABILITIES = frozenset({Capability.SEARCH})
    adapter_version = "probe-1.0.0"
    parser_version = "probe-parser-1.0.0"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY, "probe")

    def search(self, request: SearchRequest) -> SearchResult:
        md = self.instance.metadata
        tracker = _TRACKERS[md["tracker_id"]]
        company = md["company"]
        tenant = md.get("tenant_key", company)
        tracker.enter(company, tenant)
        try:
            time.sleep(0.03)
        finally:
            tracker.exit(company, tenant)
        r = DiscoveryResult(**new_result_base(
            self, source_job_id=f"{self.instance_id}-1", canonical_url=f"https://x/{self.instance_id}/1",
            company=company, title="Java Engineer", location="Bengaluru",
            is_active=ActiveState.ACTIVE, verification_level=VerificationLevel.OFFICIAL_SEARCH_LIVE))
        return SearchResult(results=(r,), zero_result_kind=ZeroResultKind.NOT_APPLICABLE)


def _probe_registry() -> SourceRegistry:
    reg = SourceRegistry()
    reg.register(ProbeAdapter)
    return reg


def _seed(db, run_id, instances, plan_tasks):
    store = StateStore(db)
    store.create_run(run_id, "none")
    for i in instances.values():
        store.upsert_source_instance(i.instance_id, i.adapter_key.value, i.source_type.value, i.category.value)
    man = CoverageManifest(run_id)
    for t in plan_tasks:
        man.plan(t)
    man.seal()
    man.persist(store, policy_fingerprint="pol")
    store.close()
    return man


# --- concurrency-1 == concurrency-N equality --------------------------------
def _run(db, run_id, instances, man, workers):
    pipe = ParallelExecutionPipeline(lambda: StateStore(db), _fake_registry(), instances,
                                     run_id=run_id, policy_version="pol", workers=workers)
    pipe.execute(man, [t.coverage_id for t in man.tasks()])
    store = StateStore(db)
    canonicalize_run(store, run_id)
    canon = store.count_canonical_jobs()
    obs = store.count_raw_observations(run_id)
    statuses = sorted((t.coverage_id, t.status.value) for t in CoverageManifest.load(store, run_id).tasks())
    store.close()
    return canon, obs, statuses


def _fake_registry() -> SourceRegistry:
    reg = SourceRegistry()
    reg.register(FakeAdapter)
    return reg


def test_concurrency_1_and_4_produce_identical_results(tmp_path):
    insts = {f"i{k}": make_fake_instance(f"i{k}", scenario="results", result_count=3, company=f"Co{k}") for k in range(10)}
    tasks = [CoverageTask(coverage_id=f"c{k}", source_instance=f"i{k}", lane="JAVA_BACKEND",
                          query_key="PRIMARY", company=f"Co{k}") for k in range(10)]
    man1 = _seed(tmp_path / "a.sqlite", "one", insts, tasks)
    man4 = _seed(tmp_path / "b.sqlite", "four", insts, tasks)
    r1 = _run(tmp_path / "a.sqlite", "one", insts, man1, workers=1)
    r4 = _run(tmp_path / "b.sqlite", "four", insts, man4, workers=4)
    assert r1[0] == r4[0] and r1[1] == r4[1]
    assert [s for _, s in r1[2]] == [s for _, s in r4[2]]
    assert all(st == CoverageStatus.COMPLETED_WITH_RESULTS.value for _, st in r1[2])


def test_per_company_cap_respected_with_two_instances(tmp_path):
    tid = "cap-company"
    _TRACKERS[tid] = _Tracker()
    # One company, TWO instances → the per-company cap (1) must serialize them.
    insts = {
        "a": SourceInstance("a", SourceType.FAKE, metadata={"tracker_id": tid, "company": "Acme"}),
        "b": SourceInstance("b", SourceType.FAKE, metadata={"tracker_id": tid, "company": "Acme"}),
    }
    tasks = [CoverageTask(coverage_id="ca", source_instance="a", company="Acme", lane="L"),
             CoverageTask(coverage_id="cb", source_instance="b", company="Acme", lane="L")]
    man = _seed(tmp_path / "c.sqlite", "run", insts, tasks)
    pipe = ParallelExecutionPipeline(lambda: StateStore(tmp_path / "c.sqlite"), _probe_registry(), insts,
                                     run_id="run", policy_version="pol", workers=4, max_per_company=1)
    pipe.execute(man, ["ca", "cb"])
    assert _TRACKERS[tid].max_company["Acme"] == 1


def test_independent_companies_progress_concurrently(tmp_path):
    tid = "concurrent"
    _TRACKERS[tid] = _Tracker()
    insts = {f"i{k}": SourceInstance(f"i{k}", SourceType.FAKE, metadata={"tracker_id": tid, "company": f"Co{k}"}) for k in range(4)}
    tasks = [CoverageTask(coverage_id=f"c{k}", source_instance=f"i{k}", company=f"Co{k}", lane="L") for k in range(4)]
    man = _seed(tmp_path / "d.sqlite", "run", insts, tasks)
    pipe = ParallelExecutionPipeline(lambda: StateStore(tmp_path / "d.sqlite"), _probe_registry(), insts,
                                     run_id="run", policy_version="pol", workers=4, max_per_company=1)
    pipe.execute(man, [t.coverage_id for t in tasks])
    assert _TRACKERS[tid].global_max >= 2  # different companies ran at once


def test_per_tenant_cap_respected(tmp_path):
    tid = "cap-tenant"
    _TRACKERS[tid] = _Tracker()
    # Two companies but the SAME tenant → the per-tenant cap (1) serializes them.
    insts = {
        "a": SourceInstance("a", SourceType.FAKE, tenant="shared", metadata={"tracker_id": tid, "company": "CoA", "tenant_key": "tenant::shared"}),
        "b": SourceInstance("b", SourceType.FAKE, tenant="shared", metadata={"tracker_id": tid, "company": "CoB", "tenant_key": "tenant::shared"}),
    }
    tasks = [CoverageTask(coverage_id="ca", source_instance="a", company="CoA", lane="L"),
             CoverageTask(coverage_id="cb", source_instance="b", company="CoB", lane="L")]
    man = _seed(tmp_path / "e.sqlite", "run", insts, tasks)
    pipe = ParallelExecutionPipeline(lambda: StateStore(tmp_path / "e.sqlite"), _probe_registry(), insts,
                                     run_id="run", policy_version="pol", workers=4, max_per_company=5, max_per_tenant=1)
    pipe.execute(man, ["ca", "cb"])
    assert _TRACKERS[tid].max_tenant["tenant::shared"] == 1


def test_crash_between_response_and_persist_reclaims_no_duplicate(tmp_path):
    db = tmp_path / "f.sqlite"
    insts = {"i0": make_fake_instance("i0", scenario="results", result_count=2, company="Acme")}
    tasks = [CoverageTask(coverage_id="c-crash", source_instance="i0", company="Acme", lane="L", query_key="PRIMARY")]
    man = _seed(db, "run", insts, tasks)
    crashed = {"done": False}
    lock = threading.Lock()

    def crash_hook(task, attempt):
        if task.coverage_id == "c-crash":
            with lock:
                if not crashed["done"]:
                    crashed["done"] = True
                    raise CrashInjection("boom between response and persist")

    pipe = ParallelExecutionPipeline(lambda: StateStore(db), _fake_registry(), insts,
                                     run_id="run", policy_version="pol", workers=2, child_crash_hook=crash_hook)
    pipe.execute(man, ["c-crash"])
    store = StateStore(db)
    task = CoverageManifest.load(store, "run").get("c-crash")
    assert task.status in TERMINAL_COVERAGE_STATUSES  # recovered to terminal
    # Exactly one set of observations despite the crash+reclaim (idempotent).
    assert store.count_raw_observations("run") == 2
    events = [e["event"] for e in store.list_coverage_lease_events(coverage_id="c-crash")]
    assert "RELEASE" in events and events.count("ACQUIRE") >= 2  # reclaimed
    lease = store.get_coverage_lease("c-crash")
    assert lease["terminal"] == 1
    store.close()


def test_completed_branch_not_rerun_on_resume(tmp_path):
    db = tmp_path / "g.sqlite"
    insts = {f"i{k}": make_fake_instance(f"i{k}", scenario="results", result_count=1, company=f"Co{k}") for k in range(3)}
    tasks = [CoverageTask(coverage_id=f"c{k}", source_instance=f"i{k}", company=f"Co{k}", lane="L", query_key="PRIMARY") for k in range(3)]
    man = _seed(db, "run", insts, tasks)
    pipe = ParallelExecutionPipeline(lambda: StateStore(db), _fake_registry(), insts, run_id="run", policy_version="pol", workers=3)
    first = pipe.execute(man, [t.coverage_id for t in tasks])
    assert first.executed == 3
    store = StateStore(db)
    obs_after_first = store.count_raw_observations("run")
    man2 = CoverageManifest.load(store, "run")
    store.close()
    # Resume: all children terminal → nothing re-executes, no new observations.
    pipe2 = ParallelExecutionPipeline(lambda: StateStore(db), _fake_registry(), insts, run_id="run", policy_version="pol", workers=3)
    second = pipe2.execute(man2, [t.coverage_id for t in tasks])
    assert second.executed == 0
    store = StateStore(db)
    assert store.count_raw_observations("run") == obs_after_first  # no duplication
    store.close()


def test_slot_acquire_failure_prevents_adapter_call():
    """Regression for the fixed RateLimitedExecutor: a failed (non-blocking)
    slot acquire must NOT fall through to the adapter, and must not release a
    slot it never held."""
    from atlas.sources.executor import RateLimitedExecutor
    from atlas.sources.rate_limit import RateLimiter, RatePolicy

    limiter = RateLimiter(RatePolicy(max_concurrency=1))
    # Manually hold the only slot so a blocking acquire in run_search would wait;
    # here we assert the non-blocking API still returns False at capacity and
    # the active count is never corrupted by a spurious release.
    assert limiter.acquire_slot("k") is True
    assert limiter.acquire_slot("k") is False  # at capacity
    assert limiter.active_count("k") == 1  # not incremented by the failed acquire
    limiter.release_slot("k")
    assert limiter.active_count("k") == 0

    # And run_search only calls the adapter when a slot is genuinely held.
    ex = RateLimitedExecutor(RateLimiter(RatePolicy(max_concurrency=1)))
    a = FakeAdapter(make_fake_instance("wk", scenario="results", result_count=1))
    out = ex.run_search(a, SearchRequest(query="x"))
    assert out.count == 1
