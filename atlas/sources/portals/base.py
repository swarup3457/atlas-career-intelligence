"""Shared helpers for read-only portal discovery adapters (Phase 1D §7/§8).

Dependency-free helpers common to the LinkedIn and Naukri READ-ONLY discovery
adapters: bounded limits, Indian-city normalization (Bengaluru/Bangalore,
Hyderabad), conservative posted-date provenance (a portal listed date is NEVER
an employer-authoritative posted date), and salary/experience text preservation.

No adapter here signs in, applies, bypasses a challenge, or uses stealth. Portal
output is always a :class:`atlas.sources.models.DiscoveryResult` that the market
layer normalizes into an append-only :class:`atlas.sources.portals.models.PortalJobLead`
— never an official verified job.
"""

from __future__ import annotations

import re
from typing import Optional

# Hard bounds — a portal search is never a broad crawl (build spec 7/8/11).
MAX_PORTAL_PAGES = 2
MAX_PORTAL_CARDS_PER_PAGE = 25
MAX_PORTAL_CARDS = MAX_PORTAL_PAGES * MAX_PORTAL_CARDS_PER_PAGE

# Portal listed date provenance marker: a portal's posted/updated text is
# portal-reported, not an employer-authoritative POSTED date, so it must never
# be treated as EMPLOYER_POSTED_AT by the freshness authority.
PORTAL_LISTED_DATE = "PORTAL_LISTED_DATE"

# City aliases relevant to the Indian market. Normalization is bidirectional-
# aware: a query for "Bangalore" and a posting located in "Bengaluru" match.
_CITY_ALIASES: dict[str, frozenset[str]] = {
    "bengaluru": frozenset({"bengaluru", "bangalore", "bengaluru urban", "bangalore urban"}),
    "hyderabad": frozenset({"hyderabad", "secunderabad", "hyderabad / secunderabad"}),
    "delhi": frozenset({"delhi", "new delhi", "delhi ncr", "ncr"}),
    "gurugram": frozenset({"gurugram", "gurgaon"}),
    "mumbai": frozenset({"mumbai", "bombay", "navi mumbai"}),
    "pune": frozenset({"pune", "poona"}),
    "chennai": frozenset({"chennai", "madras"}),
    "noida": frozenset({"noida", "greater noida"}),
}

_CANONICAL_CITY: dict[str, str] = {}
for _canon, _aliases in _CITY_ALIASES.items():
    for _a in _aliases:
        _CANONICAL_CITY[_a] = _canon


def canonical_city(location: Optional[str]) -> Optional[str]:
    """Return the canonical Indian-city token for a location string, matching on
    the FIRST city token found (e.g. "Bengaluru, Karnataka, India" -> "bengaluru";
    "Bangalore" -> "bengaluru"). Returns ``None`` when no known city matches."""
    if not location:
        return None
    low = location.lower()
    for alias, canon in sorted(_CANONICAL_CITY.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + re.escape(alias) + r"\b", low):
            return canon
    return None


def city_matches(query_location: Optional[str], posting_location: Optional[str]) -> bool:
    """True when the posting's location matches the requested location under
    Indian-city alias normalization. A missing query location matches anything;
    a missing posting location does NOT exclude (never a false negative)."""
    if not query_location:
        return True
    if not posting_location:
        return True
    q = canonical_city(query_location)
    p = canonical_city(posting_location)
    if q is None:
        # Fall back to a case-insensitive substring test on the raw tokens.
        return query_location.strip().lower() in posting_location.lower()
    return q == p


_WS_RE = re.compile(r"\s+")


def clean_text(value: Optional[str]) -> Optional[str]:
    """Collapse whitespace / strip; return None for empty."""
    if not value:
        return None
    import html as _html

    text = _WS_RE.sub(" ", _html.unescape(value)).strip()
    return text or None


_LPA_RE = re.compile(r"(₹|rs\.?|inr|lpa|lakh|lac|per annum|p\.a\.)", re.IGNORECASE)


def looks_like_salary(text: Optional[str]) -> bool:
    """Heuristic: a string that plausibly carries INR/LPA salary information."""
    return bool(text) and bool(_LPA_RE.search(text or ""))


_EXP_RE = re.compile(r"\b(\d+)\s*(?:-|to)\s*(\d+)\s*(?:yr|year|yrs|years)\b", re.IGNORECASE)
_EXP_SINGLE_RE = re.compile(r"\b(\d+)\+?\s*(?:yr|year|yrs|years)\b", re.IGNORECASE)


def looks_like_experience(text: Optional[str]) -> bool:
    return bool(text) and bool(_EXP_RE.search(text or "") or _EXP_SINGLE_RE.search(text or ""))


__all__ = [
    "MAX_PORTAL_PAGES",
    "MAX_PORTAL_CARDS_PER_PAGE",
    "MAX_PORTAL_CARDS",
    "PORTAL_LISTED_DATE",
    "canonical_city",
    "city_matches",
    "clean_text",
    "looks_like_salary",
    "looks_like_experience",
]
