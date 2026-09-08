"""Dynamic company registration from portal leads (Phase 1D §12).

For every portal lead, resolve the company against the merge-safe
:class:`atlas.company.registry.CompanyRegistry`. A genuinely new company is
registered as DYNAMICALLY_DISCOVERED with its portal provenance preserved; two
same-name companies with different domains are NEVER merged. An official domain
is resolved ONLY from trusted evidence (a portal-provided official website /
official apply URL host, or existing registry/alias evidence) — never a guess.
When identity/domain is unresolved the company is queued
COMPANY_IDENTITY_UNRESOLVED rather than pinned to a guessed domain.

The 108-company seed remains a seed, never a whitelist — a lead for an unknown
company is registered, not discarded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from atlas.company.identity import canonical_company_name, company_identity_key, normalize_domain
from atlas.company.models import CompanyObservation, DiscoveryMethod
from atlas.company.registry import CompanyRegistry
from atlas.sources.portals.models import PortalJobLead

# Portal hosts are NOT a company's official domain — a naukri.com / linkedin.com
# URL never resolves an employer's official domain.
_PORTAL_HOSTS = frozenset(
    {"linkedin.com", "naukri.com", "indeed.com", "foundit.in", "monster.com",
     "wellfound.com", "glassdoor.com", "shine.com", "timesjobs.com"}
)


def _host(url: Optional[str]) -> str:
    if not url:
        return ""
    return (urlsplit(url).netloc or "").lower().split("@")[-1].split(":")[0]


def _is_portal_host(host: str) -> bool:
    host = host.lower().lstrip(".")
    return any(host == p or host.endswith("." + p) for p in _PORTAL_HOSTS)


class DynamicResolution:
    DYNAMICALLY_DISCOVERED = "DYNAMICALLY_DISCOVERED"
    RESOLVED = "RESOLVED"
    LINKED_EXISTING = "LINKED_EXISTING"
    COMPANY_IDENTITY_UNRESOLVED = "COMPANY_IDENTITY_UNRESOLVED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass
class DynamicCompanyResult:
    lead_id: str
    company_id: Optional[str]
    resolution_status: str
    official_domain: Optional[str] = None
    normalized_name: Optional[str] = None
    reason: str = ""
    newly_registered: bool = False
    detail: dict = field(default_factory=dict)


class DynamicCompanyRegistrar:
    """Registers/links companies discovered from portal leads (merge-safe)."""

    def __init__(self, store):
        self.store = store
        self.registry = CompanyRegistry(store)

    def _official_domain_from_lead(self, lead: PortalJobLead) -> Optional[str]:
        """Resolve an official domain ONLY from trusted evidence carried by the
        lead — a non-portal official apply URL or company website host. Returns
        None (never a guess) otherwise."""
        for url in (lead.company_url, lead.official_apply_url):
            host = _host(url)
            if host and not _is_portal_host(host):
                dom = normalize_domain(host)
                if dom:
                    return dom
        return None

    def register_from_lead(self, lead: PortalJobLead, *, run_id: Optional[str] = None) -> DynamicCompanyResult:
        name = (lead.company_name or "").strip()
        normalized = canonical_company_name(name) if name else None
        if not name or not company_identity_key(name):
            result = DynamicCompanyResult(
                lead.lead_id, None, DynamicResolution.COMPANY_IDENTITY_UNRESOLVED,
                normalized_name=normalized, reason="lead has no usable company name")
            self._persist_provenance(result, lead, run_id)
            return result

        domain = self._official_domain_from_lead(lead)
        match = self.registry.resolve(name, domain)

        # Ambiguous same-name / different-domain -> NEVER merge; queue unresolved.
        if match.ambiguous:
            result = DynamicCompanyResult(
                lead.lead_id, None, DynamicResolution.AMBIGUOUS, official_domain=domain,
                normalized_name=normalized, reason=match.reason)
            self._persist_provenance(result, lead, run_id)
            return result

        if match.matched:
            company_id = match.company_id
            # Enrich an existing company's domain only if it had none.
            if domain:
                existing = self.store.get_company(company_id)
                if existing is not None and not existing["official_domain"]:
                    self.registry.set_official_domain(company_id, domain)
            status = DynamicResolution.LINKED_EXISTING
            newly = False
        else:
            obs = CompanyObservation(
                name=name, official_domain=domain, careers_url=None, country=None,
                method=DiscoveryMethod.CANDIDATE_URL if domain else DiscoveryMethod.FINGERPRINT,
                reason=f"portal lead ({lead.source_family})",
            )
            company = self.registry.register_company(obs)
            company_id = company.company_id
            status = DynamicResolution.RESOLVED if domain else DynamicResolution.DYNAMICALLY_DISCOVERED
            newly = True

        # Link the lead to its resolved company.
        self.store.set_portal_lead_company(lead.lead_id, company_id)
        result = DynamicCompanyResult(
            lead.lead_id, company_id,
            status if domain or status == DynamicResolution.LINKED_EXISTING else DynamicResolution.DYNAMICALLY_DISCOVERED,
            official_domain=domain, normalized_name=normalized,
            reason=match.reason, newly_registered=newly,
            detail={"source_family": lead.source_family})
        self._persist_provenance(result, lead, run_id)
        return result

    def _persist_provenance(self, result: DynamicCompanyResult, lead: PortalJobLead, run_id: Optional[str]) -> None:
        pid = "dynco::" + hashlib.sha1(
            f"{result.lead_id}::{result.company_id or ''}".encode()).hexdigest()[:16]
        self.store.record_dynamic_company_provenance(
            pid, f"portal:{lead.source_family}", company_id=result.company_id,
            run_id=run_id or lead.run_id, portal_lead_id=result.lead_id,
            normalized_name=result.normalized_name, resolved_domain=result.official_domain,
            resolution_status=result.resolution_status,
            evidence={"reason": result.reason, "company_url": lead.company_url,
                      "official_apply_url": lead.official_apply_url})


__all__ = ["DynamicResolution", "DynamicCompanyResult", "DynamicCompanyRegistrar"]
