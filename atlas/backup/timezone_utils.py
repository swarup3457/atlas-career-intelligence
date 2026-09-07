"""Timezone helpers (Phase 0.95).

Atlas stores and reasons about time in UTC everywhere internally; local
time is a *presentation* concern only. These helpers make that boundary
explicit and testable:

* :func:`utc_now` — the single source of "now", always timezone-aware UTC.
* :func:`to_local_display` — convert a UTC instant to a named timezone for
  human display ONLY (never for storage or comparison).
* :func:`parse_iso` / :func:`isoformat_utc` — round-trip ISO-8601 strings.

Timezone conversion uses the standard-library :mod:`zoneinfo`, so the
mechanism works for DST-observing zones (e.g. ``America/New_York``) even
though Atlas's production target is IST (``Asia/Kolkata``, which has no
DST). Proving it generically with a DST zone guards against a future
change of deployment region — WITHOUT adding any production scheduling.
"""

from __future__ import annotations

import datetime
from typing import Optional
from zoneinfo import ZoneInfo

_UTC = datetime.timezone.utc

# Atlas's production display timezone. India Standard Time is UTC+05:30
# year-round (no daylight saving), but nothing in Atlas depends on that
# assumption — conversions always go through zoneinfo.
INDIA_TZ = "Asia/Kolkata"


def utc_now() -> datetime.datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.datetime.now(_UTC)


def ensure_utc(dt: datetime.datetime) -> datetime.datetime:
    """Coerce ``dt`` to timezone-aware UTC.

    A naive datetime is assumed to already be UTC (Atlas never stores
    naive local times); an aware datetime is converted to UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def isoformat_utc(dt: datetime.datetime) -> str:
    """ISO-8601 string for ``dt`` in UTC (always includes the offset)."""
    return ensure_utc(dt).isoformat()


def parse_iso(value: str) -> datetime.datetime:
    """Parse an ISO-8601 string into a timezone-aware UTC datetime.

    Accepts a trailing ``Z`` (Zulu) as well as explicit offsets; naive
    inputs are interpreted as UTC.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(text)
    return ensure_utc(dt)


def to_local_display(dt: datetime.datetime, tz_name: str = INDIA_TZ) -> datetime.datetime:
    """Convert a UTC instant to ``tz_name`` for DISPLAY only.

    The returned datetime is timezone-aware in the target zone. This is a
    presentation-boundary conversion — do not persist or compare the
    result as if it were UTC.
    """
    return ensure_utc(dt).astimezone(ZoneInfo(tz_name))


def local_date_str(dt: datetime.datetime, tz_name: str = INDIA_TZ) -> str:
    """The calendar date (``YYYY-MM-DD``) of ``dt`` as seen in ``tz_name``.

    Because a single UTC instant can fall on different calendar dates in
    different zones, the display zone is explicit.
    """
    return to_local_display(dt, tz_name).strftime("%Y-%m-%d")


def compact_utc_stamp(dt: datetime.datetime) -> str:
    """A filesystem-friendly compact UTC stamp, e.g. ``20260906T180000Z``."""
    return ensure_utc(dt).strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "INDIA_TZ",
    "utc_now",
    "ensure_utc",
    "isoformat_utc",
    "parse_iso",
    "to_local_display",
    "local_date_str",
    "compact_utc_stamp",
]
