"""Failing-first India geography hard-gate tests (V3 pilot; audit 3.2/3.3, prompt s.7/15)."""

from __future__ import annotations

import pytest

from atlas.hunt.geography import (
    GeoDecision,
    JobGeographyDecision,
    classify_job_geography,
)
from atlas.policy.loader import load_policy


@pytest.fixture(scope="module")
def geo():
    return load_policy().geography


def _c(geo, location, **kw) -> JobGeographyDecision:
    return classify_job_geography(location, geography=geo, **kw)


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Bengaluru, India", GeoDecision.INDIA_PRIMARY.value),
        ("Bangalore", GeoDecision.INDIA_PRIMARY.value),
        ("Hyderabad, Telangana, India", GeoDecision.INDIA_PRIMARY.value),
        ("Pune", GeoDecision.INDIA_SECONDARY.value),
        ("Chennai, Tamil Nadu, India", GeoDecision.INDIA_SECONDARY.value),
        ("Noida", GeoDecision.INDIA_SECONDARY.value),
        ("Gurugram", GeoDecision.INDIA_SECONDARY.value),
        ("Gurgaon", GeoDecision.INDIA_SECONDARY.value),
        ("Mumbai", GeoDecision.INDIA_SECONDARY.value),
        ("India", GeoDecision.INDIA_WIDE.value),
    ],
)
def test_allowed_india_locations(geo, location, expected):
    d = _c(geo, location)
    assert d.decision == expected
    assert d.india_eligible is True
    assert d.allowed_in_main_output is True
    assert d.location_evidence


def test_remote_india_variants(geo):
    for loc in ["Remote India", "Remote - India", "India (Remote)"]:
        d = _c(geo, loc)
        assert d.decision == GeoDecision.REMOTE_INDIA.value, loc
        assert d.allowed_in_main_output is True
    d = _c(geo, "Bengaluru, India", work_mode="REMOTE")
    assert d.decision == GeoDecision.REMOTE_INDIA.value


@pytest.mark.parametrize(
    "location",
    [
        "United States",
        "US",
        "San Francisco, CA",
        "California",
        "Seattle, WA",
        "New York, NY",
        "Canada",
        "Toronto, ON",
        "Remote, United States",
        "EMEA",
        "London, UK",
        "Singapore",
        "Sydney, Australia",
        "Dublin, Ireland",
    ],
)
def test_foreign_locations_excluded(geo, location):
    d = _c(geo, location)
    assert d.decision == GeoDecision.FOREIGN_EXCLUDED.value, location
    assert d.india_eligible is False
    assert d.allowed_in_main_output is False


@pytest.mark.parametrize("location", ["N/A", "", "Unknown", "TBD", "Multiple Locations"])
def test_unknown_locations_not_india(geo, location):
    d = _c(geo, location)
    assert d.decision == GeoDecision.UNKNOWN_LOCATION.value, location
    assert d.india_eligible is False
    assert d.allowed_in_main_output is False


def test_remote_alone_is_not_india(geo):
    d = _c(geo, "Remote")
    assert d.decision == GeoDecision.UNKNOWN_LOCATION.value
    assert d.allowed_in_main_output is False


def test_remote_united_states_excluded(geo):
    d = _c(geo, "Remote (US)")
    assert d.decision == GeoDecision.FOREIGN_EXCLUDED.value
    assert d.allowed_in_main_output is False


def test_international_sponsorship_is_separated_not_main(geo):
    d = _c(
        geo,
        "San Francisco, USA",
        eligibility_text="Visa sponsorship available; open to candidates relocating from India.",
    )
    assert d.decision == GeoDecision.INTERNATIONAL_SPONSORED_LEAD.value
    assert d.allowed_in_main_output is False
    assert d.is_foreign_lead is True


def test_candidate_location_cannot_prove_india(geo):
    # There is no candidate input to the gate at all; a foreign job stays foreign.
    import inspect

    params = set(inspect.signature(classify_job_geography).parameters)
    assert "candidate" not in params and "location_group" not in params
    d = _c(geo, "Austin, Texas")
    assert d.decision == GeoDecision.FOREIGN_EXCLUDED.value


def test_multi_location_including_india_is_india(geo):
    d = _c(geo, "Hyderabad, India; London, UK")
    assert d.decision == GeoDecision.INDIA_PRIMARY.value
    assert d.allowed_in_main_output is True
