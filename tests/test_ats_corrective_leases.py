"""Phase 1C-A CORRECTIVE gate — run-scoped identity, lease fencing, active
heartbeat, crash recovery, and human-reopen (build spec 6/7/8/9).

Each test is a failing-first regression against a specific corrective defect:
before this gate the coverage/lease PK was GLOBAL (a terminal lease in one run
blocked the same logical coverage_id in another run), lease mutations were not
fenced (a stale worker could complete/release a lease another worker had
reclaimed), there was no periodic heartbeat, an unexpected worker exception
stranded the lease, and a BLOCKED_HUMAN child could not be explicitly reopened.
"""

from __future__ import annotations

import threading
import time

import pytest

from atlas.persistence.sqlite import StateStore, LeaseMutationResult
from atlas.sources.coverage import CoverageManifest, CoverageStatus, CoverageTask
from atlas.sources.leasing import Lease, LeaseHeartbeat, LeaseManager, ManualUTCClock
from atlas.runtime.parallel_pipeline import ParallelExecutionPipeline
from atlas.sources.registry import SourceRegistry
from atlas.sources.testing.fake import FakeAdapter, make_fake_instance

pytestmark = pytest.mark.integration


def _store(tmp_path, *runs, name="s.sqlite"):
    store = StateStore(tmp_path / name)
    for r in runs or ("run",):
        store.create_run(r, controller="none")
    return store


# ---------------------------------------------------------------------------
# §6 — run-scoped coverage/lease identity (composite PK (run_id, coverage_id))
# ---------------------------------------------------------------------------
def test_same_coverage_id_is_independent_across_two_runs(tmp_path):
    store = _store(tmp_path, "A", "B")
    clock = ManualUTCClock()
    la = LeaseManager(store, "A", ttl_seconds=60, clock=clock)
    lb = LeaseManager(store, "B", ttl_seconds=60, clock=clock)
    la.ensure("c1", company="Acme", source_instance="i1")
    lb.ensure("c1", company="Acme", source_instance="i1")

    leaseA = la.acquire("c1", "wA")
    leaseB = lb.acquire("c1", "wB")
    assert leaseA is not None and leaseB is not None

    # Each run gets its OWN row + lease.
    assert store.get_coverage_lease("c1", "A")["run_id"] == "A"
    assert store.get_coverage_lease("c1", "B")["run_id"] == "B"

    # A terminal lease in run-A does not affect run-B.
    assert la.complete(leaseA, terminal_status="DONE") == LeaseMutationResult.APPLIED
    assert store.get_coverage_lease("c1", "A")["terminal"] == 1
    assert store.get_coverage_lease("c1", "B")["terminal"] == 0
    # run-B can still be worked.
    assert lb.heartbeat(leaseB) == LeaseMutationResult.APPLIED
    store.close()


def test_coverage_records_are_run_scoped(tmp_path):
    store = _store(tmp_path, "A", "B")
    store.upsert_coverage("c1", "A", "i1", lane="JAVA_BACKEND", status="COMPLETED_WITH_RESULTS", completed=True)
    store.upsert_coverage("c1", "B", "i1", lane="JAVA_BACKEND", status="NOT_ATTEMPTED")
    assert store.get_coverage("c1", "A")["status"] == "COMPLETED_WITH_RESULTS"
    assert store.get_coverage("c1", "B")["status"] == "NOT_ATTEMPTED"
    assert len(store.list_coverage("A")) == 1 and len(store.list_coverage("B")) == 1
    store.close()


def test_terminal_lease_in_one_run_does_not_block_the_other(tmp_path):
    store = _store(tmp_path, "A", "B")
    la = LeaseManager(store, "A", ttl_seconds=60, clock=ManualUTCClock())
    lb = LeaseManager(store, "B", ttl_seconds=60, clock=ManualUTCClock())
    la.ensure("c1", source_instance="i1", terminal=True)  # completed in run A
    lb.ensure("c1", source_instance="i1")
    assert la.acquire("c1", "wA") is None  # terminal in A
    assert lb.acquire("c1", "wB") is not None  # independently leasable in B
    store.close()


