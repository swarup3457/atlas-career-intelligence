"""Phase 1A: DiscoveryResult → canonical provenance/dedupe tests.

Reuses the Phase 0.9 identity engine (never replaces it)."""

from __future__ import annotations

import pytest

from atlas.persistence.sqlite import StateStore
from atlas.sources.models import ActiveState, DiscoveryResult, SourceType
from atlas.sources.provenance import ingest_discovery_results

pytestmark = pytest.mark.unit


def _dr(stype, inst, jid, company, title, loc, when="2026-09-01T00:00:00Z"):
    return DiscoveryResult(
        source_type=stype, source_instance=inst, source_job_id=jid, company=company,
        title=title, location=loc, is_active=ActiveState.ACTIVE, discovered_at=when,
        adapter_version="1.2.3", parser_version="4.5.6",
        source_url=f"https://{inst}/{jid}",
    )


def test_same_source_observation_twice_no_duplicate(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        r = _dr(SourceType.ATS_WORKDAY, "acme-wd", "W1", "Acme", "Engineer", "Pune")
        ingest_discovery_results(store, [r, r])
        assert store.count_canonical_jobs() == 1
        assert store.count_observations() == 1


def test_multiple_sources_one_canonical_many_observations(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        results = [
            _dr(SourceType.ATS_WORKDAY, "acme-wd", "W1", "Acme Corp", "Senior Engineer", "Bengaluru, India"),
            _dr(SourceType.PORTAL_LARGE, "linkedin", "L1", "Acme Corp.", "Senior Engineer", "Bengaluru India"),
        ]
        summary = ingest_discovery_results(store, results)
        assert store.count_canonical_jobs() == 1
        assert store.count_observations() == 2
        assert summary.multi_source == 1


def test_probable_repost_flagged_not_merged(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        results = [
            _dr(SourceType.ATS_WORKDAY, "acme-wd", "W1", "Acme", "Engineer", "Pune", when="2026-01-01T00:00:00Z"),
            _dr(SourceType.ATS_WORKDAY, "acme-wd", "W2", "Acme", "Engineer", "Pune", when="2026-06-01T00:00:00Z"),
        ]
        summary = ingest_discovery_results(store, results)
        assert store.count_canonical_jobs() == 1
        # Distinct source observations retained (not silently merged).
        assert store.count_observations() == 2
        assert summary.reposts == 1
        canonical_id = summary.canonical_ids[0]
        history = store.list_status_history(canonical_id)
        assert any(h["to_status"] == "REPOST_SUSPECTED" for h in history)


def test_versions_persisted_with_observations(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        r = _dr(SourceType.ATS_LEVER, "beta", "B1", "Beta", "Data Scientist", "Remote")
        summary = ingest_discovery_results(store, [r])
        obs = store.list_observations(summary.canonical_ids[0])[0]
        assert obs["adapter_version"] == "1.2.3"
        assert obs["parser_version"] == "4.5.6"


def test_reingest_is_idempotent(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        results = [
            _dr(SourceType.ATS_WORKDAY, "acme-wd", "W1", "Acme", "Engineer", "Pune"),
            _dr(SourceType.ATS_LEVER, "beta", "B1", "Beta", "Analyst", "Remote"),
        ]
        ingest_discovery_results(store, results)
        c1, o1 = store.count_canonical_jobs(), store.count_observations()
        second = ingest_discovery_results(store, results)
        assert store.count_canonical_jobs() == c1
        assert store.count_observations() == o1
        assert second.canonical_created == 0
        assert second.observations_added == 0
