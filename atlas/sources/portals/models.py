"""Portal source model — PortalJobLead vs OfficialJobObservation (Phase 1D §6).

A job portal (LinkedIn / Naukri / …) is a DISCOVERY source, never a source of
truth. Anything observed on a portal is a :class:`PortalJobLead` — a run-scoped,
append-only observation whose verification state STARTS at
``PORTAL_CURRENT_LEAD`` and can only become official through the explicit
portal-to-official verification path (see
:mod:`atlas.sources.portals.verification`). It is NEVER placed directly into an
official verified status (build spec 6 / 13).

An :class:`OfficialJobObservation` is the parallel — but SEPARATE — evidence
event produced by an OFFICIAL source (a company career site or ATS). The two are
kept as distinct evidence records so a portal lead's provenance is never
conflated with official truth; a linkage between them is an explicit, evidence-
bearing decision, not an identity merge.

Both models are pure DATA (no network I/O, no orchestration). A portal adapter
returns the standard :class:`atlas.sources.models.DiscoveryResult`; the market
layer normalizes each result into a ``PortalJobLead`` for run-scoped append-only
persistence.
"""

from __future__ import annotations

import datetime
import enum
import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.sources.models import DiscoveryResult, WorkMode


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class PortalLeadVerification(str, enum.Enum):
    """The verification state of a portal job lead. A lead begins as
    ``PORTAL_CURRENT_LEAD`` and only advances on positive OFFICIAL evidence
    (build spec 13). Access limitation is NOT closure and never terminal here."""

    PORTAL_CURRENT_LEAD = "PORTAL_CURRENT_LEAD"
    LINKED_OFFICIAL_VERIFIED = "LINKED_OFFICIAL_VERIFIED"
    MANUAL_VERIFICATION = "MANUAL_VERIFICATION"
    CLOSED_POSITIVE_EVIDENCE = "CLOSED_POSITIVE_EVIDENCE"


