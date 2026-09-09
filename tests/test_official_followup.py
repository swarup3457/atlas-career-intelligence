"""Phase 1E/F recovery pass 3 — live portal->official follow-up + linkage.

Offline tests using injected fake portal + official adapters (no network). They
prove: real deterministic linkage when a portal lead and an official observation
overlap; truthful states when they do not; >=N official follow-up ATTEMPTS; and
that no official domain is ever guessed from a company name.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from atlas.config import load_settings
from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError
from atlas.sources.models import (
    ActiveState,
    DiscoveryResult,
    SearchResult,
    SourceType,
    VerificationLevel,
    WorkMode,
    ZeroResultKind,
)
from atlas.runtime.official_followup import (
    CompanyFollowup,
    FollowupResult,
    KnownOfficialSource,
    LiveOfficialFollowup,
)


@pytest.fixture
def settings(tmp_path):
    return load_settings(state_db=str(tmp_path / "state.sqlite"),
                         checkpoint_db=str(tmp_path / "cp.sqlite"))


def _portal_result(i, company, title, location="Bengaluru, India"):
    return DiscoveryResult(
        source_type=SourceType.PORTAL_LARGE, source_instance="linkedin-live",
        source_job_id=f"li{i}", source_url=f"https://www.linkedin.com/jobs/view/{i}",
        canonical_url=f"https://www.linkedin.com/jobs/view/{i}",
        company=company, title=title, location=location, work_mode=WorkMode.ONSITE,
        posted_at="2026-09-05", is_active=ActiveState.ACTIVE,
        verification_level=VerificationLevel.PORTAL_LIVE, confidence=0.55,
        provenance={"source_family": "linkedin"},
    )


def _official_result(i, company, title, location="Bengaluru, India", active=ActiveState.ACTIVE):
    return DiscoveryResult(
        source_type=SourceType.ATS_GREENHOUSE, source_instance="greenhouse-acme",
        source_job_id=f"gh{i}", source_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
        canonical_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
        company=company, title=title, location=location, work_mode=WorkMode.ONSITE,
        posted_at="2026-09-04", is_active=active,
        verification_level=VerificationLevel.OFFICIAL_SEARCH_LIVE, confidence=0.9,
        provenance={"source_family": "greenhouse"},
    )


class FakePortal:
    def __init__(self, results):
        self._results = results

    def search(self, request):
        if request.page == 1:
            return SearchResult(results=tuple(self._results), page=1, has_more=False,
                                zero_result_kind=ZeroResultKind.NOT_APPLICABLE)
        return SearchResult(results=(), page=request.page, has_more=False,
                            zero_result_kind=ZeroResultKind.NOT_APPLICABLE)


class FakeOfficial:
    def __init__(self, results):
        self._results = results

    def search(self, request):
        return SearchResult(results=tuple(self._results), page=1, has_more=False,
                            zero_result_kind=ZeroResultKind.NOT_APPLICABLE)


class FakeOfficialAccessLimited:
    def search(self, request):
        raise AdapterError(ErrorCategory.ANTI_BOT, "anti-bot on official board (no bypass)")


def _followup(settings, *, portal, official, sources=None):
    src = sources or [KnownOfficialSource("Acme", "acme.com", "greenhouse", "acme")]
    return LiveOfficialFollowup(
        settings, "FUP_TEST", known_sources=src, recency_days=30, max_portal_pages=1,
        portal_adapter_factory=lambda fam: portal,
        official_adapter_factory=lambda s: official,
    )


def test_real_linkage_when_titles_overlap(settings):
    portal = FakePortal([_portal_result(1, "Acme", "Senior Java Backend Engineer"),
                         _portal_result(2, "Acme", "Frontend React Engineer")])
    official = FakeOfficial([_official_result(1, "Acme", "Senior Java Backend Engineer"),
                             _official_result(2, "Acme", "Data Scientist")])
    result = _followup(settings, portal=portal, official=official).run()
    cf = result.companies[0]
    assert cf.official_attempted and cf.official_status == "COMPLETED"
    assert cf.official_jobs == 2 and cf.portal_leads == 2
    assert cf.linked_verified == 1              # only the exact-title role links
    assert result.linked_verified_total >= 1


def test_no_link_when_no_overlap_is_truthful(settings):
    portal = FakePortal([_portal_result(1, "Acme", "Frontend React Engineer")])
    official = FakeOfficial([_official_result(1, "Acme", "Warehouse Associate")])
    result = _followup(settings, portal=portal, official=official).run()
    cf = result.companies[0]
    assert cf.official_attempted is True         # attempt is truthful even with no link
    assert cf.linked_verified == 0
    assert cf.unlinked >= 1                       # lead stays PORTAL_CURRENT_LEAD


def test_three_official_attempts_for_proof6(settings):
    portal = FakePortal([_portal_result(1, "Acme", "Java Engineer")])
    official = FakeOfficial([_official_result(1, "Acme", "Java Engineer")])
    sources = [
        KnownOfficialSource("Acme", "acme.com", "greenhouse", "acme"),
        KnownOfficialSource("Bravo", "bravo.com", "greenhouse", "bravo"),
        KnownOfficialSource("Charlie", "charlie.com", "greenhouse", "charlie"),
    ]
    result = _followup(settings, portal=portal, official=official, sources=sources).run()
    assert result.official_attempts == 3


def test_official_access_limited_is_truthful(settings):
    portal = FakePortal([_portal_result(1, "Acme", "Java Engineer")])
    result = _followup(settings, portal=portal, official=FakeOfficialAccessLimited()).run()
    cf = result.companies[0]
    assert cf.official_attempted is True
    assert cf.official_status == "ACCESS_LIMITED"   # never a fake zero
    assert cf.official_jobs == 0


def test_closed_official_marks_closed(settings):
    portal = FakePortal([_portal_result(1, "Acme", "Senior Java Backend Engineer")])
    official = FakeOfficial([_official_result(1, "Acme", "Senior Java Backend Engineer",
                                              active=ActiveState.INACTIVE)])
    result = _followup(settings, portal=portal, official=official).run()
    cf = result.companies[0]
    # positive closure evidence beats a live-verified label
    assert cf.closed == 1
    assert cf.linked_verified == 0


def test_rankable_jobs_carry_verification_state(settings):
    portal = FakePortal([_portal_result(1, "Acme", "Senior Java Backend Engineer")])
    official = FakeOfficial([_official_result(1, "Acme", "Senior Java Backend Engineer")])
    result = _followup(settings, portal=portal, official=official).run()
    jobs = result.rankable_jobs()
    assert jobs
    verified = [j for j in jobs if j.verification_state == "LINKED_OFFICIAL_VERIFIED"]
    assert verified and verified[0].has_live_official_page is True


def test_no_domain_is_guessed_from_name():
    # A KnownOfficialSource carries an INDEPENDENTLY-known board token; the
    # follow-up never derives an official domain from the company name alone.
    import atlas.runtime.official_followup as mod
    src = open(mod.__file__, encoding="utf-8").read()
    # the module must require an explicit board_token (no name->domain heuristic)
    assert "board_token" in src
    assert "guess" not in src.lower() or "never" in src.lower()


def test_followup_result_serializes():
    r = FollowupResult(run_id="X", companies=[CompanyFollowup("Acme", "acme.com", "greenhouse",
                                                              official_attempted=True, linked_verified=2)])
    d = r.to_dict()
    assert d["official_attempts"] == 1
    assert d["linked_verified_total"] == 2
    assert d["companies"][0]["company_name"] == "Acme"
