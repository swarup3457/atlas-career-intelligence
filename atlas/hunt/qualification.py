"""Strict conjunctive lane qualification (build spec section 9, architecture s.4).

This is the corrected replacement for the ANY-positive
``atlas.candidate.eligibility.classify_lane``. A job qualifies for a lane only
when BOTH a permitted development role family AND every required technology
anchor group are present in the hydrated evidence. Support signals
(API/REST/SQL/microservices) corroborate but are NEVER sufficient alone, and a
dominant wrong stack with no lane anchor is rejected.

Deterministic Python only. An LLM may review a genuinely ambiguous
``AMBIGUOUS_REVIEW`` job, but it can never override a missing anchor, an
excluded role family, or a hard experience gate.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from atlas.hunt.experience_v2 import ExperienceFit, ExperienceFitBand, evaluate_experience_fit
from atlas.hunt.role_family import RoleFamilyDecision, classify_role_family
from atlas.hunt.role_intent import LaneContract, RoleIntentPolicy
from atlas.hunt.signals import find_signals, signal_present
from atlas.policy.models import ExperiencePolicy

__all__ = [
    "QualificationStatus",
    "QualifiableJob",
    "RoleQualificationDecision",
    "qualify_lane",
    "qualify_job",
    "JobQualification",
]


class QualificationStatus(str, enum.Enum):
    QUALIFIED = "QUALIFIED"
    REJECT_WRONG_STACK = "REJECT_WRONG_STACK"
    REJECT_ROLE_FAMILY = "REJECT_ROLE_FAMILY"
    REJECT_EXPERIENCE = "REJECT_EXPERIENCE"
    NEEDS_DETAIL = "NEEDS_DETAIL"
    AMBIGUOUS_REVIEW = "AMBIGUOUS_REVIEW"


@dataclass(frozen=True)
class QualifiableJob:
    job_key: str
    title: str
    description: str = ""
    mandatory_requirements: tuple[str, ...] = ()
    preferred_requirements: tuple[str, ...] = ()
    experience_text: str = ""
    has_detail: bool = True

    @property
    def evidence_text(self) -> str:
        parts = [self.title, self.description]
        parts.extend(self.mandatory_requirements)
        parts.extend(self.preferred_requirements)
        parts.append(self.experience_text)
        return " \n ".join(p for p in parts if p)


@dataclass(frozen=True)
class RoleQualificationDecision:
    job_key: str
    lane: str
    status: str
    role_family: str
    matched_anchor_groups: tuple[str, ...] = ()
    matched_anchors: tuple[str, ...] = ()
    support_signals_present: tuple[str, ...] = ()
    dominant_stack: Optional[str] = None
    exclusion_family: Optional[str] = None
    experience_fit: Optional[str] = None
    reasons: tuple[str, ...] = ()
    policy_hash: str = ""

    @property
    def qualified(self) -> bool:
        return self.status == QualificationStatus.QUALIFIED.value


def qualify_lane(
    job: QualifiableJob,
    lane: LaneContract,
    *,
    role_family: Optional[RoleFamilyDecision] = None,
    experience_policy: Optional[ExperiencePolicy] = None,
    candidate_years: Optional[float] = None,
    overall_evidence_strong: bool = True,
    policy_hash: str = "",
) -> RoleQualificationDecision:
    """Qualify one job against one lane contract (conjunctive)."""
    rf = role_family or classify_role_family(job.title, job.description)
    text = job.evidence_text
    reasons: list[str] = []

    def decide(status: QualificationStatus, **kw) -> RoleQualificationDecision:
        return RoleQualificationDecision(
            job_key=job.job_key, lane=lane.key, status=status.value, role_family=rf.family,
            policy_hash=policy_hash, reasons=tuple(reasons), **kw,
        )

    # --- 1) role-family gate ---------------------------------------------------
    if rf.family in lane.excluded_role_families:
        reasons.append(f"role family {rf.family} excluded from {lane.key}")
        return decide(QualificationStatus.REJECT_ROLE_FAMILY, exclusion_family=rf.family)
    if lane.allowed_role_families and rf.family not in lane.allowed_role_families:
        reasons.append(f"role family {rf.family} not in allowed set for {lane.key}")
        return decide(QualificationStatus.REJECT_ROLE_FAMILY, exclusion_family=rf.family)

    # --- 2) required anchor groups (conjunctive) ------------------------------
    matched_groups: list[str] = []
    matched_anchors: list[str] = []
    missing_groups: list[str] = []
    for grp in lane.required_anchor_groups:
        hits = find_signals(text, grp.any_of)
        if hits:
            matched_groups.append(grp.group)
            matched_anchors.extend(hits)
        else:
            missing_groups.append(grp.group)

    support_present = find_signals(text, lane.support_signals)
    wrong_present = find_signals(text, lane.wrong_stack_signals)
    dominant = wrong_present[0] if wrong_present else None

    if missing_groups:
        # A required anchor group is unsatisfied.
        if not job.has_detail:
            reasons.append(f"required anchor group(s) {missing_groups} unresolved without detail")
            return decide(
                QualificationStatus.NEEDS_DETAIL,
                support_signals_present=support_present, dominant_stack=dominant,
            )
        if lane.reject_when_wrong_stack_dominant and dominant:
            reasons.append(f"dominant wrong stack {dominant!r} and no {missing_groups} anchor")
        else:
            reasons.append(
                f"missing required anchor group(s) {missing_groups}; support signals are not sufficient alone"
            )
        return decide(
            QualificationStatus.REJECT_WRONG_STACK,
            matched_anchor_groups=tuple(matched_groups), matched_anchors=tuple(matched_anchors),
            support_signals_present=support_present, dominant_stack=dominant,
        )

    # --- 3) controlled fallback (GENERAL_SOFTWARE) ----------------------------
    if lane.is_fallback:
        if lane.reject_when_wrong_stack_dominant and dominant:
            reasons.append(f"dominant incompatible stack {dominant!r} rejected from fallback")
            return decide(
                QualificationStatus.REJECT_WRONG_STACK,
                support_signals_present=support_present, dominant_stack=dominant,
            )
        if lane.require_transferable_or_neutral:
            transferable = find_signals(text, lane.transferable_or_neutral_any)
            if not transferable:
                if not job.has_detail:
                    return decide(QualificationStatus.NEEDS_DETAIL)
                reasons.append("no candidate-transferable or neutral technology evidence")
                return decide(QualificationStatus.REJECT_WRONG_STACK)
            matched_anchors.extend(transferable)

    # --- 4) experience gate ---------------------------------------------------
    exp_fit: Optional[ExperienceFit] = None
    exp_band: Optional[str] = None
    if experience_policy is not None:
        exp_fit = evaluate_experience_fit(
            title=job.title, experience_text=job.experience_text or job.description,
            policy=experience_policy, candidate_years=candidate_years,
            overall_evidence_strong=overall_evidence_strong,
        )
        exp_band = exp_fit.fit
        if exp_fit.fit == ExperienceFitBand.REJECT.value:
            reasons.extend(exp_fit.reasons)
            return decide(
                QualificationStatus.REJECT_EXPERIENCE,
                matched_anchor_groups=tuple(matched_groups), matched_anchors=tuple(matched_anchors),
                support_signals_present=support_present, dominant_stack=dominant,
                experience_fit=exp_band,
            )

    # --- 5) qualified ---------------------------------------------------------
    reasons.append(
        f"role family {rf.family} + anchors {matched_groups or ['fallback']} satisfied"
    )
    return decide(
        QualificationStatus.QUALIFIED,
        matched_anchor_groups=tuple(matched_groups), matched_anchors=tuple(matched_anchors),
        support_signals_present=support_present, dominant_stack=dominant,
        experience_fit=exp_band,
    )


@dataclass(frozen=True)
class JobQualification:
    job_key: str
    primary_lane: Optional[str]
    primary: Optional[RoleQualificationDecision]
    by_lane: dict[str, RoleQualificationDecision] = field(default_factory=dict)

    @property
    def qualified(self) -> bool:
        return self.primary is not None and self.primary.qualified


def qualify_job(
    job: QualifiableJob,
    policy: RoleIntentPolicy,
    *,
    experience_policy: Optional[ExperiencePolicy] = None,
    candidate_years: Optional[float] = None,
    overall_evidence_strong: bool = True,
) -> JobQualification:
    """Qualify a job across all six lanes and select the most specific
    qualifying lane. Specificity = more satisfied required anchor groups; ties
    break to the lower priority number. A qualified specific lane always beats
    the zero-anchor GENERAL_SOFTWARE fallback."""
    rf = classify_role_family(job.title, job.description)
    by_lane: dict[str, RoleQualificationDecision] = {}
    for key in policy.lane_keys():
        lane = policy.lane(key)
        by_lane[key] = qualify_lane(
            job, lane, role_family=rf, experience_policy=experience_policy,
            candidate_years=candidate_years, overall_evidence_strong=overall_evidence_strong,
            policy_hash=policy.fingerprint,
        )

    qualified = [
        (policy.lane(k).specificity, -policy.lane(k).priority, k)
        for k, d in by_lane.items()
        if d.qualified
    ]
    if not qualified:
        return JobQualification(job_key=job.job_key, primary_lane=None, primary=None, by_lane=by_lane)
    qualified.sort(reverse=True)
    primary_key = qualified[0][2]
    return JobQualification(
        job_key=job.job_key, primary_lane=primary_key, primary=by_lane[primary_key], by_lane=by_lane
    )
