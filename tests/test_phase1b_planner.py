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


def test_company_task_carries_lane_bundle_not_per_lane_city():
    manifest = CoveragePlanner().build(_plan_input(3))
    company_tasks = [t for t in manifest.tasks() if t.source_type in (TaskArchetype.COMPANY_DEEP.value, TaskArchetype.COMPANY_DELTA.value)]
    # 3 companies × 1 source instance each = 3 tasks (NOT 3×6 lanes×cities)
    assert len(company_tasks) == 3
    for t in company_tasks:
        assert "+" in t.lane  # the whole lane bundle in one task


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
