"""Phase 1C-A CORRECTIVE gate — truthful detail hydration, Workday date/detail,
and current-run verification/matching/reporting (build spec 14/15/19).

Failing-first regressions: before this gate DETAIL_HYDRATION was a single
representative-posting telemetry probe, Workday conflated startDate with the
posting date and did not preserve externalPath separately, and
verification/match/report used hard-coded placeholders (every row
VERIFIED_OFFICIAL / LIVE_DATE_UNKNOWN, a fake matched job, all canonical jobs
globally).
"""

from __future__ import annotations

import datetime

import pytest

from atlas.config import load_settings
from atlas.persistence.sqlite import StateStore
from atlas.runtime.production import ProductionSearchRuntime
from atlas.sources.adapter import SourceAdapter, new_result_base
from atlas.sources.ats.workday import WorkdayAdapter
from atlas.sources.detail_hydration import DetailHydrator
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    ActiveState, Capability, DetailRequest, DiscoveryResult, SearchRequest, SearchResult,
    SourceFamily, SourceInstance, SourceType, VerificationLevel, ZeroResultKind,
)
from atlas.sources.registry import SourceRegistry
from atlas.sources.testing.http import FakeTransport, json_response, static

pytestmark = pytest.mark.integration


def _wd_inst():
    return SourceInstance("wd", SourceType.ATS_WORKDAY, tenant="acme", site="External",
                          metadata={"tenant": "acme", "datacenter": "wd1", "site": "External"})


# ---------------------------------------------------------------------------
# §15 — Workday date + externalPath corrections
# ---------------------------------------------------------------------------
def test_workday_preserves_externalpath_reqid_and_startdate_separately():
    payload = {"total": 1, "jobPostings": [{
        "title": "Java Backend Engineer", "externalPath": "/job/BLR/Java_Backend_R123",
        "locationsText": "IND.Bengaluru", "postedOn": "Posted 5 Days Ago", "startDate": "2026-10-01",
        "jobReqId": "R-123",
    }]}
    a = WorkdayAdapter(_wd_inst(), http_client=FakeTransport(static(json_response(payload))))
    r = a.search(SearchRequest(query="java")).results[0]
    # posted_at is NEVER an absolute date from relative postedOn; startDate is NOT posted.
    assert r.posted_at is None
    prov = r.provenance
    assert prov["date_provenance"] == "RELATIVE_POSTED_TEXT"
    assert prov["posted_raw"] == "Posted 5 Days Ago"
    assert prov["start_date_raw"] == "2026-10-01"       # preserved separately
    assert prov["requisition_id"] == "R-123"            # jobReqId preserved separately
    assert prov["external_path"] == "/job/BLR/Java_Backend_R123"  # the detail anchor
    assert prov["cxs_detail_url"].endswith("/wday/cxs/acme/External/job/BLR/Java_Backend_R123")


def test_workday_detail_uses_externalpath_not_reqid():
    detail = {"jobPostingInfo": {"title": "Java Backend Engineer", "jobReqId": "R-123",
                                 "externalPath": "/job/BLR/Java_Backend_R123",
                                 "jobDescription": "<p>Build APIs</p>", "postedOn": "Posted 5 Days Ago"}}
    tr = FakeTransport(static(json_response(detail)))
    a = WorkdayAdapter(_wd_inst(), http_client=tr)
    # Detail via the public URL (which carries the externalPath) — not the reqId.
    a.fetch_detail(DetailRequest(url="https://acme.wd1.myworkdayjobs.com/en-US/External/job/BLR/Java_Backend_R123"))
    assert tr.calls[0].url.endswith("/wday/cxs/acme/External/job/BLR/Java_Backend_R123")
    assert "R-123" not in tr.calls[0].url  # the requisition id is never the detail anchor


# ---------------------------------------------------------------------------
# §14 — real detail hydration through the shared executor
# ---------------------------------------------------------------------------
class _GhDetailAdapter(SourceAdapter):
    """A greenhouse-family DETAIL adapter scripted offline (no network)."""

    source_type = SourceType.ATS_GREENHOUSE
    source_family = SourceFamily.GREENHOUSE
    CAPABILITIES = frozenset({Capability.SEARCH, Capability.DETAIL})
    adapter_version = "gh-detail-1"
    parser_version = "gh-detail-parser-1"
    calls = {"detail": 0}

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY, "ok")

    def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult(results=(), zero_result_kind=ZeroResultKind.TRUSTED_ZERO)

    def fetch_detail(self, request: DetailRequest) -> DiscoveryResult:
        _GhDetailAdapter.calls["detail"] += 1
        return DiscoveryResult(**new_result_base(
            self, source_job_id="1", canonical_url="https://boards.greenhouse.io/acme/jobs/1",
            company="Acme", title="Java Developer", location="Bengaluru", posted_at="2026-09-01",
            description="Full hydrated description", is_active=ActiveState.ACTIVE,
            verification_level=VerificationLevel.OFFICIAL_DETAIL_LIVE, confidence=0.95))


def _stage_gh_obs(store, run_id, obs_id="obs-1"):
    store.stage_raw_observation(
        obs_id, run_id, "gh", "hash-1", coverage_id="c1", attempt_id="a1",
        source_family="greenhouse", source_job_id="1",
        canonical_url="https://boards.greenhouse.io/acme/jobs/1", company="Acme",
        title="Java Developer", location="Bengaluru",
        detail={"verification_level": "OFFICIAL_SEARCH_LIVE"})


