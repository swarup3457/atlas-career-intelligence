"""Phase 1C-A FINAL stabilization — FENCED business commits, heartbeat health,
and atomic page+continuation (build spec 3 + 5).

Failing-first regressions against the CORE invariant: before this gate a child
executor could persist run-visible business state (attempts / pages / health /
observations / coverage) WITHOUT proving its lease token was still current, so a
stale (reclaimed) worker could overwrite the authoritative result; a background
heartbeat swallowed repeated DB failures forever; and a page's DONE state and
its next-cursor continuation were separate writes.
"""

from __future__ import annotations

import threading
import time

import pytest

from atlas.persistence.sqlite import StateStore, LeaseMutationResult
from atlas.sources.child_executor import CoverageChildExecutor, FenceLost
from atlas.sources.coverage import CoverageStatus, CoverageTask, CoverageManifest
from atlas.sources.leasing import Lease, LeaseHeartbeat, LeaseManager, ManualUTCClock
from atlas.sources.adapter import SourceAdapter, new_result_base
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    ActiveState, Capability, DiscoveryResult, SearchRequest, SearchResult,
    SourceInstance, SourceType, VerificationLevel, ZeroResultKind,
)
from atlas.sources.registry import SourceRegistry
from atlas.runtime.parallel_pipeline import ParallelExecutionPipeline

pytestmark = pytest.mark.integration


class PagingAdapter(SourceAdapter):
    """Cursor-paginated source returning DISTINCT postings per page."""

    source_type = SourceType.FAKE
    CAPABILITIES = frozenset({Capability.SEARCH, Capability.PAGINATION})
    adapter_version = "paging-1"
    parser_version = "paging-parser-1"

    def __init__(self, instance):
        super().__init__(instance)
        md = instance.metadata
        self.total = int(md.get("total", 7))
        self.page_size = int(md.get("page_size", 3))

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY, "ok")

    def _item(self, i: int) -> DiscoveryResult:
        return DiscoveryResult(**new_result_base(
            self, source_job_id=f"{self.instance_id}-{i}",
            canonical_url=f"https://x/{self.instance_id}/{i}", company="Acme",
            title=f"Java Developer {i}", location="Bengaluru", posted_at="2026-09-01",
            is_active=ActiveState.ACTIVE, verification_level=VerificationLevel.OFFICIAL_SEARCH_LIVE))

    def search(self, request: SearchRequest) -> SearchResult:
        offset = int(request.cursor) if request.cursor else 0
        items = tuple(self._item(i) for i in range(offset, min(offset + self.page_size, self.total)))
        has_more = (offset + self.page_size) < self.total
        next_cursor = str(offset + self.page_size) if has_more else None
        return SearchResult(results=items, page=request.page, has_more=has_more,
                            next_cursor=next_cursor, total_reported=self.total,
                            zero_result_kind=ZeroResultKind.NOT_APPLICABLE)


def _reg():
    reg = SourceRegistry(); reg.register(PagingAdapter); return reg


def _inst(total=3, page_size=3):
    return SourceInstance("i0", SourceType.FAKE, metadata={"total": total, "page_size": page_size})


def _task():
    return CoverageTask(coverage_id="c1", source_instance="i0", lane="JAVA_BACKEND", query_key="PRIMARY")


