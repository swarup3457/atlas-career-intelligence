"""Phase 1B — query signatures + sealed coverage plans (build spec 7.7/7.8)."""

from __future__ import annotations

import pytest

from atlas.sources.coverage import (
    CoverageManifest,
    CoveragePlanState,
    CoverageStatus,
    CoverageTask,
    DuplicateCoverageError,
    EmptyPlanError,
    PlanNotSealedError,
    TerminalPlanState,
)
from atlas.sources.models import SourceFamily, SourceInstance, SourceType
from atlas.sources.query_signature import QuerySignature

pytestmark = pytest.mark.unit


# --- 7.7 query signatures -------------------------------------------------
def _sig(**kw):
    base = dict(
        source_instance="acme-wd",
        adapter_key=SourceFamily.WORKDAY,
        lane="JAVA_BACKEND",
        geography_group="PRIMARY",
        search_mode="DELTA",
        keywords=["Java", "Spring Boot"],
    )
    base.update(kw)
    return QuerySignature.build(**base)


def test_query_signature_is_stable_and_order_insensitive():
    a = _sig(keywords=["Java", "Spring Boot"])
    b = _sig(keywords=["spring boot", "java "])
    assert a.fingerprint() == b.fingerprint()


def test_query_signature_differs_by_lane_geo_mode():
    base = _sig()
    assert base.fingerprint() != _sig(lane="REACT_FRONTEND").fingerprint()
    assert base.fingerprint() != _sig(geography_group="SECONDARY").fingerprint()
    assert base.fingerprint() != _sig(search_mode="DEEP").fingerprint()
    assert base.fingerprint() != _sig(keywords=["Java"]).fingerprint()


def test_query_signature_differs_by_policy_version():
    assert _sig(policy_version="v1").fingerprint() != _sig(policy_version="v2").fingerprint()


def test_unrelated_history_cannot_collide():
    """A zero for one lane/geo must not be keyed the same as an unrelated
    search on the same instance (the false selector-drift bug)."""
    java_primary = _sig(lane="JAVA_BACKEND", geography_group="PRIMARY")
    react_secondary = _sig(lane="REACT_FRONTEND", geography_group="SECONDARY")
    assert java_primary.key != react_secondary.key


def test_query_signature_for_instance():
    inst = SourceInstance(
        instance_id="li-in", source_type=SourceType.PORTAL_LARGE, source_family=SourceFamily.LINKEDIN
    )
    sig = QuerySignature.for_instance(inst, lane="DOTNET", geography_group="PRIMARY")
    assert sig.adapter_key == SourceFamily.LINKEDIN
    assert sig.source_instance == "li-in"


def test_invalid_search_mode_rejected():
    with pytest.raises(ValueError):
        QuerySignature.build(source_instance="x", adapter_key=SourceFamily.WORKDAY, search_mode="WEEKLY")


# --- 7.8 sealed coverage plans --------------------------------------------
def test_empty_unsealed_plan_is_not_complete():
    man = CoverageManifest("run-x")
    assert man.state == CoveragePlanState.BUILDING
    assert man.is_complete() is False
    assert man.terminal_state() == TerminalPlanState.BUILDING


def test_empty_plan_cannot_seal_without_allow_empty():
    man = CoverageManifest("run-x")
    with pytest.raises(EmptyPlanError):
        man.seal()
    # explicit NO_WORK_DUE is allowed and truthful
    fp = man.seal(allow_empty=True)
    assert fp
    assert man.no_work_due is True
    assert man.terminal_state() == TerminalPlanState.NO_WORK_DUE
    assert man.is_complete() is True  # nothing due == complete-as-no-work


def test_failed_plan_is_not_no_work_due_and_not_complete():
    man = CoverageManifest("run-x")
    man.mark_failed("planner exploded")
    assert man.state == CoveragePlanState.FAILED
    assert man.terminal_state() == TerminalPlanState.FAILED
    assert man.is_complete() is False
    with pytest.raises(PlanNotSealedError):
        man.seal()


def test_duplicate_conflicting_task_id_rejected():
    man = CoverageManifest("run-x")
    man.plan(CoverageTask(coverage_id="c1", source_instance="a", lane="JAVA_BACKEND"))
    # same id, different identity -> reject silent overwrite
    with pytest.raises(DuplicateCoverageError):
        man.plan(CoverageTask(coverage_id="c1", source_instance="b", lane="REACT_FRONTEND"))


def test_exact_duplicate_plan_insertion_is_idempotent():
    man = CoverageManifest("run-x")
    t = CoverageTask(coverage_id="c1", source_instance="a", lane="JAVA_BACKEND")
    man.plan(t)
    man.plan(CoverageTask(coverage_id="c1", source_instance="a", lane="JAVA_BACKEND"))
    assert len(man.tasks()) == 1


def test_sealed_plan_rejects_new_tasks_but_fingerprint_is_stable():
    man = CoverageManifest("run-x")
    man.plan(CoverageTask(coverage_id="c1", source_instance="a", lane="JAVA_BACKEND"))
    fp = man.seal()
    assert man.is_executable() is True
    assert man.sealed_fingerprint == fp
    with pytest.raises(PlanNotSealedError):
        man.plan(CoverageTask(coverage_id="c2", source_instance="b"))


def test_unsealed_plan_is_not_executable():
    man = CoverageManifest("run-x")
    man.plan(CoverageTask(coverage_id="c1", source_instance="a"))
    assert man.is_executable() is False
    with pytest.raises(PlanNotSealedError):
        man.require_sealed()


def test_sealed_plan_completes_only_when_all_terminal():
    man = CoverageManifest("run-x")
    man.plan(CoverageTask(coverage_id="c1", source_instance="a"))
    man.plan(CoverageTask(coverage_id="c2", source_instance="b"))
    man.seal()
    assert man.terminal_state() == TerminalPlanState.IN_PROGRESS
    man.mark("c1", CoverageStatus.COMPLETED_WITH_RESULTS, jobs_found=3)
    assert man.terminal_state() == TerminalPlanState.IN_PROGRESS
    man.mark("c2", CoverageStatus.ATTEMPTED_ZERO)
    assert man.terminal_state() == TerminalPlanState.COMPLETE
    assert man.is_complete() is True


def test_batch_size_is_not_completion():
    """A plan with many tasks is not 'complete' merely because a batch ran;
    completion requires every planned task terminal."""
    man = CoverageManifest("run-x")
    for i in range(40):
        man.plan(CoverageTask(coverage_id=f"c{i}", source_instance=f"s{i}"))
    man.seal()
    for i in range(25):  # a 25-40 style batch
        man.mark(f"c{i}", CoverageStatus.ATTEMPTED_ZERO)
    assert man.is_complete() is False
    assert man.terminal_state() == TerminalPlanState.IN_PROGRESS