@dataclass(frozen=True)
class PortalJobLead:
    """One append-only, run-scoped observation of a job on a portal.

    Preserves the full provenance required by build spec 6: source family +
    instance, portal job id, canonical URL, title, company display name,
    location, posted/freshness text WITH provenance, work mode, the exact result
    page/query/cursor it was seen on, ``observed_at``, a bounded raw-evidence
    reference, health/parse findings, an official apply/company URL when the
    portal exposes one, and a verification state (initially
    ``PORTAL_CURRENT_LEAD``)."""

    run_id: str
    source_family: str
    source_instance: Optional[str] = None
    portal_job_id: Optional[str] = None
    canonical_url: Optional[str] = None
    title: Optional[str] = None
    company_name: Optional[str] = None
    company_id: Optional[str] = None
    location: Optional[str] = None
    posted_text: Optional[str] = None
    posted_provenance: Optional[str] = None
    work_mode: str = WorkMode.UNKNOWN.value
    lane: Optional[str] = None
    result_query: Optional[str] = None
    result_page: Optional[int] = None
    result_cursor: Optional[str] = None
    salary_text: Optional[str] = None
    experience_text: Optional[str] = None
    official_apply_url: Optional[str] = None
    company_url: Optional[str] = None
    verification_state: PortalLeadVerification = PortalLeadVerification.PORTAL_CURRENT_LEAD
    raw_evidence_ref: Optional[str] = None
    health_findings: tuple[str, ...] = ()
    detail: dict = field(default_factory=dict)
    campaign_id: Optional[str] = None
    wave_id: Optional[str] = None
    observed_at: str = field(default_factory=_utcnow)

    # -- identity -----------------------------------------------------------
    def has_min_identity(self) -> bool:
        """A ghost / partial card can NOT become a canonical lead. Minimum
        identity is a stable portal job id OR a canonical URL, plus a title
        (build spec 7/8)."""
        anchor = (self.portal_job_id or "").strip() or (self.canonical_url or "").strip()
        return bool(anchor) and bool((self.title or "").strip())

    @property
    def source_anchor(self) -> str:
        return (self.portal_job_id or "").strip() or (self.canonical_url or "").strip() or (self.title or "")

    @property
    def lead_id(self) -> str:
        digest = hashlib.sha1(
            f"{self.source_family}::{self.source_anchor}".encode("utf-8")
        ).hexdigest()[:16]
        return f"lead::{self.run_id}::{self.source_family}::{digest}"

    def content_hash(self) -> str:
        parts = [
            (self.company_name or "").lower().strip(),
            (self.title or "").lower().strip(),
            (self.location or "").lower().strip(),
            (self.canonical_url or "").lower().strip(),
        ]
        return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()

    # -- construction -------------------------------------------------------
    @classmethod
    def from_discovery_result(
        cls,
        result: DiscoveryResult,
        *,
        run_id: str,
        source_family: str,
        lane: Optional[str] = None,
        result_query: Optional[str] = None,
        result_page: Optional[int] = None,
        result_cursor: Optional[str] = None,
        campaign_id: Optional[str] = None,
        wave_id: Optional[str] = None,
        health_findings: tuple[str, ...] = (),
    ) -> "PortalJobLead":
        prov = dict(result.provenance or {})
        return cls(
            run_id=run_id,
            source_family=source_family,
            source_instance=result.source_instance,
            portal_job_id=result.source_job_id,
            canonical_url=result.canonical_url or result.source_url,
            title=result.title,
            company_name=result.company,
            location=result.location,
            posted_text=result.posted_at,
            posted_provenance=prov.get("date_provenance"),
            work_mode=result.work_mode.value if isinstance(result.work_mode, WorkMode) else str(result.work_mode),
            lane=lane,
            result_query=result_query,
            result_page=result_page,
            result_cursor=result_cursor,
            salary_text=result.salary_text,
            experience_text=result.experience_text,
            official_apply_url=prov.get("official_apply_url"),
            company_url=prov.get("company_url"),
            raw_evidence_ref=result.raw_observation_ref,
            health_findings=tuple(health_findings),
            detail={
                "employment_type": result.employment_type,
                "skills": list(result.skills),
                "deadline": result.deadline,
                "provenance": prov,
            },
            campaign_id=campaign_id,
            wave_id=wave_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "run_id": self.run_id,
            "source_family": self.source_family,
            "source_instance": self.source_instance,
            "portal_job_id": self.portal_job_id,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "company_name": self.company_name,
            "company_id": self.company_id,
            "location": self.location,
            "posted_text": self.posted_text,
            "posted_provenance": self.posted_provenance,
            "work_mode": self.work_mode,
            "lane": self.lane,
            "result_query": self.result_query,
            "result_page": self.result_page,
            "result_cursor": self.result_cursor,
            "salary_text": self.salary_text,
            "experience_text": self.experience_text,
            "official_apply_url": self.official_apply_url,
            "company_url": self.company_url,
            "verification_state": self.verification_state.value,
            "raw_evidence_ref": self.raw_evidence_ref,
            "health_findings": list(self.health_findings),
            "detail": dict(self.detail),
            "campaign_id": self.campaign_id,
            "wave_id": self.wave_id,
            "observed_at": self.observed_at,
        }

    def persist(self, store) -> None:
        """Append-only persistence to the run-scoped ``portal_leads`` table."""
        store.record_portal_lead(
            self.lead_id, self.run_id, self.source_family,
            campaign_id=self.campaign_id, wave_id=self.wave_id,
            source_instance=self.source_instance, portal_job_id=self.portal_job_id,
            canonical_url=self.canonical_url, title=self.title, company_name=self.company_name,
            company_id=self.company_id, location=self.location, posted_text=self.posted_text,
            posted_provenance=self.posted_provenance, work_mode=self.work_mode, lane=self.lane,
            result_query=self.result_query, result_page=self.result_page,
            result_cursor=self.result_cursor, salary_text=self.salary_text,
            experience_text=self.experience_text, official_apply_url=self.official_apply_url,
            company_url=self.company_url, verification_state=self.verification_state.value,
            raw_evidence_ref=self.raw_evidence_ref, health_findings=list(self.health_findings),
            detail=self.detail, content_hash=self.content_hash(), observed_at=self.observed_at,
        )

    @classmethod
    def from_row(cls, row) -> "PortalJobLead":
        import json as _json

        try:
            state = PortalLeadVerification(row["verification_state"])
        except (ValueError, KeyError):
            state = PortalLeadVerification.PORTAL_CURRENT_LEAD
        try:
            findings = tuple(_json.loads(row["health_findings_json"] or "[]"))
        except (ValueError, TypeError):
            findings = ()
        try:
            detail = _json.loads(row["detail_json"] or "{}")
        except (ValueError, TypeError):
            detail = {}
        return cls(
            run_id=row["run_id"], source_family=row["source_family"],
            source_instance=row["source_instance"], portal_job_id=row["portal_job_id"],
            canonical_url=row["canonical_url"], title=row["title"], company_name=row["company_name"],
            company_id=row["company_id"], location=row["location"], posted_text=row["posted_text"],
            posted_provenance=row["posted_provenance"], work_mode=row["work_mode"] or WorkMode.UNKNOWN.value,
            lane=row["lane"], result_query=row["result_query"], result_page=row["result_page"],
            result_cursor=row["result_cursor"], salary_text=row["salary_text"],
            experience_text=row["experience_text"], official_apply_url=row["official_apply_url"],
            company_url=row["company_url"], verification_state=state,
            raw_evidence_ref=row["raw_evidence_ref"], health_findings=findings, detail=detail,
            campaign_id=row["campaign_id"], wave_id=row["wave_id"], observed_at=row["observed_at"],
        )


