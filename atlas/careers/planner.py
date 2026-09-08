"""OfficialCareerCoveragePlanner (Phase 1C-B, build spec G/7).

Turns routed official-career targets (a due company + its trusted, routed source
instance + lane bundle + geography group + DELTA/DEEP mode) into a SEALED,
fingerprinted :class:`atlas.sources.coverage.CoverageManifest` — reusing the
proven :class:`atlas.planning.CoveragePlanner` so EVERY required lane stays
independently accountable (one child per company×instance×lane) and there is no
second planning path. It additionally returns the SourceInstance objects the
runtime must register, so the pilot can drive the standard production pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from atlas.careers.router import RouteDecision
from atlas.planning import CoveragePlanner, PlanInput, PlannedCompany
from atlas.sources.coverage import CoverageManifest
from atlas.sources.models import SourceInstance


@dataclass(frozen=True)
class OfficialCareerTarget:
    """One due company routed to a trusted official source instance."""

    company_id: str
    name: str
    official_domain: str
    route: RouteDecision
    tier: str = "A"
    mode: str = "DELTA"                 # DELTA | DEEP
    geography_group: str = "PRIMARY"

    @property
    def source_instance(self) -> Optional[SourceInstance]:
        return self.route.source_instance

    @property
    def routable(self) -> bool:
        return self.route.routable


@dataclass
class CareerCoveragePlan:
    manifest: CoverageManifest
    instances: dict[str, SourceInstance] = field(default_factory=dict)
    companies: list[PlannedCompany] = field(default_factory=list)
    unroutable: list[OfficialCareerTarget] = field(default_factory=list)

    @property
    def sealed_fingerprint(self) -> Optional[str]:
        return self.manifest.sealed_fingerprint


class OfficialCareerCoveragePlanner:
    """Builds a sealed official-career coverage plan from routed targets."""

    def build(
        self,
        run_id: str,
        targets: list[OfficialCareerTarget],
        *,
        lanes: list[str],
        geography_groups: Optional[list[str]] = None,
        policy_fingerprint: str = "unversioned",
        seal: bool = True,
    ) -> CareerCoveragePlan:
        instances: dict[str, SourceInstance] = {}
        companies: list[PlannedCompany] = []
        unroutable: list[OfficialCareerTarget] = []

        for target in targets:
            if not target.routable or target.source_instance is None:
                # A company whose entry point could not be routed still needs a
                # source-discovery coverage child so the plan is truthful (never
                # silently dropped).
                unroutable.append(target)
                companies.append(
                    PlannedCompany(
                        company_id=target.company_id, name=target.name, tier=target.tier,
                        mode=target.mode, source_instances=(), geography_group=target.geography_group,
                        needs_source_discovery=True,
                    )
                )
                continue
            inst = target.source_instance
            instances[inst.instance_id] = inst
            companies.append(
                PlannedCompany(
                    company_id=target.company_id, name=target.name, tier=target.tier,
                    mode=target.mode, source_instances=(inst.instance_id,),
                    geography_group=target.geography_group,
                )
            )

        plan_input = PlanInput(
            run_id=run_id, companies=companies, portal_families=[], ats_families=[],
            lanes=list(lanes), geography_groups=list(geography_groups or ["PRIMARY"]),
            policy_fingerprint=policy_fingerprint,
        )
        planner = CoveragePlanner()
        if seal:
            manifest = planner.build_and_seal(plan_input)
        else:
            manifest = planner.build(plan_input)
        return CareerCoveragePlan(
            manifest=manifest, instances=instances, companies=companies, unroutable=unroutable
        )


__all__ = [
    "OfficialCareerTarget",
    "CareerCoveragePlan",
    "OfficialCareerCoveragePlanner",
]
