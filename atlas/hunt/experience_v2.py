"""Experience compatibility V2 (build spec section 11).

A typed result that layers candidate-aware fit bands on top of the EXISTING
deterministic extractor (:func:`atlas.policy.rules.extract_experience`). The
user's latest instruction supersedes older narrower wording: 0-3 is normally
eligible, a suitable 3+ may qualify, and a hard 4+ minimum fails unless the real
private candidate years satisfy it.

A title NEVER invents years; senior/staff/lead/principal with no usable range is
manual review, not an automatic reject.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from atlas.hunt.signals import signal_present
from atlas.policy.models import ExperiencePolicy
from atlas.policy.rules import extract_experience

__all__ = ["ExperienceFitBand", "ExperienceFit", "evaluate_experience_fit"]

_SENIORITY_TOKENS = ("senior", "staff", "lead", "principal", "architect", "sr.", "sr")


class ExperienceFitBand(str, enum.Enum):
    ELIGIBLE = "ELIGIBLE"
    STRONG_REVIEW = "STRONG_REVIEW"
    STRETCH = "STRETCH"
    REJECT = "REJECT"
    MANUAL_VERIFICATION = "MANUAL_VERIFICATION"


@dataclass(frozen=True)
class ExperienceFit:
    fit: str
    mandatory_min_years: Optional[float] = None
    mandatory_max_years: Optional[float] = None
    preferred_years: Optional[float] = None
    ambiguous_seniority: bool = False
    candidate_years: Optional[float] = None
    reasons: tuple[str, ...] = ()
    raw: str = ""

    @property
    def eligible(self) -> bool:
        return self.fit in (
            ExperienceFitBand.ELIGIBLE.value,
            ExperienceFitBand.STRONG_REVIEW.value,
            ExperienceFitBand.STRETCH.value,
        )


def _title_is_senior(title: object) -> bool:
    for tok in _SENIORITY_TOKENS:
        if signal_present(title, tok):
            return True
    return False


def evaluate_experience_fit(
    *,
    title: object,
    experience_text: object,
    policy: ExperiencePolicy,
    candidate_years: Optional[float] = None,
    overall_evidence_strong: bool = True,
) -> ExperienceFit:
    """Interpret the mandatory/preferred/max experience for a job and decide a
    fit band. The mandatory minimum drives eligibility; preferred/max never
    reject. An explicit range overrides a misleading senior title."""
    extracted = extract_experience(experience_text)
    senior_title = _title_is_senior(title)
    reasons: list[str] = []
    lo = extracted.min_years
    hi = extracted.max_years
    pref = extracted.preferred_years

    if lo is not None and senior_title:
        reasons.append("explicit experience overrides senior-looking title")

    # No hard mandatory minimum stated.
    if lo is None:
        if senior_title:
            reasons.append("senior/staff/lead title with no usable year range")
            return ExperienceFit(
                fit=ExperienceFitBand.MANUAL_VERIFICATION.value,
                mandatory_min_years=None, mandatory_max_years=hi, preferred_years=pref,
                ambiguous_seniority=True, candidate_years=candidate_years,
                reasons=tuple(reasons), raw=extracted.raw,
            )
        reasons.append("no hard minimum stated")
        return ExperienceFit(
            fit=ExperienceFitBand.ELIGIBLE.value,
            mandatory_min_years=None, mandatory_max_years=hi, preferred_years=pref,
            candidate_years=candidate_years, reasons=tuple(reasons), raw=extracted.raw,
        )

    # A hard mandatory minimum at/above the reject threshold (default 4).
    if lo >= policy.hard_reject_min_years:
        if candidate_years is not None and candidate_years >= lo:
            reasons.append(f"hard {lo:g}+ minimum satisfied by candidate {candidate_years:g}y")
            return ExperienceFit(
                fit=ExperienceFitBand.STRETCH.value,
                mandatory_min_years=lo, mandatory_max_years=hi, preferred_years=pref,
                candidate_years=candidate_years, reasons=tuple(reasons), raw=extracted.raw,
            )
        reasons.append(f"hard mandatory {lo:g}+ minimum exceeds policy")
        return ExperienceFit(
            fit=ExperienceFitBand.REJECT.value,
            mandatory_min_years=lo, mandatory_max_years=hi, preferred_years=pref,
            candidate_years=candidate_years, reasons=tuple(reasons), raw=extracted.raw,
        )

    # Exactly a 3-year minimum: eligible when evidence strong, else review.
    if lo == 3:
        if not policy.allow_three_year_when_strong:
            reasons.append("3-year minimum and strong-evidence allowance disabled")
            return ExperienceFit(
                fit=ExperienceFitBand.STRETCH.value,
                mandatory_min_years=lo, mandatory_max_years=hi, preferred_years=pref,
                candidate_years=candidate_years, reasons=tuple(reasons), raw=extracted.raw,
            )
        band = ExperienceFitBand.ELIGIBLE if overall_evidence_strong else ExperienceFitBand.STRONG_REVIEW
        reasons.append("3-year minimum accepted (review evidence strength)")
        return ExperienceFit(
            fit=band.value,
            mandatory_min_years=lo, mandatory_max_years=hi, preferred_years=pref,
            candidate_years=candidate_years, reasons=tuple(reasons), raw=extracted.raw,
        )

    # Mandatory minimum <= 3 (and not exactly 3): eligible.
    reasons.append("mandatory minimum within range")
    return ExperienceFit(
        fit=ExperienceFitBand.ELIGIBLE.value,
        mandatory_min_years=lo, mandatory_max_years=hi, preferred_years=pref,
        candidate_years=candidate_years, reasons=tuple(reasons), raw=extracted.raw,
    )
