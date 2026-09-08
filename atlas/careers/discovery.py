"""CareerSourceDiscoveryService — company + official domain -> trusted entry points.

Given a VERIFIED official domain (from the Company Registry) and optional known
careers URL, discover trusted career entry points using BOUNDED, DETERMINISTIC
evidence only:

    * a user/config-supplied known careers URL;
    * homepage navigation links whose label/path signal a careers section;
    * a small fixed set of approved subdomains / common official paths (NOT a
      brute-force of hundreds of paths);
    * ``robots.txt`` ``Sitemap:`` references and job/career-signalled sitemap URLs;
    * redirects that land on a known ATS tenant host.

Every candidate is gated by :class:`atlas.careers.trust.OfficialUrlTrustPolicy`
(fail closed). A URL found only inside job-posting text is never used. No live
search-engine result is treated as official truth. Every discovery observation
is recorded append-only; a company may have MANY current entry points
(global / India / graduate / acquired-business), and one never overwrites
another.

The service is usable BOTH live (inject a :class:`ReadOnlyHttpClient`) and
fully offline (inject ``homepage_html`` / ``robots_txt`` / a fetch map).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from atlas.careers import extract as X
from atlas.careers.trust import OfficialUrlTrustPolicy, TrustDecision
from atlas.models import ErrorCategory
from atlas.sources.ats.base import detect_challenge
from atlas.sources.http_client import HttpError, HttpRequest, ReadOnlyHttpClient

# Bounds — discovery is never a broad crawl.
MAX_CANDIDATE_PROBES = 8
MAX_SITEMAP_FETCHES = 3
MAX_ENTRY_POINTS = 12


class DiscoveryMethod:
    USER_SUPPLIED = "USER_SUPPLIED"
    NAV_LINK = "NAV_LINK"
    SUBDOMAIN = "SUBDOMAIN"
    COMMON_PATH = "COMMON_PATH"
    ROBOTS_SITEMAP = "ROBOTS_SITEMAP"
    REDIRECT_ATS = "REDIRECT_ATS"


# Discovery methods whose CANDIDATE is backed by real reachability/content/
# redirect evidence (so the entry point is VALIDATED, not merely a shape-trusted
# guess). A COMMON_PATH / SUBDOMAIN guess and even a USER_SUPPLIED URL are LEADS
# until a bounded fetch validates them (build spec 5.3).
_VALIDATED_METHODS: frozenset[str] = frozenset(
    {DiscoveryMethod.NAV_LINK, DiscoveryMethod.ROBOTS_SITEMAP, DiscoveryMethod.REDIRECT_ATS}
)
_REACHABILITY_FOR_METHOD: dict[str, str] = {
    DiscoveryMethod.NAV_LINK: "PRESENT_IN_HOMEPAGE",
    DiscoveryMethod.ROBOTS_SITEMAP: "PRESENT_IN_SITEMAP",
    DiscoveryMethod.REDIRECT_ATS: "REDIRECT_TO_ATS",
}


@dataclass(frozen=True)
class CareerEntryPoint:
    url: str
    label: str
    discovery_method: str
    trusted: bool
    trust_kind: str
    confidence: float
    company_id: Optional[str] = None
    validated: bool = False
    reachability: Optional[str] = None
    evidence: dict = field(default_factory=dict)

    @property
    def entry_id(self) -> str:
        # Identity includes company_id so two companies whose careers pages share
        # a URL get DISTINCT append-only observations (build spec 5.3).
        digest = hashlib.sha1(f"{self.company_id or ''}::{self.url}".encode("utf-8")).hexdigest()[:16]
        return "entry::" + digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "label": self.label,
            "discovery_method": self.discovery_method,
            "trusted": self.trusted,
            "trust_kind": self.trust_kind,
            "confidence": round(self.confidence, 3),
            "company_id": self.company_id,
            "validated": self.validated,
            "reachability": self.reachability,
            "evidence": dict(self.evidence),
        }


@dataclass
class DiscoveryOutcome:
    company_id: Optional[str]
    official_domain: str
    entry_points: list[CareerEntryPoint] = field(default_factory=list)
    rejected: list[CareerEntryPoint] = field(default_factory=list)
    status: str = "UNRESOLVED"
    notes: list[str] = field(default_factory=list)

    @property
    def trusted_entry_points(self) -> list[CareerEntryPoint]:
        return [e for e in self.entry_points if e.trusted]

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "official_domain": self.official_domain,
            "status": self.status,
            "entry_points": [e.to_dict() for e in self.entry_points],
            "rejected": [e.to_dict() for e in self.rejected],
            "notes": list(self.notes),
        }


@dataclass
class _Fetched:
    status: int
    text: str
    final_url: str
    challenge: bool = False
    login: bool = False
    error: Optional[str] = None


class CareerSourceDiscoveryService:
    """Discovers trusted career entry points for one company at a time."""

    def __init__(
        self,
        *,
        http_client: Optional[ReadOnlyHttpClient] = None,
        allow_known_ats: bool = True,
    ):
        self.http_client = http_client
        self.allow_known_ats = allow_known_ats

    # -- bounded fetch ------------------------------------------------------
    def _fetch(self, url: str, *, accept: str = "text/html,application/xhtml+xml,*/*;q=0.8") -> Optional[_Fetched]:
        if self.http_client is None:
            return None
        try:
            resp = self.http_client.fetch(HttpRequest(url, headers={"Accept": accept}))
        except HttpError as exc:
            return _Fetched(status=0, text="", final_url=url, error=exc.message)
        challenge = detect_challenge(resp)
        return _Fetched(
            status=resp.status,
            text=resp.text() if resp.status == 200 else "",
            final_url=resp.url,
            challenge=challenge == ErrorCategory.ANTI_BOT,
            login=challenge == ErrorCategory.LOGIN_WALL,
        )

    # -- discovery ----------------------------------------------------------
    def discover(
        self,
        official_domain: str,
        *,
        company_id: Optional[str] = None,
        name: str = "",
        known_careers_url: Optional[str] = None,
        approved_hosts: tuple[str, ...] = (),
        homepage_html: Optional[str] = None,
        robots_txt: Optional[str] = None,
        sitemap_map: Optional[dict[str, str]] = None,
        fetch: bool = True,
    ) -> DiscoveryOutcome:
        domain = official_domain.strip().lower().lstrip(".")
        policy = OfficialUrlTrustPolicy(domain, approved_hosts=approved_hosts, allow_known_ats=self.allow_known_ats)
        outcome = DiscoveryOutcome(company_id=company_id, official_domain=domain)
        seen: set[str] = set()

        def _add(url: Optional[str], label: str, method: str, base_conf: float,
                 evidence: Optional[dict] = None, *, from_posting: bool = False) -> None:
            norm = X.normalize_url(f"https://{domain}", url) if url and not url.lower().startswith("http") else url
            norm = X.normalize_url(None, norm) if norm else None
            if not norm or norm in seen:
                return
            seen.add(norm)
            decision = policy.classify(norm, from_posting_text=from_posting)
            is_validated = decision.trusted and method in _VALIDATED_METHODS
            ep = CareerEntryPoint(
                url=norm, label=label[:120], discovery_method=method,
                trusted=decision.trusted, trust_kind=decision.kind.value,
                confidence=base_conf if decision.trusted else 0.0,
                company_id=company_id,
                validated=is_validated,
                reachability=_REACHABILITY_FOR_METHOD.get(method) if is_validated else None,
                evidence={**(evidence or {}), "trust_reason": decision.reason},
            )
            if len(outcome.entry_points) + len(outcome.rejected) >= MAX_ENTRY_POINTS * 3:
                return
            (outcome.entry_points if decision.trusted else outcome.rejected).append(ep)

        # 1. Known careers URL (highest confidence when trusted).
        if known_careers_url:
            _add(known_careers_url, "known careers URL", DiscoveryMethod.USER_SUPPLIED, 0.95,
                 {"source": "user/config"})

        # 2. Homepage nav links.
        home_html = homepage_html
        if home_html is None and fetch:
            fetched = self._fetch(f"https://{domain}/")
            if fetched is not None:
                if fetched.challenge:
                    outcome.notes.append("homepage anti-bot/challenge detected")
                elif fetched.login:
                    outcome.notes.append("homepage login wall detected")
                home_html = fetched.text or None
        if home_html:
            for cand in X.score_career_links(home_html, base_url=f"https://{domain}/"):
                _add(cand.url, cand.label, DiscoveryMethod.NAV_LINK, min(0.6 + cand.score * 0.2, 0.9),
                     {"link_score": cand.score, "reason": cand.reason})

        # 3. Approved subdomains + a small fixed set of common official paths.
        for sub in ("careers", "jobs"):
            _add(f"https://{sub}.{domain}/", f"{sub} subdomain", DiscoveryMethod.SUBDOMAIN, 0.55,
                 {"probe": "subdomain"})
        for path in ("careers", "jobs", "careers/jobs"):
            _add(f"https://{domain}/{path}", f"/{path}", DiscoveryMethod.COMMON_PATH, 0.5,
                 {"probe": "common_path"})

        # 4. robots.txt -> sitemaps -> job-signalled URLs.
        robots = robots_txt
        if robots is None and fetch:
            fetched = self._fetch(f"https://{domain}/robots.txt", accept="text/plain,*/*;q=0.8")
            if fetched is not None and fetched.status == 200:
                robots = fetched.text
        if robots:
            sitemaps = X.sitemaps_from_robots(robots, base_url=f"https://{domain}/")[:MAX_SITEMAP_FETCHES]
            for sm_url in sitemaps:
                sm_text = None
                if sitemap_map is not None:
                    sm_text = sitemap_map.get(sm_url)
                elif fetch:
                    f2 = self._fetch(sm_url, accept="application/xml,text/xml,*/*;q=0.8")
                    if f2 is not None and f2.status == 200:
                        sm_text = f2.text
                if not sm_text:
                    continue
                result = X.parse_sitemap(sm_text, base_url=f"https://{domain}/")
                if result.is_index:
                    for child in X.child_sitemaps_with_job_signal(result)[:MAX_SITEMAP_FETCHES]:
                        child_text = (sitemap_map or {}).get(child)
                        if child_text is None and fetch:
                            f3 = self._fetch(child, accept="application/xml,text/xml,*/*;q=0.8")
                            child_text = f3.text if (f3 is not None and f3.status == 200) else None
                        if not child_text:
                            continue
                        child_result = X.parse_sitemap(child_text, base_url=f"https://{domain}/")
                        for u in child_result.job_like()[:MAX_ENTRY_POINTS]:
                            _add(u, "sitemap job URL", DiscoveryMethod.ROBOTS_SITEMAP, 0.6, {"via": "sitemap-index"})
                else:
                    for u in result.job_like()[:MAX_ENTRY_POINTS]:
                        _add(u, "sitemap job URL", DiscoveryMethod.ROBOTS_SITEMAP, 0.6, {"via": "sitemap"})

        # Deduplicate + bound the trusted set, strongest first.
        outcome.entry_points.sort(key=lambda e: (-e.confidence, e.url))
        outcome.entry_points = outcome.entry_points[:MAX_ENTRY_POINTS]
        outcome.status = "RESOLVED" if outcome.trusted_entry_points else "UNRESOLVED"
        if not outcome.trusted_entry_points and not outcome.notes:
            outcome.notes.append("no trusted career entry point discovered from deterministic evidence")
        return outcome

    # -- persistence --------------------------------------------------------
    def persist(self, store, outcome: DiscoveryOutcome) -> None:
        """Record every discovered entry point append-only (trusted and
        rejected), so provenance is durable and a company can accrue multiple
        current entry points across runs."""
        for ep in outcome.entry_points + outcome.rejected:
            store.record_career_entry_point(
                ep.entry_id, ep.url, company_id=ep.company_id or outcome.company_id, label=ep.label,
                discovery_method=ep.discovery_method, trusted=ep.trusted,
                trust_kind=ep.trust_kind, confidence=ep.confidence,
                validated=ep.validated, reachability=ep.reachability, evidence=ep.evidence,
            )


__all__ = [
    "DiscoveryMethod",
    "CareerEntryPoint",
    "DiscoveryOutcome",
    "CareerSourceDiscoveryService",
    "MAX_ENTRY_POINTS",
]
