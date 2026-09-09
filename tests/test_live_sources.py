"""Phase 1E/F recovery — live portal discovery bridge + unhydrated-lead fit.

Offline/deterministic tests using injected fake adapters (no network). They prove
the live-source bridge produces truthful RankableJobs, that portal leads never
claim a fabricated match (candidate_fit unknown), and that access/auth limits are
reported truthfully rather than as a fake zero.
"""

from __future__ import annotations

import datetime

import pytest

from atlas.candidate.eligibility import CandidateProfile, EligibilityStatus, RankableJob
from atlas.candidate.importer import build_synthetic_ledger
from atlas.candidate.ranking import DeepEvaluator, TriageRanker
from atlas.policy import load_policy
from atlas.sources.adapter import AdapterError
from atlas.models import ErrorCategory
from atlas.sources.models import (
    ActiveState,
    DiscoveryResult,
    SearchResult,
    SourceType,
    VerificationLevel,
    WorkMode,
    ZeroResultKind,
)
from atlas.runtime.live_sources import LivePortalDiscovery

TODAY = datetime.date(2026, 9, 8)


@pytest.fixture(scope="module")
def policy():
    return load_policy()


@pytest.fixture(scope="module")
def candidate():
    return CandidateProfile.from_ledger(
        build_synthetic_ledger(), total_experience_years=4.5,
        target_lanes=("JAVA_BACKEND", "GENERAL_SOFTWARE"),
    )


def _result(i):
    return DiscoveryResult(
        source_type=SourceType.PORTAL_LARGE,
        source_instance="linkedin-live", source_job_id=str(1000 + i),
        source_url=f"https://www.linkedin.com/jobs/view/{1000+i}",
        canonical_url=f"https://www.linkedin.com/jobs/view/{1000+i}",
        company=f"Company {i}", title="Java Backend Engineer", location="Bengaluru, India",
        work_mode=WorkMode.ONSITE, posted_at="2026-09-05", is_active=ActiveState.ACTIVE,
        verification_level=VerificationLevel.PORTAL_LIVE, confidence=0.55,
        provenance={"source_family": "linkedin"},
    )


class FakeAdapter:
    def __init__(self, results, has_more_pages=0):
        self._results = results
        self._has_more_pages = has_more_pages

    def search(self, request):
        page = request.page
        if page <= self._has_more_pages:
            return SearchResult(results=tuple(self._results), page=page, has_more=True,
                                next_cursor=str(page * 10), zero_result_kind=ZeroResultKind.NOT_APPLICABLE)
        if page == self._has_more_pages + 1:
            return SearchResult(results=tuple(self._results), page=page, has_more=False,
                                zero_result_kind=ZeroResultKind.NOT_APPLICABLE)
        return SearchResult(results=(), page=page, has_more=False,
                            zero_result_kind=ZeroResultKind.NOT_APPLICABLE)


class AuthWallAdapter:
    def search(self, request):
        raise AdapterError(ErrorCategory.LOGIN_WALL, "login/auth wall (no bypass)")


class AntiBotAdapter:
    def search(self, request):
        raise AdapterError(ErrorCategory.ANTI_BOT, "anti-bot challenge (no bypass)")


# --------------------------------------------------------------------------- #
# Live-source bridge
# --------------------------------------------------------------------------- #
def test_live_discovery_produces_portal_leads():
    disco = LivePortalDiscovery(lane="JAVA_BACKEND", location="India", max_pages=1,
                                adapter_factory=lambda fam: FakeAdapter([_result(i) for i in range(3)]))
    out = disco.discover(("linkedin",))
    assert len(out.jobs) == 3
    for j in out.jobs:
        assert j.verification_state == "PORTAL_CURRENT_LEAD"
        assert j.mandatory_requirements == ()   # guest cards carry no requirements
        assert j.is_fetchable is True
        assert j.source_family == "linkedin"
        assert j.job_key.startswith("job_")
    h = out.health_dict()["linkedin"]
    assert h["status"] == "COMPLETED" and h["unique_leads"] == 3


def test_live_discovery_follows_bounded_pagination():
    disco = LivePortalDiscovery(max_pages=2,
                                adapter_factory=lambda fam: FakeAdapter([_result(i) for i in range(2)], has_more_pages=1))
    out = disco.discover(("linkedin",))
    # 2 pages x 2 unique-per-page (distinct ids) -> but ids repeat per page, so dedup applies
    assert out.health_dict()["linkedin"]["pages"] == 2


def test_live_discovery_dedupes_by_job_key():
    disco = LivePortalDiscovery(max_pages=2,
                                adapter_factory=lambda fam: FakeAdapter([_result(1)], has_more_pages=1))
    out = disco.discover(("linkedin",))
    assert len(out.jobs) == 1  # same job id across pages collapses to one lead


def test_auth_wall_is_truthful_not_zero():
    disco = LivePortalDiscovery(adapter_factory=lambda fam: AuthWallAdapter())
    out = disco.discover(("linkedin",))
    assert out.jobs == []
    h = out.health_dict()["linkedin"]
    assert h["status"] == "AUTH_REQUIRED"
    assert h["attempted"] is True


def test_anti_bot_is_access_limited_not_zero():
    disco = LivePortalDiscovery(adapter_factory=lambda fam: AntiBotAdapter())
    out = disco.discover(("naukri",))
    h = out.health_dict()["naukri"]
    assert h["status"] == "ACCESS_LIMITED"


def test_unknown_family_not_reached():
    disco = LivePortalDiscovery()
    out = disco.discover(("wellfound",))
    assert out.health_dict()["wellfound"]["status"] == "NOT_REACHED"


# --------------------------------------------------------------------------- #
# Unhydrated lead fit (no fabricated match)
# --------------------------------------------------------------------------- #
def _lead(job_key="lead1"):
    return RankableJob(
        job_key=job_key, company="Acme", title="Java Backend Engineer", location="Bengaluru, India",
        lane="JAVA_BACKEND", mandatory_requirements=(), preferred_requirements=(),
        experience_text="", eligibility_text="Bengaluru, India", posted_date=TODAY,
        verification_state="PORTAL_CURRENT_LEAD", has_live_official_page=False, is_fetchable=True,
        source_family="linkedin",
    )


def test_triage_lead_with_no_requirements_has_unknown_fit(policy, candidate):
    ranker = TriageRanker(policy, candidate)
    result = ranker.rank([_lead()], today=TODAY)
    ev = result.by_key("lead1")
    assert ev.candidate_fit is None            # never a fabricated perfect score
    assert ev.recommendation == "MANUAL_VERIFICATION"
    assert ev.eligibility == EligibilityStatus.DEFERRED.value


def test_deep_lead_with_no_requirements_has_unknown_fit(policy, candidate):
    ev = DeepEvaluator(policy, candidate).evaluate(_lead(), today=TODAY)
    assert ev.candidate_fit is None
    assert ev.recommendation == "MANUAL_VERIFICATION"


def test_job_with_requirements_still_scores(policy, candidate):
    job = RankableJob(
        job_key="k", company="Acme", title="Java Backend Engineer", location="Bengaluru, India",
        lane="JAVA_BACKEND", mandatory_requirements=("Java", "Spring Boot"), preferred_requirements=(),
        experience_text="2+ years", eligibility_text="Bengaluru, India", posted_date=TODAY,
        verification_state="VERIFIED_OFFICIAL", has_live_official_page=True, is_fetchable=True,
    )
    ev = DeepEvaluator(policy, candidate).evaluate(job, today=TODAY)
    assert ev.candidate_fit is not None and ev.candidate_fit > 0
