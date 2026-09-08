"""Atlas coverage planner (Phase 1B, build spec section 15).

Turns due companies + policy into a SEALED, fingerprinted
:class:`CoverageManifest` using task archetypes that avoid a blind Cartesian
explosion of ``108 companies × every source × 6 lanes × every city``:

    * COMPANY_DELTA / COMPANY_DEEP — ONE task per due company that carries a
      validated query bundle (lanes) and a geography GROUP, not one task per
      lane×city.
    * PORTAL_DISCOVERY — per portal family × lane × geography group, NOT per
      company.
    * ATS_CROSS_COMPANY_DISCOVERY — per ATS family × geography group.
    * OFFICIAL_VERIFICATION / COMPANY_SOURCE_DISCOVERY / DETAIL_HYDRATION —
      created only for specific leads/needs (not part of the base sweep).

Every plan is deterministic, sealed, fingerprinted, and measurable as
PLANNED vs TERMINAL.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from atlas.sources.coverage import CoverageManifest, CoverageTask


class TaskArchetype(str, enum.Enum):
    COMPANY_DELTA = "COMPANY_DELTA"
    COMPANY_DEEP = "COMPANY_DEEP"
    PORTAL_DISCOVERY = "PORTAL_DISCOVERY"
    ATS_CROSS_COMPANY_DISCOVERY = "ATS_CROSS_COMPANY_DISCOVERY"
    OFFICIAL_VERIFICATION = "OFFICIAL_VERIFICATION"
    COMPANY_SOURCE_DISCOVERY = "COMPANY_SOURCE_DISCOVERY"
    DETAIL_HYDRATION = "DETAIL_HYDRATION"


@dataclass(frozen=True)
class PlannedCompany:
    company_id: str
    name: str
    tier: str = "A"
    mode: str = "DELTA"                 # DELTA | DEEP
    source_instances: tuple[str, ...] = ()   # official/ATS instance ids
    geography_group: str = "PRIMARY"
    needs_source_discovery: bool = False     # unknown/new company w/o source instance


@dataclass
class PlanInput:
    run_id: str
    companies: list[PlannedCompany] = field(default_factory=list)
    portal_families: list[str] = field(default_factory=list)
    ats_families: list[str] = field(default_factory=list)
    lanes: list[str] = field(default_factory=list)
    geography_groups: list[str] = field(default_factory=lambda: ["PRIMARY"])
    policy_fingerprint: str = "unversioned"


def _lane_bundle_key(lanes: list[str]) -> str:
    return "+".join(sorted(lanes)) if lanes else "ALL"


class CoveragePlanner:
    """Builds a sealed coverage plan from typed archetypes."""

    def build(self, plan: PlanInput) -> CoverageManifest:
        manifest = CoverageManifest(plan.run_id)
        lane_key = _lane_bundle_key(plan.lanes)
        # Required lanes for per-lane child accountability (build spec 8 / P0-11).
        req_lanes = list(plan.lanes) or ["ALL"]

        # 1. One company task per due company. To keep EVERY required lane
        #    independently accountable (P0-11) we emit ONE child coverage row
        #    per company×source_instance×lane — a failed React extraction can
        #    never be hidden by a successful Java result — while the shared
        #    parent batch key lets execution combine compatible lane queries
        #    for efficient transport. Geography stays a GROUP (never per-city),
        #    so there is no blind company×source×lane×city explosion.
        for company in plan.companies:
            if company.needs_source_discovery or not company.source_instances:
                manifest.plan(
                    CoverageTask(
                        coverage_id=f"csd::{company.company_id}",
                        source_instance="__discovery__",
                        company=company.name,
                        source_type=TaskArchetype.COMPANY_SOURCE_DISCOVERY.value,
                        lane=lane_key,
                        query_key=f"{company.geography_group}",
                        next_action="DISCOVER_SOURCE",
                        detail={"archetype": TaskArchetype.COMPANY_SOURCE_DISCOVERY.value},
                    )
                )
                continue
            archetype = TaskArchetype.COMPANY_DEEP if company.mode.upper() == "DEEP" else TaskArchetype.COMPANY_DELTA
            for instance_id in company.source_instances:
                parent_batch = f"{archetype.value.lower()}::{company.company_id}::{instance_id}"
                for lane in req_lanes:
                    manifest.plan(
                        CoverageTask(
                            coverage_id=f"{parent_batch}::{lane}",
                            source_instance=instance_id,
                            company=company.name,
                            source_type=archetype.value,
                            lane=lane,
                            query_key=f"{company.geography_group}",
                            next_action="SEARCH",
                            detail={
                                "archetype": archetype.value,
                                "geography_group": company.geography_group,
                                "parent_batch": parent_batch,
                            },
                        )
                    )

        # 2. Portal discovery: per portal × lane × geography group — NOT per
        #    company. This is where the Cartesian explosion is avoided.
        for portal in sorted(plan.portal_families):
            for lane in sorted(plan.lanes):
                for geo in sorted(plan.geography_groups):
                    manifest.plan(
                        CoverageTask(
                            coverage_id=f"portal::{portal}::{lane}::{geo}",
                            source_instance=f"portal::{portal}",
                            company=None,
                            source_type=TaskArchetype.PORTAL_DISCOVERY.value,
                            lane=lane,
                            query_key=geo,
                            next_action="DISCOVER",
                            detail={"archetype": TaskArchetype.PORTAL_DISCOVERY.value},
                        )
                    )

        # 3. ATS cross-company discovery: per ATS family × geography group.
        for ats in sorted(plan.ats_families):
            for geo in sorted(plan.geography_groups):
                manifest.plan(
                    CoverageTask(
                        coverage_id=f"ats::{ats}::{geo}",
                        source_instance=f"ats::{ats}",
                        company=None,
                        source_type=TaskArchetype.ATS_CROSS_COMPANY_DISCOVERY.value,
                        lane=lane_key,
                        query_key=geo,
                        next_action="DISCOVER",
                        detail={"archetype": TaskArchetype.ATS_CROSS_COMPANY_DISCOVERY.value},
                    )
                )

        return manifest

    def build_and_seal(self, plan: PlanInput) -> CoverageManifest:
        manifest = self.build(plan)
        if not manifest.tasks():
            manifest.seal(allow_empty=True)  # truthful NO_WORK_DUE
        else:
            manifest.seal()
        return manifest


def cartesian_upper_bound(companies: int, sources: int, lanes: int, cities: int) -> int:
    """The naive blind product the planner MUST stay well under."""
    return companies * sources * lanes * cities


__all__ = [
    "TaskArchetype",
    "PlannedCompany",
    "PlanInput",
    "CoveragePlanner",
    "cartesian_upper_bound",
]
