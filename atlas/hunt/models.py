"""Immutable Search Hunt data model (architecture s.3).

Frozen, serializable records for the collect -> prefilter -> hydrate -> qualify
-> coverage pipeline. Board snapshots and detail revisions are immutable: a
policy or parser change creates a derived run, never a rewrite. Graph state
carries only compact ids/counters; raw content lives in these records and in
SQLite / immutable run artifacts.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.data_integrity.normalizers import identity_token

__all__ = [
    "RawJob",
    "BoardSnapshot",
    "JobDetailRevision",
    "LanePrefilterDecision",
    "CoverageDecision",
    "SourceCoverage",
    "RunLineage",
    "canonical_job_key",
    "as_dict",
]


def as_dict(obj: Any) -> Any:
    """JSON-friendly recursive conversion for dataclasses/dates/tuples."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: as_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [as_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: as_dict(v) for k, v in obj.items()}
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    return obj


def canonical_job_key(company: str, title: str, location: str = "", requisition_id: str = "", source_job_id: str = "") -> str:
    """Canonical identity: the unique ATS job id when present (build spec 12, V1
    New Text Document). ``requisition_id`` is often a placeholder ("See
    openings") that collapses distinct postings, so it is used only when it
    looks unique; otherwise fall back to the ATS job id, then to normalized
    company + title + location."""
    if source_job_id and str(source_job_id).strip():
        return f"job:{identity_token(company)}:{identity_token(source_job_id)}"
    req = (requisition_id or "").strip()
    if req and _looks_unique_req(req):
        return f"req:{identity_token(company)}:{identity_token(req)}"
    return "ctl:" + ":".join(identity_token(x) for x in (company, title, location))


# Requisition placeholders seen on public boards that must NOT be used as an
# identity (they are shared across every posting).
_PLACEHOLDER_REQ = frozenset(
    {"see opening", "see openings", "see open", "n/a", "na", "none", "tbd", "various", ""}
)


def _looks_unique_req(req: str) -> bool:
    tok = identity_token(req)
    if tok in _PLACEHOLDER_REQ:
        return False
    # a usable requisition id contains at least one digit or is reasonably long
    return any(c.isdigit() for c in tok) or len(tok) >= 6


@dataclass(frozen=True)
class RawJob:
    """One posting as seen on a board listing (pre-hydration)."""

    source_job_id: str
    title: str
    location: str = ""
    url: str = ""
    summary: str = ""
    posted_date: Optional[datetime.date] = None
    requisition_id: str = ""


@dataclass(frozen=True)
class BoardSnapshot:
    """Immutable board fetch. One snapshot per (campaign, company, source) task;
    it feeds all six local lane prefilters without new network calls."""

    snapshot_id: str
    campaign_id: str
    company_id: str
    company_name: str
    source_instance_id: str
    source_family: str
    route_family: str  # OFFICIAL_ATS | OFFICIAL_CAREERS | GENERIC_HTTP | GENERIC_BROWSER
    source_url: str = ""
    fetch_started_at: str = ""
    fetch_completed_at: str = ""
    pages: int = 1
    adapter_version: str = ""
    parser_version: str = ""
    source_health: str = "OK"
    raw_jobs: tuple[RawJob, ...] = ()
    snapshot_status: str = "COMPLETE"  # COMPLETE | PARTIAL | FAILED | ACCESS_BLOCKED
    parent_snapshot_id: Optional[str] = None
    access_status: str = "OK"

    @property
    def raw_job_count(self) -> int:
        return len(self.raw_jobs)

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        for j in self.raw_jobs:
            h.update((j.source_job_id + "|" + j.title + "|" + j.location).encode("utf-8"))
        return h.hexdigest()


@dataclass(frozen=True)
class JobDetailRevision:
    """Immutable hydrated job evidence, qualified with zero further network on a
    policy-only rerun."""

    revision_id: str
    snapshot_id: str
    source_job_id: str
    company: str
    title: str
    description: str = ""
    mandatory_requirements: tuple[str, ...] = ()
    preferred_requirements: tuple[str, ...] = ()
    experience_text: str = ""
    location: str = ""
    work_mode: str = "UNKNOWN"
    posted_date: Optional[datetime.date] = None
    deadline: Optional[datetime.date] = None
    requisition_id: str = ""
    official_url: str = ""
    verification_state: str = "VERIFIED_OFFICIAL"
    has_live_official_page: bool = True
    evidence_texts: tuple[str, ...] = ()
    eligibility_text: str = ""
    source_family: str = ""
    parser_version: str = ""
    retrieved_at: str = ""
    record_class: str = "OFFICIAL_DIRECT"
    discovery_channels: tuple[str, ...] = ()

    @property
    def canonical_key(self) -> str:
        return canonical_job_key(self.company, self.title, self.location, self.requisition_id, self.source_job_id)


@dataclass(frozen=True)
class LanePrefilterDecision:
    snapshot_job_id: str
    lane: str
    candidate_for_hydration: bool
    signals: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    policy_hash: str = ""


@dataclass
class CoverageDecision:
    """One row per company x lane obligation (workbook Company_Coverage)."""

    campaign_id: str
    company: str
    tier: str
    group: str
    official_domain: str
    source: str
    route: str
    check_type: str          # DELTA | DEEP
    lane: str
    snapshot_id: str = ""
    pages: int = 0
    raw_jobs: int = 0
    prefiltered_jobs: int = 0
    hydrated_jobs: int = 0
    qualified_jobs: int = 0
    wrong_stack_rejected: int = 0
    role_family_rejected: int = 0
    experience_rejected: int = 0
    location_rejected: int = 0
    freshness_rejected: int = 0
    manual_verification: int = 0
    access_status: str = "OK"
    terminal_status: str = "PENDING"   # CHECKED | ZERO | ACCESS_LIMITED | ERROR | PENDING
    checked_at: str = ""
    next_check: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.terminal_status in ("CHECKED", "ZERO", "ACCESS_LIMITED", "ERROR")


@dataclass
class SourceCoverage:
    """One row per attempted source instance / query family (Source_Coverage)."""

    source_instance_id: str
    source_family: str
    route: str
    health: str = "OK"
    pages: int = 0
    request_count: int = 0
    raw_jobs: int = 0
    qualified_jobs: int = 0
    limitation: str = ""
    retry_state: str = "NONE"
    adapter_version: str = ""
    parser_version: str = ""
    companies: int = 0


@dataclass(frozen=True)
class RunLineage:
    run_id: str
    run_kind: str            # COLLECTION | QUALIFICATION | REPORT | RESUME
    parent_run_id: Optional[str] = None
    supersedes_run_id: Optional[str] = None
    collection_run_id: Optional[str] = None
    reused_snapshot_ids: tuple[str, ...] = ()
    code_commit: str = ""
    role_policy_hash: str = ""
    experience_policy_hash: str = ""
    company_plan_hash: str = ""
    candidate_profile_hash: str = ""
    created_at: str = ""
    completed_at: str = ""
    terminal_status: str = "IN_PROGRESS"
    retry_reason: str = ""
