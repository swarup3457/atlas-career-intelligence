"""End-to-end market pilot orchestrator (Phase 1D §14).

Seals ONE small, hashed pilot BEFORE execution (fresh unique run id), then drives
the COMPLETE market capability under a single run:

    A. domain-only OFFICIAL discovery + routing + execution through the ONE
       LangGraph production governor (ATS / generic HTTP / generic browser);
    B. READ-ONLY portal discovery (LinkedIn + Naukri) -> append-only leads;
    C. dynamic company registration + portal->official verification;
    D. bounded concurrency across worker pools (single authenticated owner);
    E. ONE current-run report.

Official coverage runs with the report DISABLED so exactly ONE report writer (the
market report) emits after fan-in. Individual access-limited/unsupported sources
are recorded truthfully and never fabricated.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from atlas.careers.pilot import CareerPilot, PilotCompany, PilotConfig
from atlas.config import Settings
from atlas.market.campaign import CampaignBudget
from atlas.market.runtime import MarketRunResult, MarketSearchRuntime
from atlas.planning import PlannedCompany
from atlas.sources.http_client import ReadOnlyHttpClient


@dataclass
class MarketPilotConfig:
    run_id: str
    official_companies: list[PilotCompany] = field(default_factory=list)
    lanes: list[str] = field(default_factory=lambda: ["JAVA_BACKEND", "GENERAL_SOFTWARE"])
    geography_groups: list[str] = field(default_factory=lambda: ["PRIMARY"])
    portal_families: list[str] = field(default_factory=lambda: ["linkedin", "naukri"])
    recency_days: int = 7
    max_pages: int = 2
    max_cards: int = 20

    def canonical(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "official_companies": [c.to_dict() for c in self.official_companies],
            "lanes": sorted(self.lanes),
            "geography_groups": sorted(self.geography_groups),
            "portal_families": sorted(self.portal_families),
            "recency_days": self.recency_days,
            "max_pages": self.max_pages,
            "max_cards": self.max_cards,
        }

    def seal_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class MarketPilotResult:
    run_id: str
    config_hash: str
    status: str
    market: Optional[dict] = None
    official_terminal: Optional[str] = None
    official_reported: int = 0
    portal_leads: int = 0
    dynamic_companies: int = 0
    verified_links: int = 0
    report_path: Optional[str] = None
    report_valid: Optional[bool] = None
    max_concurrency: dict = field(default_factory=dict)
    auth_profile_owner_max: int = 0
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items()}


class MarketPilot:
    def __init__(
        self,
        settings: Settings,
        config: MarketPilotConfig,
        *,
        budget: Optional[CampaignBudget] = None,
        portal_metadata: Optional[dict[str, dict]] = None,
        portal_client_factory: Optional[Callable[[], ReadOnlyHttpClient]] = None,
        official_client_factory: Optional[Callable[[], ReadOnlyHttpClient]] = None,
        browser_profile_dir: Optional[Path] = None,
        pool_caps: Optional[dict[str, int]] = None,
    ):
        self.settings = settings
        self.config = config
        self.budget = budget or CampaignBudget(
            max_waves=2, max_portal_pages=config.max_pages, max_portal_cards=config.max_cards)
        self.portal_metadata = portal_metadata or {}
        self.portal_client_factory = portal_client_factory
        self.official_client_factory = official_client_factory
        self.browser_profile_dir = browser_profile_dir
        self.pool_caps = pool_caps

    def _official_pilot_callable(self) -> Optional[Callable[[], Any]]:
        """Run OFFICIAL coverage (discovery + routing + the ONE production
        governor) with the report DISABLED, so only the market report writes.
        Returns None when there are no official companies."""
        if not self.config.official_companies:
            return None

        def _run() -> None:
            from atlas.runtime.production import ProductionSearchRuntime
            from atlas.sources.generic import build_careers_registry

            pilot_cfg = PilotConfig(
                run_id=self.config.run_id, companies=list(self.config.official_companies),
                lanes=list(self.config.lanes), geography_groups=list(self.config.geography_groups),
                max_pages=self.config.max_pages, max_jobs_per_company=self.config.max_cards)
            pilot = CareerPilot(self.settings, pilot_cfg, browser_profile_dir=self.browser_profile_dir)
            prepared = pilot.prepare(live=True)
            pilot._persist_prepared(prepared)
            instances = {}
            companies: list[PlannedCompany] = []
            for entry in prepared.routable:
                inst = entry.decision.source_instance
                instances[inst.instance_id] = inst
                companies.append(PlannedCompany(
                    company_id=entry.company.company_id, name=entry.company.name, tier=entry.company.tier,
                    mode=entry.company.mode, source_instances=(inst.instance_id,),
                    geography_group=entry.company.geography_group))
            if not companies:
                return
            runtime = ProductionSearchRuntime(
                self.settings, self.config.run_id, fixture_mode=True, companies=companies,
                instances=instances, registry=build_careers_registry(), parallel_workers=1,
                lane_override=list(self.config.lanes), max_per_company=1, max_per_instance=1,
                max_per_tenant=1, retry_budget=2, write_report=False, use_run_lock=False)
            rr = runtime.run()
            pilot._mark_successful_profiles_healthy(instances)
            self._official_terminal = rr.terminal_state

        return _run

    def run(self, *, live: bool, resume: bool = False, report_path: Optional[Path] = None) -> MarketPilotResult:
        started = time.monotonic()
        config_hash = self.config.seal_hash()
        self._official_terminal = None
        # Seal the pilot config BEFORE execution.
        from atlas.persistence.sqlite import StateStore

        with StateStore(self.settings.state_db) as store:
            store.upsert_career_pilot_run(
                f"marketpilot::{self.config.run_id}", config_hash,
                config=self.config.canonical(), status="SEALED")

        rt = MarketSearchRuntime(
            self.settings, self.config.run_id, lanes=list(self.config.lanes),
            geography_groups=list(self.config.geography_groups),
            portal_families=tuple(self.config.portal_families), budget=self.budget,
            portal_metadata=self.portal_metadata, portal_client_factory=self.portal_client_factory,
            official_pilot=self._official_pilot_callable() if live else None,
            pool_caps=self.pool_caps, recency_days=self.config.recency_days)
        market: MarketRunResult = rt.run(live=live, resume=resume, report_path=report_path)

        notes: list[str] = []
        official_reported = 0
        with StateStore(self.settings.state_db) as store:
            for row in store.list_raw_observations(self.config.run_id):
                fam = (row["source_family"] or "").lower()
                if fam not in ("linkedin", "naukri", "foundit", "indeed", "portal_generic"):
                    official_reported += 1

        status = market.status
        if live and self.config.official_companies and self._official_terminal is None:
            notes.append("official coverage produced no terminal (check discovery)")
        result = MarketPilotResult(
            run_id=self.config.run_id, config_hash=config_hash, status=status,
            market=market.to_dict(), official_terminal=self._official_terminal,
            official_reported=official_reported, portal_leads=market.portal_leads,
            dynamic_companies=market.dynamic_companies, verified_links=market.verified_links,
            report_path=market.report_path, report_valid=market.report_valid,
            max_concurrency=market.summary.get("max_observed_concurrency", {}),
            auth_profile_owner_max=market.summary.get("authenticated_profile_owner_max", 0),
            elapsed_s=time.monotonic() - started, notes=notes)
        with StateStore(self.settings.state_db) as store:
            store.upsert_career_pilot_run(
                f"marketpilot::{self.config.run_id}", config_hash,
                config=self.config.canonical(), status=status, results=result.to_dict())
        return result


__all__ = ["MarketPilotConfig", "MarketPilotResult", "MarketPilot"]
