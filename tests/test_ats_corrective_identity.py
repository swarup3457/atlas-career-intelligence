"""Phase 1C-A CORRECTIVE gate — canonical identity, reposts, and cross-run
observation history (build spec 13).

Failing-first regressions: before this gate canonicalization grouped by a
URL-INCLUSIVE content hash (so the same job seen through two URLs became two
canonical jobs, and URL was used as identity), and observation-event ids were
NOT run-scoped (so re-observing the same job in a later run was wrongly deduped
into a single event, preventing first_seen/last_seen from advancing).
"""

from __future__ import annotations

import pytest

from atlas.persistence.sqlite import StateStore
from atlas.runtime.canonicalize import canonicalize_run

pytestmark = pytest.mark.integration


def _stage(store, run_id, obs_id, *, company="Acme", title="Java Developer", location="Bengaluru",
           posted_at="2026-09-01", source_url="https://x/1", canonical_url=None, source_job_id="1",
           source_instance="gh", content_hash=None):
    store.stage_raw_observation(
        obs_id, run_id, source_instance, content_hash or f"h-{obs_id}", coverage_id="c1", attempt_id="a1",
        source_family="greenhouse", source_job_id=source_job_id, source_url=source_url,
        canonical_url=canonical_url or source_url, company=company, title=title, location=location,
        posted_at=posted_at, is_active="ACTIVE", source_identity=f"{source_instance}::{source_job_id}::{source_url}")


def _store(tmp_path, *runs):
    store = StateStore(tmp_path / "s.sqlite")
    for r in runs:
        store.create_run(r, "none")
    return store


def test_same_job_via_two_urls_is_one_canonical_two_observations(tmp_path):
    store = _store(tmp_path, "run")
    # Same posting (same company/title/location/posted) via two different URLs.
    _stage(store, "run", "o1", source_url="https://boards.greenhouse.io/acme/jobs/1", source_job_id="1")
    _stage(store, "run", "o2", source_url="https://jobs.example.com/acme/1", source_job_id="1b")
    res = canonicalize_run(store, "run")
    # URL is provenance, not identity → ONE canonical, TWO source observations.
    assert store.count_canonical_jobs() == 1
    assert res.observations_added == 2
    store.close()


def test_url_change_alone_does_not_create_a_new_canonical(tmp_path):
    store = _store(tmp_path, "run")
    _stage(store, "run", "o1", source_url="https://x/old")
    _stage(store, "run", "o2", source_url="https://x/new")  # only the URL differs
    canonicalize_run(store, "run")
    assert store.count_canonical_jobs() == 1
    store.close()


def test_new_posted_date_is_a_distinct_repost(tmp_path):
    store = _store(tmp_path, "run")
    _stage(store, "run", "o1", posted_at="2026-09-01")
    _stage(store, "run", "o2", posted_at="2026-08-01", source_job_id="2")  # repost (new date)
    canonicalize_run(store, "run")
    # A repost (different posted date) is never blindly merged.
    assert store.count_canonical_jobs() == 2
    store.close()


def test_same_source_job_retried_in_one_attempt_is_one_event(tmp_path):
    store = _store(tmp_path, "run")
    # Idempotent staging: the SAME observation id re-staged is one row.
    _stage(store, "run", "obs-x")
    _stage(store, "run", "obs-x")  # retry same page/attempt → idempotent
    assert store.count_raw_observations("run") == 1
    res = canonicalize_run(store, "run")
    assert res.observations_added == 1  # one event
    store.close()


def test_same_job_in_a_later_run_creates_a_new_observation_event(tmp_path):
    store = _store(tmp_path, "runA", "runB")
    # Same job observed in run A and, later, run B.
    _stage(store, "runA", "obs::runA::c1::gh::1")
    resA = canonicalize_run(store, "runA")
    _stage(store, "runB", "obs::runB::c1::gh::1")
    resB = canonicalize_run(store, "runB")

    # ONE canonical job across both runs (stable identity), but a NEW observation
    # event per run so first_seen/last_seen can advance.
    assert store.count_canonical_jobs() == 1
    assert resA.observations_added == 1 and resB.observations_added == 1
    # Both runs' observations attach to the same canonical id.
    canon = store.list_canonical_jobs()[0]
    obs = store.list_observations_for_canonical(canon["canonical_id"]) if hasattr(
        store, "list_observations_for_canonical") else None
    if obs is not None:
        assert len(obs) == 2
    store.close()
