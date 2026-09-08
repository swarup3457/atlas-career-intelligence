"""Phase 1B.1 — common production execution path over fixture adapters
(build spec 4/9/10/12/27 / P0-1, P0-12, P0-13, P0-15).

Proves the sealed coverage plan is executed through the REAL pipeline:
SourceRegistry -> adapter -> shared RateLimitedExecutor -> SourceSearchWorker
-> centralized retry -> append-only attempts -> signature-keyed health -> raw
observation staging -> coverage terminal status, then canonicalization.
"""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory
from atlas.persistence.sqlite import StateStore
from atlas.runtime.canonicalize import canonicalize_run
from atlas.runtime.fixture_pipeline import FixtureExecutionPipeline, canonical_dedupe_key
from atlas.sources.coverage import CoverageManifest, CoverageStatus, CoverageTask
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.rate_limit import ManualClock, RateLimiter, RatePolicy
from atlas.sources.registry import SourceRegistry
from atlas.sources.testing.fake import FakeAdapter, make_fake_instance
from atlas.sources.testing.fixture import FixtureAdapter, make_fixture_instance

pytestmark = pytest.mark.integration


def _registry() -> SourceRegistry:
    reg = SourceRegistry()
    reg.register(FakeAdapter)
    reg.register(FixtureAdapter)
    return reg


def _executor() -> RateLimitedExecutor:
    clock = ManualClock()
    return RateLimitedExecutor(RateLimiter(RatePolicy(min_interval_seconds=0.0), clock=clock.time, sleeper=clock.sleep))


def _pipeline(tmp_path, instances):
    store = StateStore(tmp_path / "s.sqlite")
    store.create_run("run-p", controller="none")
    for inst in instances.values():
        store.upsert_source_instance(
            inst.instance_id, inst.adapter_key.value, inst.source_type.value, inst.category.value
        )
    pipe = FixtureExecutionPipeline(
        store, _registry(), instances, executor=_executor(), run_id="run-p", policy_version="v1"
    )
    return store, pipe


def test_pipeline_executes_all_scenarios_and_persists_attempts(tmp_path):
    instances = {
        "res": make_fake_instance("res", scenario="results", result_count=3, company="Acme"),
        "trans": make_fake_instance("trans", scenario="results", result_count=2, fail_first_n=1,
                                    transient_category=ErrorCategory.TIMEOUT.value),
        "rl": make_fake_instance("rl", scenario="error", error_category=ErrorCategory.HTTP_429.value, retry_after=5),
        "zero": make_fake_instance("zero", scenario="zero"),
        "untrusted": make_fake_instance("untrusted", scenario="untrusted_zero", sentinel="healthy"),
        "drift": make_fake_instance("drift", scenario="parse_failure"),
        "access": make_fake_instance("access", scenario="error", error_category=ErrorCategory.ANTI_BOT.value),
    }
    store, pipe = _pipeline(tmp_path, instances)
    man = CoverageManifest("run-p")
    for i, iid in enumerate(instances):
        man.plan(CoverageTask(coverage_id=f"c-{iid}", source_instance=iid, lane="JAVA_BACKEND",
                              query_key="PRIMARY", company="Acme"))
    man.seal()
    man.persist(store, policy_fingerprint="pol")

    res = pipe.execute(man, [t.coverage_id for t in man.tasks()])
    assert res.executed == len(instances)

    # Every child reached a durable terminal (or human-blocked) status.
    statuses = {t.coverage_id: t.status for t in man.tasks()}
    assert statuses["c-res"] == CoverageStatus.COMPLETED_WITH_RESULTS
    assert statuses["c-trans"] == CoverageStatus.COMPLETED_WITH_RESULTS  # transient retried then succeeded
    assert statuses["c-rl"] == CoverageStatus.RATE_LIMITED               # 429 exhausted
    assert statuses["c-zero"] == CoverageStatus.ATTEMPTED_ZERO           # trusted zero
    assert statuses["c-drift"] == CoverageStatus.EXTRACTION_UNRESOLVED   # parse drift
    assert statuses["c-access"] == CoverageStatus.ACCESS_LIMITED         # anti-bot

    # Append-only attempts: the transient task recorded >1 attempt.
    trans_attempts = store.list_coverage_attempts("c-trans")
    assert len(trans_attempts) >= 2
    # Attempts persisted with query signature.
    assert all(a["query_signature"] for a in store.list_coverage_attempts("c-res"))
    # Raw observations were staged, NOT written to canonical jobs during DISCOVER.
    assert store.count_raw_observations("run-p") >= 3
    assert store.count_canonical_jobs() == 0
    store.close()


