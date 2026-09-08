"""Deterministic policy rules (Phase 1B, build spec 11/12/18/19).

Pure, testable functions implementing the deterministic gates that must NOT
depend on an LLM: experience extraction/eligibility, geography/international
eligibility, exclusion families, freshness bands, closure detection, and the
official-verification guard. LLM reasoning stays optional and typed for
genuinely ambiguous semantic judgments only.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Optional

from atlas.policy.models import (
    ExclusionPolicy,
    ExperiencePolicy,
    GeographyPolicy,
    VerificationPolicy,
)
from atlas.policy.status import JobLifecycleStatus, VerificationLevel


# ---------------------------------------------------------------------------
# Experience (build spec 11) — represent min/max/preferred/ambiguous SEPARATELY
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExtractedExperience:
    min_years: Optional[float] = None
    max_years: Optional[float] = None
    preferred_years: Optional[float] = None
    ambiguous: bool = False
    raw: str = ""


_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs|year)", re.I)
_PLUS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+\s*(?:years|yrs|year)", re.I)
_MIN_RE = re.compile(r"(?:minimum|min|at least)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*(?:years|yrs|year)", re.I)
_SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:years|yrs|year)", re.I)


def extract_experience(text: Optional[str]) -> ExtractedExperience:
    """Deterministically extract an experience requirement. NEVER invents a
    range from a title — text with no explicit number is ``ambiguous``."""
    if not text or not str(text).strip():
        return ExtractedExperience(ambiguous=True, raw=text or "")
    s = str(text)
    m = _RANGE_RE.search(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return ExtractedExperience(min_years=min(lo, hi), max_years=max(lo, hi), raw=s)
    m = _MIN_RE.search(s)
    if m:
        return ExtractedExperience(min_years=float(m.group(1)), raw=s)
    m = _PLUS_RE.search(s)
    if m:
        return ExtractedExperience(min_years=float(m.group(1)), raw=s)
    m = _SINGLE_RE.search(s)
    if m:
        return ExtractedExperience(min_years=float(m.group(1)), max_years=float(m.group(1)), raw=s)
    return ExtractedExperience(ambiguous=True, raw=s)


@dataclass(frozen=True)
class ExperienceVerdict:
    eligible: bool
    reason: str


def experience_eligible(
    extracted: ExtractedExperience,
    policy: ExperiencePolicy,
    *,
    overall_evidence_strong: bool = False,
) -> ExperienceVerdict:
    """Eligibility is decided by the *actual mandatory* experience, never by a
    job title. A hard mandatory minimum at/above ``hard_reject_min_years``
    normally fails; a suitable three-year role may survive when evidence is
    strong."""
    if extracted.min_years is None:
        return ExperienceVerdict(True, "no hard minimum stated")
    if extracted.min_years >= policy.hard_reject_min_years:
        return ExperienceVerdict(False, f"hard mandatory {extracted.min_years:g}+ minimum")
    if extracted.min_years == 3 and not policy.allow_three_year_when_strong:
        return ExperienceVerdict(False, "3-year minimum and strong-evidence allowance disabled")
    if extracted.min_years == 3 and not overall_evidence_strong:
        return ExperienceVerdict(True, "3-year minimum accepted (review evidence strength)")
    return ExperienceVerdict(True, "mandatory minimum within range")


# ---------------------------------------------------------------------------
# Geography / international eligibility (build spec 10)
# ---------------------------------------------------------------------------
class IntlEligibility(str):
    pass


ELIGIBLE_FROM_INDIA = "ELIGIBLE_FROM_INDIA"
ELIGIBLE_WITH_EVIDENCE = "ELIGIBLE_WITH_EVIDENCE"
UNCLEAR = "UNCLEAR"
NOT_ELIGIBLE = "NOT_ELIGIBLE"


def international_eligibility(text: Optional[str], policy: GeographyPolicy) -> str:
    """Classify India eligibility from explicit wording. "Remote" ALONE is
    never worldwide eligibility (build spec 10)."""
    if not text:
        return UNCLEAR
    low = " ".join(str(text).strip().lower().split())
    for signal in policy.international_eligibility_signals:
        if signal.lower() in low:
            return ELIGIBLE_WITH_EVIDENCE
    # A named India location mentioned => eligible from India.
    if _mentions_india_location(low, policy):
        return ELIGIBLE_FROM_INDIA
    # Country-scoped remote excludes India unless stated.
    if re.search(r"\b(us|u\.s\.|usa|eu|uk|emea|canada|germany)\b.{0,12}remote", low) or re.search(
        r"remote.{0,12}\b(us|u\.s\.|usa|eu|uk|emea|canada|germany)\b", low
    ):
        return NOT_ELIGIBLE
    if "remote" in low:
        return UNCLEAR  # remote alone is not worldwide
    return UNCLEAR


def _mentions_india_location(low: str, policy: GeographyPolicy) -> bool:
    if re.search(r"\bindia\b", low):
        return True
    for loc in policy.locations:
        names = [loc.canonical.lower()] + [a.lower() for a in loc.aliases]
        for name in names:
            if re.search(r"\b" + re.escape(name) + r"\b", low):
                return True
    return False


# ---------------------------------------------------------------------------
# Exclusions (build spec 12)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExclusionVerdict:
    excluded: bool
    family: Optional[str] = None
    reason: str = ""


def evaluate_exclusions(text: Optional[str], policy: ExclusionPolicy) -> ExclusionVerdict:
    """Exclude only clearly irrelevant role families. A development role that
    merely mentions on-call/support is NOT excluded by that alone."""
    if not text:
        return ExclusionVerdict(False)
    low = " ".join(str(text).strip().lower().split())
    for fam in policy.families:
        for term in fam.terms:
            if term.lower() in low:
                return ExclusionVerdict(True, family=fam.key, reason=f"matched {term!r}")
    return ExclusionVerdict(False)


# ---------------------------------------------------------------------------
# Freshness + closure (build spec 18)
# ---------------------------------------------------------------------------
def freshness_band(
    posted_date: Optional[datetime.date],
    *,
    today: Optional[datetime.date] = None,
    has_live_official_page: bool = False,
) -> str:
    """Compute a freshness band. A missing posted date is NEVER "stale": a
    live official page with no reliable date is ``LIVE_DATE_UNKNOWN``. Crawl/
    cache/first_seen dates must NOT be passed here as the posted date."""
    if posted_date is None:
        return "LIVE_DATE_UNKNOWN" if has_live_official_page else "LIVE_DATE_UNKNOWN"
    today = today or datetime.date.today()
    age = (today - posted_date).days
    if age < 0:
        return "LIVE_DATE_UNKNOWN"
    if age <= 7:
        return "0-7 days"
    if age <= 14:
        return "8-14 days"
    if age <= 30:
        return "15-30 days"
    if age <= 45:
        return "31-45 days exceptional"
    return "STALE"


def is_closed(evidence_text: Optional[str], policy: VerificationPolicy) -> bool:
    """CLOSED requires POSITIVE evidence. Blocked/login/CAPTCHA/missing-date/
    failed-apply are NEVER closure evidence (build spec 18)."""
    if not evidence_text:
        return False
    low = " ".join(str(evidence_text).strip().lower().split())
    # Non-closure conditions never close a role.
    for cond in policy.non_closure_conditions:
        if cond.lower() in low:
            return False
    for term in policy.closure_evidence_terms:
        if term.lower() in low:
            return True
    return False


# ---------------------------------------------------------------------------
# Official verification guard (build spec 18)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VerificationInput:
    page_kind: str            # "specific_role_page" | "generic_landing" | "search_results" | "portal"
    identity_aligned: bool    # company/title/location/(reqid) align
    current_content: bool     # the role appears current
    positive_closure: bool = False
    apply_path_confirmable: bool = True


def classify_verification(vi: VerificationInput) -> VerificationLevel:
    """Deterministic verification guard. A generic landing/search page can
    NEVER be VERIFIED_OFFICIAL. VERIFIED_OFFICIAL does NOT require completing
    a final Apply submission — only a specific current aligned official page
    with no positive closure signal."""
    if vi.positive_closure:
        # Closure is a lifecycle fact; verification of a closed role is not official-live.
        return VerificationLevel.SUSPICIOUS_REJECTED if vi.page_kind == "portal" else VerificationLevel.MANUAL_VERIFICATION
    if vi.page_kind == "portal":
        return VerificationLevel.PORTAL_CURRENT_LEAD
    if vi.page_kind in ("generic_landing", "search_results"):
        # discovery evidence, not specific official-role verification
        return VerificationLevel.MANUAL_VERIFICATION
    if vi.page_kind == "specific_role_page" and vi.identity_aligned and vi.current_content:
        if vi.apply_path_confirmable:
            return VerificationLevel.VERIFIED_OFFICIAL
        return VerificationLevel.OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED
    return VerificationLevel.MANUAL_VERIFICATION


__all__ = [
    "ExtractedExperience",
    "extract_experience",
    "ExperienceVerdict",
    "experience_eligible",
    "IntlEligibility",
    "ELIGIBLE_FROM_INDIA",
    "ELIGIBLE_WITH_EVIDENCE",
    "UNCLEAR",
    "NOT_ELIGIBLE",
    "international_eligibility",
    "ExclusionVerdict",
    "evaluate_exclusions",
    "freshness_band",
    "is_closed",
    "VerificationInput",
    "classify_verification",
]
