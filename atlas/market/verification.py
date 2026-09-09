"""Portal-lead -> official-evidence verification (Phase 1D §13).

Links an append-only :class:`PortalJobLead` to OFFICIAL job evidence
(:table:`raw_discovery_observations` produced by a company career site / ATS) and
records a durable, evidence-bearing verification decision. Rules:

    * a matching official REQUISITION ID / official apply URL is the STRONGEST
      signal -> VERIFIED_OFFICIAL;
    * a fallback company + title + location match is PROBABILISTIC and PRESERVES
      ambiguity (multiple candidates -> MANUAL_VERIFICATION, never a silent pick);
    * the portal URL is provenance, not canonical identity;
    * an official CURRENT role page is sufficient for VERIFIED_OFFICIAL (no final
      Apply / Submit);
    * access limitation is NOT closure; closure requires positive evidence
      (an official INACTIVE/closed observation) -> CLOSED_POSITIVE_EVIDENCE;
    * no official match leaves the lead PORTAL_CURRENT_LEAD (or
      MANUAL_VERIFICATION when an ambiguous candidate exists);
    * portal and official observations remain SEPARATE evidence events — the link
      is a relationship, never a merge.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from atlas.company.identity import company_identity_key
from atlas.sources.portals.base import canonical_city
from atlas.sources.portals.models import PortalJobLead, PortalLeadVerification

_TOKEN_RE = re.compile(r"[a-z0-9#.+]+")
# Very common job-title words that carry no discriminating signal.
_STOP = frozenset({"the", "a", "an", "of", "and", "for", "to", "in", "at", "senior", "sr",
                   "junior", "jr", "lead", "staff", "principal", "engineer", "developer"})


class LinkMatchKind:
    REQ_ID = "REQ_ID"
    URL_MATCH = "URL_MATCH"
    COMPANY_TITLE_LOCATION = "COMPANY_TITLE_LOCATION"
    NONE = "NONE"


def _title_tokens(title: Optional[str]) -> set[str]:
    if not title:
        return set()
    return {t for t in _TOKEN_RE.findall(title.lower()) if t not in _STOP and len(t) > 1}


def _title_similarity(a: Optional[str], b: Optional[str]) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    return len(inter) / len(ta | tb)


@dataclass
class LinkResult:
    lead_id: str
    match_kind: str
    verification_state: PortalLeadVerification
    canonical_id: Optional[str] = None
    official_observation_id: Optional[str] = None
    confidence: float = 0.0
    ambiguous: bool = False
    reason: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def link_id(self) -> str:
        return "polink::" + hashlib.sha1(
            f"{self.lead_id}::{self.official_observation_id or 'none'}".encode()).hexdigest()[:16]


@dataclass
class _OfficialObs:
    observation_id: str
    canonical_id: Optional[str]
    source_family: str
    source_job_id: Optional[str]
    canonical_url: Optional[str]
    company: Optional[str]
    title: Optional[str]
    location: Optional[str]
    is_active: Optional[str]


_PORTAL_FAMILIES = frozenset({"linkedin", "naukri", "foundit", "indeed", "portal_generic"})


class PortalOfficialVerifier:
    """Links run-scoped portal leads to official evidence (deterministic)."""

    def __init__(self, store, *, title_threshold: float = 0.5):
        self.store = store
        self.title_threshold = title_threshold

    def _official_observations(self, run_id: str) -> list[_OfficialObs]:
        # Collapse multiple REVISIONS of the same job (a SEARCH row and its
        # hydrated DETAIL row share a canonical id) to ONE candidate, so a lead
        # is not falsely marked ambiguous by two revisions of a single official
        # job. Rows without a canonical id yet are keyed by observation id.
        by_key: dict[str, _OfficialObs] = {}
        for row in self.store.list_raw_observations(run_id):
            fam = (row["source_family"] or "").lower()
            if fam in _PORTAL_FAMILIES:
                continue  # portal observations are not official evidence
            key = row["canonical_id"] or row["observation_id"]
            obs = _OfficialObs(
                observation_id=row["observation_id"], canonical_id=row["canonical_id"],
                source_family=fam, source_job_id=row["source_job_id"], canonical_url=row["canonical_url"],
                company=row["company"], title=row["title"], location=row["location"],
                is_active=(row["is_active"] or "").upper() or None,
            )
            prev = by_key.get(key)
            if prev is None:
                by_key[key] = obs
            elif obs.source_job_id and not prev.source_job_id:
                by_key[key] = obs  # prefer the revision that carries a source job id
        return list(by_key.values())

    def _match_lead(self, lead: PortalJobLead, official: list[_OfficialObs]) -> LinkResult:
        # 1. Strongest: official apply URL / requisition id alignment.
        apply_url = (lead.official_apply_url or "").strip()
        if apply_url:
            for obs in official:
                if obs.canonical_url and obs.canonical_url.strip() == apply_url:
                    return self._verified(lead, obs, LinkMatchKind.URL_MATCH, 0.97,
                                          "official apply URL matches official observation")
                if obs.source_job_id and obs.source_job_id in apply_url:
                    return self._verified(lead, obs, LinkMatchKind.REQ_ID, 0.95,
                                          "official requisition id present in apply URL")
        # 2. Probabilistic company + title + location.
        lead_key = company_identity_key(lead.company_name or "")
        candidates: list[tuple[float, _OfficialObs]] = []
        for obs in official:
            if not obs.company or company_identity_key(obs.company) != lead_key or not lead_key:
                continue
            sim = _title_similarity(lead.title, obs.title)
            if sim < self.title_threshold:
                continue
            loc_ok = True
            lc, oc = canonical_city(lead.location), canonical_city(obs.location)
            if lc and oc:
                loc_ok = lc == oc
            if not loc_ok:
                continue
            candidates.append((sim, obs))
        if not candidates:
            return LinkResult(lead.lead_id, LinkMatchKind.NONE,
                              PortalLeadVerification.PORTAL_CURRENT_LEAD, reason="no official match")
        candidates.sort(key=lambda c: -c[0])
        best_sim, best = candidates[0]
        ambiguous = len(candidates) > 1 and (candidates[0][0] - candidates[1][0]) < 0.15
        if ambiguous:
            return LinkResult(
                lead.lead_id, LinkMatchKind.COMPANY_TITLE_LOCATION,
                PortalLeadVerification.MANUAL_VERIFICATION, canonical_id=best.canonical_id,
                official_observation_id=best.observation_id, confidence=round(best_sim * 0.7, 3),
                ambiguous=True, reason=f"{len(candidates)} probable official candidates; ambiguity preserved")
        return self._verified(lead, best, LinkMatchKind.COMPANY_TITLE_LOCATION, round(0.6 + best_sim * 0.3, 3),
                              f"company+title+location match (title_sim={best_sim:.2f})")

    def _verified(self, lead: PortalJobLead, obs: _OfficialObs, kind: str, confidence: float,
                  reason: str) -> LinkResult:
        # Positive closure evidence beats a live-verified label.
        if obs.is_active == "INACTIVE":
            return LinkResult(lead.lead_id, kind, PortalLeadVerification.CLOSED_POSITIVE_EVIDENCE,
                              canonical_id=obs.canonical_id, official_observation_id=obs.observation_id,
                              confidence=confidence, reason=f"{reason}; official observation is CLOSED")
        return LinkResult(lead.lead_id, kind, PortalLeadVerification.LINKED_OFFICIAL_VERIFIED,
                          canonical_id=obs.canonical_id, official_observation_id=obs.observation_id,
                          confidence=confidence, reason=reason)

    def link_run(self, run_id: str) -> list[LinkResult]:
        official = self._official_observations(run_id)
        results: list[LinkResult] = []
        for row in self.store.list_portal_leads(run_id):
            lead = PortalJobLead.from_row(row)
            result = self._match_lead(lead, official)
            # Persist the link + update the lead's verification state (append-only
            # link row; the lead observation itself is never deleted/merged).
            self.store.record_portal_official_link(
                result.link_id, run_id, lead.lead_id, canonical_id=result.canonical_id,
                official_observation_id=result.official_observation_id, match_kind=result.match_kind,
                confidence=result.confidence, verification_state=result.verification_state.value,
                ambiguous=result.ambiguous, evidence={"reason": result.reason})
            if result.verification_state != PortalLeadVerification.PORTAL_CURRENT_LEAD:
                self.store.set_portal_lead_verification(lead.lead_id, result.verification_state.value)
            results.append(result)
        return results


__all__ = ["LinkMatchKind", "LinkResult", "PortalOfficialVerifier"]
