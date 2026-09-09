"""Candidate matching + diversified shortlist (build spec 14, 17).

Runs ONLY on strictly qualified jobs (build spec: matching after qualification).
Preserves all qualified/raw records; the visible shortlist applies diversity
caps (default 5 per company, 3 per company-per-lane, 15 total) WITHOUT deleting
anything from side data. Packages are built only for apply-family
recommendations — there is no forced-package fallback.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from atlas.candidate.eligibility import Recommendation, match_requirement
from atlas.candidate.models import EvidenceClass, _STRENGTH
from atlas.data_integrity.normalizers import identity_token
from atlas.hunt.experience_v2 import ExperienceFitBand
from atlas.hunt.models import JobDetailRevision
from atlas.hunt.qualification import RoleQualificationDecision
from atlas.hunt.role_intent import RoleIntentPolicy
from atlas.policy.rules import freshness_band

__all__ = ["HuntCandidate", "CandidateMatchDecision", "match_qualified", "diversify_shortlist", "APPLY_FAMILY"]

APPLY_FAMILY = frozenset(
    {
        Recommendation.PRIORITY_APPLY.value,
        Recommendation.STRONG_APPLY.value,
        Recommendation.APPLY_AFTER_TAILORING.value,
    }
)

_REC_RANK = {
    Recommendation.PRIORITY_APPLY.value: 0,
    Recommendation.STRONG_APPLY.value: 1,
    Recommendation.APPLY_AFTER_TAILORING.value: 2,
    Recommendation.STRETCH.value: 3,
    Recommendation.MANUAL_VERIFICATION.value: 4,
    Recommendation.MONITOR.value: 5,
    Recommendation.REJECT.value: 6,
}

_FRESH_SCORE = {"0-7 days": 5, "8-14 days": 4, "15-30 days": 2}


@dataclass(frozen=True)
class HuntCandidate:
    """Compact, PII-free evidence-classed candidate view for matching."""

    total_experience_years: float = 2.0
    target_lanes: tuple[str, ...] = ()
    location_group: str = "PRIMARY"
    evidence: Mapping[str, EvidenceClass] = field(default_factory=dict)
    strong_overall: bool = True

    @classmethod
    def from_skills(cls, skills: Sequence[str], **kw) -> "HuntCandidate":
        ev = {identity_token(s): EvidenceClass.PROFESSIONAL for s in skills if identity_token(s)}
        return cls(evidence=ev, **kw)


@dataclass(frozen=True)
class CandidateMatchDecision:
    job_key: str
    lane: str
    company: str
    title: str
    match_score: int
    recommendation: str
    requirements_matched: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    experience_fit: str = ""
    freshness_band: str = ""
    verification_state: str = ""
    reasons: tuple[str, ...] = ()

    @property
    def is_apply_family(self) -> bool:
        return self.recommendation in APPLY_FAMILY


def _coverage(reqs: Sequence[str], evidence: Mapping[str, EvidenceClass]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    matched: list[str] = []
    missing: list[str] = []
    for req in reqs:
        if match_requirement(req, evidence):
            matched.append(req)
        else:
            missing.append(req)
    return tuple(matched), tuple(missing)


def match_qualified(
    detail: JobDetailRevision,
    decision: RoleQualificationDecision,
    candidate: HuntCandidate,
    *,
    today=None,
) -> CandidateMatchDecision:
    """Score a qualified job with the V1 evidence-weighted model and assign a
    recommendation band. Verification/experience gates can cap the band."""
    reasons: list[str] = []
    mand_matched, mand_missing = _coverage(detail.mandatory_requirements, candidate.evidence)
    pref_matched, _ = _coverage(detail.preferred_requirements, candidate.evidence)

    mand_total = len(detail.mandatory_requirements) or 1
    score = 0.0
    score += 35.0 * (len(mand_matched) / mand_total)
    # anchors themselves are strong mandatory evidence when reqs are sparse
    if not detail.mandatory_requirements and decision.matched_anchors:
        score += 20.0

    exp = decision.experience_fit or ExperienceFitBand.ELIGIBLE.value
    score += {
        ExperienceFitBand.ELIGIBLE.value: 20,
        ExperienceFitBand.STRONG_REVIEW.value: 16,
        ExperienceFitBand.STRETCH.value: 12,
        ExperienceFitBand.MANUAL_VERIFICATION.value: 8,
    }.get(exp, 10)

    score += 15.0 if decision.support_signals_present else 10.0
    score += 10.0 if (not candidate.target_lanes or decision.lane in candidate.target_lanes) else 6.0
    score += {"PRIMARY": 10, "SECONDARY": 8}.get(candidate.location_group, 5)
    score += 5.0 * (len(pref_matched) / (len(detail.preferred_requirements) or 1)) if detail.preferred_requirements else 0.0

    fresh = freshness_band(detail.posted_date, today=today, has_live_official_page=detail.has_live_official_page)
    score += _FRESH_SCORE.get(fresh, 2)

    score_i = int(round(min(100.0, max(0.0, score))))

    official = detail.verification_state in ("VERIFIED_OFFICIAL", "VERIFIED_AUTHORIZED_RECRUITER")
    if exp == ExperienceFitBand.MANUAL_VERIFICATION.value:
        rec = Recommendation.MANUAL_VERIFICATION.value
        reasons.append("ambiguous seniority requires manual verification")
    elif not official:
        rec = Recommendation.MANUAL_VERIFICATION.value
        reasons.append("needs official verification before apply")
    elif score_i >= 85:
        rec = Recommendation.PRIORITY_APPLY.value
    elif score_i >= 75:
        rec = Recommendation.STRONG_APPLY.value
    elif score_i >= 68:
        rec = Recommendation.APPLY_AFTER_TAILORING.value
    elif score_i >= 58:
        rec = Recommendation.STRETCH.value
    else:
        rec = Recommendation.MONITOR.value

    return CandidateMatchDecision(
        job_key=detail.canonical_key, lane=decision.lane, company=detail.company, title=detail.title,
        match_score=score_i, recommendation=rec, requirements_matched=mand_matched,
        missing_requirements=mand_missing, experience_fit=exp, freshness_band=fresh,
        verification_state=detail.verification_state, reasons=tuple(reasons),
    )


def diversify_shortlist(
    matches: Sequence[CandidateMatchDecision],
    policy: RoleIntentPolicy,
    *,
    company_of: Optional[Mapping[str, str]] = None,
) -> list[CandidateMatchDecision]:
    """Apply diversity caps to produce the candidate-facing shortlist WITHOUT
    deleting anything from the input. A single company cannot dominate the
    visible list (build spec 17)."""
    ranked = sorted(
        matches,
        key=lambda m: (
            _REC_RANK.get(m.recommendation, 9),
            -m.match_score,
            policy.lane(m.lane).priority if m.lane in policy.lanes else 99,
            m.company,
        ),
    )
    per_company: dict[str, int] = {}
    per_company_lane: dict[tuple[str, str], int] = {}
    shortlist: list[CandidateMatchDecision] = []
    for m in ranked:
        if len(shortlist) >= policy.max_shortlist_total:
            break
        c = per_company.get(m.company, 0)
        cl = per_company_lane.get((m.company, m.lane), 0)
        if c >= policy.max_display_per_company:
            continue
        if cl >= policy.max_display_per_lane_per_company:
            continue
        per_company[m.company] = c + 1
        per_company_lane[(m.company, m.lane)] = cl + 1
        shortlist.append(m)
    return shortlist