def test_detail_hydration_creates_new_immutable_version_via_executor_and_resumes(tmp_path):
    _GhDetailAdapter.calls["detail"] = 0
    store = StateStore(tmp_path / "s.sqlite"); store.create_run("run", "none")
    _stage_gh_obs(store, "run")
    reg = SourceRegistry(); reg.register(_GhDetailAdapter)
    inst = SourceInstance("gh", SourceType.ATS_GREENHOUSE, metadata={"board_token": "acme"})
    hyd = DetailHydrator(store, reg, {"gh": inst}, run_id="run", executor=RateLimitedExecutor())
    res = hyd.hydrate()
    assert res.selected == 1 and res.hydrated == 1
    assert _GhDetailAdapter.calls["detail"] == 1
    # A NEW immutable hydrated version exists; the ORIGINAL is untouched.
    original = store.get_raw_observation("obs-1")
    hydrated = store.get_raw_observation("obs-1::detail")
    assert original is not None and original["processing_status"] == "STAGED"
    assert hydrated is not None and hydrated["processing_status"] == "HYDRATED"
    # Resume: a second hydration does NOT repeat the completed detail call.
    res2 = hyd.hydrate()
    assert res2.skipped_existing == 1 and res2.hydrated == 0
    assert _GhDetailAdapter.calls["detail"] == 1
    store.close()


# ---------------------------------------------------------------------------
# §19 — truthful current-run verification / matching / reporting
# ---------------------------------------------------------------------------
def _settings(tmp_path):
    s = load_settings(
        state_db=tmp_path / "state" / "s.sqlite", checkpoint_db=tmp_path / "state" / "c.sqlite",
        output_dir=tmp_path / "out", logs_dir=tmp_path / "logs", browser_profile=tmp_path / "prof",
        agents_dir=tmp_path / "agents", skills_dir=tmp_path / "skills",
    )
    s.ensure_directories()
    return s


def _stage(store, run_id, obs_id, evidence, *, posted_at=None, date_prov=None, title="Java Developer",
           company="Acme", is_active="ACTIVE"):
    store.stage_raw_observation(
        obs_id, run_id, "gh", f"h-{obs_id}", coverage_id="c1", attempt_id="a1", source_family="greenhouse",
        source_job_id=obs_id, canonical_url=f"https://boards.greenhouse.io/acme/jobs/{obs_id}",
        company=company, title=title, location="Bengaluru", posted_at=posted_at, is_active=is_active,
        detail={"verification_level": evidence, "date_provenance": date_prov})


def test_report_is_current_run_scoped_with_distinct_truthful_labels(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "runX", fixture_mode=True)
    now = datetime.datetime(2026, 9, 8, tzinfo=datetime.timezone.utc)
    with StateStore(rt.settings.state_db) as store:
        store.create_run("runX", "none"); store.create_run("prior", "none")
        _stage(store, "runX", "a", "OFFICIAL_DETAIL_LIVE", posted_at="2026-09-04", date_prov="EMPLOYER_POSTED_AT")
        _stage(store, "runX", "b", "OFFICIAL_SEARCH_LIVE", title="Backend Engineer")  # no date
        _stage(store, "prior", "z", "OFFICIAL_DETAIL_LIVE", title="Prior Run Job", company="OldCo")

    jobs = rt._current_run_jobs(now=now)
    labels = sorted(j["verification_level"] for j in jobs)
    # ONLY the current run's jobs (prior run absent).
    assert len(jobs) == 2 and all(j["company"] == "Acme" for j in jobs)
    assert "Prior Run Job" not in [j["title"] for j in jobs]
    # Search-only vs detail-verified are DISTINCT — never a blanket VERIFIED_OFFICIAL.
    assert labels == ["OFFICIAL_SEARCH_LIVE", "VERIFIED_OFFICIAL"]
    # Known employer date -> a real freshness band; unknown date -> LIVE_DATE_UNKNOWN.
    by_title = {j["title"]: j for j in jobs}
    assert by_title["Java Developer"]["freshness_band"] == "0-7 days"
    assert by_title["Backend Engineer"]["freshness_band"] == "LIVE_DATE_UNKNOWN"

    # Report rows equal the selected current-run jobs; Verified_Official is the
    # REAL count (1), not len(rows).
    data = rt._report_data_from_state({"terminal_state": "COMPLETE", "counters": {}})
    assert len(data["All_Jobs"]) == 2
    assert data["Run_Summary"][0]["Verified_Official"] == 1
    assert {r["verification_level"] for r in data["All_Jobs"]} == {"VERIFIED_OFFICIAL", "OFFICIAL_SEARCH_LIVE"}


def test_synthetic_candidate_match_is_not_evaluated(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "runY", fixture_mode=True)
    with StateStore(rt.settings.state_db) as store:
        store.create_run("runY", "none")
        _stage(store, "runY", "a", "OFFICIAL_SEARCH_LIVE")
        _stage(store, "runY", "b", "OFFICIAL_DETAIL_LIVE", title="Backend Engineer")
    rt.used_synthetic_candidate = True
    state = {"counters": {}}
    rt._h_match(state, None)
    assert state["counters"]["matched"] == 0
    assert state["counters"]["not_evaluated"] == 2  # synthetic -> NOT_EVALUATED, never invented
    assert "CANDIDATE_MATCH_NOT_EVALUATED_SYNTHETIC" in state.get("notes", [])
