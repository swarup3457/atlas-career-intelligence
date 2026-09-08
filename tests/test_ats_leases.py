"""Phase 1C-A — atomic coverage-task lease tests (build spec 7, matrix A)."""

from __future__ import annotations

import threading

import pytest

from atlas.persistence.sqlite import StateStore
from atlas.sources.leasing import LeaseManager, ManualUTCClock

pytestmark = pytest.mark.integration


def _store(tmp_path, name="s.sqlite"):
    store = StateStore(tmp_path / name)
    store.create_run("run", controller="none")
    return store


def test_twenty_workers_contend_one_active_lease(tmp_path):
    db = tmp_path / "leases.sqlite"
    seed = StateStore(db)
    seed.create_run("run", controller="none")
    clock = ManualUTCClock()
    LeaseManager(seed, "run", ttl_seconds=60, clock=clock).ensure("c1", company="Acme", source_instance="i1")
    seed.close()

    winners: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker(n: int):
        store = StateStore(db)
        try:
            lm = LeaseManager(store, "run", ttl_seconds=60, clock=clock)
            barrier.wait()
            lease = lm.acquire("c1", worker_id=f"w{n}")
            if lease is not None:
                with lock:
                    winners.append(lease.worker_id)
        finally:
            store.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"exactly one worker may hold the lease, got {winners}"


def test_expired_lease_is_reclaimable(tmp_path):
    store = _store(tmp_path)
    clock = ManualUTCClock()
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=clock)
    lm.ensure("c1", company="Acme", source_instance="i1")
    assert lm.acquire("c1", "w1") is not None
    # Before expiry: a second worker cannot claim.
    clock.advance(10)
    assert lm.acquire("c1", "w2") is None
    # After expiry: reclaimable.
    clock.advance(30)
    reclaimed = lm.acquire("c1", "w2")
    assert reclaimed is not None and reclaimed.worker_id == "w2"
    assert reclaimed.attempt == 2
    store.close()


def test_heartbeat_extends_lease(tmp_path):
    store = _store(tmp_path)
    clock = ManualUTCClock()
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=clock)
    lm.ensure("c1", source_instance="i1")
    lm.acquire("c1", "w1")
    clock.advance(20)
    assert lm.heartbeat("c1", "w1") is True
    # After heartbeat, the lease is fresh again — still not reclaimable at t=40.
    clock.advance(20)
    assert lm.acquire("c1", "w2") is None
    # A non-owner cannot heartbeat.
    assert lm.heartbeat("c1", "w2") is False
    store.close()


def test_terminal_lease_never_reclaimed(tmp_path):
    store = _store(tmp_path)
    clock = ManualUTCClock()
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=clock)
    lm.ensure("c1", source_instance="i1")
    lm.acquire("c1", "w1")
    lm.complete("c1", worker_id="w1")
    clock.advance(10_000)  # long past any expiry
    assert lm.acquire("c1", "w2") is None
    assert lm.acquire_next("w2") is None
    store.close()


def test_already_terminal_child_is_never_leasable(tmp_path):
    store = _store(tmp_path)
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=ManualUTCClock())
    lm.ensure("c1", source_instance="i1", terminal=True)  # completed in a prior run
    assert lm.acquire("c1", "w1") is None
    store.close()


def test_duplicate_complete_is_harmless(tmp_path):
    store = _store(tmp_path)
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=ManualUTCClock())
    lm.ensure("c1", source_instance="i1")
    lm.acquire("c1", "w1")
    lm.complete("c1", worker_id="w1")
    lm.complete("c1", worker_id="w1")  # idempotent
    row = store.get_coverage_lease("c1")
    assert row["terminal"] == 1
    store.close()


def test_lease_events_are_auditable_and_utc(tmp_path):
    store = _store(tmp_path)
    clock = ManualUTCClock()
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=clock)
    lm.ensure("c1", source_instance="i1")
    lm.acquire("c1", "w1")
    lm.heartbeat("c1", "w1")
    lm.complete("c1", worker_id="w1")
    events = store.list_coverage_lease_events(coverage_id="c1")
    kinds = [e["event"] for e in events]
    assert kinds == ["ACQUIRE", "HEARTBEAT", "COMPLETE"]
    for e in events:
        assert e["at"].endswith("+00:00")  # UTC only, fixed-width ISO


def test_acquire_next_is_deterministic_and_respects_eligible(tmp_path):
    store = _store(tmp_path)
    lm = LeaseManager(store, "run", ttl_seconds=30, clock=ManualUTCClock())
    for cid in ("c3", "c1", "c2"):
        lm.ensure(cid, source_instance="i1")
    # Deterministic order: c1 first.
    assert lm.acquire_next("w1").coverage_id == "c1"
    # Restrict to eligible set.
    assert lm.acquire_next("w2", eligible=["c3"]).coverage_id == "c3"
    assert lm.acquire_next("w3", eligible=["c1", "c3"]) is None  # both taken
    assert lm.acquire_next("w3").coverage_id == "c2"
    store.close()