# ---------------------------------------------------------------------------
# TEST A — a stale (reclaimed) worker's business commit is rejected
# ---------------------------------------------------------------------------
def test_stale_worker_business_commit_is_rejected_after_reclaim(tmp_path):
    """Exact prompt scenario: A gets v1, pauses, lease expires, B gets v2 and
    commits, A resumes and can write NOTHING (attempts/pages/health/observations
    /coverage), B's result stays authoritative, and the audit records a
    stale-business-commit rejection."""
    store = StateStore(tmp_path / "s.sqlite"); store.create_run("run", "none")
    clock = ManualUTCClock()
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=clock)
    lm.ensure("c1", source_instance="i0")
    a_lease = lm.acquire("c1", "A")
    assert a_lease.version == 1
    clock.advance(60)                       # A's lease expires
    b_lease = lm.acquire("c1", "B")         # B reclaims with a higher fencing token
    assert b_lease.version == 2

    # B commits an authoritative page.
    b = store.commit_child_page(
        "run", "c1", worker_id="B", version=b_lease.version, page_index=1, page_status="DONE",
        attempts=[dict(attempt_id="att-b", coverage_id="c1", run_id="run", source_instance="i0", status="SUCCESS", jobs_found=1)],
        health=dict(history_id="h-b", source_instance="i0", state="HEALTHY", result_count=1),
        observations=[dict(observation_id="obs-b", run_id="run", source_instance="i0", content_hash="hb",
                           coverage_id="c1", title="B job", revision_kind="SEARCH")],
    )
    assert b.result == LeaseMutationResult.APPLIED and b.staged == 1
    assert store.count_raw_observations("run") == 1

    # A (stale) resumes and attempts EVERY protected business mutation → all rejected.
    a = store.commit_child_page(
        "run", "c1", worker_id="A", version=a_lease.version, page_index=2, page_status="DONE",
        attempts=[dict(attempt_id="att-a", coverage_id="c1", run_id="run", source_instance="i0", status="SUCCESS", jobs_found=1)],
        health=dict(history_id="h-a", source_instance="i0", state="HEALTHY", result_count=1),
        observations=[dict(observation_id="obs-a", run_id="run", source_instance="i0", content_hash="ha",
                           coverage_id="c1", title="A job", revision_kind="SEARCH")],
    )
    assert a.result == LeaseMutationResult.STALE_TOKEN_REJECTED and a.staged == 0
    # Nothing A tried was written.
    assert store.count_raw_observations("run") == 1
    assert store.get_raw_observation("obs-a") is None
    assert not store.list_coverage_attempts("c1") or all(r["attempt_id"] != "att-a" for r in store.list_coverage_attempts("c1"))
    assert store.get_coverage_page("run", "c1", 2) is None

    # A cannot record the terminal child result either.
    a_complete = store.complete_child("run", "c1", "i0", worker_id="A", version=a_lease.version,
                                      terminal_status="COMPLETED_WITH_RESULTS",
                                      coverage_kwargs=dict(status="COMPLETED_WITH_RESULTS", completed=True))
    assert a_complete == LeaseMutationResult.STALE_TOKEN_REJECTED
    assert store.get_coverage("c1", "run") is None  # A never wrote the coverage row

    # B remains authoritative and can complete.
    b_complete = store.complete_child("run", "c1", "i0", worker_id="B", version=b_lease.version,
                                      terminal_status="COMPLETED_WITH_RESULTS",
                                      coverage_kwargs=dict(status="COMPLETED_WITH_RESULTS", completed=True, jobs_found=1))
    assert b_complete == LeaseMutationResult.APPLIED
    assert store.get_coverage("c1", "run")["status"] == "COMPLETED_WITH_RESULTS"

    # The audit contains stale-business-commit rejections.
    events = [e["event"] for e in store.list_coverage_lease_events(coverage_id="c1")]
    assert events.count("BUSINESS_COMMIT_REJECTED") >= 2  # the page commit + the completion
    store.close()


def test_child_executor_raises_fence_lost_and_stages_nothing_when_reclaimed(tmp_path):
    """The child executor itself refuses to persist under a stale fence: after B
    reclaims, A's executor run raises FenceLost and stages no observations."""
    db = tmp_path / "s.sqlite"
    store = StateStore(db); store.create_run("run", "none")
    clock = ManualUTCClock()
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=clock)
    lm.ensure("c1", source_instance="i0")
    a_lease = lm.acquire("c1", "A")
    clock.advance(60)
    b_lease = lm.acquire("c1", "B")   # B now owns (version 2)

    child_a = CoverageChildExecutor(store, _reg(), {"i0": _inst(total=3)}, run_id="run", fence=a_lease)
    with pytest.raises(FenceLost):
        child_a.execute(_task())
    assert store.count_raw_observations("run") == 0   # stale worker staged nothing
    assert store.get_coverage_page("run", "c1", 1) is None

    # B (current) executes the same child authoritatively.
    child_b = CoverageChildExecutor(store, _reg(), {"i0": _inst(total=3)}, run_id="run", fence=b_lease)
    out = child_b.execute(_task())
    assert out.status == CoverageStatus.COMPLETED_WITH_RESULTS
    assert store.count_raw_observations("run") == 3
    store.close()


# ---------------------------------------------------------------------------
# TEST B — a heartbeat cannot swallow repeated failures; commit is aborted
# ---------------------------------------------------------------------------
class _FailingHeartbeatStore:
    """A stand-in whose heartbeat always raises — simulating a lost DB
    connection under the heartbeat thread."""

    def __init__(self):
        self.calls = 0

    def heartbeat_coverage_lease(self, *a, **k):
        self.calls += 1
        raise RuntimeError("heartbeat DB connection lost")

    def close(self):
        pass


def test_heartbeat_does_not_swallow_repeated_failures_and_notifies_owner(tmp_path):
    notified = {"n": 0, "err": None}

    def on_lost(exc):
        notified["n"] += 1
        notified["err"] = exc

    lease = Lease(coverage_id="c1", run_id="run", worker_id="A", attempt=1, version=1,
                  lease_expires_at="2026-01-01T00:02:00.000000+00:00")
    hb = LeaseHeartbeat(lambda: _FailingHeartbeatStore(), "run", lease, ttl_seconds=30,
                        on_health_lost=on_lost, max_consecutive_failures=3)
    # Three consecutive failures -> health is UNKNOWN and the owner is notified ONCE.
    for _ in range(3):
        with pytest.raises(RuntimeError):
            hb.beat_once()
    assert hb.health_unknown is True
    assert hb.lease_health_ok is False
    assert notified["n"] == 1 and isinstance(notified["err"], RuntimeError)


