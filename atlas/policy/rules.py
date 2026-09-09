"""Deterministic policy rules (Phase 1B, build spec 11/12/18/19).

Pure, testable functions implementing the deterministic gates that must NOT
depend on an LLM: experience extraction/eligibility, geography/international
eligibility, exclusion families, freshness bands, closure detection, and the
official-verification guard. LLM reasoning stays optional and typed for
genuinely ambiguous semantic judgments only.
"""

from __future__ import annotations

import datetime
import html
import re
import unicodedata
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


# Explicit separator alternation (Phase 1B.1, build spec 18) — NOT the loose
# ``[-–to]+`` character class, which matched stray letters/dashes (e.g. "3o5").
_SEP = r"(?:\s*(?:-|–|—|to|through|until)\s*)"
_YEARS = r"(?:years|yrs|yr|year)"
_RANGE_RE = re.compile(rf"(\d+(?:\.\d+)?){_SEP}(\d+(?:\.\d+)?)\s*\+?\s*{_YEARS}", re.I)
_PLUS_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*\+\s*{_YEARS}", re.I)
_MIN_RE = re.compile(rf"(?:minimum|min\.?|at\s+least|no\s+less\s+than)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*\+?\s*{_YEARS}", re.I)
# Maximum / up-to wording is a ceiling, NEVER a mandatory minimum.
_MAX_RE = re.compile(rf"(?:up\s*to|no\s+more\s+than|at\s+most|maximum|max\.?)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*{_YEARS}", re.I)
# Preferred / desirable wording is NOT a hard mandatory minimum. The gap must
# not cross another number so the figure nearest the qualifier is captured.
_PREF_BEFORE_RE = re.compile(
    rf"(?:preferred|preferably|desirable|desired|ideally|nice\s+to\s+have|a\s+plus)\b[^.0-9]{{0,40}}?(\d+(?:\.\d+)?)\s*\+?\s*{_YEARS}",
    re.I,
)
_PREF_AFTER_RE = re.compile(
    rf"(\d+(?:\.\d+)?)\s*\+?\s*{_YEARS}[^.0-9]{{0,20}}?(?:preferred|desirable|a\s+plus|nice\s+to\s+have|ideally)",
    re.I,
)
_SINGLE_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*{_YEARS}", re.I)
# A preferred/desirable figure written as "N+ preferred" (no "years" token) — a
# common Workday shape (e.g. "2+ years required, 5+ preferred"). It must be
# captured as PREFERRED, never as a mandatory minimum.
_PREF_PLUS_AFTER_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*\+[^.0-9]{0,18}?(?:preferred|desirable|desired|a\s+plus|nice\s+to\s+have|ideally|good\s+to\s+have|bonus)",
    re.I,
)

# Any dash-like code point normalized to ASCII hyphen-minus so "8–12+" parses.
_DASHES_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2043\uFE58\uFE63\uFF0D]")


def _normalize_experience_text(s: str) -> str:
    """Canonicalize experience text BEFORE number extraction: decode HTML
    entities (numeric ``&#43;`` included), NFKC-normalize, and fold dash/
    non-breaking-space variants. This is the defensive fix for audit root
    cause 5 — a hard ``4+`` written ``4&#43;`` must be seen as ``4+``."""
    s = html.unescape(s)
    s = unicodedata.normalize("NFKC", s)
    s = _DASHES_RE.sub("-", s)
    s = s.replace("\u00a0", " ")
    return s


