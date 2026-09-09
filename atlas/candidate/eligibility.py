"""Deterministic candidate eligibility gate + shared evaluation types
(Phase 1E/F §8.A).

This composes the EXISTING deterministic policy rules (`atlas.policy.rules`) into
one job-level gate that is completely independent of any LLM: target-role lanes,
the hard 4+ experience minimum, geography / remote-India and international
eligibility, exclusion families, verification/freshness state, closure, and any
known-unsupported candidate requirement. An LLM may later REORDER eligible jobs,
but it can never override a deterministic veto here (the gate is the ceiling).
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Mapping, Optional

from atlas.candidate.ledger import CandidateLedger
from atlas.candidate.models import EvidenceClass, _STRENGTH
from atlas.data_integrity.normalizers import identity_token
from atlas.policy.loader import PolicyBundle
from atlas.policy.rules import (
    NOT_ELIGIBLE,
    ClosureVerdict,
    classify_closure,
    evaluate_exclusions,
    experience_eligible,
    extract_experience,
    freshness_band,
    international_eligibility,
)


class EligibilityStatus(str, enum.Enum):
    ELIGIBLE = "ELIGIBLE"
    DEFERRED = "DEFERRED"          # eligible in principle but needs verification/tailoring
    EXCLUDED = "EXCLUDED"          # exclusion family / off-lane
    NOT_ELIGIBLE = "NOT_ELIGIBLE"  # hard geography / experience veto
    CLOSED = "CLOSED"              # positive closure evidence


class Recommendation(str, enum.Enum):
    PRIORITY_APPLY = "PRIORITY_APPLY"
    STRONG_APPLY = "STRONG_APPLY"
    APPLY_AFTER_TAILORING = "APPLY_AFTER_TAILORING"
    STRETCH = "STRETCH"
    MANUAL_VERIFICATION = "MANUAL_VERIFICATION"
    MONITOR = "MONITOR"
    REJECT = "REJECT"
    CLOSED = "CLOSED"
    NOT_EVALUATED = "NOT_EVALUATED"


@dataclass(frozen=True)
class RankableJob:
    """The compact, evidence-bearing job view ranking operates on. Built from a
    canonical job, a portal lead, or a synthetic fixture — never from a raw
    untrusted posting treated as instructions."""

    job_key: str
    company: str
    title: str
    location: str = ""
    lane: Optional[str] = None
    work_mode: str = "UNKNOWN"
    description: str = ""
    mandatory_requirements: tuple[str, ...] = ()
    preferred_requirements: tuple[str, ...] = ()
    experience_text: str = ""
    eligibility_text: str = ""          # location + sponsorship wording
    posted_date: Optional[datetime.date] = None
    deadline: Optional[datetime.date] = None
    verification_state: str = "PORTAL_CURRENT_LEAD"
    has_live_official_page: bool = False
    is_fetchable: bool = True            # dead/unfetchable postings are NOT scored from title
    evidence_texts: tuple[str, ...] = () # for closure classification
    source_family: str = ""
    canonical_id: Optional[str] = None
    url: Optional[str] = None
    # Phase 2A official-first record classification + provenance.
    record_class: str = ""               # OFFICIAL_DIRECT | PORTAL_OFFICIAL_LINKED | PORTAL_ONLY
    primary_source: str = ""             # the authoritative source family (official for linked)
    discovery_channels: tuple[str, ...] = ()  # every channel the job was seen on
    official_requisition_id: str = ""    # official requisition id when available


@dataclass(frozen=True)
class CandidateProfile:
    """A compact, evidence-classed candidate view for eligibility + matching. Can
    be built from the private ledger OR a synthetic ledger (PII-free). The real
    profile is only used with explicit consent elsewhere; nothing here logs PII."""

    total_experience_years: float
    target_lanes: tuple[str, ...]
    location_group: str = "PRIMARY"
    evidence: Mapping[str, EvidenceClass] = field(default_factory=dict)  # topic token -> strongest class
    evidence_ids: Mapping[str, str] = field(default_factory=dict)        # topic token -> claim id
    hard_unsupported: frozenset[str] = frozenset()                       # normalized reqs the candidate cannot meet
    strong_overall: bool = True

    @classmethod
    def from_ledger(
        cls,
        ledger: CandidateLedger,
        *,
        total_experience_years: float,
        target_lanes: tuple[str, ...],
        location_group: str = "PRIMARY",
        hard_unsupported: tuple[str, ...] = (),
        strong_overall: bool = True,
    ) -> "CandidateProfile":
        evidence: dict[str, EvidenceClass] = {}
        evidence_ids: dict[str, str] = {}
        for claim in ledger.claims():
            token = identity_token(claim.topic)
            if not token:
                continue
            existing = evidence.get(token)
            if existing is None or _STRENGTH[claim.evidence_class] > _STRENGTH[existing]:
                evidence[token] = claim.evidence_class
                evidence_ids[token] = claim.claim_id
        return cls(
            total_experience_years=float(total_experience_years),
            target_lanes=tuple(target_lanes),
            location_group=location_group,
            evidence=evidence,
            evidence_ids=evidence_ids,
            hard_unsupported=frozenset(identity_token(x) for x in hard_unsupported),
            strong_overall=strong_overall,
        )


@dataclass(frozen=True)
class EligibilityAxes:
    """Separate, deterministic axes preserved for every job (never collapsed into
    one opaque number)."""

    status: str                          # EligibilityStatus value
    lane: Optional[str]
    vetoes: tuple[str, ...] = ()
    experience_reason: str = ""
    intl_class: str = ""
    exclusion_family: Optional[str] = None
    freshness: str = ""
    verification_state: str = ""
    closed: bool = False


def match_requirement(req: str, evidence: Mapping[str, EvidenceClass]) -> Optional[str]:
    """Return the evidence topic token supporting ``req``, or None. Matching is
    conservative token-overlap (exact, or one token-set is a subset of the
    other) so 'Spring Boot' matches 'spring boot' evidence but not unrelated
    tokens."""
    r = identity_token(req)
    if not r:
        return None
    if r in evidence:
        return r
    r_tokens = set(r.split())
    for topic in evidence:
        t_tokens = set(topic.split())
        if not t_tokens:
            continue
        if r_tokens <= t_tokens or t_tokens <= r_tokens:
            return topic
    return None


def classify_lane(job: RankableJob, policy: PolicyBundle) -> Optional[str]:
    """Deterministically classify a job into at most ONE configured lane (first
    by policy order). Uses the job's declared lane when present; otherwise
    matches positive titles / technology terms. Keeps the six lanes separate."""
    if job.lane:
        return job.lane
    title_tok = identity_token(job.title)
    desc_tok = identity_token(job.description)
    for lane in policy.lanes.values():
        for pt in lane.positive_titles:
            if identity_token(pt) and identity_token(pt) in title_tok:
                return lane.key
        for term in lane.technology_terms:
            tt = identity_token(term)
            if tt and (tt in title_tok or tt in desc_tok):
                return lane.key
    return None


class EligibilityGate:
    """Composes the deterministic policy rules into one job-level verdict."""

    def __init__(self, policy: PolicyBundle) -> None:
        self.policy = policy

    def evaluate(
        self,
        job: RankableJob,
        candidate: CandidateProfile,
        *,
        today: Optional[datetime.date] = None,
    ) -> EligibilityAxes:
        vetoes: list[str] = []
        lane = classify_lane(job, self.policy)

        # --- exclusion families ------------------------------------------------
        excl = evaluate_exclusions(f"{job.title} {job.description}", self.policy.exclusions)

        # --- experience (hard 4+ minimum) -------------------------------------
        extracted = extract_experience(job.experience_text or job.description)
        exp_verdict = experience_eligible(
            extracted, self.policy.experience, overall_evidence_strong=candidate.strong_overall
        )

        # --- geography / international eligibility -----------------------------
        intl = international_eligibility(
            job.eligibility_text or job.description, self.policy.geography
        )

        # --- closure ----------------------------------------------------------
        closure: ClosureVerdict = classify_closure(
            self.policy.verification, evidence_texts=list(job.evidence_texts),
            deadline=job.deadline, today=today,
        )

        # --- freshness --------------------------------------------------------
        fresh = freshness_band(
            job.posted_date, today=today, has_live_official_page=job.has_live_official_page
        )

        # --- known-unsupported mandatory requirement --------------------------
        for req in job.mandatory_requirements:
            if identity_token(req) in candidate.hard_unsupported:
                vetoes.append(f"unsupported mandatory requirement: {req}")

        # --- lane / target-role veto ------------------------------------------
        off_lane = lane is None or (candidate.target_lanes and lane not in candidate.target_lanes)

        # --- fold into a status (deterministic precedence) --------------------
        status = EligibilityStatus.ELIGIBLE
        if closure.closed:
            status = EligibilityStatus.CLOSED
            vetoes.append(closure.reason)
        elif excl.excluded:
            status = EligibilityStatus.EXCLUDED
            vetoes.append(f"exclusion:{excl.family}")
        elif not exp_verdict.eligible:
            status = EligibilityStatus.NOT_ELIGIBLE
            vetoes.append(f"experience:{exp_verdict.reason}")
        elif intl == NOT_ELIGIBLE:
            status = EligibilityStatus.NOT_ELIGIBLE
            vetoes.append("geography: not eligible from India")
        elif any(v.startswith("unsupported mandatory") for v in vetoes):
            status = EligibilityStatus.NOT_ELIGIBLE
        elif off_lane:
            status = EligibilityStatus.EXCLUDED
            vetoes.append("off target lane")
        elif job.verification_state in ("PORTAL_CURRENT_LEAD", "MANUAL_VERIFICATION"):
            # eligible in principle but needs official verification before apply
            status = EligibilityStatus.DEFERRED

        return EligibilityAxes(
            status=status.value,
            lane=lane,
            vetoes=tuple(vetoes),
            experience_reason=exp_verdict.reason,
            intl_class=intl,
            exclusion_family=excl.family,
            freshness=fresh,
            verification_state=job.verification_state,
            closed=closure.closed,
        )


__all__ = [
    "EligibilityStatus",
    "Recommendation",
    "RankableJob",
    "CandidateProfile",
    "EligibilityAxes",
    "EligibilityGate",
    "match_requirement",
    "classify_lane",
]