# ---------------------------------------------------------------------------
# §7 — lease fencing / stale-owner rejection
# ---------------------------------------------------------------------------
def test_stale_owner_cannot_complete_release_or_heartbeat_after_reclaim(tmp_path):
    store = _store(tmp_path)
    clock = ManualUTCClock()
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=clock)
    lm.ensure("c1", source_instance="i1")
    a_lease = lm.acquire("c1", "A")
    assert a_lease.version == 1
    # A's lease expires; B reclaims with a HIGHER fencing token.
    clock.advance(60)
    b_lease = lm.acquire("c1", "B")
    assert b_lease.version == 2 and b_lease.worker_id == "B"

    # A (stale) can neither complete, release, nor heartbeat.
    assert lm.complete(a_lease, terminal_status="DONE") == LeaseMutationResult.STALE_TOKEN_REJECTED
    assert lm.release(a_lease) == LeaseMutationResult.STALE_TOKEN_REJECTED
    assert lm.heartbeat(a_lease) == LeaseMutationResult.STALE_TOKEN_REJECTED
    # The lease is still owned by B and non-terminal.
    row = store.get_coverage_lease("c1", "run")
    assert row["worker_id"] == "B" and row["terminal"] == 0

    # B completes; a duplicate B completion is harmless and cannot change status.
    assert lm.complete(b_lease, terminal_status="DONE") == LeaseMutationResult.APPLIED
    assert lm.complete(b_lease, terminal_status="FAILED") == LeaseMutationResult.ALREADY_APPLIED_IDEMPOTENTLY
    assert store.get_coverage_lease("c1", "run")["status"] == "DONE"

    # Every rejected stale mutation is recorded as an append-only lease event.
    events = [e["event"] for e in store.list_coverage_lease_events(coverage_id="c1")]
    assert "COMPLETE_REJECTED" in events and "RELEASE_REJECTED" in events and "HEARTBEAT_REJECTED" in events
    store.close()


def test_token_from_one_run_cannot_mutate_another_run(tmp_path):
    store = _store(tmp_path, "A", "B")
    la = LeaseManager(store, "A", ttl_seconds=60, clock=ManualUTCClock())
    la.ensure("c1", source_instance="i1")
    a_lease = la.acquire("c1", "wA")
    # A token used against run B (different run_id) must not mutate B.
    lb = LeaseManager(store, "B", ttl_seconds=60, clock=ManualUTCClock())
    lb.ensure("c1", source_instance="i1")
    b_lease = lb.acquire("c1", "wB")
    cross = Lease(coverage_id="c1", run_id="B", worker_id="wA", attempt=a_lease.attempt,
                  version=a_lease.version, lease_expires_at=a_lease.lease_expires_at)
    assert lb.complete(cross, terminal_status="DONE") == LeaseMutationResult.STALE_TOKEN_REJECTED
    assert store.get_coverage_lease("c1", "B")["terminal"] == 0
    # The real B owner still works.
    assert lb.complete(b_lease, terminal_status="DONE") == LeaseMutationResult.APPLIED
    store.close()


# ---------------------------------------------------------------------------
# §8 — active heartbeat + crash recovery
# ---------------------------------------------------------------------------
def test_beat_once_keeps_lease_unreclaimable_under_virtual_clock(tmp_path):
    db = tmp_path / "s.sqlite"
    store = _store(tmp_path)  # creates 'run'
    clock = ManualUTCClock()
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=clock)
    lm.ensure("c1", source_instance="i1")
    lease = lm.acquire("c1", "A")
    hb = LeaseHeartbeat(lambda: StateStore(db), "run", lease, ttl_seconds=30, clock=clock)
    other = LeaseManager(store, "run", ttl_seconds=30, clock=clock)
    # Long child: advance 20s, heartbeat renews (expiry -> now+30), still not reclaimable at 25s.
    clock.advance(20)
    assert hb.beat_once() == LeaseMutationResult.APPLIED
    clock.advance(5)  # t=25, lease renewed at 20 to expire 50
    assert other.acquire("c1", "B") is None
    # After heartbeats stop and TTL expires, reclaim succeeds.
    clock.advance(40)  # t=65 > 50
    assert other.acquire("c1", "B") is not None
    hb.stop()
    store.close()


def test_periodic_heartbeat_thread_renews_a_long_lease(tmp_path):
    """Real background thread: a child that outlives the original TTL stays
    unreclaimable because the heartbeat renews it, then becomes reclaimable
    after the heartbeat stops and the lease truly expires."""
    db = tmp_path / "s.sqlite"
    store = _store(tmp_path)
    lm = LeaseManager(store, "run", ttl_seconds=0.4)  # real system clock
    lm.ensure("c1", source_instance="i1")
    lease = lm.acquire("c1", "A")
    other = LeaseManager(StateStore(db), "run", ttl_seconds=0.4)
    with LeaseHeartbeat(lambda: StateStore(db), "run", lease, ttl_seconds=0.4, interval_seconds=0.1):
        time.sleep(0.9)  # ~2x the TTL; heartbeats keep it alive
        assert other.acquire("c1", "B") is None  # unreclaimable while heartbeat runs
    # Heartbeat stopped: after the TTL elapses the lease is reclaimable.
    time.sleep(0.6)
    assert other.acquire("c1", "B") is not None
    other.store.close()
    store.close()


