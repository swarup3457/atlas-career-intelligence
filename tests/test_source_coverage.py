"""Phase 1A: coverage manifest + persistence tests."""

from __future__ import annotations

import pytest

from atlas.models import TaskStatus
from atlas.persistence.sqlite import StateStore
from atlas.sources.coverage import (
    CoverageManifest,
    CoverageStatus,
    CoverageTask,
    coverage_status_for,
)
from atlas.sources.models import ZeroResultKind

pytestmark = pytest.mark.unit


def test_status_mapping():
    assert coverage_status_for(TaskStatus.SUCCESS, result_count=3) == CoverageStatus.COMPLETED_WITH_RESULTS
    assert coverage_status_for(TaskStatus.SUCCESS, result_count=0) == CoverageStatus.ATTEMPTED_ZERO
    assert coverage_status_for(TaskStatus.NO_RELEVANT_RESULTS) == CoverageStatus.ATTEMPTED_ZERO
    assert coverage_status_for(TaskStatus.EXTRACTION_UNRESOLVED) == CoverageStatus.EXTRACTION_UNRESOLVED
    assert coverage_status_for(TaskStatus.ACCESS_LIMITED) == CoverageStatus.ACCESS_LIMITED
    assert coverage_status_for(TaskStatus.RATE_LIMITED) == CoverageStatus.RATE_LIMITED
    assert coverage_status_for(TaskStatus.SOURCE_UNAVAILABLE) == CoverageStatus.SOURCE_UNAVAILABLE
    assert coverage_status_for(TaskStatus.PERMANENT_FAILURE) == CoverageStatus.FAILED


def test_untrusted_zero_maps_to_extraction_unresolved():
    assert coverage_status_for(
        TaskStatus.NO_RELEVANT_RESULTS, zero_kind=ZeroResultKind.EXTRACTION_UNRESOLVED
    ) == CoverageStatus.EXTRACTION_UNRESOLVED


def test_manifest_completion_is_planned_vs_terminal():
    man = CoverageManifest("run-x")
    man.plan(CoverageTask(coverage_id="c1", source_instance="a"))
    man.plan(CoverageTask(coverage_id="c2", source_instance="b"))
    assert man.is_complete() is False
    man.mark("c1", CoverageStatus.COMPLETED_WITH_RESULTS, jobs_found=3)
    assert man.is_complete() is False  # c2 still not attempted
    man.mark("c2", CoverageStatus.ATTEMPTED_ZERO)
    assert man.is_complete() is True
    summary = man.summary()
    assert summary["_planned"] == 2 and summary["_terminal"] == 2


def test_not_attempted_distinct_from_attempted_zero():
    man = CoverageManifest("run-x")
    man.plan(CoverageTask(coverage_id="c1", source_instance="a"))
    assert man.get("c1").status == CoverageStatus.NOT_ATTEMPTED
    assert man.get("c1").is_terminal is False


def test_persistence_roundtrip(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        man = CoverageManifest("run-1")
        man.plan(CoverageTask(coverage_id="c1", source_instance="a", lane="L1"))
        man.plan(CoverageTask(coverage_id="c2", source_instance="b"))
        man.mark("c1", CoverageStatus.COMPLETED_WITH_RESULTS, jobs_found=5)
        man.persist(store)

        reloaded = CoverageManifest.load(store, "run-1")
        assert {t.coverage_id for t in reloaded.tasks()} == {"c1", "c2"}
        c1 = reloaded.get("c1")
        assert c1.status == CoverageStatus.COMPLETED_WITH_RESULTS
        assert c1.jobs_found == 5
        assert c1.lane == "L1"

        summary = store.coverage_summary("run-1")
        assert summary["_planned"] == 2
        assert summary["COMPLETED_WITH_RESULTS"] == 1
