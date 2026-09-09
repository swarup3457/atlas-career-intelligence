"""Live portal -> official follow-up + linkage (Phase 1E/F recovery §5.5 / §13.6/7).

Implements the spec 5.5 chain for LIVE portal leads, composing EXISTING pieces
(no new orchestration framework):

    portal lead (LinkedIn) -> merge-safe company identity -> known/resolved
    official source -> real official adapter search (Greenhouse/Lever/Ashby)
    -> PortalOfficialVerifier deterministic linkage

For every configured "known official source" company (a company whose official
ATS board is independently known — NEVER a domain guessed from a name), this:

  1. runs the REAL read-only LinkedIn adapter for that company -> real portal
     leads (persisted append-only);
  2. runs the REAL official ATS adapter for that company's board -> real official
     observations (staged append-only);
  3. runs :class:`PortalOfficialVerifier` to link leads to official evidence.

Result preserves every truthful state: LINKED_OFFICIAL_VERIFIED,
CLOSED_POSITIVE_EVIDENCE, MANUAL_VERIFICATION (ambiguous), OFFICIAL_SOURCE_
UNRESOLVED (no official jobs found), and PORTAL_CURRENT_LEAD (no match). Nothing
here signs in, applies, or guesses a domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from atlas.config import Settings
from atlas.persistence.sqlite import StateStore
from atlas.runtime.canonicalize import canonicalize_run
from atlas.runtime.official_universe import project_run_official_jobs
from atlas.sources.adapter import AdapterError
from atlas.sources.ats.ashby import AshbyAdapter
from atlas.sources.ats.greenhouse import GreenhouseAdapter
from atlas.sources.ats.lever import LeverAdapter
from atlas.sources.detail_hydration import DetailHydrator
from atlas.sources.generic import build_careers_registry
from atlas.sources.http_client import ReadOnlyHttpClient
from atlas.sources.models import (
    SearchRequest,
    SourceFamily,
    SourceInstance,
    SourceType,
)
from atlas.sources.portals import make_portal_instance
from atlas.sources.portals.models import PortalJobLead
from atlas.sources.portals.registry import adapter_class_for_family
from atlas.market.verification import PortalOfficialVerifier

_ATS = {
    "greenhouse": (GreenhouseAdapter, SourceType.ATS_GREENHOUSE),
    "lever": (LeverAdapter, SourceType.ATS_LEVER),
    "ashby": (AshbyAdapter, SourceType.ATS_ASHBY),
}


@dataclass(frozen=True)
class KnownOfficialSource:
    """A company whose OFFICIAL ATS board is independently known (not guessed).
    ``board_token`` is the public board slug for the ATS family."""

    company_name: str
    official_domain: str
    family: str            # greenhouse | lever | ashby
    board_token: str
    linkedin_query: Optional[str] = None   # defaults to company_name


@dataclass
class CompanyFollowup:
    company_name: str
    official_domain: str
    family: str
    portal_leads: int = 0
    official_jobs: int = 0
    official_attempted: bool = False
    official_status: str = "NOT_ATTEMPTED"     # COMPLETED | ZERO | ACCESS_LIMITED | AUTH_REQUIRED | SOURCE_UNAVAILABLE
    linked_verified: int = 0
    manual_verification: int = 0
    closed: int = 0
    unlinked: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_name": self.company_name, "official_domain": self.official_domain,
            "family": self.family, "portal_leads": self.portal_leads,
            "official_jobs": self.official_jobs, "official_attempted": self.official_attempted,
            "official_status": self.official_status, "linked_verified": self.linked_verified,
            "manual_verification": self.manual_verification, "closed": self.closed,
            "unlinked": self.unlinked, "detail": self.detail,
        }


@dataclass
class FollowupResult:
    run_id: str
    companies: list[CompanyFollowup] = field(default_factory=list)
    jobs: list = field(default_factory=list)          # PORTAL_OFFICIAL_LINKED official RankableJobs
    portal_only: list = field(default_factory=list)   # portal-only lead rows (NEVER in All_Jobs)

    def rankable_jobs(self) -> list:
        return list(self.jobs)

    def portal_only_leads(self) -> list:
        return list(self.portal_only)

    @property
    def portal_only_count(self) -> int:
        return len(self.portal_only)

    @property
    def linked_official_jobs(self) -> int:
        return len(self.jobs)

    @property
    def official_attempts(self) -> int:
        return sum(1 for c in self.companies if c.official_attempted)

    @property
    def linked_verified_total(self) -> int:
        return sum(c.linked_verified for c in self.companies)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "official_attempts": self.official_attempts,
            "linked_verified_total": self.linked_verified_total,
            "portal_official_linked_jobs": self.linked_official_jobs,
            "portal_only_leads": self.portal_only_count,
            "companies": [c.to_dict() for c in self.companies],
        }


class LiveOfficialFollowup:
    """Runs live portal->official follow-up + linkage for known-source companies."""

    def __init__(
        self,
        settings: Settings,
        run_id: str,
        *,
        known_sources: Sequence[KnownOfficialSource],
        location: str = "",
        recency_days: int = 30,
        max_portal_pages: int = 2,
        max_portal_cards: int = 10,
        max_official: int = 200,
        portal_adapter_factory: Optional[Callable[[str], Any]] = None,
        official_adapter_factory: Optional[Callable[[KnownOfficialSource], Any]] = None,
        registry=None,
        store_path=None,
    ) -> None:
        self.settings = settings
        self.run_id = run_id
        self.known_sources = list(known_sources)
        self.location = location
        self.recency_days = recency_days
        self.max_portal_pages = max(1, max_portal_pages)
        self.max_portal_cards = max(1, max_portal_cards)
        self.max_official = max(1, max_official)
        self._portal_factory = portal_adapter_factory
        self._official_factory = official_adapter_factory
        self.registry = registry or build_careers_registry()
        self.store_path = store_path or settings.state_db

    # -- adapters -----------------------------------------------------------
    def _portal_adapter(self):
        if self._portal_factory is not None:
            return self._portal_factory("linkedin")
        inst = make_portal_instance(SourceFamily.LINKEDIN, f"li-followup-{self.run_id}",
                                    metadata={"max_pages": self.max_portal_pages,
                                              "max_results": self.max_portal_cards})
        client = ReadOnlyHttpClient(accept="text/html,application/xhtml+xml,*/*;q=0.8",
                                    request_budget=self.max_portal_pages + 2)
        return adapter_class_for_family(SourceFamily.LINKEDIN)(inst, http_client=client)

    def _official_adapter(self, src: KnownOfficialSource):
        if self._official_factory is not None:
            return self._official_factory(src)
        cls, stype = _ATS[src.family]
        inst = SourceInstance(instance_id=f"{src.family}-{src.board_token}", source_type=stype,
                              company_id=src.board_token, metadata={"board_token": src.board_token})
        return cls(inst, http_client=ReadOnlyHttpClient(request_budget=5))

    # -- per-company --------------------------------------------------------
    def _discover_portal_leads(self, src: KnownOfficialSource, store) -> int:
        adapter = self._portal_adapter()
        query = src.linkedin_query or src.company_name
        seen: set[str] = set()
        staged = 0
        for page in range(1, self.max_portal_pages + 1):
            req = SearchRequest(query=query, location=self.location, recency_days=self.recency_days,
                                limit=self.max_portal_cards, page=page)
            try:
                res = adapter.search(req)
            except AdapterError:
                break
            for r in res.results:
                lead = PortalJobLead.from_discovery_result(
                    r, run_id=self.run_id, source_family="linkedin",
                    result_query=query, result_page=res.page, result_cursor=res.next_cursor)
                if not lead.has_min_identity():
                    continue
                ch = lead.content_hash()
                if ch in seen:
                    continue
                seen.add(ch)
                lead.persist(store)
                staged += 1
            if not res.has_more:
                break
        return staged

    def _discover_official(self, src: KnownOfficialSource, store, cf: CompanyFollowup) -> int:
        adapter = self._official_adapter(src)
        cf.official_attempted = True
        try:
            res = adapter.search(SearchRequest(query="", location=self.location, limit=self.max_official))
        except AdapterError as exc:
            cf.official_status = {
                "ANTI_BOT": "ACCESS_LIMITED", "LOGIN_WALL": "AUTH_REQUIRED",
                "HTTP_429": "RATE_LIMITED",
            }.get(exc.category.value, "SOURCE_UNAVAILABLE")
            cf.detail = exc.message
            return 0
        staged = 0
        for r in res.results:
            oid = f"off::{self.run_id}::{src.family}::{r.source_job_id or r.canonical_url or staged}"
            store.stage_raw_observation(
                oid, self.run_id, r.source_instance or f"{src.family}-{src.board_token}",
                r.content_hash(), source_family=src.family,
                source_job_id=r.source_job_id, source_url=r.source_url, canonical_url=r.canonical_url,
                company=src.company_name, title=r.title, location=r.location,
                posted_at=r.posted_at,
                is_active=r.is_active.value if hasattr(r.is_active, "value") else str(r.is_active),
                source_identity=r.source_job_id or r.canonical_url, adapter_version=r.adapter_version,
                parser_version=r.parser_version,
                detail={"verification_level": r.verification_level.value if hasattr(r.verification_level, "value") else None,
                        "requisition_id": (r.provenance or {}).get("requisition_id"),
                        "date_provenance": (r.provenance or {}).get("date_provenance")})
            staged += 1
        cf.official_status = "COMPLETED" if staged else "ZERO"
        return staged

    # -- run ----------------------------------------------------------------
    def _hydration_instances(self) -> dict:
        fam_enum = {"greenhouse": SourceFamily.GREENHOUSE, "lever": SourceFamily.LEVER,
                    "ashby": SourceFamily.ASHBY}
        instances: dict[str, SourceInstance] = {}
        for src in self.known_sources:
            cls, stype = _ATS[src.family]
            iid = f"{src.family}-{src.board_token}"
            instances[iid] = SourceInstance(
                instance_id=iid, source_type=stype, source_family=fam_enum.get(src.family),
                company_id=src.board_token, metadata={"board_token": src.board_token})
        return instances

    def run(self) -> FollowupResult:
        result = FollowupResult(run_id=self.run_id)
        self._rankable_jobs = []
        with StateStore(self.store_path) as store:
            for src in self.known_sources:
                cf = CompanyFollowup(company_name=src.company_name, official_domain=src.official_domain,
                                     family=src.family)
                cf.portal_leads = self._discover_portal_leads(src, store)
                cf.official_jobs = self._discover_official(src, store, cf)
                result.companies.append(cf)

            # Hydrate + canonicalize the run's STAGED official observations so the
            # linked official jobs carry real requirements and a canonical id
            # BEFORE linkage attaches portal leads to them.
            try:
                DetailHydrator(store, self.registry, self._hydration_instances(),
                               run_id=self.run_id, max_details=self.max_official).hydrate()
            except Exception:  # noqa: BLE001 - hydration failure is not fatal to linkage
                pass
            canonicalize_run(store, self.run_id)

            # Deterministic linkage across ALL run-scoped leads vs official obs.
            links = PortalOfficialVerifier(store).link_run(self.run_id)

            # attribute link outcomes back to companies (by lead company identity)
            from atlas.company.identity import company_identity_key

            by_key: dict[str, CompanyFollowup] = {
                company_identity_key(c.company_name): c for c in result.companies
            }
            lead_rows = {row["lead_id"]: row for row in store.list_portal_leads(self.run_id)}
            linked_canonicals: set[str] = set()
            for lk in links:
                row = lead_rows.get(lk.lead_id)
                if row is None:
                    continue
                state = lk.verification_state.value
                if state == "LINKED_OFFICIAL_VERIFIED" and lk.canonical_id:
                    linked_canonicals.add(lk.canonical_id)
                cf = by_key.get(company_identity_key(row["company_name"] or ""))
                if cf is None:
                    continue
                if state == "LINKED_OFFICIAL_VERIFIED":
                    cf.linked_verified += 1
                elif state == "CLOSED_POSITIVE_EVIDENCE":
                    cf.closed += 1
                elif state == "MANUAL_VERIFICATION":
                    cf.manual_verification += 1
                else:
                    cf.unlinked += 1
            for cf in result.companies:
                if cf.official_status == "COMPLETED" and cf.official_jobs == 0:
                    cf.official_status = "ZERO"
                if cf.official_jobs == 0 and cf.official_status not in (
                        "ACCESS_LIMITED", "AUTH_REQUIRED", "RATE_LIMITED", "SOURCE_UNAVAILABLE"):
                    cf.official_status = cf.official_status or "OFFICIAL_SOURCE_UNRESOLVED"

            # LINKED leads become OFFICIAL canonical jobs (PORTAL_OFFICIAL_LINKED):
            # the official source/url/requisition is PRIMARY; the portal channel is
            # retained only as provenance. A lead with no official match stays a
            # PORTAL_ONLY lead (persisted, never promoted into All_Jobs).
            linked_jobs, _ = project_run_official_jobs(
                store, self.run_id, canonical_filter=linked_canonicals,
                record_class="PORTAL_OFFICIAL_LINKED", extra_channels=("linkedin",))
            self._rankable_jobs = linked_jobs
            result.jobs = list(linked_jobs)
            result.portal_only = [
                self._lead_row_to_portal_only(row)
                for row in store.list_portal_leads(self.run_id)
                if (row["verification_state"] or "PORTAL_CURRENT_LEAD") != "LINKED_OFFICIAL_VERIFIED"
            ]
        return result

    @staticmethod
    def _lead_row_to_portal_only(row) -> dict:
        return {
            "lead_id": row["lead_id"], "source_family": row["source_family"],
            "company": row["company_name"], "title": row["title"], "location": row["location"],
            "url": row["canonical_url"], "verification_state": row["verification_state"] or "PORTAL_CURRENT_LEAD",
            "record_class": "PORTAL_ONLY",
        }

    def rankable_jobs(self) -> list:
        return list(getattr(self, "_rankable_jobs", []))


# A small, independently-verified set of companies whose OFFICIAL ATS boards are
# public and that post the same roles on LinkedIn (so a natural linkage exists).
# These board tokens are known facts, NOT domains guessed from a name.
DEFAULT_KNOWN_SOURCES: tuple[KnownOfficialSource, ...] = (
    KnownOfficialSource("GitLab", "gitlab.com", "greenhouse", "gitlab"),
    KnownOfficialSource("Groww", "groww.in", "greenhouse", "groww"),
    KnownOfficialSource("GitLab Demo (Lever)", "lever.co", "lever", "leverdemo", linkedin_query="Lever"),
)


__all__ = [
    "KnownOfficialSource",
    "CompanyFollowup",
    "FollowupResult",
    "LiveOfficialFollowup",
    "DEFAULT_KNOWN_SOURCES",
]
