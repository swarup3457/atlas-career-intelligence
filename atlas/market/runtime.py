"""Market search runtime — the deterministic adaptive campaign controller (§10/11/14).

Drives ONE market-discovery run: create + seal a campaign, plan Wave 0 baseline
coverage, execute each SEALED wave through the bounded worker pools, register
dynamic companies from portal leads, link portal leads to official evidence, then
let the deterministic :class:`CoverageDeficitAnalyzer` decide whether to seal a
bounded Wave N+1 — or finish. Completion, budgets, and the wave loop are
deterministic Python; the ONE LangGraph production governor owns OFFICIAL-coverage
completion (run via :class:`atlas.careers.pilot.CareerPilot`), and an LLM never
declares completion.

Global campaign budget (HTTP/browser/results/wall-clock) is enforced across all
adapters via the run-scoped ``campaign_budget`` ledger; exhaustion is
PARTIAL_BUDGET, never COMPLETE. A fresh live run requires a unique run id (a
completed campaign is never silently reused). One report writer emits a single
current-run workbook after fan-in.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from atlas.config import Settings
from atlas.market.campaign import (
    CampaignBudget,
    CampaignStatus,
    MarketCampaign,
    MarketWave,
    WaveStatus,
    WaveTask,
    WaveTaskKind,
)
from atlas.market.deficit import (
    CoverageDeficitAnalyzer,
    LaneSourceOutcome,
    WaveExpansionPlanner,
    WaveOutcome,
)
from atlas.market.dynamic_company import DynamicCompanyRegistrar
from atlas.market.pools import MarketPoolDispatcher, PoolTask, pool_for_class
from atlas.market.report import write_market_report
from atlas.market.verification import PortalOfficialVerifier
from atlas.persistence.sqlite import StateStore
from atlas.planning.query_compiler import SearchQueryCompiler
from atlas.policy import load_policy
from atlas.sources.http_client import ReadOnlyHttpClient
from atlas.sources.models import SearchRequest, SourceFamily
from atlas.sources.portals import PortalJobLead, make_portal_instance
from atlas.sources.portals.registry import PORTAL_FAMILIES, build_portals_registry


class MarketRunReuseError(RuntimeError):
    """Raised when a FRESH live campaign reuses a completed run id (build spec 5.8)."""


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


_FAMILY_BY_NAME = {"linkedin": SourceFamily.LINKEDIN, "naukri": SourceFamily.NAUKRI}


@dataclass
class MarketRunResult:
    run_id: str
    campaign_id: str
    status: str
    terminal_reason: Optional[str] = None
    waves: list[dict] = field(default_factory=list)
    portal_leads: int = 0
    dynamic_companies: int = 0
    verified_links: int = 0
    report_path: Optional[str] = None
    report_valid: Optional[bool] = None
    budget: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "campaign_id": self.campaign_id, "status": self.status,
            "terminal_reason": self.terminal_reason, "waves": self.waves,
            "portal_leads": self.portal_leads, "dynamic_companies": self.dynamic_companies,
            "verified_links": self.verified_links, "report_path": self.report_path,
            "report_valid": self.report_valid, "budget": self.budget,
            "elapsed_s": round(self.elapsed_s, 2), "summary": dict(self.summary),
        }


class MarketSearchRuntime:
    def __init__(
        self,
        settings: Settings,
        run_id: str,
        *,
        lanes: list[str],
        geography_groups: Optional[list[str]] = None,
        portal_families: tuple[str, ...] = ("linkedin", "naukri"),
        budget: Optional[CampaignBudget] = None,
        policy_dir: Optional[Path] = None,
        portal_metadata: Optional[dict[str, dict]] = None,
        portal_client_factory: Optional[Callable[[], ReadOnlyHttpClient]] = None,
        official_pilot: Optional[Callable[[], Any]] = None,
        pool_caps: Optional[dict[str, int]] = None,
        recency_days: int = 7,
        mode: str = "DELTA",
        query_strategist=None,
    ):
        self.settings = settings
        self.run_id = run_id
        self.campaign_id = f"camp::{run_id}"
        self.lanes = list(lanes)
        self.geography_groups = list(geography_groups or ["PRIMARY"])
        self.portal_families = tuple(f for f in portal_families if f in PORTAL_FAMILIES)
        self.budget = budget or CampaignBudget()
        self.policy_dir = policy_dir
        self.portal_metadata = dict(portal_metadata or {})
        self.portal_client_factory = portal_client_factory
        self.official_pilot = official_pilot
        self.pool_caps = pool_caps
        self.recency_days = recency_days
        self.mode = mode
        self.query_strategist = query_strategist
        self.registry = build_portals_registry()
        self.policy = load_policy(policy_dir)
        self.compiler = SearchQueryCompiler(
            self.policy.lanes, self.policy.geography, policy_version=self.policy.short_fingerprint)

    # -- baseline wave planning --------------------------------------------
    def _wave0_tasks(self) -> list[WaveTask]:
        import hashlib

        tasks: list[WaveTask] = []
        for fam in self.portal_families:
            for lane in self.lanes:
                for geo in self.geography_groups:
                    try:
                        compiled = self.compiler.compile(lane, geo, mode=self.mode)
                    except Exception:  # noqa: BLE001
                        continue
                    query = compiled.primary_query or lane
                    tid = "wtask::" + hashlib.sha1(
                        f"{self.campaign_id}::0::{fam}::{lane}::{geo}::{query}".encode()).hexdigest()[:14]
                    tasks.append(WaveTask(
                        kind=WaveTaskKind.PORTAL, task_id=tid, lane=lane, geography=geo,
                        source_family=fam, query=query, recency_days=self.recency_days, origin="BASELINE"))
        return tasks

    def _build_wave0(self, campaign: MarketCampaign) -> MarketWave:
        wave = MarketWave(campaign_id=campaign.campaign_id, run_id=self.run_id, wave_index=0,
                          tasks=self._wave0_tasks())
        wave.seal()
        return wave

    # -- portal task execution ---------------------------------------------
    def _geo_location(self, lane: str, geo: str) -> Optional[str]:
        try:
            compiled = self.compiler.compile(lane, geo, mode=self.mode)
        except Exception:  # noqa: BLE001
            return None
        return compiled.geography_terms[0] if compiled.geography_terms else None

    def _run_portal_task(self, task: WaveTask, wave: MarketWave) -> LaneSourceOutcome:
        """Execute ONE portal task with its OWN StateStore connection (thread-
        safe) and its own bounded HTTP client. Persists append-only leads and
        bumps the global campaign budget."""
        family = _FAMILY_BY_NAME.get(task.source_family)
        outcome = LaneSourceOutcome(task.source_family, task.lane, task.geography)
        if family is None:
            return outcome
        md = dict(self.portal_metadata.get(task.source_family, {}))
        md.setdefault("max_pages", self.budget.max_portal_pages)
        md.setdefault("max_results", self.budget.max_portal_cards)
        instance = make_portal_instance(family, f"{task.source_family}-{task.lane}-{task.geography}", metadata=md)
        client = self.portal_client_factory() if self.portal_client_factory else None
        adapter = self.registry.create(instance) if client is None else \
            self.registry.adapter_for_family(family)(instance, http_client=client)
        location = self._geo_location(task.lane, task.geography) or task.geography
        request = SearchRequest(query=task.query or task.lane, location=location,
                                recency_days=task.recency_days, limit=self.budget.max_portal_cards)
        with StateStore(self.settings.state_db) as store:
            # Global budget reservation (build spec 9): a browser-anonymous portal
            # call atomically reserves against the campaign-wide browser budget.
            # Concurrent workers can never collectively exceed the cap.
            if not store.reserve_campaign_budget(self.campaign_id, browser=1):
                outcome.attempted = False
                outcome.terminal_status = "PARTIAL_BUDGET"
                return outcome
            outcome.attempted = True
            try:
                from atlas.sources.adapter import AdapterError

                res = adapter.search(request)
            except AdapterError as exc:
                outcome.terminal_status = {
                    "ANTI_BOT": "ACCESS_LIMITED", "LOGIN_WALL": "AUTH_REQUIRED",
                    "HTTP_429": "RATE_LIMITED",
                }.get(exc.category.value, "SOURCE_UNAVAILABLE")
                outcome.health = outcome.terminal_status
                return outcome
            seen: set[str] = set()
            staged = 0
            dups = 0
            for r in res.results:
                lead = PortalJobLead.from_discovery_result(
                    r, run_id=self.run_id, source_family=task.source_family, lane=task.lane,
                    result_query=task.query, result_page=res.page, result_cursor=res.next_cursor,
                    campaign_id=self.campaign_id, wave_id=wave.wave_id,
                    health_findings=res.parse_findings)
                if not lead.has_min_identity():
                    continue
                ch = lead.content_hash()
                if ch in seen:
                    dups += 1
                    continue
                seen.add(ch)
                lead.persist(store)
                staged += 1
            store.bump_campaign_budget(self.campaign_id, results=staged)
            outcome.results = len(res.results)
            outcome.unique = staged
            outcome.duplicates = dups
            outcome.health = "HEALTHY" if staged else "SELECTOR_DRIFT_SUSPECTED" if res.parse_findings else "HEALTHY"
            if staged:
                outcome.terminal_status = "COMPLETED_WITH_RESULTS"
            elif res.zero_result_kind.value in ("TRUSTED_ZERO", "NOT_APPLICABLE"):
                outcome.terminal_status = "ATTEMPTED_ZERO"
            else:
                outcome.terminal_status = "EXTRACTION_UNRESOLVED"
        return outcome

    def _execute_wave(self, campaign: MarketCampaign, wave: MarketWave) -> WaveOutcome:
        wave.status = WaveStatus.RUNNING
        with StateStore(self.settings.state_db) as store:
            wave.persist(store)
        # Portal tasks run through the bounded pool (per concurrency class).
        portal_tasks = [t for t in wave.tasks if t.kind == WaveTaskKind.PORTAL]
        lane_sources: list[LaneSourceOutcome] = []
        with MarketPoolDispatcher(self.pool_caps) as dispatcher:
            pool_tasks = []
            for t in portal_tasks:
                family = _FAMILY_BY_NAME.get(t.source_family)
                cls = self.registry.adapter_for_family(family).concurrency_class if family else None
                pool = pool_for_class(cls) if cls else "PUBLIC_BROWSER_ANONYMOUS"
                pool_tasks.append(PoolTask(t.task_id, pool, (lambda task=t: self._run_portal_task(task, wave))))
            run = dispatcher.run(pool_tasks)
            for pool, v in run.max_observed.items():
                self._max_observed[pool] = max(self._max_observed.get(pool, 0), v)
            self._auth_owner_max = max(self._auth_owner_max, run.profile_owner_max)
            for tid, out in run.results.items():
                if isinstance(out, LaneSourceOutcome):
                    lane_sources.append(out)
            for tid, exc in run.errors.items():
                lane_sources.append(LaneSourceOutcome("unknown", None, None, attempted=True,
                                                      terminal_status="FAILED", health="FAILED"))
        # Official follow-up + domain discovery tasks (run inline, bounded).
        self._run_official_followups(campaign, wave)
        # Dynamic company registration + portal->official verification.
        self._post_wave(campaign, wave)
        # Build the deterministic wave outcome.
        return self._wave_outcome(campaign, wave, lane_sources)

    def _run_official_followups(self, campaign: MarketCampaign, wave: MarketWave) -> None:
        from atlas.careers.discovery import CareerSourceDiscoveryService

        follow = [t for t in wave.tasks if t.kind in (WaveTaskKind.DOMAIN_DISCOVERY,)]
        if not follow:
            return
        client = self.portal_client_factory() if self.portal_client_factory else None
        with StateStore(self.settings.state_db) as store:
            svc = CareerSourceDiscoveryService(http_client=client)
            for t in follow:
                if not t.company_id:
                    continue
                company = store.get_company(t.company_id)
                domain = company["official_domain"] if company else t.official_domain
                if not domain:
                    continue
                outcome = svc.discover(domain, company_id=t.company_id, name=t.company_name or "", fetch=bool(client))
                svc.persist(store, outcome)

    def _post_wave(self, campaign: MarketCampaign, wave: MarketWave) -> None:
        with StateStore(self.settings.state_db) as store:
            registrar = DynamicCompanyRegistrar(store)
            for row in store.list_portal_leads(self.run_id):
                if row["company_id"]:
                    continue
                lead = PortalJobLead.from_row(row)
                registrar.register_from_lead(lead, run_id=self.run_id)
            PortalOfficialVerifier(store).link_run(self.run_id)

    def _wave_outcome(self, campaign: MarketCampaign, wave: MarketWave,
                      lane_sources: list[LaneSourceOutcome]) -> WaveOutcome:
        with StateStore(self.settings.state_db) as store:
            leads = store.list_portal_leads(self.run_id)
            unverified = sum(1 for r in leads if r["verification_state"] == "PORTAL_CURRENT_LEAD")
            # Dynamic companies discovered but still lacking any official source.
            no_source: list[str] = []
            for prov in store.list_dynamic_company_provenance(run_id=self.run_id):
                cid = prov["company_id"]
                if not cid:
                    continue
                rels = store.list_relationships_for_company(cid)
                entries = store.list_career_entry_points(company_id=cid)
                if not rels and not any(e["validated"] for e in entries) and cid not in no_source:
                    no_source.append(cid)
            b = store.get_campaign_budget(self.campaign_id)
        attempted = frozenset(ls.source_family for ls in lane_sources if ls.attempted)
        any_partial = any(ls.terminal_status == "PARTIAL_BUDGET" for ls in lane_sources)
        budget_exhausted = bool(
            any_partial or (b is not None and (
                (b["max_browser"] is not None and b["browser_calls"] >= b["max_browser"]) or
                (b["max_results"] is not None and b["results"] >= b["max_results"])
            ))
        )
        return WaveOutcome(
            lane_sources=lane_sources, required_families=frozenset(self.portal_families),
            attempted_families=attempted, portal_leads_total=len(leads),
            portal_leads_unverified=unverified, dynamic_companies_without_source=tuple(no_source[:10]),
            budget_exhausted=budget_exhausted,
        )

    # -- top-level run ------------------------------------------------------
    def plan(self) -> dict:
        """Dry-run: create + seal the campaign and Wave 0 baseline, persist them,
        and return a plan summary. No network/browser I/O."""
        campaign = MarketCampaign(
            campaign_id=self.campaign_id, run_id=self.run_id,
            policy_fingerprint=self.policy.short_fingerprint, mode=self.mode,
            budget=self.budget, status=CampaignStatus.SEALED,
            config={"lanes": self.lanes, "geographies": self.geography_groups,
                    "portal_families": list(self.portal_families)})
        wave0 = self._build_wave0(campaign)
        with StateStore(self.settings.state_db) as store:
            campaign.persist(store)
            store.init_campaign_budget(
                self.campaign_id, self.run_id, max_http=self.budget.max_http_calls,
                max_browser=self.budget.max_browser_calls, max_llm=self.budget.max_llm_calls,
                max_results=self.budget.max_results, max_wall_clock_s=self.budget.max_wall_clock_s)
            wave0.persist(store)
        return {"campaign_id": self.campaign_id, "run_id": self.run_id,
                "wave0_tasks": len(wave0.tasks), "wave0_seal": wave0.seal_hash,
                "lanes": self.lanes, "geographies": self.geography_groups,
                "portal_families": list(self.portal_families), "budget": self.budget.to_dict()}

    def run(self, *, live: bool, resume: bool = False, report_path: Optional[Path] = None) -> MarketRunResult:
        started = time.monotonic()
        # §5.8 fresh-run guard.
        with StateStore(self.settings.state_db) as store:
            existing = store.get_market_campaign(self.campaign_id)
            if live and existing is not None and not resume and existing["status"] in (
                    "COMPLETE", "PARTIAL_BUDGET"):
                raise MarketRunReuseError(
                    f"campaign {self.campaign_id!r} already terminal ({existing['status']}); "
                    "use a fresh run_id or resume=True")

        # Optional: run OFFICIAL coverage first (the ONE LangGraph governor) so
        # official observations exist for portal->official verification.
        if live and self.official_pilot is not None:
            try:
                self.official_pilot()
            except Exception:  # noqa: BLE001 - official failure is truthful, not fatal
                pass

        plan = self.plan()
        campaign = MarketCampaign(
            campaign_id=self.campaign_id, run_id=self.run_id,
            policy_fingerprint=self.policy.short_fingerprint, mode=self.mode, budget=self.budget,
            status=CampaignStatus.RUNNING)
        with StateStore(self.settings.state_db) as store:
            campaign.persist(store)
        analyzer = CoverageDeficitAnalyzer()
        expander = WaveExpansionPlanner(self.compiler, self.policy.geography, self.budget,
                                        portal_families=self.portal_families,
                                        query_strategist=self.query_strategist)
        prior_queries: set[tuple[str, str, str]] = set()
        wave = MarketWave.from_row(self._load_wave_row(0))
        wave_summaries: list[dict] = []
        terminal_reason = None
        status = CampaignStatus.COMPLETE
        self._max_observed = {}
        self._auth_owner_max = 0

        if live:
            wave_index = 0
            while True:
                for t in wave.tasks:
                    if t.query and t.lane:
                        prior_queries.add((t.source_family or "", t.lane, t.query.lower()))
                outcome = self._execute_wave(campaign, wave)
                wave.status = WaveStatus.COMPLETE
                with StateStore(self.settings.state_db) as store:
                    wave.persist(store)
                deficits = analyzer.analyze(outcome)
                with StateStore(self.settings.state_db) as store:
                    for d in deficits:
                        store.record_wave_deficit(
                            d.deficit_id + f"::{wave.wave_index}", self.campaign_id, wave.wave_id,
                            d.kind.value, lane=d.lane, geography=d.geography,
                            source_family=d.source_family, detail=d.detail)
                wave_summaries.append({
                    "wave_index": wave.wave_index, "seal": wave.seal_hash,
                    "tasks": len(wave.tasks), "deficits": sorted({d.kind.value for d in deficits}),
                    "leads": outcome.portal_leads_total})
                if outcome.budget_exhausted or outcome.time_exhausted or \
                        (time.monotonic() - started) > self.budget.max_wall_clock_s:
                    status = CampaignStatus.PARTIAL_BUDGET
                    terminal_reason = "budget/time exhausted with obligations remaining"
                    break
                next_wave = expander.plan_next_wave(campaign, wave, deficits, prior_queries=prior_queries)
                if next_wave is None:
                    status = CampaignStatus.COMPLETE
                    terminal_reason = "all required waves terminal; no bounded expansion warranted"
                    break
                next_wave.seal()
                with StateStore(self.settings.state_db) as store:
                    next_wave.persist(store)
                wave = next_wave
                wave_index += 1
                if wave_index >= self.budget.max_waves:
                    status = CampaignStatus.PARTIAL_BUDGET
                    terminal_reason = "max waves reached"
                    break
        else:
            status = CampaignStatus.SEALED
            terminal_reason = "dry-run (no --live)"

        # ONE report writer after fan-in.
        report_written = report_valid = None
        if live:
            out = report_path or (self.settings.output_dir / f"Atlas_Market_{self.run_id}.xlsx")
            try:
                rr = write_market_report(self.settings, self.run_id, self.campaign_id, out)
                report_written, report_valid = rr
            except Exception as exc:  # noqa: BLE001
                report_valid = False
                terminal_reason = (terminal_reason or "") + f"; report failed: {exc}"

        with StateStore(self.settings.state_db) as store:
            campaign.status = status
            campaign.terminal_reason = terminal_reason
            campaign.current_wave = wave.wave_index
            campaign.persist(store)
            leads = store.list_portal_leads(self.run_id)
            dynco = store.list_dynamic_company_provenance(run_id=self.run_id)
            links = store.list_portal_official_links(self.run_id)
            budget_row = store.get_campaign_budget(self.campaign_id)
        verified = sum(1 for l in links if l["verification_state"] in
                       ("LINKED_OFFICIAL_VERIFIED", "CLOSED_POSITIVE_EVIDENCE"))
        result = MarketRunResult(
            run_id=self.run_id, campaign_id=self.campaign_id, status=status.value,
            terminal_reason=terminal_reason, waves=wave_summaries, portal_leads=len(leads),
            dynamic_companies=len({d["company_id"] for d in dynco if d["company_id"]}),
            verified_links=verified, report_path=report_written, report_valid=report_valid,
            budget=dict(budget_row) if budget_row else {},
            elapsed_s=time.monotonic() - started,
            summary={"max_observed_concurrency": getattr(self, "_max_observed", {}),
                     "authenticated_profile_owner_max": getattr(self, "_auth_owner_max", 0)})
        return result

    def _load_wave_row(self, index: int):
        with StateStore(self.settings.state_db) as store:
            for row in store.list_market_waves(self.campaign_id):
                if int(row["wave_index"]) == index:
                    return row
        raise KeyError(f"wave {index} not found for campaign {self.campaign_id}")


__all__ = ["MarketSearchRuntime", "MarketRunResult", "MarketRunReuseError"]
