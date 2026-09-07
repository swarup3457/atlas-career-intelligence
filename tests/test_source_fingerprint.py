"""Phase 1A: ATS fingerprinting tests."""

from __future__ import annotations

import pytest

from atlas.sources.fingerprint import fingerprint_all, fingerprint_ats
from atlas.sources.models import SourceType

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://acme.wd1.myworkdayjobs.com/en-US/careers", SourceType.ATS_WORKDAY),
        ("https://boards.greenhouse.io/acme", SourceType.ATS_GREENHOUSE),
        ("https://jobs.lever.co/acme", SourceType.ATS_LEVER),
        ("https://jobs.smartrecruiters.com/Acme", SourceType.ATS_SMARTRECRUITERS),
        ("https://acme.taleo.net/careersection/x", SourceType.ATS_ORACLE),
        ("https://career5.successfactors.com/careersection", SourceType.ATS_SUCCESSFACTORS),
        ("https://acme.icims.com/jobs/search", SourceType.ATS_ICIMS),
        ("https://acme.phenompeople.com/prod/x", SourceType.ATS_PHENOM),
        ("https://acme.eightfold.ai/careershub/jobs", SourceType.ATS_EIGHTFOLD),
    ],
)
def test_known_hosts(url, expected):
    fp = fingerprint_ats(url)
    assert fp.source_type == expected
    assert fp.confidence >= 0.85


def test_case_insensitive_host():
    fp = fingerprint_ats("https://ACME.MyWorkdayJobs.COM/careers")
    assert fp.source_type == SourceType.ATS_WORKDAY


def test_redirect_used_when_primary_unknown():
    fp = fingerprint_ats("https://careers.acme.com", redirect_url="https://acme.wd1.myworkdayjobs.com/x")
    assert fp.source_type == SourceType.ATS_WORKDAY


def test_marker_match_lower_confidence():
    fp = fingerprint_ats("https://careers.acme.com", markers=["Powered by Greenhouse"])
    assert fp.source_type == SourceType.ATS_GREENHOUSE
    assert fp.confidence <= 0.7


def test_false_positive_lookalike_hosts_not_matched():
    assert fingerprint_ats("https://myworkday.evil.com/jobs").source_type is None
    assert fingerprint_ats("https://notgreenhouse-evil.com/careers").source_type is None


def test_unknown_returns_none():
    fp = fingerprint_ats("https://careers.acme.com/jobs")
    assert fp.matched is False
    assert fp.source_type is None


def test_fingerprint_all_can_return_multiple():
    results = fingerprint_all(
        "https://boards.greenhouse.io/x", markers=["also mentions workday"]
    )
    types = {f.source_type for f in results}
    assert SourceType.ATS_GREENHOUSE in types
    assert SourceType.ATS_WORKDAY in types
