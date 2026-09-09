"""Typed schemas for the LLM-directed India official-career pilot (architecture s.5).

These are the non-bypassable data contracts between the LLM company-search agent, the
constrained Atlas tools, the deterministic validators, and the report writer. The LLM can
only ever *propose* values here; deterministic Python validates them from tool evidence.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class CompanyStatus(str, enum.Enum):
    COMPLETE = "COMPLETE"
    TRUSTED_ZERO = "TRUSTED_ZERO"
    COMPLETE_NO_MATCHES = "COMPLETE_NO_MATCHES"
    ACCESS_LIMITED = "ACCESS_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    OFFICIAL_SOURCE_UNRESOLVED = "OFFICIAL_SOURCE_UNRESOLVED"
    UNSUPPORTED_SITE = "UNSUPPORTED_SITE"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    FAILED = "FAILED"


#: Statuses that count as a truthful, terminal "the site was actually searched" outcome.
SEARCHED_TERMINAL = frozenset(
    {CompanyStatus.COMPLETE.value, CompanyStatus.TRUSTED_ZERO.value, CompanyStatus.COMPLETE_NO_MATCHES.value}
)
#: All terminal statuses (a company is done, truthfully, one way or another).
TERMINAL_STATUSES = frozenset(s.value for s in CompanyStatus)

PRIMARY_LANES = (
    "JAVA_BACKEND",
    "JAVA_FULLSTACK",
    "REACT_FRONTEND",
    "DOTNET",
    "ENTERPRISE_HR_PAYROLL_INTEGRATION",
)


@dataclass
class LaneCoverage:
    lane: str
    attempted: bool = False
    queries: list[str] = field(default_factory=list)
    pages: int = 0
    candidates: int = 0
    board_snapshot_evaluated: bool = False  # a list-only board fully evaluated for this lane

    def to_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "queries": list(self.queries),
            "pages": self.pages,
            "candidates": self.candidates,
            "board_snapshot_evaluated": self.board_snapshot_evaluated,
        }


@dataclass
class JobCard:
    """A structured job listing surfaced by the search tools (pre-detail)."""

    title: str
    location: str = ""
    url: str = ""
    lane_hint: str = ""
    requisition_id: str = ""
    posted_date: str = ""
    updated_date: str = ""
    snippet: str = ""
    source_family: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class JobDetailEvidence:
    """Bounded, structured evidence extracted from an official job detail page."""

    title: str
    company: str
    location: str = ""
    work_mode: str = ""
    description: str = ""
    mandatory_requirements: tuple[str, ...] = ()
    preferred_requirements: tuple[str, ...] = ()
    experience_text: str = ""
    posted_date: str = ""
    updated_date: str = ""
    requisition_id: str = ""
    official_url: str = ""
    eligibility_text: str = ""
    source_family: str = ""
    evidence_snippets: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k in ("mandatory_requirements", "preferred_requirements", "evidence_snippets"):
            d[k] = list(d[k])
        return d


@dataclass
class JobRejection:
    title: str
    lane: str
    reason_code: str
    detail: str = ""
    location: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class CompanySearchResult:
    """The typed result one company agent returns (architecture s.5 schema)."""

    company: str
    official_domain: str = ""
    career_entry_url: str = ""
    route: str = ""
    source_family: str = ""
    status: str = CompanyStatus.FAILED.value
    lanes: dict[str, LaneCoverage] = field(default_factory=dict)
    jobs: list[JobDetailEvidence] = field(default_factory=list)
    rejections: list[JobRejection] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    evidence_urls: list[str] = field(default_factory=list)
    # audit / usage
    model: str = ""
    task_id: str = ""
    tool_calls: int = 0
    queries_attempted: list[str] = field(default_factory=list)
    pages_or_interactions: int = 0
    escalated: bool = False
    retries: int = 0

    def lane_checklist_complete(self, required_lanes: tuple[str, ...]) -> bool:
        for lane in required_lanes:
            cov = self.lanes.get(lane)
            if cov is None:
                return False
            if not (cov.attempted or cov.board_snapshot_evaluated):
                return False
        return True

    def to_dict(self) -> dict:
        return {
            "company": self.company,
            "official_domain": self.official_domain,
            "career_entry_url": self.career_entry_url,
            "route": self.route,
            "source_family": self.source_family,
            "status": self.status,
            "lanes": {k: v.to_dict() for k, v in self.lanes.items()},
            "jobs": [j.to_dict() for j in self.jobs],
            "rejections": [r.to_dict() for r in self.rejections],
            "limitations": list(self.limitations),
            "evidence_urls": list(self.evidence_urls),
            "model": self.model,
            "task_id": self.task_id,
            "tool_calls": self.tool_calls,
            "queries_attempted": list(self.queries_attempted),
            "pages_or_interactions": self.pages_or_interactions,
            "escalated": self.escalated,
            "retries": self.retries,
        }


__all__ = [
    "CompanyStatus",
    "SEARCHED_TERMINAL",
    "TERMINAL_STATUSES",
    "PRIMARY_LANES",
    "LaneCoverage",
    "JobCard",
    "JobDetailEvidence",
    "JobRejection",
    "CompanySearchResult",
]
