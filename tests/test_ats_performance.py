"""Phase 1C-A — offline parallel-dispatch performance benchmark (build spec 22).

Injected transport (FakeAdapter, no network): 1,000 coverage children, four
workers, four ATS families' worth of mixed zero/one/ten results plus transient
retries and rate-limit signals and lease contention. Records throughput, DB and
checkpoint sizes, duplicate/reclaim counts, and the canonical count. No
universal promise — this is a local, deterministic benchmark, not the live
canary.
"""

from __future__ import annotations

import time

import pytest

from atlas.models import ErrorCategory
from atlas.persistence.sqlite import StateStore
from atlas.runtime.canonicalize import canonicalize_run
from atlas.runtime.parallel_pipeline import ParallelExecutionPipeline
from atlas.sources.coverage import TERMINAL_COVERAGE_STATUSES, CoverageManifest, CoverageTask
from atlas.sources.rate_limit import RatePolicy, RateLimiter
from atlas.sources.registry import SourceRegistry
from atlas.sources.testing.fake import FakeAdapter, make_fake_instance

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _registry():
    reg = SourceRegistry()
    reg.register(FakeAdapter)
    return reg


def _make_instance(k: int):
    m = k % 10
    if m == 0:
        return make_fake_instance(f"i{k}", scenario="zero", company=f"Co{k}")
    if m == 1:
        return make_fake_instance(f"i{k}", scenario="results", result_count=1, company=f"Co{k}")
    if m in (2, 3):
        return make_fake_instance(f"i{k}", scenario="results", result_count=10, company=f"Co{k}")
    if m == 4:
        # transient-then-success (retryable TIMEOUT — no backoff sleep)
        return make_fake_instance(f"i{k}", scenario="results", result_count=3, company=f"Co{k}",
                                  fail_first_n=1, transient_category=ErrorCategory.TIMEOUT.value)
    if m == 5:
        # rate-limit signal, terminal immediately at retry_budget=0 (no sleep)
        return make_fake_instance(f"i{k}", scenario="error", error_category=ErrorCategory.HTTP_429.value, company=f"Co{k}")
    return make_fake_instance(f"i{k}", scenario="results", result_count=5, company=f"Co{k}")


def test_parallel_dispatch_benchmark_1000_children(tmp_path):
    n = 1000
    db = tmp_path / "perf.sqlite"
    store = StateStore(db)
    store.create_run("perf", "none")
    instances = {}
    man = CoverageManifest("perf")
    for k in range(n):
        inst = _make_instance(k)
        instances[inst.instance_id] = inst
        store.upsert_source_instance(inst.instance_id, inst.adapter_key.value, inst.source_type.value, inst.category.value)
        man.plan(CoverageTask(coverage_id=f"c{k}", source_instance=inst.instance_id, company=f"Co{k}",
                              lane="JAVA_BACKEND", query_key="PRIMARY"))
    man.seal()
    man.persist(store, policy_fingerprint="pol")
    store.close()

    pipe = ParallelExecutionPipeline(
        lambda: StateStore(db), _registry(), instances, run_id="perf", policy_version="pol",
        retry_budget=0, workers=4,
        rate_limiter=RateLimiter(RatePolicy(min_interval_seconds=0.0)),
    )
    t0 = time.perf_counter()
    result = pipe.execute(man, [f"c{k}" for k in range(n)])
    elapsed = time.perf_counter() - t0

    store = StateStore(db)
    canonicalize_run(store, "perf")
    man2 = CoverageManifest.load(store, "perf")
    terminal = sum(1 for t in man2.tasks() if t.status in TERMINAL_COVERAGE_STATUSES)
    obs = store.list_raw_observations("perf")
    duplicate_obs = len(obs) - len({o["observation_id"] for o in obs})
    leases = store.list_coverage_leases("perf")
    reclaims = sum(max(0, int(l["attempt"]) - 1) for l in leases)
    canon = store.count_canonical_jobs()
    db_bytes = db.stat().st_size
    store.close()

    throughput = n / elapsed if elapsed > 0 else float("inf")
    print(f"[phase1c-perf] children={n} workers=4 elapsed={elapsed:.2f}s throughput={throughput:.0f}/s "
          f"terminal={terminal} canonical={canon} observations={len(obs)} duplicate_obs={duplicate_obs} "
          f"reclaims={reclaims} db_bytes={db_bytes}")

    assert result.executed == n
    assert terminal == n  # every child reached a durable terminal state
    assert duplicate_obs == 0  # crash-free run stages no duplicate observation
    assert canon > 0
