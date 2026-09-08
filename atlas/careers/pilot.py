"""Controlled live-pilot for official career-site coverage (Phase 1C-B §15).

Seals a small pilot configuration (8–12 companies) BEFORE execution, hashes it,
then drives the COMPLETE existing production path for each company:

    company registry entry (domain) -> source discovery -> routing (ATS / generic
    HTTP / generic browser) -> leased execution through the ONE LangGraph
    production governor -> observations -> hydration -> canonicalization ->
    durable verification decision -> ONE current-run Excel report.

The pilot never invents results, never fakes success, never bypasses a challenge
or login, uses no private candidate profile (a synthetic candidate; match is
NOT_EVALUATED), and runs the browser route headlessly. Individual access-limited
/ unsupported companies are recorded truthfully and do not, by themselves, fail
the phase.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from atlas.careers.discovery import CareerSourceDiscoveryService, DiscoveryMethod
from atlas.careers.profile import (
    CareerSiteProfile,
    RecipeHealth,
    RouteKind,
    load_profile,
    save_profile,
)
from atlas.careers.router import CareerSourceRouter, RouteDecision
from atlas.careers.trust import OfficialUrlTrustPolicy, TrustDecision
from atlas.config import Settings
from atlas.models import ErrorCategory
from atlas.sources.ats.base import detect_challenge
from atlas.sources.fingerprint import fingerprint_ats
from atlas.sources.generic import build_careers_registry
from atlas.sources.http_client import HttpError, HttpRequest, ReadOnlyHttpClient
from atlas.sources.models import SourceInstance, SourceType
from atlas.persistence.sqlite import StateStore
from atlas.planning import PlannedCompany

_HTML_ACCEPT = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"

# The four ATS families the router routes to a structured adapter (mirrors
# atlas.careers.router._ATS_ROUTABLE) — used for the §5.2 fingerprint-first
# fast route in the pilot.
_ATS_ROUTABLE_TYPES: frozenset[SourceType] = frozenset(
    {SourceType.ATS_GREENHOUSE, SourceType.ATS_LEVER, SourceType.ATS_ASHBY, SourceType.ATS_WORKDAY}
)


class PilotRunReuseError(RuntimeError):
    """Raised when a FRESH live pilot is started with a run_id that already has a
    terminal (completed) sealed pilot run. A completed run id must never silently
    masquerade as a new live test (build spec 5.8). Pass ``resume=True`` to
    explicitly, idempotently resume that run instead."""


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass(frozen=True)
class PilotCompany:
    company_id: str
    name: str
    official_domain: str
    known_careers_url: Optional[str] = None
    tier: str = "A"
    mode: str = "DELTA"
    geography_group: str = "PRIMARY"
    expected_route: Optional[str] = None   # ATS | GENERIC_HTTP | GENERIC_BROWSER (hint only)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PilotConfig:
    run_id: str
    companies: list[PilotCompany] = field(default_factory=list)
    lanes: list[str] = field(default_factory=lambda: ["JAVA_BACKEND", "GENERAL_SOFTWARE"])
    geography_groups: list[str] = field(default_factory=lambda: ["PRIMARY"])
    max_pages: int = 2
    max_jobs_per_company: int = 20
    request_budget: int = 40
    max_load_cycles: int = 2

    def __post_init__(self) -> None:
        if len(self.companies) > 12:
            raise ValueError("pilot allows at most 12 companies")

    def canonical(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "companies": [c.to_dict() for c in self.companies],
            "lanes": sorted(self.lanes),
            "geography_groups": sorted(self.geography_groups),
            "max_pages": self.max_pages,
            "max_jobs_per_company": self.max_jobs_per_company,
            "request_budget": self.request_budget,
            "max_load_cycles": self.max_load_cycles,
        }

    def seal_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class PilotCompanyResult:
    company_id: str
    name: str
    official_domain: str
    entry_url: Optional[str] = None
    route_kind: str = "UNKNOWN"
    fingerprint_family: Optional[str] = None
    execution: str = "HTTP"                 # HTTP | BROWSER | ATS | NONE
    terminal_status: str = "NOT_ATTEMPTED"
    extracted: int = 0
    reported: int = 0
    pages: int = 0
    health: str = "UNKNOWN"
    limitation: Optional[str] = None
    sample: list[dict] = field(default_factory=list)
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PilotResult:
    run_id: str
    config_hash: str
    status: str
    companies: list[PilotCompanyResult] = field(default_factory=list)
    report_path: Optional[str] = None
    report_valid: Optional[bool] = None
    runtime_terminal: Optional[str] = None
    elapsed_s: float = 0.0
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "status": self.status,
            "report_path": self.report_path,
            "report_valid": self.report_valid,
            "runtime_terminal": self.runtime_terminal,
            "elapsed_s": round(self.elapsed_s, 2),
            "summary": dict(self.summary),
            "companies": [c.to_dict() for c in self.companies],
        }


@dataclass
class _Prepared:
    routable: list[tuple[PilotCompany, RouteDecision]] = field(default_factory=list)
    unroutable: list[tuple[PilotCompany, RouteDecision]] = field(default_factory=list)


@dataclass
class _PreparedEntry:
    """One company's resolved career entry point: its route decision plus the
    ACTUAL trust decision, discovery method, and reachability validation. The
    pilot persists the REAL trust/validation — never a hard-coded True."""

    company: "PilotCompany"
    decision: RouteDecision
    trust: TrustDecision
    discovery_method: str
    validated: bool
    reachability: Optional[str] = None


@dataclass
class _Prepared:
    routable: list[_PreparedEntry] = field(default_factory=list)
    unroutable: list[_PreparedEntry] = field(default_factory=list)


class CareerPilot:
    """Seals, prepares (discovery + routing) and executes a career pilot."""

    def __init__(self, settings: Settings, config: PilotConfig, *, browser_profile_dir: Optional[Path] = None):
        self.settings = settings
        self.config = config
        self.registry = build_careers_registry()
        self.browser_profile_dir = Path(browser_profile_dir) if browser_profile_dir else Path(".browser-profile-careers")

    # -- routing (bounded live fetch) --------------------------------------
    def _client(self) -> ReadOnlyHttpClient:
        return ReadOnlyHttpClient(
            connect_timeout=10.0, read_timeout=20.0,
            max_response_bytes=5 * 1024 * 1024, max_redirects=5,
            request_budget=self.config.request_budget, accept=_HTML_ACCEPT,
        )

    def _entry_url(self, company: PilotCompany) -> tuple[str, str]:
        """Resolve one entry URL + its discovery method. A user-supplied URL is
        USER_SUPPLIED; a domain-only company falls back to a COMMON_PATH guess —
        a LEAD that is only validated once a bounded fetch confirms it."""
        if company.known_careers_url:
            return company.known_careers_url, DiscoveryMethod.USER_SUPPLIED
        return f"https://{company.official_domain.strip().lower().lstrip('.')}/careers", DiscoveryMethod.COMMON_PATH

    def _discover_entry(self, company: PilotCompany, *, client: Optional[ReadOnlyHttpClient]) -> Optional[tuple[str, str, bool, str]]:
        """§5.3: for a domain-only company, integrate CareerSourceDiscoveryService
        to find a REAL entry point (homepage nav link / sitemap / redirect) from
        bounded evidence. Returns (url, method, validated, reachability) for the
        best trusted, evidence-VALIDATED entry point, or None if discovery found
        only shape-trusted leads (so the caller falls back to the common path)."""
        if company.known_careers_url or client is None:
            return None
        svc = CareerSourceDiscoveryService(http_client=client)
        outcome = svc.discover(company.official_domain, company_id=company.company_id, name=company.name, fetch=True)
        # Persist every discovered entry point (append-only, trust + validation).
        with StateStore(self.settings.state_db) as store:
            svc.persist(store, outcome)
        validated = [e for e in outcome.trusted_entry_points if e.validated]
        if validated:
            best = max(validated, key=lambda e: e.confidence)
            return best.url, best.discovery_method, True, best.reachability or "DISCOVERED"
        return None

    def prepare(self, *, live: bool) -> _Prepared:
        prepared = _Prepared()
        router = CareerSourceRouter()
        client = self._client() if live else None
        for company in self.config.companies:
            # §5.3 domain-only discovery first (evidence-validated entry wins).
            discovered = self._discover_entry(company, client=client) if live else None
            if discovered is not None:
                entry_url, method, disc_validated, disc_reach = discovered
            else:
                entry_url, method = self._entry_url(company)
                disc_validated, disc_reach = False, None

            trust = OfficialUrlTrustPolicy(company.official_domain)
            trust_decision = trust.classify(entry_url)

            # §5.1 UNTRUSTED entry: NEVER fetched, routed, or planned.
            if not trust_decision.trusted:
                decision = RouteDecision(
                    RouteKind.UNSUPPORTED_SITE, entry_url,
                    reason=f"untrusted entry ({trust_decision.reason})",
                    evidence={"trust": trust_decision.to_dict()},
                )
                prepared.unroutable.append(
                    _PreparedEntry(company, decision, trust_decision, method, False, "UNTRUSTED_NOT_FETCHED")
                )
                continue

            # §5.2 KNOWN-ATS FAST ROUTE: fingerprint the URL BEFORE any fetch. A
            # known ATS host routes to its structured adapter with no landing
            # fetch (a blocked landing never disables the API path).
            fp = fingerprint_ats(entry_url)
            if fp.matched and fp.source_type in _ATS_ROUTABLE_TYPES:
                decision = router.route(entry_url, company_id=company.company_id, html=None)
                if decision.source_instance is not None:
                    decision = self._bound_instance(decision)
                entry = _PreparedEntry(company, decision, trust_decision, method, True, "KNOWN_ATS_HOST")
                (prepared.routable if decision.routable else prepared.unroutable).append(entry)
                continue

            # Generic route: bounded live fetch, then classify + validate.
            html = None
            status = 200
            challenge = login = False
            final_url = entry_url
            validated = disc_validated
            reachability = disc_reach
            if live and client is not None:
                try:
                    resp = client.fetch(HttpRequest(entry_url, headers={"Accept": _HTML_ACCEPT}))
                    status = resp.status
                    final_url = resp.url
                    html = resp.text() if status == 200 else None
                    cat = detect_challenge(resp)
                    challenge = cat == ErrorCategory.ANTI_BOT
                    login = cat == ErrorCategory.LOGIN_WALL
                    # A redirect that lands on a known ATS host is a validated
                    # official route regardless of the original guess.
                    fp_final = fingerprint_ats(final_url)
                    if fp_final.matched and fp_final.source_type in _ATS_ROUTABLE_TYPES:
                        validated = True
                        reachability = "REDIRECT_TO_ATS"
                    elif status == 200 and html and not challenge and not login:
                        validated = True
                        reachability = "FETCH_200_CONTENT"
                    elif challenge or login or status in (401, 403, 429):
                        reachability = f"ACCESS_LIMITED_HTTP_{status}"
                    else:
                        # §5.3: a nonexistent/failed common path (404/5xx) is a
                        # LEAD that never became a validated entry — not routed.
                        reachability = f"HTTP_{status}"
                        decision = RouteDecision(
                            RouteKind.UNSUPPORTED_SITE, entry_url,
                            reason=f"entry not reachable (HTTP {status})",
                            evidence={"final_url": final_url, "status": status},
                        )
                        prepared.unroutable.append(
                            _PreparedEntry(company, decision, trust_decision, method, False, reachability)
                        )
                        continue
                except HttpError as exc:
                    prepared.unroutable.append(
                        _PreparedEntry(
                            company,
                            RouteDecision(RouteKind.UNSUPPORTED_SITE, entry_url,
                                          reason=f"fetch error: {exc.category.value}",
                                          evidence={"error": exc.message}),
                            trust_decision, method, False, f"FETCH_ERROR_{exc.category.value}",
                        )
                    )
                    continue

            decision = router.route(
                entry_url, company_id=company.company_id, html=html, status=status,
                final_url=final_url, challenge=challenge, login_wall=login,
            )
            if decision.source_instance is not None:
                decision = self._bound_instance(decision)
                entry = _PreparedEntry(company, decision, trust_decision, method, validated, reachability)
                (prepared.routable if decision.routable else prepared.unroutable).append(entry)
            else:
                prepared.unroutable.append(
                    _PreparedEntry(company, decision, trust_decision, method, validated, reachability)
                )
        return prepared

    def _bound_instance(self, decision: RouteDecision) -> RouteDecision:
        inst = decision.source_instance
        if inst is None:
            return decision
        md = dict(inst.metadata)
        md.setdefault("entry_url", decision.entry_url)
        md["max_pages"] = self.config.max_pages
        md["max_results"] = self.config.max_jobs_per_company
        md["max_load_cycles"] = self.config.max_load_cycles
        md["request_budget"] = self.config.request_budget
        md["render_wait_ms"] = 3000
        md["headless"] = True
        md["profile_dir"] = str(self.browser_profile_dir)
        bounded = SourceInstance(
            instance_id=inst.instance_id, source_type=inst.source_type, source_family=inst.source_family,
            display_name=inst.display_name, base_url=inst.base_url, tenant=inst.tenant, site=inst.site,
            company_id=inst.company_id, enabled=True, capability_overrides=inst.capability_overrides,
            capability_removals=inst.capability_removals, metadata=md,
        )
        return RouteDecision(
            route_kind=decision.route_kind, entry_url=decision.entry_url, source_instance=bounded,
            fingerprint_family=decision.fingerprint_family, confidence=decision.confidence,
            reason=decision.reason, evidence=decision.evidence,
        )

    # -- persistence of prepared routing -----------------------------------
    def _persist_prepared(self, prepared: _Prepared) -> None:
        router = CareerSourceRouter()
        with StateStore(self.settings.state_db) as store:
            for entry in prepared.routable + prepared.unroutable:
                company = entry.company
                decision = entry.decision
                # §5.1: persist the REAL trust decision + validation — NEVER a
                # hard-coded trusted=True. Entry identity includes company_id.
                entry_id = "entry::" + hashlib.sha1(
                    f"{company.company_id}::{decision.entry_url}".encode()
                ).hexdigest()[:16]
                store.record_career_entry_point(
                    entry_id, decision.entry_url, company_id=company.company_id, label=company.name,
                    discovery_method=entry.discovery_method,
                    trusted=entry.trust.trusted, trust_kind=entry.trust.kind.value,
                    confidence=decision.confidence, validated=entry.validated,
                    reachability=entry.reachability,
                    evidence={"expected_route": company.expected_route, "trust_reason": entry.trust.reason},
                )
                # Only a TRUSTED entry is routed/persisted as a route + profile.
                if not entry.trust.trusted:
                    continue
                router.persist(store, decision, company_id=company.company_id)
                if decision.source_instance is not None and decision.routable:
                    profile = CareerSiteProfile(
                        profile_id="prof::" + decision.source_instance.instance_id,
                        company_id=company.company_id,
                        source_instance_id=decision.source_instance.instance_id,
                        entry_url=decision.entry_url, route_kind=decision.route_kind,
                        confidence=decision.confidence,
                        evidence=decision.evidence,
                        parser_version=decision.source_instance.metadata.get("recipe", {}).get("extraction_method", ""),
                        health=RecipeHealth.UNVALIDATED,
                    )
                    if decision.source_instance.metadata.get("recipe"):
                        from atlas.careers.profile import ExtractionRecipe
                        profile.recipe = ExtractionRecipe.from_dict(decision.source_instance.metadata["recipe"])
                    save_profile(store, profile)

    # -- execution ----------------------------------------------------------
    def run(self, *, live: bool, report_path: Optional[Path] = None, resume: bool = False) -> PilotResult:
        started = time.monotonic()
        config_hash = self.config.seal_hash()
        # §5.8: a fresh live run must not silently reuse a completed run id.
        with StateStore(self.settings.state_db) as store:
            existing = store.get_career_pilot_run(self.config.run_id)
            if live and existing is not None and not resume:
                if existing["status"] in ("PASS", "PARTIAL", "COMPLETE"):
                    raise PilotRunReuseError(
                        f"pilot run_id {self.config.run_id!r} already completed with status "
                        f"{existing['status']!r}; pass resume=True to resume it, or use a fresh run_id."
                    )
            # Seal the config BEFORE execution begins.
            store.upsert_career_pilot_run(
                self.config.run_id, config_hash, config=self.config.canonical(),
                status="RESUMING" if (resume and existing is not None) else "SEALED",
            )
        prepared = self.prepare(live=live)
        self._persist_prepared(prepared)

        runtime_terminal = None
        report_written = None
        report_valid = None
        planned_companies: list[PlannedCompany] = []
        instances: dict[str, SourceInstance] = {}
        for entry in prepared.routable:
            company = entry.company
            inst = entry.decision.source_instance
            instances[inst.instance_id] = inst
            planned_companies.append(
                PlannedCompany(
                    company_id=company.company_id, name=company.name, tier=company.tier,
                    mode=company.mode, source_instances=(inst.instance_id,),
                    geography_group=company.geography_group,
                )
            )

        if live and planned_companies:
            from atlas.runtime.production import ProductionSearchRuntime

            out = report_path or (self.settings.output_dir / f"Atlas_Career_Pilot_{self.config.run_id}.xlsx")
            runtime = ProductionSearchRuntime(
                self.settings, self.config.run_id, fixture_mode=True,
                companies=planned_companies, instances=instances, registry=self.registry,
                parallel_workers=1, lane_override=list(self.config.lanes),
                max_per_company=1, max_per_instance=1, max_per_tenant=1,
                retry_budget=2, write_report=True, report_path=out, use_run_lock=True,
            )
            rr = runtime.run()
            runtime_terminal = rr.terminal_state
            report_written = rr.report_path
            report_valid = rr.report_valid
            # §5.7: mark the recipe/profile HEALTHY for every instance that
            # actually extracted current-run jobs, with a last_success/validation
            # time — so a later run reuses the route only after revalidation.
            self._mark_successful_profiles_healthy(instances)

        result = self._build_matrix(prepared, runtime_terminal)
        result.config_hash = config_hash
        result.report_path = report_written
        result.report_valid = report_valid
        result.runtime_terminal = runtime_terminal
        result.elapsed_s = time.monotonic() - started
        result.status = self._overall_status(result, live=live)
        with StateStore(self.settings.state_db) as store:
            store.upsert_career_pilot_run(
                self.config.run_id, config_hash, config=self.config.canonical(),
                status=result.status, results=result.to_dict(),
            )
        return result

    # -- profile health (build spec 5.7) -----------------------------------
    def _mark_successful_profiles_healthy(self, instances: dict[str, SourceInstance]) -> None:
        """Mark a persisted career-site profile HEALTHY (with last_success_at /
        last_validated_at) for each instance that staged >=1 current-run
        observation — a successful recipe is HEALTHY and revalidate-before-reuse;
        an instance that yielded nothing keeps its UNVALIDATED/drift state."""
        with StateStore(self.settings.state_db) as store:
            staged: dict[str, int] = {}
            for row in store.list_raw_observations(self.config.run_id):
                staged[row["source_instance"]] = staged.get(row["source_instance"], 0) + 1
            for instance_id in instances:
                profile = load_profile(store, instance_id)
                if profile is None:
                    continue
                if staged.get(instance_id, 0) > 0:
                    profile.mark_success(confidence=max(profile.confidence, 0.8))
                    save_profile(store, profile)

    # -- matrix -------------------------------------------------------------
    def _build_matrix(self, prepared: _Prepared, runtime_terminal: Optional[str]) -> PilotResult:
        result = PilotResult(run_id=self.config.run_id, config_hash="", status="PENDING")
        with StateStore(self.settings.state_db) as store:
            from atlas.sources.coverage import CoverageManifest

            manifest = CoverageManifest.load(store, self.config.run_id)
            tasks_by_company: dict[str, list] = {}
            for t in manifest.tasks():
                # coverage_id like "company_delta::<cid>::<inst>::<lane>"; use company name
                tasks_by_company.setdefault(t.company or "", []).append(t)
            obs = store.list_raw_observations(self.config.run_id)
            obs_by_company: dict[str, list] = {}
            for row in obs:
                obs_by_company.setdefault(row["company"] or "", []).append(row)

            for entry in prepared.routable:
                company = entry.company
                decision = entry.decision
                cr = PilotCompanyResult(
                    company_id=company.company_id, name=company.name,
                    official_domain=company.official_domain, entry_url=decision.entry_url,
                    route_kind=decision.route_kind.value,
                    fingerprint_family=decision.fingerprint_family.value if decision.fingerprint_family else None,
                    execution=("ATS" if decision.route_kind == RouteKind.ATS
                               else ("BROWSER" if decision.route_kind == RouteKind.GENERIC_BROWSER else "HTTP")),
                )
                ctasks = [t for t in manifest.tasks()
                          if decision.source_instance and t.source_instance == decision.source_instance.instance_id]
                extracted = 0
                pages = 0
                statuses = []
                healths = set()
                for t in ctasks:
                    statuses.append(t.status.value)
                    for att in store.list_coverage_attempts(t.coverage_id):
                        extracted += int(att["jobs_found"] or 0)
                    for pg in store.list_coverage_pages(self.config.run_id, t.coverage_id):
                        pages = max(pages, int(pg["page_index"]))
                    for h in store.list_source_health(t.source_instance, limit=5):
                        healths.add(h["state"])
                reported = 0
                sample: list[dict] = []
                for row in obs:
                    if decision.source_instance and row["source_instance"] == decision.source_instance.instance_id:
                        reported += 1
                        if len(sample) < 3:
                            sample.append({"title": row["title"], "url": row["canonical_url"] or row["source_url"]})
                cr.extracted = extracted
                cr.reported = reported
                cr.pages = pages
                cr.sample = sample
                cr.health = ",".join(sorted(healths)) if healths else "UNKNOWN"
                cr.terminal_status = self._company_terminal(statuses)
                result.companies.append(cr)

            for entry in prepared.unroutable:
                company = entry.company
                decision = entry.decision
                limitation = decision.reason
                cr = PilotCompanyResult(
                    company_id=company.company_id, name=company.name,
                    official_domain=company.official_domain, entry_url=decision.entry_url,
                    route_kind=decision.route_kind.value, execution="NONE",
                    terminal_status=decision.route_kind.value, limitation=limitation,
                    health="ACCESS_LIMITED" if decision.route_kind == RouteKind.ACCESS_LIMITED else "UNKNOWN",
                )
                result.companies.append(cr)

        # summary counts
        result.summary = self._summary(result)
        return result

    @staticmethod
    def _company_terminal(statuses: list[str]) -> str:
        if not statuses:
            return "NOT_ATTEMPTED"
        if any(s == "COMPLETED_WITH_RESULTS" for s in statuses):
            return "COMPLETED_WITH_RESULTS"
        order = ["ACCESS_LIMITED", "AUTH_REQUIRED", "RATE_LIMITED", "SOURCE_UNAVAILABLE",
                 "EXTRACTION_UNRESOLVED", "PARTIAL_BUDGET", "ATTEMPTED_ZERO", "FAILED"]
        for o in order:
            if any(s == o for s in statuses):
                return o
        return statuses[0]

    def _summary(self, result: PilotResult) -> dict[str, Any]:
        http_ok = sum(1 for c in result.companies if c.execution == "HTTP" and c.extracted > 0)
        browser_ok = sum(1 for c in result.companies if c.execution == "BROWSER" and c.extracted > 0)
        ats = sum(1 for c in result.companies if c.execution == "ATS")
        ats_ok = sum(1 for c in result.companies if c.execution == "ATS" and c.extracted > 0)
        terminal = sum(1 for c in result.companies if c.terminal_status not in ("NOT_ATTEMPTED", "IN_PROGRESS"))
        return {
            "companies": len(result.companies),
            "http_success": http_ok,
            "browser_success": browser_ok,
            "ats_routes": ats,
            "ats_success": ats_ok,
            "terminal": terminal,
            "reported_jobs": sum(c.reported for c in result.companies),
            "extracted_jobs": sum(c.extracted for c in result.companies),
        }

    def _overall_status(self, result: PilotResult, *, live: bool) -> str:
        if not live:
            return "DRY_RUN"
        s = result.summary
        all_terminal = s["terminal"] == s["companies"] and s["companies"] > 0
        report_ok = result.report_valid in (True, None)
        if all_terminal and s["http_success"] >= 1 and s["browser_success"] >= 1 and report_ok:
            return "PASS"
        if all_terminal and (s["http_success"] >= 1 or s["browser_success"] >= 1 or s["ats_success"] >= 1):
            return "PARTIAL"
        if s["terminal"] == 0:
            return "WAITING_FOR_HUMAN"
        return "PARTIAL"


__all__ = [
    "PilotCompany",
    "PilotConfig",
    "PilotCompanyResult",
    "PilotResult",
    "CareerPilot",
    "load_pilot_config",
]


def load_pilot_config(path: Path) -> PilotConfig:
    """Load a sealed pilot configuration from a YAML or JSON file."""
    text = Path(path).read_text(encoding="utf-8")
    if str(path).lower().endswith((".yaml", ".yml")):
        import yaml

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    companies = [
        PilotCompany(
            company_id=str(c["company_id"]),
            name=str(c.get("name", c["company_id"])),
            official_domain=str(c["official_domain"]),
            known_careers_url=c.get("known_careers_url"),
            tier=str(c.get("tier", "A")),
            mode=str(c.get("mode", "DELTA")),
            geography_group=str(c.get("geography_group", "PRIMARY")),
            expected_route=c.get("expected_route"),
        )
        for c in data.get("companies", [])
    ]
    return PilotConfig(
        run_id=str(data.get("run_id", "career-pilot")),
        companies=companies,
        lanes=list(data.get("lanes", ["JAVA_BACKEND", "GENERAL_SOFTWARE"])),
        geography_groups=list(data.get("geography_groups", ["PRIMARY"])),
        max_pages=int(data.get("max_pages", 2)),
        max_jobs_per_company=int(data.get("max_jobs_per_company", 20)),
        request_budget=int(data.get("request_budget", 40)),
        max_load_cycles=int(data.get("max_load_cycles", 2)),
    )
