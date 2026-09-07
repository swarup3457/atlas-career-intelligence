"""Company career-source discovery pipeline (Phase 1A.5).

Deterministic, offline flow:

    career URL / redirect / safe markers
        → ATS fingerprint (Phase 1A `fingerprint_ats`, unchanged)
        → tenant extraction
        → SourceDiscoveryObservation (provenance)
        → SourceInstance (factory)
        → company↔source relationship

Domain safety: only *trusted* discovery methods may register an official
source. A URL found inside job-posting text (``UNTRUSTED_POSTING``) never
registers a company source or domain — it is recorded as a REJECTED
observation instead.

No live web search, no LLM-invented URLs. Career endpoints are only
constructed from an already-known official domain or a user-supplied URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from atlas.company.identity import (
    confidence_for_method,
    confidence_from_fingerprint,
    derive_instance_id,
    derive_observation_id,
    normalize_domain,
)
from atlas.company.models import (
    Company,
    CompanyObservation,
    CompanySourceRelationship,
    DiscoveryMethod,
    RelationshipState,
    SourceConfidence,
    SourceDiscoveryObservation,
    TRUSTED_DISCOVERY_METHODS,
    VerificationState,
)
from atlas.company.tenant import extract_tenant
from atlas.sources.fingerprint import fingerprint_ats
from atlas.sources.models import Capability, SourceInstance, SourceType

# Declarative default capabilities per source family (used by the factory so
# a SourceInstance carries the family's expected capabilities even before a
# real adapter exists). These are family knowledge, not a running adapter.
_API_ATS = frozenset(
    {
        Capability.SEARCH, Capability.DETAIL, Capability.API_AVAILABLE,
        Capability.LOCATION_FILTER, Capability.KEYWORD_FILTER,
        Capability.ACTIVE_STATUS, Capability.POSTED_DATE, Capability.DESCRIPTION,
    }
)
_BROWSER_ATS = frozenset(
    {
        Capability.SEARCH, Capability.DETAIL, Capability.PAGINATION,
        Capability.LOCATION_FILTER, Capability.KEYWORD_FILTER,
    }
)
FAMILY_DEFAULT_CAPABILITIES: dict[SourceType, frozenset[Capability]] = {
    SourceType.ATS_GREENHOUSE: _API_ATS,
    SourceType.ATS_LEVER: _API_ATS,
    SourceType.ATS_SMARTRECRUITERS: _API_ATS,
    SourceType.ATS_WORKDAY: frozenset(_API_ATS | {Capability.PAGINATION}),
    SourceType.ATS_ORACLE: _BROWSER_ATS,
    SourceType.ATS_SUCCESSFACTORS: _BROWSER_ATS,
    SourceType.ATS_ICIMS: _BROWSER_ATS,
    SourceType.ATS_PHENOM: _BROWSER_ATS,
    SourceType.ATS_EIGHTFOLD: _BROWSER_ATS,
    SourceType.COMPANY_CAREER: frozenset({Capability.SEARCH, Capability.BROWSER_REQUIRED}),
}

_CONFIDENCE_ORDER = {
    SourceConfidence.UNKNOWN: 0,
    SourceConfidence.TENTATIVE: 1,
    SourceConfidence.STRONG: 2,
    SourceConfidence.CONFIRMED: 3,
}


def _stronger(a: SourceConfidence, b: SourceConfidence) -> SourceConfidence:
    return a if _CONFIDENCE_ORDER[a] >= _CONFIDENCE_ORDER[b] else b


# ---------------------------------------------------------------------------
# Fingerprint pipeline → observation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _PipelineOutcome:
    observation: SourceDiscoveryObservation
    source_type: Optional[SourceType]
    resolved_url: Optional[str]
    tenant: Optional[str]


def run_fingerprint_pipeline(
    company_id: str,
    method: DiscoveryMethod,
    *,
    careers_url: Optional[str] = None,
    redirect_url: Optional[str] = None,
    markers: tuple[str, ...] = (),
    observed_at: Optional[str] = None,
) -> _PipelineOutcome:
    """career URL/redirect/markers → fingerprint → tenant → observation."""
    fp = fingerprint_ats(careers_url, redirect_url=redirect_url, markers=tuple(markers))
    resolved_url = redirect_url or careers_url
    source_type = fp.source_type
    if source_type is None and (careers_url or redirect_url):
        source_type = SourceType.COMPANY_CAREER  # a careers page we could not fingerprint
    tenant = extract_tenant(fp.source_type, resolved_url)

    confidence = confidence_for_method(method)
    if fp.matched:
        confidence = _stronger(confidence, confidence_from_fingerprint(fp.confidence))

    observation = SourceDiscoveryObservation(
        observation_id=derive_observation_id(
            company_id, method.value, careers_url, resolved_url,
            source_type.value if source_type else None,
        ),
        company_id=company_id,
        method=method,
        input_url=careers_url,
        resolved_url=resolved_url,
        detected_ats=fp.source_type.value if fp.source_type else None,
        tenant=tenant,
        confidence=confidence,
        verification_state=VerificationState.HEURISTIC if fp.matched else VerificationState.UNVERIFIED,
        observed_at=observed_at,
        detail={"fingerprint": fp.to_dict()},
    )
    return _PipelineOutcome(observation, source_type, resolved_url, tenant)


# ---------------------------------------------------------------------------
# SourceInstance factory
# ---------------------------------------------------------------------------
def build_source_instance(
    company_id: str,
    source_type: SourceType,
    base_url: Optional[str],
    tenant: Optional[str],
) -> SourceInstance:
    """SourceDiscoveryObservation → SourceInstance. Deterministic instance
    id; capabilities seeded from family defaults; disabled by default since
    no real adapter exists yet."""
    instance_id = derive_instance_id(company_id, source_type.value, tenant=tenant, base_url=base_url)
    caps = FAMILY_DEFAULT_CAPABILITIES.get(source_type, frozenset({Capability.SEARCH}))
    return SourceInstance(
        instance_id=instance_id,
        source_type=source_type,
        display_name=f"{source_type.value} ({tenant or 'careers'})",
        base_url=base_url,
        tenant=tenant,
        company_id=company_id,
        enabled=False,  # discovered, but no runnable adapter in Phase 1A.5
        capability_overrides=frozenset(caps),
    )


# ---------------------------------------------------------------------------
# Career endpoint discovery contract (deterministic, no live web)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CareerEndpointCandidate:
    url: str
    confidence: SourceConfidence
    reason: str

    def to_dict(self) -> dict:
        return {"url": self.url, "confidence": self.confidence.value, "reason": self.reason}


@dataclass(frozen=True)
class CareerDiscoveryResult:
    company_name: str
    candidates: tuple[CareerEndpointCandidate, ...] = ()
    status: str = "UNRESOLVED"

    def to_dict(self) -> dict:
        return {
            "company_name": self.company_name,
            "status": self.status,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def discover_career_endpoints(
    name: str,
    *,
    known_domain: Optional[str] = None,
    candidate_url: Optional[str] = None,
) -> CareerDiscoveryResult:
    """Produce candidate career endpoints from an already-known official
    domain or a user-supplied URL. Never fetches, never invents random
    URLs, never asks an LLM."""
    candidates: list[CareerEndpointCandidate] = []
    if candidate_url:
        candidates.append(CareerEndpointCandidate(candidate_url, SourceConfidence.CONFIRMED, "user-supplied URL"))
    domain = normalize_domain(known_domain)
    if domain:
        for path in ("careers", "jobs"):
            candidates.append(
                CareerEndpointCandidate(f"https://{domain}/{path}", SourceConfidence.TENTATIVE,
                                        "constructed from confirmed official domain")
            )
    status = "RESOLVED" if candidates else "UNRESOLVED"
    return CareerDiscoveryResult(name, tuple(candidates), status)


# ---------------------------------------------------------------------------
# Dynamic employer registration flow (domain-safe)
# ---------------------------------------------------------------------------
@dataclass
class EmployerRegistrationResult:
    company: Company
    relationship: Optional[CompanySourceRelationship] = None
    source_instance: Optional[SourceInstance] = None
    observation: Optional[SourceDiscoveryObservation] = None
    source_registered: bool = False
    rejected_untrusted: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "company": self.company.to_dict(),
            "relationship": self.relationship.to_dict() if self.relationship else None,
            "source_instance": self.source_instance.to_dict() if self.source_instance else None,
            "observation": self.observation.to_dict() if self.observation else None,
            "source_registered": self.source_registered,
            "rejected_untrusted": self.rejected_untrusted,
            "notes": list(self.notes),
        }


def register_employer(registry, obs: CompanyObservation) -> EmployerRegistrationResult:
    """new employer observation → normalize company → check registry →
    register/attach provenance → career-source candidate → fingerprint →
    SourceInstance → persist relationship (only if trusted).

    An UNTRUSTED_POSTING observation registers only the company *name*
    (domain/careers stripped) and records a REJECTED source observation —
    it never registers an official domain or source."""
    trusted = obs.method in TRUSTED_DISCOVERY_METHODS
    has_source_candidate = bool(obs.careers_url or obs.candidate_url or obs.redirect_url or obs.markers)

    if not trusted:
        # Domain safety: strip any posting-derived domain/careers before
        # registering the company identity.
        safe_obs = CompanyObservation(
            name=obs.name, country=obs.country, method=obs.method,
            reason=obs.reason, aliases=obs.aliases,
        )
        company = registry.register_company(safe_obs)
        result = EmployerRegistrationResult(company=company, rejected_untrusted=has_source_candidate)
        if has_source_candidate:
            rejected = SourceDiscoveryObservation(
                observation_id=derive_observation_id(
                    company.company_id, obs.method.value,
                    obs.careers_url or obs.candidate_url, obs.redirect_url, None,
                ),
                company_id=company.company_id,
                method=obs.method,
                input_url=obs.careers_url or obs.candidate_url,
                resolved_url=obs.redirect_url,
                confidence=SourceConfidence.UNKNOWN,
                verification_state=VerificationState.REJECTED,
                detail={"reason": "untrusted posting-derived URL; source not registered"},
            )
            registry.record_observation(rejected)
            result.observation = rejected
            result.notes.append("untrusted posting URL rejected for source registration")
        return result

    company = registry.register_company(obs)
    result = EmployerRegistrationResult(company=company)

    careers_url = obs.careers_url or obs.candidate_url
    if not (careers_url or obs.redirect_url or obs.markers):
        return result

    outcome = run_fingerprint_pipeline(
        company.company_id, obs.method,
        careers_url=careers_url, redirect_url=obs.redirect_url, markers=obs.markers,
    )
    registry.record_observation(outcome.observation)
    result.observation = outcome.observation

    if outcome.source_type is not None:
        instance = build_source_instance(
            company.company_id, outcome.source_type, outcome.resolved_url, outcome.tenant
        )
        relationship = registry.attach_source_instance(
            company.company_id, instance.instance_id, outcome.source_type.value,
            base_url=instance.base_url, tenant=outcome.tenant,
            state=RelationshipState.DISCOVERED, confidence=outcome.observation.confidence,
            provenance={"method": obs.method.value, "observation_id": outcome.observation.observation_id},
        )
        result.relationship = relationship
        result.source_instance = instance
        result.source_registered = True

    return result


def mark_source_replaced(registry, company_id: str, old_instance_id: str) -> bool:
    """Mark a company's existing source relationship REPLACED (non-current),
    retaining it as history. Used for ATS migrations — never a destructive
    delete."""
    from atlas.company.identity import derive_relationship_id

    relationship_id = derive_relationship_id(company_id, old_instance_id)
    if registry.store.get_source_relationship(relationship_id) is None:
        return False
    registry.mark_relationship_state(relationship_id, RelationshipState.REPLACED, is_current=False)
    return True


__all__ = [
    "FAMILY_DEFAULT_CAPABILITIES",
    "run_fingerprint_pipeline",
    "build_source_instance",
    "CareerEndpointCandidate",
    "CareerDiscoveryResult",
    "discover_career_endpoints",
    "EmployerRegistrationResult",
    "register_employer",
    "mark_source_replaced",
]
