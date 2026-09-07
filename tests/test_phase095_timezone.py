"""Phase 0.95 — timezone helpers and sortable-id helpers.

Proves UTC-internal / local-display separation works generically for a
DST-observing zone (America/New_York) even though Atlas's production
display target is IST (Asia/Kolkata, no DST) — without adding any
production scheduling. Offline, no I/O.
"""

from __future__ import annotations

import datetime

import pytest

from atlas.backup.timezone_utils import (
    INDIA_TZ,
    compact_utc_stamp,
    ensure_utc,
    isoformat_utc,
    local_date_str,
    parse_iso,
    to_local_display,
    utc_now,
)
from atlas.utils.ids import is_ulid, new_ulid

pytestmark = [pytest.mark.unit]

_UTC = datetime.timezone.utc


def test_utc_now_is_aware_utc():
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.timedelta(0)


def test_parse_iso_roundtrip_z_and_offset():
    a = parse_iso("2026-09-06T18:00:00Z")
    b = parse_iso("2026-09-06T23:30:00+05:30")
    assert a == b  # same instant
    assert a.tzinfo is not None
    assert isoformat_utc(a).endswith("+00:00")


def test_ensure_utc_treats_naive_as_utc():
    naive = datetime.datetime(2026, 9, 6, 18, 0, 0)
    assert ensure_utc(naive).utcoffset() == datetime.timedelta(0)


# ---------------------------------------------------------------------------
# Date-boundary / midnight crossing
# ---------------------------------------------------------------------------
def test_midnight_crossing_changes_local_date():
    # 02:30 UTC on Jan 1 is still Dec 31 in New York (UTC-5 in winter).
    instant = datetime.datetime(2026, 1, 1, 2, 30, tzinfo=_UTC)
    local = to_local_display(instant, "America/New_York")
    assert local.date() == datetime.date(2025, 12, 31)
    assert local_date_str(instant, "America/New_York") == "2025-12-31"
    # ...but the same instant is already Jan 1 in India (UTC+5:30).
    assert local_date_str(instant, INDIA_TZ) == "2026-01-01"


# ---------------------------------------------------------------------------
# DST behaviour (New York) vs no-DST (India)
# ---------------------------------------------------------------------------
def test_new_york_dst_offset_changes_between_seasons():
    winter = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=_UTC)
    summer = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=_UTC)
    ny_winter = to_local_display(winter, "America/New_York")
    ny_summer = to_local_display(summer, "America/New_York")
    # EST = UTC-5, EDT = UTC-4 — the mechanism honours DST.
    assert ny_winter.utcoffset() == datetime.timedelta(hours=-5)
    assert ny_summer.utcoffset() == datetime.timedelta(hours=-4)
    assert ny_winter.utcoffset() != ny_summer.utcoffset()


def test_india_offset_is_constant_year_round():
    winter = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=_UTC)
    summer = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=_UTC)
    ist_winter = to_local_display(winter, INDIA_TZ)
    ist_summer = to_local_display(summer, INDIA_TZ)
    ist = datetime.timedelta(hours=5, minutes=30)
    assert ist_winter.utcoffset() == ist
    assert ist_summer.utcoffset() == ist  # IST never shifts (no DST)


def test_compact_utc_stamp_format():
    stamp = compact_utc_stamp(datetime.datetime(2026, 9, 6, 18, 0, 0, tzinfo=_UTC))
    assert stamp == "20260906T180000Z"


# ---------------------------------------------------------------------------
# Sortable id helper
# ---------------------------------------------------------------------------
def test_ulid_shape_and_validation():
    u = new_ulid()
    assert len(u) == 26
    assert is_ulid(u)
    assert not is_ulid("too-short")
    assert not is_ulid("i" * 26)  # 'I' is not in the Crockford alphabet


def test_ulid_is_time_sortable():
    early = new_ulid(datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_UTC))
    late = new_ulid(datetime.datetime(2026, 12, 31, 0, 0, 0, tzinfo=_UTC))
    assert early < late  # lexicographic order matches chronological order