def extract_experience(text: Optional[str]) -> ExtractedExperience:
    """Deterministically extract an experience requirement, distinguishing a
    hard mandatory minimum, a maximum/up-to ceiling, and a preferred/desirable
    figure SEPARATELY (build spec 18). NEVER invents a range from a title —
    text with no explicit number is ``ambiguous``; a preferred/max-only figure
    does NOT establish a mandatory minimum."""
    if not text or not str(text).strip():
        return ExtractedExperience(ambiguous=True, raw=text or "")
    s = _normalize_experience_text(str(text))

    preferred_years: Optional[float] = None
    pm = _PREF_BEFORE_RE.search(s) or _PREF_AFTER_RE.search(s) or _PREF_PLUS_AFTER_RE.search(s)
    if pm:
        preferred_years = float(pm.group(1))

    max_only: Optional[float] = None
    xm = _MAX_RE.search(s)
    if xm:
        max_only = float(xm.group(1))

    # Mandatory band: explicit range wins, then explicit "minimum", then "N+".
    m = _RANGE_RE.search(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return ExtractedExperience(
            min_years=min(lo, hi), max_years=max(lo, hi), preferred_years=preferred_years, raw=s
        )
    m = _MIN_RE.search(s)
    if m:
        return ExtractedExperience(
            min_years=float(m.group(1)), max_years=max_only, preferred_years=preferred_years, raw=s
        )
    m = _PLUS_RE.search(s)
    if m:
        return ExtractedExperience(
            min_years=float(m.group(1)), max_years=max_only, preferred_years=preferred_years, raw=s
        )

    # No mandatory minimum stated. A preferred/max-only figure is recorded but
    # does NOT become a mandatory minimum (so it can never auto-reject).
    if preferred_years is not None or max_only is not None:
        return ExtractedExperience(
            min_years=None, max_years=max_only, preferred_years=preferred_years, raw=s
        )

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


# Explicitly negated sponsorship (either word order) — generic sponsorship
# wording must NOT grant eligibility when it is negated (build spec 18).
_SPONSOR_NEG_RE = re.compile(
    r"(?:\b(?:no|not|without|cannot|can'?t|unable\s+to|does\s+not|do\s+not|won'?t|isn'?t|aren'?t|non)\b"
    r"[^.\n]{0,40}?\b(?:sponsor\w*|visa\w*)\b)"
    r"|(?:\b(?:sponsor\w*|visa\w*)\b[^.\n]{0,40}?"
    r"\b(?:not\s+available|unavailable|not\s+offered|not\s+provided|not\s+possible|not\s+supported)\b)",
    re.I,
)
_COUNTRY_REMOTE_RE = re.compile(
    r"\b(us|u\.s\.|usa|eu|uk|emea|canada|germany|australia|singapore)\b.{0,12}remote"
    r"|remote.{0,12}\b(us|u\.s\.|usa|eu|uk|emea|canada|germany|australia|singapore)\b(?![^.]*\bindia\b)",
    re.I,
)


def international_eligibility(text: Optional[str], policy: GeographyPolicy) -> str:
    """Classify India eligibility from explicit wording. "Remote" ALONE is
    never worldwide eligibility, generic sponsorship wording that is explicitly
    negated does NOT grant eligibility, and a country-restricted remote role
    excludes India unless India is named (build spec 10/18)."""
    if not text:
        return UNCLEAR
    low = " ".join(str(text).strip().lower().split())
    negated_sponsorship = bool(_SPONSOR_NEG_RE.search(low))

    # Positive worldwide/sponsorship wording => eligible, UNLESS negated.
    if not negated_sponsorship:
        for signal in policy.international_eligibility_signals:
            if signal.lower() in low:
                return ELIGIBLE_WITH_EVIDENCE
    # A named India location => eligible from India (a domestic role needs no
    # sponsorship, so this survives even a general "no sponsorship" clause).
    if _mentions_india_location(low, policy):
        return ELIGIBLE_FROM_INDIA
    # Country-scoped remote excludes India unless India is stated.
    if _COUNTRY_REMOTE_RE.search(low):
        return NOT_ELIGIBLE
    # Explicit sponsorship negation with no India anchor => not eligible.
    if negated_sponsorship:
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
# Freshness bands for an absent/contradictory employer posted date. These are
# DISTINCT states (P0-10): a confirmed live page with no date is LIVE_DATE_
# UNKNOWN; an unverified lead with no date is only DATE_UNKNOWN (never "live").
FRESHNESS_LIVE_DATE_UNKNOWN = "LIVE_DATE_UNKNOWN"
FRESHNESS_DATE_UNKNOWN = "DATE_UNKNOWN"
FRESHNESS_DATA_CONFLICT = "DATA_CONFLICT"


def freshness_band(
    posted_date: Optional[datetime.date],
    *,
    today: Optional[datetime.date] = None,
    has_live_official_page: bool = False,
) -> str:
    """Compute a freshness band. A missing employer posted date is NEVER
    "stale", but it is only ``LIVE_DATE_UNKNOWN`` when a live official page is
    CONFIRMED; without a confirmed live page an absent date is merely
    ``DATE_UNKNOWN`` and must NOT be labeled live (build spec 18, P0-10). A
    future/contradictory date is a ``DATA_CONFLICT`` for manual review. Crawl/
    cache/first_seen dates must NOT be passed here as the posted date."""
    if posted_date is None:
        return FRESHNESS_LIVE_DATE_UNKNOWN if has_live_official_page else FRESHNESS_DATE_UNKNOWN
    today = today or datetime.date.today()
    age = (today - posted_date).days
    if age < 0:
        return FRESHNESS_DATA_CONFLICT
    if age <= 7:
        return "0-7 days"
    if age <= 14:
        return "8-14 days"
    if age <= 30:
        return "15-30 days"
    if age <= 45:
        return "31-45 days exceptional"
    return "STALE"


@dataclass(frozen=True)
class ClosureVerdict:
    """Structured closure verdict. Positive closure evidence dominates access-
    state metadata; both observations are retained for audit (build spec 18)."""

    closed: bool
    reason: str = ""
    closure_signal: Optional[str] = None
    access_blocked: bool = False


def classify_closure(
    policy: VerificationPolicy,
    *,
    evidence_texts: Optional[list[str]] = None,
    deadline: Optional[datetime.date] = None,
    today: Optional[datetime.date] = None,
) -> ClosureVerdict:
    """Structured closure classification over independent evidence signals
    (never one concatenated string). POSITIVE explicit closure evidence (a
    closed banner, "no longer accepting", a reliably-parsed expired employer
    deadline) dominates access-state metadata; a login/CAPTCHA/access block
    ALONE never closes a role, but a block combined with an explicit closed
    banner is still CLOSED with both observations retained."""
    texts = [t for t in (evidence_texts or []) if t]
    access_blocked = False
    closure_signal: Optional[str] = None
    for raw in texts:
        low = " ".join(str(raw).strip().lower().split())
        for term in policy.closure_evidence_terms:
            if term.lower() in low:
                closure_signal = term
                break
        if closure_signal is None:
            for cond in policy.non_closure_conditions:
                if cond.lower() in low:
                    access_blocked = True
                    break
    if closure_signal is not None:
        return ClosureVerdict(True, reason=f"closure evidence {closure_signal!r}",
                              closure_signal=closure_signal, access_blocked=access_blocked)
    if deadline is not None:
        today = today or datetime.date.today()
        if deadline < today:
            return ClosureVerdict(True, reason="employer deadline expired", access_blocked=access_blocked)
    return ClosureVerdict(False, reason="access blocked" if access_blocked else "no closure evidence",
                          access_blocked=access_blocked)


def is_closed(evidence_text: Optional[str], policy: VerificationPolicy) -> bool:
    """CLOSED requires POSITIVE evidence, which DOMINATES access-state metadata
    (build spec 18): a closed banner still closes even when a block/login/
    CAPTCHA notice is present. Blocked/login/CAPTCHA/missing-date/failed-apply
    ALONE are never closure evidence."""
    if not evidence_text:
        return False
    low = " ".join(str(evidence_text).strip().lower().split())
    # Positive closure evidence is checked FIRST so it dominates any access
    # limitation mentioned in the same text (fixes the precedence bug).
    for term in policy.closure_evidence_terms:
        if term.lower() in low:
            return True
    for cond in policy.non_closure_conditions:
        if cond.lower() in low:
            return False
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
    NEVER be VERIFIED_OFFICIAL. VERIFIED_OFFICIAL requires a specific, employer-
    controlled, identity-aligned role page whose CURRENT content is visible and
    that carries no positive closure signal — a confirmable Apply control is
    CORROBORATION, never a mandatory criterion (build spec 17, P0-9). The
    ``OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED`` level is used ONLY when the
    inability to confirm the route leaves genuine uncertainty about the role's
    currentness/identity — not merely because a button could not be clicked."""
    if vi.positive_closure:
        # Closure is a lifecycle fact; verification of a closed role is not official-live.
        return VerificationLevel.SUSPICIOUS_REJECTED if vi.page_kind == "portal" else VerificationLevel.MANUAL_VERIFICATION
    if vi.page_kind == "portal":
        return VerificationLevel.PORTAL_CURRENT_LEAD
    if vi.page_kind in ("generic_landing", "search_results"):
        # discovery evidence, not specific official-role verification
        return VerificationLevel.MANUAL_VERIFICATION
    if vi.page_kind == "specific_role_page" and vi.identity_aligned:
        if vi.current_content:
            # Specific, aligned, current official role page. The Apply route is
            # corroboration only and is NOT required for VERIFIED_OFFICIAL.
            return VerificationLevel.VERIFIED_OFFICIAL
        # Currentness is not directly visible. The Apply route is the only
        # corroborator: when confirmable it corroborates currentness; when it
        # cannot be confirmed, genuine uncertainty remains.
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
    "FRESHNESS_LIVE_DATE_UNKNOWN",
    "FRESHNESS_DATE_UNKNOWN",
    "FRESHNESS_DATA_CONFLICT",
    "freshness_band",
    "ClosureVerdict",
    "classify_closure",
    "is_closed",
    "VerificationInput",
    "classify_verification",
]
