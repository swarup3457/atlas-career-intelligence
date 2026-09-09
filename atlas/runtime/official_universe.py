"""Direct official company-universe job pipeline (Phase 2A official-first §5).

`OfficialCompanyUniverseRunner` is the daily run's OFFICIAL-FIRST producer. It
begins from the COMPANY UNIVERSE (a due-cadence subset of the company registry,
or an explicit sealed target list for a bounded acceptance) and, for every due
company, drives the COMPLETE existing official path — never a new orchestration
or persistence framework:

    verified official domain -> CareerSourceDiscoveryService (evidence-validated
    entry point) -> OfficialUrlTrustPolicy (fail-closed) -> fingerprint / router
    (Greenhouse / Lever / Ashby / Workday / generic HTTP / generic browser) ->
    the ONE LangGraph ProductionSearchRuntime (sealed coverage plan, atomic
    leases, shared rate limiter, source health, detail hydration,
    canonicalization, durable verification decisions)

and then converts the run's DIRECT OFFICIAL canonical jobs into ``RankableJob``
rows (``record_class = OFFICIAL_DIRECT``) that are returned to the SAME daily run
for candidate evaluation and the ONE published workbook. Nothing here signs in,
applies, submits, or guesses an official domain from a company name.

The runtime is generic over any target list; the bounded acceptance simply seals
a 10–15 company subset. Portal discovery is a SEPARATE, supplemental producer —
it can never substitute for unfinished official-company coverage.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from atlas.candidate.eligibility import RankableJob
from atlas.candidate.jobkey import job_key_for
from atlas.careers.discovery import CareerSourceDiscoveryService, DiscoveryMethod
from atlas.careers.profile import RouteKind
from atlas.careers.router import CareerSourceRouter, RouteDecision
from atlas.careers.trust import OfficialUrlTrustPolicy
from atlas.config import Settings
from atlas.models import ErrorCategory
from atlas.persistence.sqlite import StateStore
from atlas.planning import PlannedCompany
from atlas.sources.ats.base import detect_challenge
from atlas.sources.fingerprint import fingerprint_ats
from atlas.sources.generic import build_careers_registry
from atlas.sources.http_client import HttpError, HttpRequest, ReadOnlyHttpClient
from atlas.sources.models import SourceInstance, SourceType
from atlas.sources.registry import SourceRegistry

_HTML_ACCEPT = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"

# Specialized ATS families the router routes to a structured adapter.
_ATS_ROUTABLE_TYPES = frozenset(
    {SourceType.ATS_GREENHOUSE, SourceType.ATS_LEVER, SourceType.ATS_ASHBY, SourceType.ATS_WORKDAY}
)
_SPECIALIZED_FAMILIES = frozenset({"greenhouse", "lever", "ashby", "workday"})
_GENERIC_HTTP_FAMILY = "company_career"
_GENERIC_BROWSER_FAMILY = "company_career_browser"

# Highest-evidence revision ranking (mirrors runtime.production / canonicalize).
_EVIDENCE_RANK = {"OFFICIAL_DETAIL_LIVE": 3, "OFFICIAL_SEARCH_LIVE": 2, "PORTAL_LIVE": 1}
_OFFICIAL_EVIDENCE = ("OFFICIAL_DETAIL_LIVE", "OFFICIAL_SEARCH_LIVE")

_DEFAULT_LANES = ("JAVA_BACKEND", "GENERAL_SOFTWARE")


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _parse_posted(row, detail) -> Optional[datetime.date]:
    if (detail.get("date_provenance") or "") != "EMPLOYER_POSTED_AT":
        return None
    raw = row["posted_at"]
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _official_job_from_revision(canonical_id, row, detail, decision, *, record_class, extra_channels):
    """Build ONE official RankableJob from the highest-evidence revision of a
    canonical job. Shared by direct official coverage (OFFICIAL_DIRECT) and
    portal->official linkage (PORTAL_OFFICIAL_LINKED) so both carry the official
    source/url/requisition as PRIMARY."""
    company = (row["company"] or "").strip() or "(unknown)"
    title = (row["title"] or "").strip()
    location = (row["location"] or "").strip()
    family = (row["source_family"] or "").strip()
    url = row["canonical_url"] or row["source_url"]
    requisition = str(detail.get("requisition_id") or "").strip()
    verification = (decision["verification_level"] if decision else None) or (
        "VERIFIED_OFFICIAL" if detail.get("verification_level") == "OFFICIAL_DETAIL_LIVE"
        else "OFFICIAL_SEARCH_LIVE")
    if record_class == "PORTAL_OFFICIAL_LINKED":
        verification = "LINKED_OFFICIAL_VERIFIED"
    mandatory = tuple(detail.get("mandatory_requirements") or ())
    preferred = tuple(detail.get("preferred_requirements") or ())
    description = detail.get("description_fragment") or ""
    experience = detail.get("requirement_experience_text") or detail.get("experience_text") or ""
    eligibility = detail.get("eligibility_text") or location
    work_mode = detail.get("work_mode") or "UNKNOWN"
    channels = tuple(dict.fromkeys((family,) + tuple(extra_channels)))
    key = job_key_for(company=company, title=title, location=location,
                      source_family=family, source_job_id=row["source_job_id"], official=True)
    return RankableJob(
        job_key=key, company=company, title=title, location=location, lane=row["lane"],
        work_mode=work_mode, description=description,
        mandatory_requirements=mandatory, preferred_requirements=preferred,
        experience_text=experience, eligibility_text=eligibility,
        posted_date=_parse_posted(row, detail),
        verification_state=verification, has_live_official_page=True, is_fetchable=True,
        source_family=family, canonical_id=canonical_id, url=url,
        record_class=record_class, primary_source=family,
        discovery_channels=channels, official_requisition_id=requisition,
    )


def project_run_official_jobs(store, run_id, *, canonical_filter=None,
                              record_class="OFFICIAL_DIRECT", extra_channels=()):
    """Project the run's DIRECT OFFICIAL canonical jobs into RankableJob rows.

    The highest-evidence revision per canonical wins, so a detail-hydrated job
    appears once with its extracted requirements. When ``canonical_filter`` is
    given, only those canonical ids are projected (used by portal->official
    linkage). Returns ``(jobs, by_instance_count)``."""
    best: dict[str, dict] = {}
    decisions = {d["canonical_id"]: d for d in store.list_verification_decisions(run_id)
                 if d["canonical_id"]}
    for row in store.list_raw_observations(run_id):
        cid = row["canonical_id"]
        if not cid:
            continue
        if canonical_filter is not None and cid not in canonical_filter:
            continue
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except (ValueError, TypeError):
            detail = {}
        evidence = detail.get("verification_level") or ""
        rank = _EVIDENCE_RANK.get(evidence, 0)
        prev = best.get(cid)
        if prev is not None and prev["_rank"] >= rank:
            continue
        best[cid] = {"_rank": rank, "row": row, "detail": detail, "decision": decisions.get(cid)}

    jobs: list[RankableJob] = []
    by_instance: dict[str, int] = {}
    for cid, entry in best.items():
        row, detail, decision = entry["row"], entry["detail"], entry["decision"]
        evidence = detail.get("verification_level") or ""
        is_official = (evidence in _OFFICIAL_EVIDENCE or (
            decision and decision["verification_level"] in ("VERIFIED_OFFICIAL", "OFFICIAL_SEARCH_LIVE")))
        if not is_official:
            continue
        jobs.append(_official_job_from_revision(
            cid, row, detail, decision, record_class=record_class, extra_channels=extra_channels))
        inst_id = row["source_instance"] or ""
        by_instance[inst_id] = by_instance.get(inst_id, 0) + 1
    jobs.sort(key=lambda j: (j.company or "", j.title or ""))
    return jobs, by_instance


@dataclass(frozen=True)
class OfficialCompanyTarget:
    """One official company to cover. ``official_domain`` is a VERIFIED official
    domain (from the registry or a sealed config) — NEVER guessed from a name."""

    company_id: str
    name: str
    official_domain: str
    known_careers_url: Optional[str] = None
    tier: str = "A"
    mode: str = "DELTA"
    geography_group: str = "PRIMARY"
    board_token: Optional[str] = None    # explicit ATS board slug (skips discovery)
    family: Optional[str] = None         # explicit ATS family hint (greenhouse/lever/ashby/workday)
    expected_route: Optional[str] = None

    def canonical(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id, "name": self.name,
            "official_domain": self.official_domain, "known_careers_url": self.known_careers_url,
            "tier": self.tier, "mode": self.mode, "geography_group": self.geography_group,
            "board_token": self.board_token, "family": self.family,
        }


@dataclass
class OfficialCompanyRecord:
    company_id: str
    name: str
    official_domain: str
    entry_url: Optional[str] = None
    route_kind: str = "UNKNOWN"
    family: Optional[str] = None
    terminal_status: str = "NOT_ATTEMPTED"
    direct_jobs: int = 0
    limitation: Optional[str] = None
    last_success_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id, "name": self.name, "official_domain": self.official_domain,
            "entry_url": self.entry_url, "route_kind": self.route_kind, "family": self.family,
            "terminal_status": self.terminal_status, "direct_jobs": self.direct_jobs,
            "limitation": self.limitation, "last_success_at": self.last_success_at,
        }


@dataclass
class OfficialUniverseResult:
    run_id: str
    config_hash: str
    jobs: list = field(default_factory=list)                 # OFFICIAL_DIRECT RankableJobs
    companies: list[OfficialCompanyRecord] = field(default_factory=list)
    runtime_terminal: Optional[str] = None
    elapsed_s: float = 0.0

    # -- metrics (Section 8) -------------------------------------------------
    @property
    def companies_planned(self) -> int:
        return len(self.companies)

    @property
    def companies_terminal(self) -> int:
        return sum(1 for c in self.companies
                   if c.terminal_status not in ("NOT_ATTEMPTED", "IN_PROGRESS"))

    @property
    def companies_with_results(self) -> int:
        return sum(1 for c in self.companies if c.direct_jobs > 0)

    @property
    def direct_official_jobs(self) -> int:
        return len(self.jobs)

    @property
    def route_families_with_results(self) -> list[str]:
        fams = {(j.primary_source or j.source_family) for j in self.jobs if (j.primary_source or j.source_family)}
        return sorted(fams)

    @property
    def specialized_ats_jobs(self) -> int:
        return sum(1 for j in self.jobs if (j.source_family or "") in _SPECIALIZED_FAMILIES)

    @property
    def generic_http_jobs(self) -> int:
        return sum(1 for j in self.jobs if (j.source_family or "") == _GENERIC_HTTP_FAMILY)

    @property
    def generic_browser_jobs(self) -> int:
        return sum(1 for j in self.jobs if (j.source_family or "") == _GENERIC_BROWSER_FAMILY)

    def metrics(self) -> dict[str, Any]:
        return {
            "official_companies_planned": self.companies_planned,
            "official_companies_terminal": self.companies_terminal,
            "official_companies_with_results": self.companies_with_results,
            "direct_official_jobs": self.direct_official_jobs,
            "official_route_families_with_results": self.route_families_with_results,
            "official_generic_http_jobs": self.generic_http_jobs,
            "official_generic_browser_jobs": self.generic_browser_jobs,
            "official_specialized_ats_jobs": self.specialized_ats_jobs,
            "runtime_terminal": self.runtime_terminal,
            "config_hash": self.config_hash,
        }

    def rankable_jobs(self) -> list:
        return list(self.jobs)

    def to_dict(self) -> dict[str, Any]:
        d = self.metrics()
        d["run_id"] = self.run_id
        d["elapsed_s"] = round(self.elapsed_s, 2)
        d["companies"] = [c.to_dict() for c in self.companies]
        return d


@dataclass
class _RoutedCompany:
    target: OfficialCompanyTarget
    decision: RouteDecision
    instance: SourceInstance


class OfficialCompanyUniverseRunner:
    """Runs direct official coverage for a company universe and returns the
    resulting OFFICIAL_DIRECT jobs to the daily pipeline."""

    def __init__(
        self,
        settings: Settings,
        run_id: str,
        *,
        targets: Sequence[OfficialCompanyTarget],
        lanes: Sequence[str] = _DEFAULT_LANES,
        geography_group: str = "PRIMARY",
        parallel_workers: int = 1,
        max_pages: int = 2,
        max_jobs_per_company: int = 20,
        request_budget: int = 40,
        live: bool = True,
        registry: Optional[SourceRegistry] = None,
        browser_profile_dir: Optional[Path] = None,
        prepared_routes: Optional[Sequence[_RoutedCompany]] = None,
        use_run_lock: bool = False,
        detail_batch: int = 25,
    ) -> None:
        self.settings = settings
        self.run_id = run_id
        self.targets = list(targets)
        self.lanes = list(lanes) or list(_DEFAULT_LANES)
        self.geography_group = geography_group
        self.parallel_workers = max(1, int(parallel_workers))
        self.max_pages = max(1, int(max_pages))
        self.max_jobs_per_company = max(1, int(max_jobs_per_company))
        self.request_budget = max(5, int(request_budget))
        self.live = live
        self.registry = registry or build_careers_registry()
        self.browser_profile_dir = Path(browser_profile_dir) if browser_profile_dir else Path(".browser-profile-careers")
        self._prepared_routes = list(prepared_routes) if prepared_routes is not None else None
        self.use_run_lock = use_run_lock
        self.detail_batch = detail_batch

    # -- config seal --------------------------------------------------------
    def config_hash(self) -> str:
        blob = json.dumps(
            {
                "run_id": self.run_id,
                "targets": [t.canonical() for t in self.targets],
                "lanes": sorted(self.lanes),
                "geography_group": self.geography_group,
                "max_pages": self.max_pages,
                "max_jobs_per_company": self.max_jobs_per_company,
            },
            sort_keys=True, ensure_ascii=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # -- HTTP -----------------------------------------------------------------
    def _client(self) -> ReadOnlyHttpClient:
        return ReadOnlyHttpClient(
            connect_timeout=10.0, read_timeout=20.0, max_response_bytes=5 * 1024 * 1024,
            max_redirects=5, request_budget=self.request_budget, accept=_HTML_ACCEPT,
        )

    # -- discovery + routing (composes existing components) -----------------
    def _entry_url(self, target: OfficialCompanyTarget) -> tuple[str, str]:
        if target.known_careers_url:
            return target.known_careers_url, DiscoveryMethod.USER_SUPPLIED
        return (f"https://{target.official_domain.strip().lower().lstrip('.')}/careers",
                DiscoveryMethod.COMMON_PATH)

    def _discover_entry(self, target: OfficialCompanyTarget, client) -> Optional[str]:
        if target.known_careers_url or client is None:
            return None
        svc = CareerSourceDiscoveryService(http_client=client)
        outcome = svc.discover(target.official_domain, company_id=target.company_id,
                               name=target.name, fetch=True)
        with StateStore(self.settings.state_db) as store:
            svc.persist(store, outcome)
        validated = [e for e in outcome.trusted_entry_points if e.validated]
        if validated:
            return max(validated, key=lambda e: e.confidence).url
        return None

    def _route_target(self, target: OfficialCompanyTarget, router: CareerSourceRouter,
                      client) -> Optional[_RoutedCompany]:
        """Resolve ONE company to a trusted, routed official source instance.
        Untrusted / access-limited / unsupported entries are returned as a
        non-routable decision (recorded truthfully, never fetched further)."""
        # An explicit known ATS board (from registry/config) skips discovery and
        # routes straight to the structured adapter — a proven official source.
        if target.board_token and target.family in _SPECIALIZED_FAMILIES:
            inst = self._ats_instance(target)
            decision = RouteDecision(RouteKind.ATS, inst.metadata.get("entry_url", target.official_domain),
                                     source_instance=inst, reason="known ATS board (config)")
            return _RoutedCompany(target, decision, inst)

        discovered = self._discover_entry(target, client) if self.live else None
        entry_url = discovered or self._entry_url(target)[0]

        trust = OfficialUrlTrustPolicy(target.official_domain).classify(entry_url)
        if not trust.trusted:
            return _RoutedCompany(target, RouteDecision(
                RouteKind.UNSUPPORTED_SITE, entry_url, reason=f"untrusted entry ({trust.reason})"), None)

        # Known-ATS fast route (URL fingerprint, marker-independent).
        fp = fingerprint_ats(entry_url)
        if fp.matched and fp.source_type in _ATS_ROUTABLE_TYPES:
            decision = router.route(entry_url, company_id=target.company_id, html=None)
            decision = self._bound_instance(decision, target)
            if decision.source_instance is not None and decision.routable:
                return _RoutedCompany(target, decision, decision.source_instance)
            return _RoutedCompany(target, decision, None)

        # Generic route: bounded live fetch, then classify.
        html = None
        status = 200
        challenge = login = False
        final_url = entry_url
        if self.live and client is not None:
            try:
                resp = client.fetch(HttpRequest(entry_url, headers={"Accept": _HTML_ACCEPT}))
                status = resp.status
                final_url = resp.url
                html = resp.text() if status == 200 else None
                cat = detect_challenge(resp)
                challenge = cat == ErrorCategory.ANTI_BOT
                login = cat == ErrorCategory.LOGIN_WALL
                if status not in (200, 401, 403, 429) and not (challenge or login):
                    return _RoutedCompany(target, RouteDecision(
                        RouteKind.UNSUPPORTED_SITE, entry_url,
                        reason=f"entry not reachable (HTTP {status})"), None)
            except HttpError as exc:
                return _RoutedCompany(target, RouteDecision(
                    RouteKind.UNSUPPORTED_SITE, entry_url,
                    reason=f"fetch error: {exc.category.value}"), None)

        decision = router.route(entry_url, company_id=target.company_id, html=html, status=status,
                                final_url=final_url, challenge=challenge, login_wall=login)
        decision = self._bound_instance(decision, target)
        if decision.source_instance is not None and decision.routable:
            return _RoutedCompany(target, decision, decision.source_instance)
        return _RoutedCompany(target, decision, None)

    def _ats_instance(self, target: OfficialCompanyTarget) -> SourceInstance:
        stype = {
            "greenhouse": SourceType.ATS_GREENHOUSE, "lever": SourceType.ATS_LEVER,
            "ashby": SourceType.ATS_ASHBY, "workday": SourceType.ATS_WORKDAY,
        }[target.family]
        entry = target.known_careers_url or f"https://{target.official_domain}"
        inst = SourceInstance(
            instance_id=f"{target.company_id}::{target.family}::{target.board_token}",
            source_type=stype, company_id=target.company_id,
            display_name=target.name,
            metadata={"board_token": target.board_token, "entry_url": entry},
        )
        return self._bound_source_instance(inst)

    def _bound_instance(self, decision: RouteDecision, target: OfficialCompanyTarget) -> RouteDecision:
        if decision.source_instance is None:
            return decision
        bounded = self._bound_source_instance(decision.source_instance)
        return RouteDecision(
            route_kind=decision.route_kind, entry_url=decision.entry_url, source_instance=bounded,
            fingerprint_family=decision.fingerprint_family, confidence=decision.confidence,
            reason=decision.reason, evidence=decision.evidence,
        )

    def _bound_source_instance(self, inst: SourceInstance) -> SourceInstance:
        md = dict(inst.metadata)
        md["max_pages"] = self.max_pages
        md["max_results"] = self.max_jobs_per_company
        md["max_load_cycles"] = 2
        md["request_budget"] = self.request_budget
        md.setdefault("render_wait_ms", 3000)
        md["headless"] = True
        md["profile_dir"] = str(self.browser_profile_dir)
        return SourceInstance(
            instance_id=inst.instance_id, source_type=inst.source_type, source_family=inst.source_family,
            display_name=inst.display_name, base_url=inst.base_url, tenant=inst.tenant, site=inst.site,
            company_id=inst.company_id, enabled=True, capability_overrides=inst.capability_overrides,
            capability_removals=inst.capability_removals, metadata=md,
        )

    def _resolve_routes(self) -> list[_RoutedCompany]:
        if self._prepared_routes is not None:
            return list(self._prepared_routes)
        router = CareerSourceRouter()
        client = self._client() if self.live else None
        routed: list[_RoutedCompany] = []
        for target in self.targets:
            try:
                rc = self._route_target(target, router, client)
            except Exception as exc:  # noqa: BLE001 - one company must not crash the universe
                rc = _RoutedCompany(target, RouteDecision(
                    RouteKind.UNSUPPORTED_SITE, target.official_domain,
                    reason=f"routing error: {type(exc).__name__}: {exc}"), None)
            routed.append(rc)
        # Persist trusted route classifications (append-only provenance).
        with StateStore(self.settings.state_db) as store:
            for rc in routed:
                if rc.instance is not None:
                    router.persist(store, rc.decision, company_id=rc.target.company_id)
        return routed

    # -- execution ----------------------------------------------------------
    def run(self) -> OfficialUniverseResult:
        started = time.monotonic()
        cfg_hash = self.config_hash()
        result = OfficialUniverseResult(run_id=self.run_id, config_hash=cfg_hash)

        routed = self._resolve_routes()
        routable = [rc for rc in routed if rc.instance is not None]
        instances: dict[str, SourceInstance] = {}
        planned: list[PlannedCompany] = []
        for rc in routable:
            instances[rc.instance.instance_id] = rc.instance
            planned.append(PlannedCompany(
                company_id=rc.target.company_id, name=rc.target.name, tier=rc.target.tier,
                mode=rc.target.mode, source_instances=(rc.instance.instance_id,),
                geography_group=rc.target.geography_group,
            ))

        runtime_terminal = None
        if planned:
            from atlas.runtime.production import ProductionSearchRuntime

            runtime = ProductionSearchRuntime(
                self.settings, self.run_id, fixture_mode=True,
                companies=planned, instances=instances, registry=self.registry,
                parallel_workers=self.parallel_workers, lane_override=list(self.lanes),
                max_per_company=1, max_per_instance=1, max_per_tenant=1, retry_budget=2,
                write_report=False, use_run_lock=self.use_run_lock,
            )
            runtime.detail_batch = self.detail_batch
            rr = runtime.run()
            runtime_terminal = rr.terminal_state
        result.runtime_terminal = runtime_terminal

        # Convert direct official canonical jobs -> OFFICIAL_DIRECT RankableJobs.
        official_jobs, jobs_by_instance = self._official_rankable_jobs()
        result.jobs = official_jobs

        # Build per-company records (terminal status + result counts).
        result.companies = self._company_records(routed, jobs_by_instance)
        result.elapsed_s = time.monotonic() - started
        return result

    # -- conversion ---------------------------------------------------------
    def _official_rankable_jobs(self) -> tuple[list[RankableJob], dict[str, int]]:
        with StateStore(self.settings.state_db) as store:
            return project_run_official_jobs(store, self.run_id, record_class="OFFICIAL_DIRECT")

    # -- coverage records ---------------------------------------------------
    def _company_records(self, routed: list[_RoutedCompany],
                         jobs_by_instance: dict[str, int]) -> list[OfficialCompanyRecord]:
        records: list[OfficialCompanyRecord] = []
        manifest_tasks: dict[str, list] = {}
        with StateStore(self.settings.state_db) as store:
            from atlas.sources.coverage import CoverageManifest

            manifest = CoverageManifest.load(store, self.run_id)
            for t in manifest.tasks():
                manifest_tasks.setdefault(t.source_instance, []).append(t)

        for rc in routed:
            target = rc.target
            rec = OfficialCompanyRecord(
                company_id=target.company_id, name=target.name, official_domain=target.official_domain,
                entry_url=rc.decision.entry_url, route_kind=rc.decision.route_kind.value,
            )
            if rc.instance is None:
                # unroutable: classified terminal (never fetched further)
                rec.family = None
                rec.terminal_status = rc.decision.route_kind.value
                rec.limitation = rc.decision.reason
            else:
                rec.family = rc.instance.adapter_key.value
                rec.direct_jobs = jobs_by_instance.get(rc.instance.instance_id, 0)
                tasks = manifest_tasks.get(rc.instance.instance_id, [])
                rec.terminal_status = self._company_terminal([t.status.value for t in tasks])
                if rec.direct_jobs > 0:
                    rec.last_success_at = _utcnow()
            records.append(rec)
        return records

    @staticmethod
    def _company_terminal(statuses: list[str]) -> str:
        if not statuses:
            return "NOT_ATTEMPTED"
        if any(s == "COMPLETED_WITH_RESULTS" for s in statuses):
            return "COMPLETED_WITH_RESULTS"
        order = ["ACCESS_LIMITED", "AUTH_REQUIRED", "RATE_LIMITED", "SOURCE_UNAVAILABLE",
                 "EXTRACTION_UNRESOLVED", "PARTIAL_BUDGET", "ATTEMPTED_ZERO", "COMPLETED_ZERO", "FAILED"]
        for o in order:
            if any(s == o for s in statuses):
                return o
        return statuses[0]


__all__ = [
    "OfficialCompanyTarget",
    "OfficialCompanyRecord",
    "OfficialUniverseResult",
    "OfficialCompanyUniverseRunner",
    "project_run_official_jobs",
]
