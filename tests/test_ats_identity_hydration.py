"""Phase 1C-A FINAL stabilization — hydration revision reconciliation and the
requisition-first identity contract (build spec 6 + 7).

Failing-first regressions: before this gate a hydrated DETAIL revision misused
``canonical_id`` to hold the parent observation id (so it was never linked and
the same source job produced two report rows), and canonical identity used only
company+title+location+posted (so two DIFFERENT official requisitions with the
same title/location/date wrongly collapsed into one canonical job).
"""

from __future__ import annotations

import datetime

import pytest

from atlas.config import load_settings
from atlas.persistence.sqlite import StateStore
from atlas.runtime.canonicalize import canonicalize_run
from atlas.runtime.production import ProductionSearchRuntime

pytestmark = pytest.mark.integration


def _settings(tmp_path):
    s = load_settings(
        state_db=tmp_path / "state" / "s.sqlite", checkpoint_db=tmp_path / "state" / "c.sqlite",
        output_dir=tmp_path / "out", logs_dir=tmp_path / "logs", browser_profile=tmp_path / "prof",
        agents_dir=tmp_path / "agents", skills_dir=tmp_path / "skills",
    )
    s.ensure_directories()
    return s


def _stage_search(store, run_id, obs_id, *, source_job_id, company="Acme", title="Java Developer",
                  location="Bengaluru", posted_at="2026-09-01", family="greenhouse",
                  canonical_url=None, evidence="OFFICIAL_SEARCH_LIVE"):
    url = canonical_url or f"https://boards.greenhouse.io/acme/jobs/{source_job_id}"
    store.stage_raw_observation(
        obs_id, run_id, "gh", f"h-{obs_id}", coverage_id="c1", attempt_id="a1", source_family=family,
        source_job_id=source_job_id, source_url=url, canonical_url=url, company=company, title=title,
        location=location, posted_at=posted_at, is_active="ACTIVE",
        source_identity=f"gh::{source_job_id}::{url}",
        detail={"verification_level": evidence, "date_provenance": "EMPLOYER_POSTED_AT"})


# ---------------------------------------------------------------------------
# TEST F — a hydrated DETAIL revision reconciles to ONE canonical + ONE row
# ---------------------------------------------------------------------------
def test_hydration_revision_is_one_canonical_and_one_report_row(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "runF", fixture_mode=True)
    now = datetime.datetime(2026, 9, 4, tzinfo=datetime.timezone.utc)
    with StateStore(rt.settings.state_db) as store:
        store.create_run("runF", "none")
        # A SEARCH observation of an official greenhouse requisition R-1 ...
        _stage_search(store, "runF", "obs-1", source_job_id="1", evidence="OFFICIAL_SEARCH_LIVE")
        # ... and its DETAIL revision (same requisition, upgraded evidence + true date).
        store.stage_raw_observation(
            "obs-1::detail", "runF", "gh", "h-obs-1", coverage_id="c1", attempt_id="a1",
            source_family="greenhouse", source_job_id="1",
            canonical_url="https://boards.greenhouse.io/acme/jobs/1", source_url="https://boards.greenhouse.io/acme/jobs/1",
            company="Acme", title="Java Developer", location="Bengaluru", posted_at="2026-09-02", is_active="ACTIVE",
            source_identity="gh::1::https://boards.greenhouse.io/acme/jobs/1",
            parent_observation_id="obs-1", revision_kind="DETAIL",
            detail={"verification_level": "OFFICIAL_DETAIL_LIVE", "date_provenance": "EMPLOYER_POSTED_AT"})

        res = canonicalize_run(store, "runF")
        # Both revisions reconcile to ONE canonical job (shared official requisition).
        assert store.count_canonical_jobs() == 1
        # One source job -> one observation event (both revisions share source identity).
        assert res.observations_added == 1

    # The current-run projection picks the HIGHEST evidence revision -> one row,
    # upgraded to VERIFIED_OFFICIAL, and the true detail posted date is retained.
    jobs = rt._current_run_jobs(now=now)
    assert len(jobs) == 1
    assert jobs[0]["verification_level"] == "VERIFIED_OFFICIAL"       # upgraded from search-only
    assert jobs[0]["freshness_band"] == "0-7 days"                    # true detail date retained

    data = rt._report_data_from_state({"terminal_state": "COMPLETE", "counters": {}})
    assert len(data["All_Jobs"]) == 1                                 # one report row, not two


def test_failed_detail_hydration_preserves_original_search_evidence(tmp_path):
    """When detail hydration fails, the original SEARCH observation is intact and
    still canonicalizes to its official requisition."""
    store = StateStore(tmp_path / "s.sqlite"); store.create_run("run", "none")
    _stage_search(store, "run", "obs-1", source_job_id="1")
    # No DETAIL revision staged (hydration failed) — the search evidence stands.
    canonicalize_run(store, "run")
    assert store.count_canonical_jobs() == 1
    assert store.get_raw_observation("obs-1")["revision_kind"] == "SEARCH"
    store.close()


# ---------------------------------------------------------------------------
# TEST G — requisition-first identity + portal alignment + repost distinction
# ---------------------------------------------------------------------------
def test_two_official_requisitions_same_title_location_date_are_two_canonical(tmp_path):
    store = StateStore(tmp_path / "s.sqlite"); store.create_run("run", "none")
    # Two DIFFERENT official requisitions with identical company/title/location/date.
    _stage_search(store, "run", "o1", source_job_id="1")
    _stage_search(store, "run", "o2", source_job_id="2")
    res = canonicalize_run(store, "run")
    # Requisition-first: two distinct vacancies -> TWO canonical jobs (never merged
    # on title/location/date), classified as a repost/new-vacancy relationship.
    assert store.count_canonical_jobs() == 2
    assert res.reposts >= 1
    store.close()


def test_portal_lead_without_reqid_aligns_to_official_canonical(tmp_path):
    store = StateStore(tmp_path / "s.sqlite"); store.create_run("run", "none")
    # Official greenhouse requisition R-1 ...
    _stage_search(store, "run", "official", source_job_id="1")
    # ... plus a PORTAL observation (non-official host, its own id) of the SAME
    # role (aligned company/title/location) -> collapses into the official canonical.
    store.stage_raw_observation(
        "portal", "run", "portal-inst", "h-portal", coverage_id="c2", attempt_id="a2",
        source_family="portal", source_job_id="P-1",
        source_url="https://jobs.example.com/acme/1", canonical_url="https://jobs.example.com/acme/1",
        company="Acme", title="Java Developer", location="Bengaluru", posted_at="2026-09-01", is_active="ACTIVE",
        source_identity="portal-inst::P-1::https://jobs.example.com/acme/1",
        detail={"verification_level": "PORTAL_LIVE"})
    res = canonicalize_run(store, "run")
    assert store.count_canonical_jobs() == 1        # portal lead aligned to official
    assert res.portal_leads_aligned == 1
    assert res.observations_added == 2              # both observations retained
    store.close()


def test_new_official_requisition_similar_role_is_separate_with_relationship(tmp_path):
    store = StateStore(tmp_path / "s.sqlite"); store.create_run("run", "none")
    _stage_search(store, "run", "o1", source_job_id="10", posted_at="2026-09-01")
    _stage_search(store, "run", "o2", source_job_id="11", posted_at="2026-08-01")  # new req, reposted role
    res = canonicalize_run(store, "run")
    assert store.count_canonical_jobs() == 2
    assert res.reposts >= 1 and res.relationships
    assert res.relationships[0]["classification"] == "PROBABLE_REPOST_OR_NEW_VACANCY"
    store.close()
