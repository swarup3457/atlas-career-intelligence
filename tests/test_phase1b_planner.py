"""Phase 1B — coverage planner archetype tests (build spec section 15/24)."""

from __future__ import annotations

import pytest

from atlas.planning import CoveragePlanner, PlanInput, PlannedCompany, TaskArchetype, cartesian_upper_bound
from atlas.sources.coverage import CoveragePlanState, TerminalPlanState

pytestmark = pytest.mark.unit

LANES = ["GENERAL_SOFTWARE", "JAVA_BACKEND", "JAVA_FULLSTACK", "REACT_FRONTEND", "DOTNET", "ENTERPRISE_HR_PAYROLL_INTEGRATION"]
PORTALS = ["linkedin", "naukri", "foundit", "indeed"]
ATS = ["workday", "greenhouse", "lever", "ashby"]
GEO = ["PRIMARY", "SECONDARY", "EXPANSION"]


def _plan_input(n_companies=108) -> PlanInput:
    companies = [
        PlannedCompany(
            company_id=f"co{i}", name=f"Company {i}", tier="A", mode="DEEP",
            source_instances=(f"co{i}-workday",), geography_group="PRIMARY",
        )
        for i in range(n_companies)
    ]
    return PlanInput(
        run_id="run-1", companies=companies, portal_families=PORTALS,
        ats_families=ATS, lanes=LANES, geography_groups=GEO, policy_fingerprint="polfp",
    )


def test_planner_avoids_blind_cartesian_explosion():
    manifest = CoveragePlanner().build_and_seal(_plan_input(108))
    n_tasks = len(manifest.tasks())
    naive = cartesian_upper_bound(companies=108, sources=len(PORTALS) + len(ATS), lanes=len(LANES), cities=15)
    # The naive product is enormous; the archetype plan must be a tiny fraction.
    assert naive > 50000
    assert n_tasks < naive / 50


def test_portal_discovery_is_per_source_not_per_company():
    manifest = CoveragePlanner().build(_plan_input(108))
    portal_tasks = [t for t in manifest.tasks() if t.source_type == TaskArchetype.PORTAL_DISCOVERY.value]
    # portals × lanes × geo groups, independent of the 108 companies
    assert len(portal_tasks) == len(PORTALS) * len(LANES) * len(GEO)


def test_company_task_has_independent_per_lane_child_coverage():
    # P0-11: each required lane is an INDEPENDENT child coverage row so a
    # failed lane cannot be hidden by a successful sibling lane. Geography
    # stays a group (never per-city), so there is no blind explosion.
    manifest = CoveragePlanner().build(_plan_input(3))
    company_tasks = [t for t in manifest.tasks() if t.source_type in (TaskArchetype.COMPANY_DEEP.value, TaskArchetype.COMPANY_DELTA.value)]
    # 3 companies × 1 source instance × 6 lanes = 18 independent child rows.
    assert len(company_tasks) == 3 * len(LANES)
    lanes_seen = {t.lane for t in company_tasks}
    assert lanes_seen == set(LANES)  # every required lane accounted for
    for t in company_tasks:
        assert "+" not in (t.lane or "")  # never a bundled lane string
        assert t.detail.get("parent_batch")  # shares a transport batch key


def test_lane_summary_is_independently_accountable():
    manifest = CoveragePlanner().build_and_seal(_plan_input(2))
    from atlas.sources.coverage import CoverageStatus

    lane_sum = manifest.lane_summary()
    for lane in LANES:
        assert lane in lane_sum
        assert lane_sum[lane]["planned"] >= 2  # 2 companies contribute per lane
        assert lane_sum[lane]["terminal"] == 0
    # Marking ONE lane's children terminal never marks another lane terminal.
    react_children = [t for t in manifest.tasks() if t.lane == "REACT_FRONTEND"
                      and t.source_type in (TaskArchetype.COMPANY_DEEP.value, TaskArchetype.COMPANY_DELTA.value)]
    for t in react_children:
        manifest.mark(t.coverage_id, CoverageStatus.EXTRACTION_UNRESOLVED)
    ls2 = manifest.lane_summary()
    assert ls2["REACT_FRONTEND"]["terminal"] == len(react_children)
    assert ls2["JAVA_BACKEND"]["terminal"] == 0  # sibling lane unaffected


def test_ats_cross_company_discovery_per_family():
    manifest = CoveragePlanner().build(_plan_input(10))
    ats_tasks = [t for t in manifest.tasks() if t.source_type == TaskArchetype.ATS_CROSS_COMPANY_DISCOVERY.value]
    assert len(ats_tasks) == len(ATS) * len(GEO)


def test_production_plan_is_sealed_and_fingerprinted():
    manifest = CoveragePlanner().build_and_seal(_plan_input(5))
    assert manifest.state == CoveragePlanState.SEALED
    assert manifest.is_executable()
    assert manifest.sealed_fingerprint
    assert manifest.terminal_state() == TerminalPlanState.IN_PROGRESS


def test_plan_fingerprint_is_deterministic():
    a = CoveragePlanner().build(_plan_input(5)).fingerprint()
    b = CoveragePlanner().build(_plan_input(5)).fingerprint()
    assert a == b


def test_unknown_company_gets_source_discovery_task():
    plan = PlanInput(
        run_id="run-x",
        companies=[PlannedCompany(company_id="new1", name="New Co", source_instances=(), needs_source_discovery=True)],
        portal_families=[], ats_families=[], lanes=LANES, geography_groups=["PRIMARY"],
    )
    manifest = CoveragePlanner().build(plan)
    types = {t.source_type for t in manifest.tasks()}
    assert TaskArchetype.COMPANY_SOURCE_DISCOVERY.value in types


def test_empty_plan_seals_as_no_work_due():
    plan = PlanInput(run_id="run-empty", companies=[], portal_families=[], ats_families=[], lanes=[], geography_groups=[])
    manifest = CoveragePlanner().build_and_seal(plan)
    assert manifest.terminal_state() == TerminalPlanState.NO_WORK_DUE
    assert manifest.is_complete() is True
