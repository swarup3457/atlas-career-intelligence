"""Atlas production discovery-layer domain models (Phase 1A).

Typed, framework-neutral models for the generic source/discovery engine.
These are deliberately GENERIC — they carry no candidate profile, no final
search keywords, no company universe, and no ranking weights. Those arrive
only with the Workspace Atlas Agent import (see docs/SOURCE_ARCHITECTURE.md).

Design rules encoded here:
    * ``source_type`` (a shared family such as ATS_WORKDAY) is separate
      from a ``SourceInstance`` (a configured deployment such as one
      company's Workday tenant). One adapter class serves a whole family;
      instances are data. See docs/SOURCE_ADAPTER_CONTRACT.md.
    * Unknown values are represented explicitly (``None`` / ``UNKNOWN`` /
      ``ActiveState.UNKNOWN``) and NEVER invented.
    * ``DiscoveryResult`` is the single normalized shape every adapter
      returns; a raw ``list[dict]`` is never the production contract.
    * Only lightweight dataclasses/enums are used — no heavy validation
      framework — matching the rest of the Atlas codebase.
"""

from __future__ import annotations

import datetime
import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class SourceType(str, enum.Enum):
    """The *family* a source belongs to. One adapter class per family; a
    concrete company/tenant is a :class:`SourceInstance`, never a new
    ``SourceType``."""

    COMPANY_CAREER = "COMPANY_CAREER"

    # ATS families (shared behavior; company/tenant supplied by instance).
    ATS_WORKDAY = "ATS_WORKDAY"
    ATS_GREENHOUSE = "ATS_GREENHOUSE"
    ATS_LEVER = "ATS_LEVER"
    ATS_ASHBY = "ATS_ASHBY"
    ATS_SMARTRECRUITERS = "ATS_SMARTRECRUITERS"
    ATS_ORACLE = "ATS_ORACLE"
    ATS_SUCCESSFACTORS = "ATS_SUCCESSFACTORS"
    ATS_ICIMS = "ATS_ICIMS"
    ATS_PHENOM = "ATS_PHENOM"
    ATS_EIGHTFOLD = "ATS_EIGHTFOLD"
    ATS_CUSTOM = "ATS_CUSTOM"

    # Portals / aggregators / fallbacks.
    PORTAL_LARGE = "PORTAL_LARGE"
    PORTAL_SPECIALIST = "PORTAL_SPECIALIST"
    AGGREGATOR_API = "AGGREGATOR_API"
    SEARCH_FALLBACK = "SEARCH_FALLBACK"
    MANUAL_VERIFICATION = "MANUAL_VERIFICATION"

    # Test doubles (never a real network source).
    FAKE = "FAKE"
    FIXTURE = "FIXTURE"


ATS_SOURCE_TYPES: frozenset[SourceType] = frozenset(
    t for t in SourceType if t.value.startswith("ATS_")
)


# ---------------------------------------------------------------------------
# Source taxonomy correction (Phase 1B, build spec 7.1)
#
# The legacy ``SourceType`` conflated a broad *category* (e.g. PORTAL_LARGE)
# with a concrete *adapter family* — so LinkedIn and Naukri could not both be
# registered under PORTAL_LARGE. Phase 1B separates THREE concepts:
#
#   * SourceCategory : the broad role a source plays (OFFICIAL/ATS/PORTAL/...)
#   * SourceFamily   : the adapter key (workday, linkedin, naukri, ...). One
#                      adapter class per family; MANY families may share a
#                      category. Registry identity is the family, never the
#                      category.
#   * SourceInstance : a configured tenant/deployment/market (data, below).
#
# ``SourceType`` is retained as a backward-compatible alias and every value
# maps deterministically to a (category, family) pair.
# ---------------------------------------------------------------------------
class SourceCategory(str, enum.Enum):
    OFFICIAL = "OFFICIAL"
    ATS = "ATS"
    PORTAL = "PORTAL"
    SPECIALIST = "SPECIALIST"
    AGGREGATOR = "AGGREGATOR"
    FALLBACK = "FALLBACK"
    MANUAL = "MANUAL"
    TEST = "TEST"


