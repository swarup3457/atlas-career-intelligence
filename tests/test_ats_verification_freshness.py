"""Phase 1C-A FINAL stabilization — current-run scoped counts, durable
verification decisions, and the single freshness authority (build spec 9/10/11).

Failing-first regressions: the run summary used a GLOBAL canonical-job count
(prior runs inflated it), the verification classifier's result was discarded
(no durable per-job decision), and a duplicate runtime freshness function clamped
an impossible FUTURE posted date to '0-7 days' instead of DATA_CONFLICT.
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


def _stage(store, run_id, obs_id, *, source_job_id, evidence, company="Acme", title="Java Developer",
           posted_at="2026-09-04", date_prov="EMPLOYER_POSTED_AT", is_active="ACTIVE", family="greenhouse",
           revision_kind="SEARCH", parent=None, url=None):
    u = url or f"https://boards.greenhouse.io/{company.lower()}/jobs/{source_job_id}"
    store.stage_raw_observation(
        obs_id, run_id, "gh", f"h-{obs_id}", coverage_id="c1", attempt_id="a1", source_family=family,
        source_job_id=source_job_id, source_url=u, canonical_url=u, company=company, title=title,
        location="Bengaluru", posted_at=posted_at, is_active=is_active,
        source_identity=f"gh::{source_job_id}::{u}", revision_kind=revision_kind, parent_observation_id=parent,
        detail={"verification_level": evidence, "date_provenance": date_prov})


# ---------------------------------------------------------------------------
# TEST I — current-run summary is unaffected by a prior run
# ---------------------------------------------------------------------------
def test_current_run_summary_ignores_prior_run_jobs(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "cur", fixture_mode=True)
    with StateStore(rt.settings.state_db) as store:
        store.create_run("prior", "none"); store.create_run("cur", "none")
        # A PRIOR run with many canonical jobs.
        for i in range(8):
            _stage(store, "prior", f"p{i}", source_job_id=f"P{i}", evidence="OFFICIAL_DETAIL_LIVE",
                   title=f"Prior Role {i}", company="OldCo", revision_kind="DETAIL")
        canonicalize_run(store, "prior")
        # The CURRENT run has exactly two jobs.
        _stage(store, "cur", "a", source_job_id="1", evidence="OFFICIAL_DETAIL_LIVE", revision_kind="DETAIL")
        _stage(store, "cur", "b", source_job_id="2", evidence="OFFICIAL_SEARCH_LIVE", title="Backend Engineer")
        canonicalize_run(store, "cur")
        rt._persist_verification_decisions(store)

        # Global table holds BOTH runs; the current-run count is exactly two.
        assert store.count_canonical_jobs() >= 10
        assert store.count_current_run_canonical_jobs("cur") == 2

    jobs = rt._current_run_jobs()
    assert len(jobs) == 2 and all(j["company"] == "Acme" for j in jobs)
    data = rt._report_data_from_state({"terminal_state": "COMPLETE",
                                       "counters": {"unique_after_dedupe": 2, "verified": 1, "discovered": 2}})
    assert len(data["All_Jobs"]) == 2
    assert data["Run_Summary"][0]["Relevant_Discoveries"] == 2      # NOT the global 10+
    assert data["Run_Summary"][0]["Verified_Official"] == 1         # only the current run's detail-verified job


# ---------------------------------------------------------------------------
# TEST J — durable per-job verification decisions
# ---------------------------------------------------------------------------
def test_verification_decisions_are_durable_and_bounded(tmp_path):
    rt = ProductionSearchRuntime(_settings(tmp_path), "runJ", fixture_mode=True)
    with StateStore(rt.settings.state_db) as store:
        store.create_run("runJ", "none")
        _stage(store, "runJ", "search", source_job_id="1", evidence="OFFICIAL_SEARCH_LIVE")
        _stage(store, "runJ", "detail", source_job_id="2", evidence="OFFICIAL_DETAIL_LIVE",
               title="Detail Role", revision_kind="DETAIL")
        _stage(store, "runJ", "closed", source_job_id="3", evidence="OFFICIAL_SEARCH_LIVE",
               title="Closed Role", is_active="INACTIVE")
        # A portal lead (non-official host) for a DISTINCT role so it stays its own canonical.
        store.stage_raw_observation(
            "portal", "runJ", "portal-inst", "h-portal", coverage_id="c9", attempt_id="a9",
            source_family="portal", source_job_id="Z-1", source_url="https://jobs.example.com/z/1",
            canonical_url="https://jobs.example.com/z/1", company="Acme", title="Portal Only Role",
            location="Bengaluru", posted_at="2026-09-01", is_active="ACTIVE",
            source_identity="portal-inst::Z-1", detail={"verification_level": "PORTAL_LIVE"})
        canonicalize_run(store, "runJ")
        rt._persist_verification_decisions(store)

        decisions = {d["canonical_id"]: d for d in store.list_verification_decisions("runJ")}
        levels = sorted(d["verification_level"] for d in decisions.values())
        # Search-only is NOT auto-verified; a detail-hydrated official IS; a portal
        # observation stays a lead; a closed official is not "live-verified".
        assert "OFFICIAL_SEARCH_LIVE" in levels
        assert "VERIFIED_OFFICIAL" in levels
        assert "PORTAL_CURRENT_LEAD" in levels
        # Every decision records the classifier + policy version and evidence revisions.
        for d in decisions.values():
            assert d["classifier_version"] and d["policy_fingerprint"] is not None
            import json as _json
            assert isinstance(_json.loads(d["evidence_revision_ids"]), list)
        # The closed role carries positive closure evidence and a CLOSED lifecycle.
        closed = [d for d in decisions.values() if d["lifecycle_result"] == "CLOSED"]
        assert closed and closed[0]["closure_evidence"] == "CLOSED_POSITIVE_EVIDENCE"

        # Append-only + idempotent: re-persisting does not duplicate decisions.
        n = len(store.list_verification_decisions("runJ"))
        rt._persist_verification_decisions(store)
        assert len(store.list_verification_decisions("runJ")) == n


# ---------------------------------------------------------------------------
# TEST K — single freshness authority; future date -> DATA_CONFLICT
# ---------------------------------------------------------------------------
def test_future_posted_date_is_data_conflict(tmp_path):
    now = datetime.datetime(2026, 9, 8, tzinfo=datetime.timezone.utc)
    band = ProductionSearchRuntime._freshness_band("2027-01-01", "EMPLOYER_POSTED_AT", now=now)
    assert band == "DATA_CONFLICT"          # an impossible future date, never '0-7 days'


def test_known_posted_date_maps_to_band(tmp_path):
    now = datetime.datetime(2026, 9, 8, tzinfo=datetime.timezone.utc)
    assert ProductionSearchRuntime._freshness_band("2026-09-04", "EMPLOYER_POSTED_AT", now=now) == "0-7 days"
    assert ProductionSearchRuntime._freshness_band("2026-08-20", "EMPLOYER_POSTED_AT", now=now) == "15-30 days"


def test_updated_only_date_does_not_masquerade_as_posted(tmp_path):
    now = datetime.datetime(2026, 9, 8, tzinfo=datetime.timezone.utc)
    # An employer UPDATED date is not a posted date -> no age band, just live-date-unknown.
    band = ProductionSearchRuntime._freshness_band("2026-09-04", "EMPLOYER_UPDATED_AT", now=now,
                                                   has_live_official_page=True)
    assert band == "LIVE_DATE_UNKNOWN"


def test_uses_canonical_policy_freshness_function():
    # The runtime must delegate to the ONE canonical policy freshness function
    # (single authority) — not a private duplicate.
    from atlas.policy.rules import freshness_band as policy_fn
    now = datetime.datetime(2026, 9, 8, tzinfo=datetime.timezone.utc)
    for iso, expect in [("2026-09-08", "0-7 days"), ("2026-07-01", "STALE")]:
        d = datetime.date.fromisoformat(iso)
        assert ProductionSearchRuntime._freshness_band(iso, "EMPLOYER_POSTED_AT", now=now) == policy_fn(
            d, today=now.date(), has_live_official_page=True)
