"""India-only geography hard gate (V3 pilot correction; audit root causes 3.2 / 3.3).

The V2 pipeline decided geography from eligibility/sponsorship *text* and ignored the
job's structured ``location``, and awarded location points from the *candidate's*
location group rather than the *job's* location. This module introduces a typed
:class:`JobGeographyDecision` that evaluates the STRUCTURED JOB LOCATION FIRST, then
remote scope, then eligibility/sponsorship text, then official country metadata.

Hard invariants (deterministic, non-bypassable):

* The candidate's own location can NEVER prove a job is in India.
* ``UNKNOWN`` / ``N/A`` is not India.
* A foreign role cannot enter the main output unless explicit India eligibility exists
  (which downgrades it to an :data:`INTERNATIONAL_SPONSORED_LEAD` side artifact, never a
  main-output row).
* Only the four India-positive decisions may enter the main ``All_Jobs`` output.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from atlas.policy.models import GeographyPolicy
from atlas.policy.rules import (
    ELIGIBLE_FROM_INDIA,
    ELIGIBLE_WITH_EVIDENCE,
    NOT_ELIGIBLE,
    international_eligibility,
)

__all__ = [
    "GeoDecision",
    "RemoteScope",
    "JobGeographyDecision",
    "MAIN_OUTPUT_ALLOWED",
    "classify_job_geography",
    "DEFAULT_FOREIGN_TOKENS",
]


class GeoDecision(str, enum.Enum):
    INDIA_PRIMARY = "INDIA_PRIMARY"
    INDIA_SECONDARY = "INDIA_SECONDARY"
    REMOTE_INDIA = "REMOTE_INDIA"
    INDIA_WIDE = "INDIA_WIDE"
    FOREIGN_EXCLUDED = "FOREIGN_EXCLUDED"
    UNKNOWN_LOCATION = "UNKNOWN_LOCATION"
    INTERNATIONAL_SPONSORED_LEAD = "INTERNATIONAL_SPONSORED_LEAD"


class RemoteScope(str, enum.Enum):
    NONE = "NONE"
    REMOTE_INDIA = "REMOTE_INDIA"
    REMOTE_FOREIGN = "REMOTE_FOREIGN"
    REMOTE_UNSCOPED = "REMOTE_UNSCOPED"


#: Only these four may enter the pilot's main ``All_Jobs`` sheet.
MAIN_OUTPUT_ALLOWED = frozenset(
    {
        GeoDecision.INDIA_PRIMARY.value,
        GeoDecision.INDIA_SECONDARY.value,
        GeoDecision.REMOTE_INDIA.value,
        GeoDecision.INDIA_WIDE.value,
    }
)

# Built-in foreign tokens so the gate is correct even with only the geography policy.
# Word-boundary matched (case-insensitive). The pilot config's ``forbidden_main_output``
# list is merged on top of this at call time.
DEFAULT_FOREIGN_TOKENS: tuple[str, ...] = (
    # countries / regions
    "united states", "u.s.a", "u.s.", "usa", "america", "canada",
    "united kingdom", "u.k.", "england", "scotland", "ireland", "britain",
    "germany", "france", "netherlands", "spain", "italy", "sweden", "switzerland",
    "poland", "romania", "portugal", "belgium", "austria", "denmark", "norway", "finland",
    "europe", "emea", "apac", "latam", "mena",
    "singapore", "australia", "new zealand", "japan", "china", "hong kong",
    "united arab emirates", "u.a.e", "uae", "dubai", "abu dhabi", "qatar", "saudi arabia",
    "philippines", "malaysia", "indonesia", "vietnam", "thailand", "south korea",
    "mexico", "brazil", "argentina", "colombia", "costa rica",
    # US states
    "california", "texas", "washington state", "virginia", "massachusetts",
    "georgia", "illinois", "florida", "colorado", "oregon", "arizona", "north carolina",
    # foreign cities
    "san francisco", "new york", "seattle", "austin", "boston", "chicago", "atlanta",
    "dallas", "mountain view", "sunnyvale", "san jose", "los angeles", "denver",
    "palo alto", "redmond", "cupertino", "toronto", "vancouver", "montreal", "ottawa",
    "london", "manchester", "dublin", "berlin", "munich", "amsterdam", "paris",
    "sydney", "melbourne", "tokyo", "manila", "kuala lumpur", "warsaw", "bucharest",
    "tel aviv", "zurich", "stockholm", "barcelona", "madrid",
)

# US/UK/EU standalone abbreviations need explicit word-boundary handling.
_ABBREV_FOREIGN = ("us", "uk", "eu")

_REMOTE_RE = re.compile(r"\b(remote|work\s*from\s*home|wfh|anywhere|distributed)\b", re.I)
_INDIA_RE = re.compile(r"\bindia\b", re.I)


def _norm(text: Optional[str]) -> str:
    if not text:
        return ""
    return " ".join(str(text).strip().lower().split())


def _word_present(token: str, low: str) -> bool:
    token = token.strip().lower()
    if not token:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", low) is not None


def _india_location_index(policy: GeographyPolicy) -> list[tuple[str, str]]:
    """Return (name, group) India location names + aliases from the policy."""
    index: list[tuple[str, str]] = []
    for loc in policy.locations:
        index.append((loc.canonical.lower(), loc.group))
        for alias in loc.aliases:
            index.append((alias.lower(), loc.group))
    return index


def _find_india_city(low: str, index: Sequence[tuple[str, str]]) -> Optional[tuple[str, str]]:
    # longest names first so "remote india" / "bengaluru/bangalore" match before "india"
    for name, group in sorted(index, key=lambda x: -len(x[0])):
        if _word_present(name, low):
            return name, group
    return None


def _find_foreign(low: str, forbidden: Sequence[str]) -> Optional[str]:
    for token in sorted(forbidden, key=lambda t: -len(t)):
        if _word_present(token, low):
            return token
    for ab in _ABBREV_FOREIGN:
        # abbreviations only when not part of a larger word and India is not the anchor
        if re.search(r"(?<![a-z0-9])" + ab + r"(?![a-z0-9])", low):
            return ab
    return None


@dataclass(frozen=True)
class JobGeographyDecision:
    decision: str
    india_eligible: bool
    location_evidence: str
    remote_scope: str = RemoteScope.NONE.value
    country: str = ""
    reasons: tuple[str, ...] = ()

    @property
    def allowed_in_main_output(self) -> bool:
        return self.decision in MAIN_OUTPUT_ALLOWED

    @property
    def is_foreign_lead(self) -> bool:
        return self.decision == GeoDecision.INTERNATIONAL_SPONSORED_LEAD.value


def classify_job_geography(
    location: Optional[str],
    *,
    work_mode: str = "",
    eligibility_text: str = "",
    description: str = "",
    geography: GeographyPolicy,
    forbidden_tokens: Sequence[str] = (),
) -> JobGeographyDecision:
    """Classify a job's geography from the JOB's own evidence.

    Precedence: structured job location -> remote scope -> eligibility/sponsorship text
    -> country metadata. The candidate's location is intentionally not an input.
    """
    loc = _norm(location)
    txt = _norm(f"{eligibility_text} {description}")
    index = _india_location_index(geography)
    forbidden = tuple(dict.fromkeys(list(DEFAULT_FOREIGN_TOKENS) + [t.lower() for t in forbidden_tokens]))

    remote_flag = bool(_REMOTE_RE.search(loc)) or _norm(work_mode) == "remote" or "remote" in _norm(work_mode)

    india_in_loc = _find_india_city(loc, index)
    india_word_loc = bool(_INDIA_RE.search(loc))
    foreign_in_loc = _find_foreign(loc, forbidden)

    def _india_text_signal() -> bool:
        if _INDIA_RE.search(txt) or _find_india_city(txt, index):
            return True
        intl = international_eligibility(eligibility_text or description, geography)
        return intl in (ELIGIBLE_FROM_INDIA, ELIGIBLE_WITH_EVIDENCE)

    # --- 1) explicit India signal in the job's own structured location -------------
    if india_in_loc and not (foreign_in_loc and not india_word_loc):
        name, group = india_in_loc
        if name == "remote india" or (remote_flag):
            return JobGeographyDecision(
                GeoDecision.REMOTE_INDIA.value, True, location or "",
                RemoteScope.REMOTE_INDIA.value, "India",
                (f"job location names India hub {name!r} (remote)",),
            )
        if group == "PRIMARY":
            return JobGeographyDecision(
                GeoDecision.INDIA_PRIMARY.value, True, location or "", RemoteScope.NONE.value,
                "India", (f"job location is primary India hub {name!r}",),
            )
        return JobGeographyDecision(
            GeoDecision.INDIA_SECONDARY.value, True, location or "", RemoteScope.NONE.value,
            "India", (f"job location is India hub {name!r} (group {group})",),
        )

    # bare "India" in the location (no specific city) --------------------------------
    if india_word_loc and not foreign_in_loc:
        if remote_flag:
            return JobGeographyDecision(
                GeoDecision.REMOTE_INDIA.value, True, location or "",
                RemoteScope.REMOTE_INDIA.value, "India", ("job location is remote India",),
            )
        return JobGeographyDecision(
            GeoDecision.INDIA_WIDE.value, True, location or "", RemoteScope.NONE.value,
            "India", ("job location is India-wide",),
        )

    # --- 2) explicit foreign token in the job's location ---------------------------
    if foreign_in_loc:
        scope = RemoteScope.REMOTE_FOREIGN.value if remote_flag else RemoteScope.NONE.value
        if _india_text_signal():
            return JobGeographyDecision(
                GeoDecision.INTERNATIONAL_SPONSORED_LEAD.value, False, location or "",
                scope, foreign_in_loc,
                (f"foreign location {foreign_in_loc!r} but posting states India eligibility",),
            )
        return JobGeographyDecision(
            GeoDecision.FOREIGN_EXCLUDED.value, False, location or "", scope, foreign_in_loc,
            (f"foreign location {foreign_in_loc!r}; no India eligibility",),
        )

    # --- 3) location has no country signal (empty / unknown / remote-unscoped) ------
    intl = international_eligibility(eligibility_text or description, geography)
    india_txt = bool(_INDIA_RE.search(txt) or _find_india_city(txt, index))
    foreign_txt = _find_foreign(txt, forbidden)

    if remote_flag:
        if india_txt or intl in (ELIGIBLE_FROM_INDIA, ELIGIBLE_WITH_EVIDENCE):
            return JobGeographyDecision(
                GeoDecision.REMOTE_INDIA.value, True, location or "remote",
                RemoteScope.REMOTE_INDIA.value, "India",
                ("remote role with explicit India eligibility",),
            )
        if foreign_txt or intl == NOT_ELIGIBLE:
            return JobGeographyDecision(
                GeoDecision.FOREIGN_EXCLUDED.value, False, location or "remote",
                RemoteScope.REMOTE_FOREIGN.value, foreign_txt or "",
                ("remote role scoped to a non-India country",),
            )
        return JobGeographyDecision(
            GeoDecision.UNKNOWN_LOCATION.value, False, location or "remote",
            RemoteScope.REMOTE_UNSCOPED.value, "",
            ("remote role with no India scope; remote alone is not India",),
        )

    # non-remote, location gives no country signal
    if india_txt or intl == ELIGIBLE_FROM_INDIA:
        return JobGeographyDecision(
            GeoDecision.INDIA_WIDE.value, True, location or "", RemoteScope.NONE.value,
            "India", ("posting text explicitly names India eligibility",),
        )
    if intl == ELIGIBLE_WITH_EVIDENCE and india_txt:
        return JobGeographyDecision(
            GeoDecision.INDIA_WIDE.value, True, location or "", RemoteScope.NONE.value,
            "India", ("worldwide-including-India eligibility",),
        )
    if foreign_txt or intl == NOT_ELIGIBLE:
        return JobGeographyDecision(
            GeoDecision.FOREIGN_EXCLUDED.value, False, location or "", RemoteScope.NONE.value,
            foreign_txt or "", ("posting evidence indicates a non-India country",),
        )
    return JobGeographyDecision(
        GeoDecision.UNKNOWN_LOCATION.value, False, location or "", RemoteScope.NONE.value,
        "", ("no usable India location evidence",),
    )