class SourceFamily(str, enum.Enum):
    """Adapter key. One adapter *class* serves one family; a family may have
    many configured :class:`SourceInstance` objects."""

    # Official / company-owned
    COMPANY_CAREER = "company_career"
    COMPANY_CAREER_BROWSER = "company_career_browser"
    # ATS families
    WORKDAY = "workday"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    ORACLE = "oracle"
    SUCCESSFACTORS = "successfactors"
    ICIMS = "icims"
    PHENOM = "phenom"
    EIGHTFOLD = "eightfold"
    CUSTOM_ATS = "custom_ats"
    # Large portals
    LINKEDIN = "linkedin"
    NAUKRI = "naukri"
    FOUNDIT = "foundit"
    INDEED = "indeed"
    # Specialist / rotating sources
    WELLFOUND = "wellfound"
    TALENT500 = "talent500"
    INSTAHYRE = "instahyre"
    CUTSHORT = "cutshort"
    HIRIST = "hirist"
    WEEKDAY = "weekday"
    TOPHIRE = "tophire"
    YC = "yc"
    BUILTIN = "builtin"
    REMOTEOK = "remoteok"
    WEWORKREMOTELY = "weworkremotely"
    HIMALAYAS = "himalayas"
    JOBGETHER = "jobgether"
    UNSTOP = "unstop"
    FRESHERSWORLD = "freshersworld"
    SUPERSET = "superset"
    INTERNSHALA = "internshala"
    # Generic buckets (used only when an instance does not name a family)
    PORTAL_GENERIC = "portal_generic"
    SPECIALIST_GENERIC = "specialist_generic"
    AGGREGATOR = "aggregator"
    SEARCH_FALLBACK = "search_fallback"
    MANUAL = "manual"
    # Test doubles
    FAKE = "fake"
    FIXTURE = "fixture"


# Deterministic legacy alias: every SourceType resolves to one default family.
SOURCE_TYPE_TO_FAMILY: dict[SourceType, SourceFamily] = {
    SourceType.COMPANY_CAREER: SourceFamily.COMPANY_CAREER,
    SourceType.ATS_WORKDAY: SourceFamily.WORKDAY,
    SourceType.ATS_GREENHOUSE: SourceFamily.GREENHOUSE,
    SourceType.ATS_LEVER: SourceFamily.LEVER,
    SourceType.ATS_ASHBY: SourceFamily.ASHBY,
    SourceType.ATS_SMARTRECRUITERS: SourceFamily.SMARTRECRUITERS,
    SourceType.ATS_ORACLE: SourceFamily.ORACLE,
    SourceType.ATS_SUCCESSFACTORS: SourceFamily.SUCCESSFACTORS,
    SourceType.ATS_ICIMS: SourceFamily.ICIMS,
    SourceType.ATS_PHENOM: SourceFamily.PHENOM,
    SourceType.ATS_EIGHTFOLD: SourceFamily.EIGHTFOLD,
    SourceType.ATS_CUSTOM: SourceFamily.CUSTOM_ATS,
    SourceType.PORTAL_LARGE: SourceFamily.PORTAL_GENERIC,
    SourceType.PORTAL_SPECIALIST: SourceFamily.SPECIALIST_GENERIC,
    SourceType.AGGREGATOR_API: SourceFamily.AGGREGATOR,
    SourceType.SEARCH_FALLBACK: SourceFamily.SEARCH_FALLBACK,
    SourceType.MANUAL_VERIFICATION: SourceFamily.MANUAL,
    SourceType.FAKE: SourceFamily.FAKE,
    SourceType.FIXTURE: SourceFamily.FIXTURE,
}