def test_unexpected_worker_exception_releases_lease_and_leaves_no_duplicate(tmp_path):
    """A generic (non-CrashInjection) exception mid-child must release the owned
    lease (finally) and leave no partial observations; the reclaim then completes
    once with the correct observation count."""
    db = tmp_path / "s.sqlite"
    insts = {"i0": make_fake_instance("i0", scenario="results", result_count=2, company="Acme")}
    tasks = [CoverageTask(coverage_id="c-x", source_instance="i0", company="Acme", lane="L", query_key="PRIMARY")]
    store = StateStore(db); store.create_run("run", "none")
    man = CoverageManifest("run")
    man.plan(tasks[0]); man.seal(); man.persist(store, policy_fingerprint="pol"); store.close()

    fired = {"n": 0}
    lock = threading.Lock()

    def boom(task, attempt):
        with lock:
            fired["n"] += 1
            if fired["n"] == 1:
                raise ValueError("unexpected fault before persist")

    reg = SourceRegistry(); reg.register(FakeAdapter)
    pipe = ParallelExecutionPipeline(lambda: StateStore(db), reg, insts, run_id="run",
                                     policy_version="pol", workers=2, child_crash_hook=boom)
    pipe.execute(man, ["c-x"])
    store = StateStore(db)
    task = CoverageManifest.load(store, "run").get("c-x")
    assert task.status == CoverageStatus.COMPLETED_WITH_RESULTS  # recovered
    assert store.count_raw_observations("run") == 2  # no duplicate after reclaim
    events = [e["event"] for e in store.list_coverage_lease_events(coverage_id="c-x")]
    assert "RELEASE" in events and events.count("ACQUIRE") >= 2
    assert store.get_coverage_lease("c-x", "run")["terminal"] == 1
    store.close()


def test_report_writers_must_be_exactly_one(tmp_path):
    with pytest.raises(ValueError):
        ParallelExecutionPipeline(lambda: StateStore(tmp_path / "s.sqlite"), SourceRegistry(), {},
                                  run_id="run", report_writers=2)
    with pytest.raises(ValueError):
        ParallelExecutionPipeline(lambda: StateStore(tmp_path / "s.sqlite"), SourceRegistry(), {},
                                  run_id="run", report_writers=0)


# ---------------------------------------------------------------------------
# §9 — explicit human reopen
# ---------------------------------------------------------------------------
def test_human_reopen_is_explicit_audited_and_only_for_blocked(tmp_path):
    store = _store(tmp_path)
    lm = LeaseManager(store, "run", ttl_seconds=60, clock=ManualUTCClock())
    lm.ensure("c-block", source_instance="i1")
    lm.ensure("c-done", source_instance="i1")
    bl = lm.acquire("c-block", "w1")
    dn = lm.acquire("c-done", "w2")
    lm.complete(bl, terminal_status="BLOCKED_HUMAN")
    lm.complete(dn, terminal_status="DONE")

    # Ordinary resume does not reopen a BLOCKED_HUMAN child (it stays terminal).
    assert lm.acquire("c-block", "w3") is None

    # A normally-completed child is never reopened by the human path.
    assert lm.reopen_human("c-done", reason="oops") == LeaseMutationResult.STALE_TOKEN_REJECTED
    assert store.get_coverage_lease("c-done", "run")["terminal"] == 1

    # Explicit authorized reopen makes ONLY the blocked child leaseable again.
    assert lm.reopen_human("c-block", reason="human resolved", reference="TICKET-9") == LeaseMutationResult.APPLIED
    row = store.get_coverage_lease("c-block", "run")
    assert row["terminal"] == 0 and row["status"] == "AVAILABLE" and row["version"] == 2
    reopened = lm.acquire("c-block", "w4")
    assert reopened is not None and reopened.version == 3

    # The reopen is audited with reason/reference.
    events = [e for e in store.list_coverage_lease_events(coverage_id="c-block")]
    reopen_ev = [e for e in events if e["event"] == "HUMAN_REOPEN"]
    assert len(reopen_ev) == 1
    import json as _json
    detail = _json.loads(reopen_ev[0]["detail_json"])
    assert detail["reason"] == "human resolved" and detail["reference"] == "TICKET-9"
    store.close()


# ---------------------------------------------------------------------------
# §6 (migration/backup) — v9 preservation and run retention
# ---------------------------------------------------------------------------
def test_v9_schema_and_backup_restore_retains_both_runs(tmp_path):
    from atlas.backup.sqlite_backup import backup_sqlite_database

    db = tmp_path / "s.sqlite"
    store = StateStore(db)
    assert store.schema_version() == 12
    store.create_run("A", "none"); store.create_run("B", "none")
    LeaseManager(store, "A", clock=ManualUTCClock()).ensure("c1", source_instance="i1")
    LeaseManager(store, "B", clock=ManualUTCClock()).ensure("c1", source_instance="i1")
    store.upsert_coverage("c1", "A", "i1", status="COMPLETED_WITH_RESULTS", completed=True)
    store.upsert_coverage("c1", "B", "i1", status="NOT_ATTEMPTED")
    store.close()

    # Online snapshot (the backup path) then reopen: both runs survive intact.
    dest = tmp_path / "restored.sqlite"
    backup_sqlite_database(db, dest)
    restored = StateStore(dest)
    assert restored.schema_version() == 12
    assert restored.get_coverage("c1", "A")["status"] == "COMPLETED_WITH_RESULTS"
    assert restored.get_coverage("c1", "B")["status"] == "NOT_ATTEMPTED"
    assert restored.get_coverage_lease("c1", "A")["run_id"] == "A"
    assert restored.get_coverage_lease("c1", "B")["run_id"] == "B"
    restored.close()