def test_untrusted_zero_runs_exactly_one_sentinel(tmp_path):
    instances = {"u": make_fake_instance("u", scenario="untrusted_zero", sentinel="healthy")}
    store, pipe = _pipeline(tmp_path, instances)
    man = CoverageManifest("run-p")
    man.plan(CoverageTask(coverage_id="c-u", source_instance="u", lane="DOTNET", query_key="PRIMARY"))
    man.seal()
    man.persist(store)
    res = pipe.execute(man, ["c-u"])
    assert res.sentinels_run == 1
    store.close()


def test_signature_keyed_health_does_not_contaminate_other_lane(tmp_path):
    instances = {"i": make_fake_instance("i", scenario="results", result_count=5)}
    store, pipe = _pipeline(tmp_path, instances)
    man = CoverageManifest("run-p")
    man.plan(CoverageTask(coverage_id="java", source_instance="i", lane="JAVA_BACKEND", query_key="PRIMARY"))
    man.plan(CoverageTask(coverage_id="dotnet", source_instance="i", lane="DOTNET", query_key="MUMBAI"))
    man.seal()
    man.persist(store)
    pipe.execute(man, ["java", "dotnet"])
    # Health/yield history is keyed by the FULL query signature, so the two
    # lanes have DIFFERENT signatures and their yields never mix (P0-12).
    from atlas.sources.query_signature import QuerySignature

    inst = instances["i"]
    java_sig = QuerySignature.for_instance(inst, lane="JAVA_BACKEND", geography_group="PRIMARY",
                                           keywords=["JAVA_BACKEND"], policy_version="v1")
    dotnet_sig = QuerySignature.for_instance(inst, lane="DOTNET", geography_group="MUMBAI",
                                             keywords=["DOTNET"], policy_version="v1")
    assert java_sig.fingerprint() != dotnet_sig.fingerprint()
    assert store.recent_yields_for_signature(java_sig.fingerprint()) == [5]
    assert store.recent_yields_for_signature(dotnet_sig.fingerprint()) == [5]
    store.close()


def test_canonicalization_dedupes_cross_source_keeps_reposts_separate(tmp_path):
    same = dict(title="Java Dev", company="Acme", location="Bengaluru", posted_at="2026-09-01", url="https://x/1")
    repost = dict(title="Java Dev", company="Acme", location="Bengaluru", posted_at="2026-08-01", url="https://x/2")
    instances = {
        "a": make_fixture_instance("a", [dict(id="1", **same)]),
        "b": make_fixture_instance("b", [dict(id="99", **same)]),   # cross-source duplicate
        "c": make_fixture_instance("c", [dict(id="2", **repost)]),  # probable repost (diff date)
    }
    store, pipe = _pipeline(tmp_path, instances)
    man = CoverageManifest("run-p")
    for iid in instances:
        man.plan(CoverageTask(coverage_id=f"c-{iid}", source_instance=iid, lane="JAVA_BACKEND", query_key="PRIMARY"))
    man.seal()
    man.persist(store)
    pipe.execute(man, [t.coverage_id for t in man.tasks()])

    result = canonicalize_run(store, "run-p")
    # a+b collapse to ONE canonical (cross-source dup); c is a separate repost.
    assert store.count_canonical_jobs() == 2
    assert result.canonical_created == 2
    assert result.cross_source_duplicates == 1
    # Idempotent: a second canonicalization creates nothing new.
    again = canonicalize_run(store, "run-p")
    assert again.canonical_created == 0
    assert store.count_canonical_jobs() == 2
    store.close()