SOURCE_FAMILY_TO_CATEGORY: dict[SourceFamily, SourceCategory] = {
    SourceFamily.COMPANY_CAREER: SourceCategory.OFFICIAL,
    SourceFamily.COMPANY_CAREER_BROWSER: SourceCategory.OFFICIAL,
    SourceFamily.WORKDAY: SourceCategory.ATS,
    SourceFamily.GREENHOUSE: SourceCategory.ATS,
    SourceFamily.LEVER: SourceCategory.ATS,
    SourceFamily.ASHBY: SourceCategory.ATS,
    SourceFamily.SMARTRECRUITERS: SourceCategory.ATS,
    SourceFamily.ORACLE: SourceCategory.ATS,
    SourceFamily.SUCCESSFACTORS: SourceCategory.ATS,
    SourceFamily.ICIMS: SourceCategory.ATS,
    SourceFamily.PHENOM: SourceCategory.ATS,
    SourceFamily.EIGHTFOLD: SourceCategory.ATS,
    SourceFamily.CUSTOM_ATS: SourceCategory.ATS,
    SourceFamily.LINKEDIN: SourceCategory.PORTAL,
    SourceFamily.NAUKRI: SourceCategory.PORTAL,
    SourceFamily.FOUNDIT: SourceCategory.PORTAL,
    SourceFamily.INDEED: SourceCategory.PORTAL,
    SourceFamily.PORTAL_GENERIC: SourceCategory.PORTAL,
    SourceFamily.WELLFOUND: SourceCategory.SPECIALIST,
    SourceFamily.TALENT500: SourceCategory.SPECIALIST,
    SourceFamily.INSTAHYRE: SourceCategory.SPECIALIST,
    SourceFamily.CUTSHORT: SourceCategory.SPECIALIST,
    SourceFamily.HIRIST: SourceCategory.SPECIALIST,
    SourceFamily.WEEKDAY: SourceCategory.SPECIALIST,
    SourceFamily.TOPHIRE: SourceCategory.SPECIALIST,
    SourceFamily.YC: SourceCategory.SPECIALIST,
    SourceFamily.BUILTIN: SourceCategory.SPECIALIST,
    SourceFamily.REMOTEOK: SourceCategory.SPECIALIST,
    SourceFamily.WEWORKREMOTELY: SourceCategory.SPECIALIST,
    SourceFamily.HIMALAYAS: SourceCategory.SPECIALIST,
    SourceFamily.JOBGETHER: SourceCategory.SPECIALIST,
    SourceFamily.UNSTOP: SourceCategory.SPECIALIST,
    SourceFamily.FRESHERSWORLD: SourceCategory.SPECIALIST,
    SourceFamily.SUPERSET: SourceCategory.SPECIALIST,
    SourceFamily.INTERNSHALA: SourceCategory.SPECIALIST,
    SourceFamily.SPECIALIST_GENERIC: SourceCategory.SPECIALIST,
    SourceFamily.AGGREGATOR: SourceCategory.AGGREGATOR,
    SourceFamily.SEARCH_FALLBACK: SourceCategory.FALLBACK,
    SourceFamily.MANUAL: SourceCategory.MANUAL,
    SourceFamily.FAKE: SourceCategory.TEST,
    SourceFamily.FIXTURE: SourceCategory.TEST,
}


def family_for_source_type(source_type: SourceType) -> SourceFamily:
    """Backward-compatible default family for a legacy ``SourceType``."""
    return SOURCE_TYPE_TO_FAMILY[source_type]


def category_for_family(family: SourceFamily) -> SourceCategory:
    return SOURCE_FAMILY_TO_CATEGORY[family]


def category_for_source_type(source_type: SourceType) -> SourceCategory:
    return category_for_family(family_for_source_type(source_type))


class WorkMode(str, enum.Enum):
    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ONSITE = "ONSITE"
    UNKNOWN = "UNKNOWN"