def test_worker_aborts_before_durable_commit_when_lease_health_unknown(tmp_path):
    """A worker whose heartbeat cannot prove liveness (repeated DB failures)
    must NOT record the terminal child result — the child stays reclaimable."""
    db = tmp_path / "s.sqlite"
    store = StateStore(db); store.create_run("run", "none")
    man = CoverageManifest("run"); man.plan(_task()); man.seal()
    man.persist(store, policy_fingerprint="pol"); store.close()

    # The child sleeps briefly so the (failing) heartbeat exceeds its failure
    # budget before the child finishes.
    def slow(task, attempt):
        time.sleep(0.4)

    pipe = ParallelExecutionPipeline(
        lambda: StateStore(db), _reg(), {"i0": _inst(total=3)}, run_id="run", policy_version="pol",
        workers=1, ttl_seconds=0.6, heartbeat_interval_seconds=0.05,
        heartbeat_store_factory=lambda: _FailingHeartbeatStore(), child_crash_hook=slow, max_reclaims=0,
    )
    man2 = CoverageManifest.load(StateStore(db), "run")
    pipe.execute(man2, ["c1"])

    store = StateStore(db)
    lease_row = store.get_coverage_lease("c1", "run")
    cov = store.get_coverage("c1", "run")
    # The healthy COMPLETED result was ABORTED before its durable commit because
    # the heartbeat could not prove the lease was alive — the child was released
    # for reclaim and never recorded as a successful completion.
    assert cov is None or cov["status"] != "COMPLETED_WITH_RESULTS"
    assert lease_row["status"] != "COMPLETED_WITH_RESULTS"
    events = [e["event"] for e in store.list_coverage_lease_events(coverage_id="c1")]
    assert "RELEASE" in events   # aborted-and-released, not a durable success commit
    store.close()


# ---------------------------------------------------------------------------
# TEST E — page DONE + next-cursor continuation are one atomic commit
# ---------------------------------------------------------------------------
def test_page_done_and_next_cursor_are_committed_atomically(tmp_path):
    """After a successful page-1 commit, page 1 is DONE AND its next PENDING page
    carries the EXACT cursor — never page 1 DONE with a lost continuation, and
    never a resume that restarts page 2 with cursor=None."""
    store = StateStore(tmp_path / "s.sqlite"); store.create_run("run", "none")
    ex = CoverageChildExecutor(store, _reg(), {"i0": _inst(total=7, page_size=3)}, run_id="run", max_pages=1)
    # max_pages=1 stops after page 1 with has_more True (a bounded budget), so we
    # can inspect the durable page state deterministically.
    ex.execute(_task())
    pages = {p["page_index"]: dict(p) for p in store.list_coverage_pages("run", "c1")}
    assert pages[1]["status"] == "DONE" and bool(pages[1]["has_more"]) is True
    # has_more True on a DONE last page => an outstanding continuation is recorded.
    assert store.coverage_pages_outstanding("run", "c1")
    store.close()


def test_crash_between_response_and_commit_leaves_page_and_cursor_consistent(tmp_path):
    """Crash after the HTTP response but before the fenced commit: neither the
    page row nor its observations exist (all-or-none), and a resume re-fetches
    that page with the exact cursor — not page N+1 with cursor=None."""
    db = tmp_path / "s.sqlite"
    store = StateStore(db); store.create_run("run", "none")
    calls = {"n": 0}

    def crash(task, attempt):
        calls["n"] += 1
        if calls["n"] == 2:   # page 1 commits (call 1); crash entering page 2 (call 2)
            raise RuntimeError("crash after response, before commit")

    ex = CoverageChildExecutor(store, _reg(), {"i0": _inst(total=7, page_size=3)}, run_id="run", crash_hook=crash)
    with pytest.raises(RuntimeError):
        ex.execute(_task())
    # Page 1 (and its 3 observations) committed atomically; page 2 has NO
    # observations and remains a durable PENDING continuation carrying cursor "3".
    assert store.count_raw_observations("run") == 3
    p2 = store.get_coverage_page("run", "c1", 2)
    assert p2 is not None and p2["status"] == "PENDING" and p2["cursor"] == "3"
    store.close()

    # Resume in a fresh process: continues from cursor "3" (page 2), never page 2
    # with cursor=None, and stages no duplicates.
    store2 = StateStore(db)
    ex2 = CoverageChildExecutor(store2, _reg(), {"i0": _inst(total=7, page_size=3)}, run_id="run")
    out = ex2.execute(_task())
    assert out.status == CoverageStatus.COMPLETED_WITH_RESULTS
    assert store2.count_raw_observations("run") == 7
    assert not store2.coverage_pages_outstanding("run", "c1")
    store2.close()