@dataclass(frozen=True)
class OfficialJobObservation:
    """An OFFICIAL evidence event (career site / ATS). Distinct from a
    :class:`PortalJobLead` so the two evidence streams never merge identities.
    Built from a :class:`DiscoveryResult` produced by an official adapter and
    referenced by the portal-to-official verifier."""

    run_id: str
    source_family: str
    source_instance: Optional[str] = None
    official_requisition_id: Optional[str] = None
    canonical_url: Optional[str] = None
    title: Optional[str] = None
    company_name: Optional[str] = None
    company_id: Optional[str] = None
    location: Optional[str] = None
    posted_text: Optional[str] = None
    is_active: Optional[str] = None
    observation_id: Optional[str] = None
    observed_at: str = field(default_factory=_utcnow)

    @classmethod
    def from_discovery_result(
        cls, result: DiscoveryResult, *, run_id: str, company_id: Optional[str] = None,
        observation_id: Optional[str] = None,
    ) -> "OfficialJobObservation":
        return cls(
            run_id=run_id, source_family=result.source_type.value, source_instance=result.source_instance,
            official_requisition_id=result.source_job_id, canonical_url=result.canonical_url or result.source_url,
            title=result.title, company_name=result.company, company_id=company_id,
            location=result.location, posted_text=result.posted_at,
            is_active=result.is_active.value if hasattr(result.is_active, "value") else str(result.is_active),
            observation_id=observation_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "source_family": self.source_family,
            "source_instance": self.source_instance,
            "official_requisition_id": self.official_requisition_id,
            "canonical_url": self.canonical_url, "title": self.title,
            "company_name": self.company_name, "company_id": self.company_id,
            "location": self.location, "posted_text": self.posted_text,
            "is_active": self.is_active, "observation_id": self.observation_id,
            "observed_at": self.observed_at,
        }


__all__ = [
    "PortalLeadVerification",
    "PortalJobLead",
    "OfficialJobObservation",
]
