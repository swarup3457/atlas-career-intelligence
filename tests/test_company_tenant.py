"""Phase 1A.5: deterministic ATS tenant extraction tests."""

from __future__ import annotations

import pytest

from atlas.company.tenant import extract_tenant
from atlas.sources.models import SourceType

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "source_type,url,expected",
    [
        (SourceType.ATS_GREENHOUSE, "https://boards.greenhouse.io/acme", "acme"),
        (SourceType.ATS_GREENHOUSE, "https://boards.greenhouse.io/embed/job_board?for=acme", "acme"),
        (SourceType.ATS_GREENHOUSE, "https://boards.greenhouse.io/embed/job_board", None),
        (SourceType.ATS_LEVER, "https://jobs.lever.co/initech/", "initech"),
        (SourceType.ATS_WORKDAY, "https://acme.wd1.myworkdayjobs.com/External", "acme"),
        (SourceType.ATS_WORKDAY, "https://globex.wd5.myworkdayjobs.com/en-US/Careers", "globex"),
        (SourceType.ATS_SMARTRECRUITERS, "https://careers.smartrecruiters.com/Hooli", "hooli"),
    ],
)
def test_tenant_extraction(source_type, url, expected):
    assert extract_tenant(source_type, url) == expected


def test_unknown_or_missing_returns_none():
    assert extract_tenant(None, "https://x") is None
    assert extract_tenant(SourceType.ATS_WORKDAY, None) is None
    assert extract_tenant(SourceType.PORTAL_LARGE, "https://linkedin.com") is None
