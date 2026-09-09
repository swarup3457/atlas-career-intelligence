"""Explicit V4 company-search status model + challenge/auth classifier (prompt s.10, s.3.4).

The V3 pilot treated ``UNSUPPORTED_SITE`` / ``AUTH_REQUIRED`` / ``ACCESS_LIMITED``
as terminal even when the underlying cause was an *internal* browser-tool crash,
so all ten companies counted as terminal while only Fiserv produced job details.

V4 splits the outcome space into three disjoint classes:

* **search success** — the site was genuinely searched across the five lanes;
* **external blockers** — a truthful, terminal, *external* condition (access
  limited / confirmed auth wall / no resolvable official source);
* **internal / retryable failures** — a browser/SDK/async/observation/parser
  fault or an incomplete lane checklist. These are NEVER terminal and must retry
  the SAME company after recovery.

Only *search success* statuses count as "genuinely searched". This is pure,
deterministic policy — no LLM, no network.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "CompanySearchStatus",
    "SEARCH_SUCCESS",
    "EXTERNAL_BLOCKER",
    "INTERNAL_RETRYABLE",
    "TERMINAL",
    "GENUINELY_SEARCHED",
    "is_genuinely_searched",
    "is_terminal",
    "is_internal_retryable",
    "classify_blocker",
    "looks_like_login_wall",
    "map_legacy_status",
]


class CompanySearchStatus(str, enum.Enum):
    # --- search success (genuinely searched) ---
    SEARCHED_COMPLETE_WITH_MATCHES = "SEARCHED_COMPLETE_WITH_MATCHES"
    SEARCHED_COMPLETE_NO_MATCHES = "SEARCHED_COMPLETE_NO_MATCHES"
    # --- external blockers (truthful, terminal, not "searched") ---
    ACCESS_LIMITED_EXTERNAL = "ACCESS_LIMITED_EXTERNAL"
    AUTH_REQUIRED_CONFIRMED = "AUTH_REQUIRED_CONFIRMED"
    OFFICIAL_SOURCE_UNRESOLVED = "OFFICIAL_SOURCE_UNRESOLVED"
    # --- internal / retryable failures (never terminal) ---
    BROWSER_TOOL_ERROR = "BROWSER_TOOL_ERROR"
    SDK_TOOL_ERROR = "SDK_TOOL_ERROR"
    ASYNC_RUNTIME_ERROR = "ASYNC_RUNTIME_ERROR"
    INCOMPLETE_LANE_CHECKLIST = "INCOMPLETE_LANE_CHECKLIST"
    OBSERVATION_MISSING = "OBSERVATION_MISSING"
    PARSER_ERROR = "PARSER_ERROR"


SEARCH_SUCCESS = frozenset(
    {
        CompanySearchStatus.SEARCHED_COMPLETE_WITH_MATCHES.value,
        CompanySearchStatus.SEARCHED_COMPLETE_NO_MATCHES.value,
    }
)
EXTERNAL_BLOCKER = frozenset(
    {
        CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value,
        CompanySearchStatus.AUTH_REQUIRED_CONFIRMED.value,
        CompanySearchStatus.OFFICIAL_SOURCE_UNRESOLVED.value,
    }
)
INTERNAL_RETRYABLE = frozenset(
    {
        CompanySearchStatus.BROWSER_TOOL_ERROR.value,
        CompanySearchStatus.SDK_TOOL_ERROR.value,
        CompanySearchStatus.ASYNC_RUNTIME_ERROR.value,
        CompanySearchStatus.INCOMPLETE_LANE_CHECKLIST.value,
        CompanySearchStatus.OBSERVATION_MISSING.value,
        CompanySearchStatus.PARSER_ERROR.value,
    }
)
#: A company is *done* only when searched or blocked by an external condition.
TERMINAL = SEARCH_SUCCESS | EXTERNAL_BLOCKER
#: Only search-success counts toward the "genuinely searched" PASS threshold.
GENUINELY_SEARCHED = SEARCH_SUCCESS


def is_genuinely_searched(status: str) -> bool:
    return status in GENUINELY_SEARCHED


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def is_internal_retryable(status: str) -> bool:
    return status in INTERNAL_RETRYABLE


# ---------------------------------------------------------------------------
# Challenge / auth classification (prompt s.3.4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AccessSignals:
    http_status: Optional[int] = None
    tool_exception: Optional[str] = None       # e.g. "TimeoutError", "PlaywrightError"
    async_runtime_error: bool = False          # sync-in-async / event-loop fault
    login_wall_confirmed: bool = False         # browser-observed blocking login form
    signin_link_visible: bool = False          # a normal header/footer "Sign in" link
    content_visible: bool = False              # real job/search content was observed


def classify_blocker(signals: AccessSignals) -> Optional[str]:
    """Map raw access signals to a V4 status, or ``None`` when nothing blocks the
    search. Precedence (prompt s.3.4):

    1. an internal tool/async exception is ALWAYS retryable, never terminal;
    2. a browser-confirmed login wall is ``AUTH_REQUIRED_CONFIRMED``;
    3. HTTP 401/403/429/503 with no confirmed login wall is
       ``ACCESS_LIMITED_EXTERNAL`` (a 403 is access-limited, not an auth wall,
       unless the browser actually saw a login form);
    4. a normal visible "Sign in" link alongside visible content is NOT a wall.
    """
    if signals.async_runtime_error:
        return CompanySearchStatus.ASYNC_RUNTIME_ERROR.value
    if signals.tool_exception:
        return CompanySearchStatus.BROWSER_TOOL_ERROR.value
    if signals.login_wall_confirmed:
        return CompanySearchStatus.AUTH_REQUIRED_CONFIRMED.value
    if signals.http_status in (401, 403, 429, 503):
        return CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value
    # A normal sign-in link in the chrome is not an auth wall when content shows.
    return None


def looks_like_login_wall(
    *,
    password_field_present: bool,
    job_content_visible: bool,
    login_headline_present: bool = False,
    signin_link_only: bool = False,
) -> bool:
    """A *blocking* login wall requires a real credential form (password field)
    and the ABSENCE of job/search content. A header/footer "Sign in" link on an
    otherwise-populated results page is explicitly NOT a login wall (prompt
    s.3.4)."""
    if signin_link_only and job_content_visible:
        return False
    if not password_field_present:
        return False
    if job_content_visible:
        return False
    return True or login_headline_present  # password present + no content => wall


# ---------------------------------------------------------------------------
# Legacy interop
# ---------------------------------------------------------------------------
_LEGACY_MAP = {
    "COMPLETE": CompanySearchStatus.SEARCHED_COMPLETE_WITH_MATCHES.value,
    "TRUSTED_ZERO": CompanySearchStatus.SEARCHED_COMPLETE_NO_MATCHES.value,
    "COMPLETE_NO_MATCHES": CompanySearchStatus.SEARCHED_COMPLETE_NO_MATCHES.value,
    "ACCESS_LIMITED": CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value,
    "AUTH_REQUIRED": CompanySearchStatus.AUTH_REQUIRED_CONFIRMED.value,
    "OFFICIAL_SOURCE_UNRESOLVED": CompanySearchStatus.OFFICIAL_SOURCE_UNRESOLVED.value,
    # Ambiguous/internal V3 states map to retryable internal failures, NOT terminal.
    "UNSUPPORTED_SITE": CompanySearchStatus.BROWSER_TOOL_ERROR.value,
    "NETWORK_UNAVAILABLE": CompanySearchStatus.BROWSER_TOOL_ERROR.value,
    "FAILED": CompanySearchStatus.INCOMPLETE_LANE_CHECKLIST.value,
    "WAITING_FOR_HUMAN": CompanySearchStatus.AUTH_REQUIRED_CONFIRMED.value,
}


def map_legacy_status(legacy: str, *, has_matches: bool = False) -> str:
    """Translate a legacy :class:`atlas.pilot.models.CompanyStatus` value into a
    V4 status. ``COMPLETE`` becomes WITH/NO matches based on ``has_matches``."""
    if legacy == "COMPLETE":
        return (
            CompanySearchStatus.SEARCHED_COMPLETE_WITH_MATCHES.value
            if has_matches
            else CompanySearchStatus.SEARCHED_COMPLETE_NO_MATCHES.value
        )
    return _LEGACY_MAP.get(legacy, CompanySearchStatus.INCOMPLETE_LANE_CHECKLIST.value)
