"""Atlas company & source-discovery domain models (Phase 1A.5).

These model COMPANY IDENTITY and COMPANY↔SOURCE relationships — never
candidate preference. There is deliberately no company tier, candidate
score, relevance, cadence, or search policy here; those arrive with the
Workspace Atlas Agent import.

Three concepts are kept strictly distinct (see docs/COMPANY_REGISTRY.md):

    * ``RelationshipState`` — lifecycle of a company↔source relationship
      (DISCOVERED → VERIFIED → …). NOT the same as ``SourceHealthState``
      (is the source currently trustworthy?) or ``TaskStatus`` (what
      happened to a task?).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


class CompanyStatus(str, enum.Enum):
    """Identity-level status of a company record (not candidate relevance)."""

    ACTIVE = "ACTIVE"
    MERGED = "MERGED"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


class RelationshipState(str, enum.Enum):
    """Lifecycle of a company↔source relationship. Distinct from
    SourceHealthState and TaskStatus."""

    DISCOVERED = "DISCOVERED"
    VERIFIED = "VERIFIED"
    DEGRADED = "DEGRADED"
    REPLACED = "REPLACED"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


# Relationship states that are no longer the current way to reach a company.
NONCURRENT_RELATIONSHIP_STATES = frozenset(
    {RelationshipState.REPLACED, RelationshipState.INACTIVE}
)


class SourceConfidence(str, enum.Enum):
    """Categorical confidence for a discovered company↔source relationship.

    Categorical (not fake numeric precision) — see docs/SOURCE_DISCOVERY.md
    for the rationale. Numeric fingerprint scores are mapped into these
    buckets by :func:`atlas.company.identity.confidence_from_fingerprint`.
    """

    CONFIRMED = "CONFIRMED"   # explicit config / user-confirmed identity
    STRONG = "STRONG"         # host-level ATS fingerprint / validated redirect
    TENTATIVE = "TENTATIVE"   # path/marker fingerprint only
    UNKNOWN = "UNKNOWN"


class DiscoveryMethod(str, enum.Enum):
    """How a company/source relationship was discovered. Only *trusted*
    methods may register an official source (see
    :data:`TRUSTED_DISCOVERY_METHODS`); an untrusted posting-derived URL
    never registers a source on its own."""

    EXPLICIT_CONFIG = "EXPLICIT_CONFIG"
    CONFIRMED_IDENTITY = "CONFIRMED_IDENTITY"
    CANDIDATE_URL = "CANDIDATE_URL"       # a URL the user supplied
    VALIDATED_REDIRECT = "VALIDATED_REDIRECT"
    FINGERPRINT = "FINGERPRINT"           # from an already-trusted careers URL
    UNTRUSTED_POSTING = "UNTRUSTED_POSTING"  # a URL found inside posting text


# Methods whose provenance is trusted enough to register an official source.
TRUSTED_DISCOVERY_METHODS: frozenset[DiscoveryMethod] = frozenset(
    {
        DiscoveryMethod.EXPLICIT_CONFIG,
        DiscoveryMethod.CONFIRMED_IDENTITY,
        DiscoveryMethod.CANDIDATE_URL,
        DiscoveryMethod.VALIDATED_REDIRECT,
        DiscoveryMethod.FINGERPRINT,
    }
)


class VerificationState(str, enum.Enum):
    """Verification state of a single discovery observation."""

    UNVERIFIED = "UNVERIFIED"
    HEURISTIC = "HEURISTIC"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Company:
    """A company IDENTITY record (persistent, run-independent)."""

    company_id: str
    canonical_name: str
    identity_key: str
    display_name: str = ""
    official_domain: Optional[str] = None
    careers_url: Optional[str] = None
    country: Optional[str] = None
    status: CompanyStatus = CompanyStatus.ACTIVE
    discovered_at: Optional[str] = None
    last_verified_at: Optional[str] = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "canonical_name": self.canonical_name,
            "identity_key": self.identity_key,
            "display_name": self.display_name,
            "official_domain": self.official_domain,
            "careers_url": self.careers_url,
            "country": self.country,
            "status": self.status.value,
            "discovered_at": self.discovered_at,
            "last_verified_at": self.last_verified_at,
            "provenance": dict(self.provenance),
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class CompanyAlias:
    company_id: str
    alias: str
    alias_key: str
    source: str = "explicit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "alias": self.alias,
            "alias_key": self.alias_key,
            "source": self.source,
        }


@dataclass(frozen=True)
class CompanySourceRelationship:
    """A many-to-many link between a company and a configured SourceInstance.
    A company may have multiple simultaneously-current relationships (global
    ATS + India ATS + internship portal)."""

    relationship_id: str
    company_id: str
    instance_id: str
    source_type: str
    base_url: Optional[str] = None
    tenant: Optional[str] = None
    state: RelationshipState = RelationshipState.DISCOVERED
    confidence: SourceConfidence = SourceConfidence.UNKNOWN
    is_current: bool = True
    discovered_at: Optional[str] = None
    updated_at: Optional[str] = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationship_id": self.relationship_id,
            "company_id": self.company_id,
            "instance_id": self.instance_id,
            "source_type": self.source_type,
            "base_url": self.base_url,
            "tenant": self.tenant,
            "state": self.state.value,
            "confidence": self.confidence.value,
            "is_current": self.is_current,
            "discovered_at": self.discovered_at,
            "updated_at": self.updated_at,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class SourceDiscoveryObservation:
    """Immutable provenance for one discovery of a company/source
    relationship. Historical observations are never overwritten."""

    observation_id: str
    company_id: str
    method: DiscoveryMethod
    input_url: Optional[str] = None
    resolved_url: Optional[str] = None
    detected_ats: Optional[str] = None
    tenant: Optional[str] = None
    instance_id: Optional[str] = None
    confidence: SourceConfidence = SourceConfidence.UNKNOWN
    evidence_ref: Optional[str] = None
    verification_state: VerificationState = VerificationState.UNVERIFIED
    observed_at: Optional[str] = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_trusted(self) -> bool:
        return self.method in TRUSTED_DISCOVERY_METHODS

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "company_id": self.company_id,
            "method": self.method.value,
            "input_url": self.input_url,
            "resolved_url": self.resolved_url,
            "detected_ats": self.detected_ats,
            "tenant": self.tenant,
            "instance_id": self.instance_id,
            "confidence": self.confidence.value,
            "evidence_ref": self.evidence_ref,
            "verification_state": self.verification_state.value,
            "observed_at": self.observed_at,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class CompanyObservation:
    """A raw, unresolved observation of a company (input to discovery). Not
    persisted directly — it is normalized/resolved into a Company + optional
    source relationship."""

    name: str
    official_domain: Optional[str] = None
    careers_url: Optional[str] = None
    candidate_url: Optional[str] = None
    redirect_url: Optional[str] = None
    markers: tuple[str, ...] = ()
    country: Optional[str] = None
    method: DiscoveryMethod = DiscoveryMethod.CONFIRMED_IDENTITY
    reason: str = ""
    aliases: tuple[str, ...] = ()


@dataclass
class CompanyDiscoveryTask:
    """Generic queue item for future employer discovery. Priority is a
    neutral placeholder — real prioritization is a Workspace policy."""

    company_name: str
    reason: str = ""
    known_url: Optional[str] = None
    priority: int = 0  # neutral default; Workspace policy supplies real values
    attempt_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_name": self.company_name,
            "reason": self.reason,
            "known_url": self.known_url,
            "priority": self.priority,
            "attempt_count": self.attempt_count,
        }


__all__ = [
    "CompanyStatus",
    "RelationshipState",
    "NONCURRENT_RELATIONSHIP_STATES",
    "SourceConfidence",
    "DiscoveryMethod",
    "TRUSTED_DISCOVERY_METHODS",
    "VerificationState",
    "Company",
    "CompanyAlias",
    "CompanySourceRelationship",
    "SourceDiscoveryObservation",
    "CompanyObservation",
    "CompanyDiscoveryTask",
]