class ActiveState(str, enum.Enum):
    """Whether a posting is observed to be live. ``UNKNOWN`` is a genuine,
    common third state — the absence of a "closed" banner is NOT proof a
    job is open, so adapters must not coerce uncertainty to ACTIVE."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


class SourceEvidenceLevel(str, enum.Enum):
    """Strength of evidence that a discovered posting is real/live, as observed
    by a SOURCE ADAPTER. This is the generic discovery-engine ladder and is
    DISTINCT from the business :class:`atlas.policy.status.VerificationLevel`
    (the candidate-facing verification decision). It was renamed from
    ``VerificationLevel`` in Phase 1B.1 (build spec 19 / P1-8) to remove that
    ambiguity; ``VerificationLevel`` remains a backward-compatible alias."""

    OFFICIAL_DETAIL_LIVE = "OFFICIAL_DETAIL_LIVE"
    OFFICIAL_SEARCH_LIVE = "OFFICIAL_SEARCH_LIVE"
    PORTAL_LIVE = "PORTAL_LIVE"
    INDEXED_ONLY = "INDEXED_ONLY"
    REDIRECTED = "REDIRECTED"
    NOT_FOUND = "NOT_FOUND"
    CLOSED_BANNER = "CLOSED_BANNER"
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"
    UNKNOWN = "UNKNOWN"


# Backward-compatible alias. Prefer ``SourceEvidenceLevel`` in new code; this
# name is retained so existing adapters/tests keep working after the rename.
VerificationLevel = SourceEvidenceLevel


class Capability(str, enum.Enum):
    """A declared capability of an adapter. Planning/orchestration branches
    on capabilities, NEVER on hard-coded source names."""

    DISCOVER = "DISCOVER"
    SEARCH = "SEARCH"
    DETAIL = "DETAIL"
    PAGINATION = "PAGINATION"
    RECENCY_FILTER = "RECENCY_FILTER"
    LOCATION_FILTER = "LOCATION_FILTER"
    KEYWORD_FILTER = "KEYWORD_FILTER"
    COMPANY_FILTER = "COMPANY_FILTER"
    ACTIVE_STATUS = "ACTIVE_STATUS"
    POSTED_DATE = "POSTED_DATE"
    DEADLINE = "DEADLINE"
    DESCRIPTION = "DESCRIPTION"
    AUTHENTICATED = "AUTHENTICATED"
    BROWSER_REQUIRED = "BROWSER_REQUIRED"
    API_AVAILABLE = "API_AVAILABLE"


class ConcurrencyClass(str, enum.Enum):
    """Scheduling class the run/scheduler uses to bound parallelism. The
    authenticated browser profile can only ever be driven serially."""

    HTTP = "HTTP"
    BROWSER_ANONYMOUS = "BROWSER_ANONYMOUS"
    BROWSER_AUTHENTICATED = "BROWSER_AUTHENTICATED"
    HUMAN_INTERVENTION = "HUMAN_INTERVENTION"


# ---------------------------------------------------------------------------
# Source instance (a configured deployment of a source family)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceInstance:
    """A single configured deployment of a :class:`SourceType` family.

    Example: ``SourceInstance(instance_id="acme-workday",
    source_type=SourceType.ATS_WORKDAY, tenant="acme")``. Credentials are
    NEVER stored here — ``auth_ref`` is a *reference* (e.g. an env-var
    name) resolved elsewhere, never a secret value.
    """

    instance_id: str
    source_type: SourceType
    display_name: str = ""
    base_url: Optional[str] = None
    tenant: Optional[str] = None
    site: Optional[str] = None
    company_id: Optional[str] = None
    enabled: bool = True
    source_family: Optional[SourceFamily] = None
    capability_overrides: frozenset[Capability] = field(default_factory=frozenset)
    capability_removals: frozenset[Capability] = field(default_factory=frozenset)
    rate_policy: Optional[str] = None
    auth_ref: Optional[str] = None
    lifecycle_state: str = "ACTIVE"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def adapter_key(self) -> SourceFamily:
        """Registry identity: the explicit ``source_family`` when configured,
        otherwise the deterministic default family for the ``source_type``."""
        return self.source_family or family_for_source_type(self.source_type)

    @property
    def category(self) -> SourceCategory:
        return category_for_family(self.adapter_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "source_type": self.source_type.value,
            "source_family": self.adapter_key.value,
            "category": self.category.value,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "tenant": self.tenant,
            "site": self.site,
            "company_id": self.company_id,
            "enabled": self.enabled,
            "lifecycle_state": self.lifecycle_state,
            "capability_overrides": sorted(c.value for c in self.capability_overrides),
            "capability_removals": sorted(c.value for c in self.capability_removals),
            "rate_policy": self.rate_policy,
            "auth_ref": self.auth_ref,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Canonical normalized discovery result
# ---------------------------------------------------------------------------
# Fields that describe *this observation* (when/where/how seen) rather than
# the substantive content of the posting. Excluded from the content hash so
# that re-observing an unchanged posting does not look like a content change.
_OBSERVATIONAL_FIELDS: frozenset[str] = frozenset(
    {
        "discovered_at",
        "updated_at",
        "confidence",
        "provenance",
        "raw_observation_ref",
        "verification_level",
        "is_active",
        "adapter_version",
        "parser_version",
    }
)


@dataclass(frozen=True)
class DiscoveryResult:
    """The single normalized shape every adapter returns.

    Missing values stay ``None`` / ``UNKNOWN`` and are never invented.
    ``description`` is populated only when the result has been hydrated by
    ``fetch_detail`` (see the two-stage extraction design). Raw evidence is
    stored separately and referenced by ``raw_observation_ref`` — this
    object never carries whole HTML pages.
    """

    source_type: SourceType
    source_instance: str
    source_job_id: Optional[str] = None
    source_url: Optional[str] = None
    canonical_url: Optional[str] = None
    company: Optional[str] = None
    title: Optional[str] = None
    location: Optional[str] = None
    work_mode: WorkMode = WorkMode.UNKNOWN
    posted_at: Optional[str] = None
    updated_at: Optional[str] = None
    deadline: Optional[str] = None
    employment_type: Optional[str] = None
    experience_text: Optional[str] = None
    salary_text: Optional[str] = None
    skills: tuple[str, ...] = ()
    description: Optional[str] = None
    is_active: ActiveState = ActiveState.UNKNOWN
    verification_level: VerificationLevel = VerificationLevel.UNKNOWN
    discovered_at: str = field(default_factory=_utcnow_iso)
    adapter_version: str = ""
    parser_version: str = ""
    confidence: Optional[float] = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    raw_observation_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError(f"confidence must be within [0, 1]; got {self.confidence!r}")
        if not self.source_instance:
            raise ValueError("DiscoveryResult.source_instance must be a non-empty instance id")
        if not isinstance(self.source_type, SourceType):
            raise ValueError("DiscoveryResult.source_type must be a SourceType")
        # Normalize skills to a tuple even if a list slipped through.
        if not isinstance(self.skills, tuple):
            object.__setattr__(self, "skills", tuple(self.skills))

    # -- Identity / hashing -------------------------------------------------
    def content_hash(self) -> str:
        """Stable hash of the *substantive* content (observational fields
        excluded). Used for idempotent canonical upserts: re-seeing the
        same posting yields the same hash and touches nothing."""
        stable: dict[str, Any] = {}
        for name, value in self.to_dict().items():
            if name in _OBSERVATIONAL_FIELDS:
                continue
            stable[name] = value
        blob = json.dumps(stable, sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def source_identity(self) -> Optional[str]:
        """Deterministic per-source identity key (instance + source job id
        or URL). ``None`` when the adapter could not identify the posting —
        which the caller must treat as unidentifiable, never as a match."""
        anchor = self.source_job_id or self.canonical_url or self.source_url
        if not anchor:
            return None
        return f"{self.source_type.value}::{self.source_instance}::{anchor}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "source_instance": self.source_instance,
            "source_job_id": self.source_job_id,
            "source_url": self.source_url,
            "canonical_url": self.canonical_url,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "work_mode": self.work_mode.value,
            "posted_at": self.posted_at,
            "updated_at": self.updated_at,
            "deadline": self.deadline,
            "employment_type": self.employment_type,
            "experience_text": self.experience_text,
            "salary_text": self.salary_text,
            "skills": list(self.skills),
            "description": self.description,
            "is_active": self.is_active.value,
            "verification_level": self.verification_level.value,
            "discovered_at": self.discovered_at,
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "confidence": self.confidence,
            "provenance": dict(self.provenance),
            "raw_observation_ref": self.raw_observation_ref,
        }


# ---------------------------------------------------------------------------
# Typed requests / results for the adapter operations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiscoverRequest:
    """Ask an adapter which search entry points exist for a company."""

    company: str
    hints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveredEntryPoint:
    url: str
    label: str = ""
    source_type: Optional[SourceType] = None
    tenant: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "label": self.label,
            "source_type": self.source_type.value if self.source_type else None,
            "tenant": self.tenant,
        }


@dataclass(frozen=True)
class DiscoverResult:
    entry_points: tuple[DiscoveredEntryPoint, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"entry_points": [e.to_dict() for e in self.entry_points]}


@dataclass(frozen=True)
class SearchRequest:
    """A single search task's query. Supplied by a SearchStrategy (which is
    itself candidate-agnostic in Phase 1A) and compiled to whatever the
    adapter's capabilities support. Adapters must never bake constants
    from one candidate into their own code."""

    query: Optional[str] = None
    location: Optional[str] = None
    work_mode: Optional[WorkMode] = None
    recency_days: Optional[int] = None
    company: Optional[str] = None
    page: int = 1
    cursor: Optional[str] = None
    limit: int = 25
    experience_hint: Optional[str] = None
    extra_filters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("SearchRequest.page must be >= 1")
        if self.limit < 1:
            raise ValueError("SearchRequest.limit must be >= 1")
        if self.recency_days is not None and self.recency_days < 0:
            raise ValueError("SearchRequest.recency_days must be >= 0 when provided")


class ZeroResultKind(str, enum.Enum):
    """Why a search returned no results — a reachable source returning zero
    is NOT automatically proof that no jobs exist."""

    NOT_APPLICABLE = "NOT_APPLICABLE"  # results were returned
    TRUSTED_ZERO = "TRUSTED_ZERO"  # genuinely no matches, trusted
    UNTRUSTED_ZERO = "UNTRUSTED_ZERO"  # zero but suspicious (needs sentinel)
    EXTRACTION_UNRESOLVED = "EXTRACTION_UNRESOLVED"  # could not tell


@dataclass(frozen=True)
class SearchResult:
    """The typed result of one search attempt."""

    results: tuple[DiscoveryResult, ...] = ()
    page: int = 1
    has_more: bool = False
    next_cursor: Optional[str] = None
    total_reported: Optional[int] = None
    zero_result_kind: ZeroResultKind = ZeroResultKind.NOT_APPLICABLE
    parse_findings: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "page": self.page,
            "has_more": self.has_more,
            "next_cursor": self.next_cursor,
            "total_reported": self.total_reported,
            "zero_result_kind": self.zero_result_kind.value,
            "parse_findings": list(self.parse_findings),
            "results": [r.to_dict() for r in self.results],
        }


@dataclass(frozen=True)
class DetailRequest:
    """Hydrate one posting to full detail. Exactly one of the anchors must
    be provided (the anchor the adapter itself emitted)."""

    source_job_id: Optional[str] = None
    url: Optional[str] = None

    def __post_init__(self) -> None:
        if not (self.source_job_id or self.url):
            raise ValueError("DetailRequest requires source_job_id or url")


__all__ = [
    "SourceType",
    "ATS_SOURCE_TYPES",
    "SourceCategory",
    "SourceFamily",
    "SOURCE_TYPE_TO_FAMILY",
    "SOURCE_FAMILY_TO_CATEGORY",
    "family_for_source_type",
    "category_for_family",
    "category_for_source_type",
    "WorkMode",
    "ActiveState",
    "SourceEvidenceLevel",
    "VerificationLevel",
    "Capability",
    "ConcurrencyClass",
    "SourceInstance",
    "DiscoveryResult",
    "DiscoverRequest",
    "DiscoveredEntryPoint",
    "DiscoverResult",
    "SearchRequest",
    "SearchResult",
    "ZeroResultKind",
    "DetailRequest",
]
