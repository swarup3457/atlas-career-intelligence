"""Phase 0.9 — reconciliation, canonical store, migration & transaction tests.

Exercises the StateStore v3 canonical layer (canonical jobs / observations /
status history / quarantine), idempotent re-ingestion, multi-source and
repost detection, closed-status reporting, migration safety (v1/v2 remain
intact), and transaction rollback. All offline, tmp_path only.
"""

from __future__ import annotations

import random

import pytest

from atlas.data_integrity import adversarial as adv
from atlas.data_integrity.identity import IdentityResolver
from atlas.data_integrity.ingestion import WorkbookIngestor, ingest_workbook
from atlas.data_integrity.mapping import default_mapping
from atlas.data_integrity.reconciliation import Reconciler
from atlas.data_integrity.validation import Validator
from atlas.persistence.sqlite import StateStore

pytestmark = [pytest.mark.integration]


def _reconcile(store, path, mapping, di_run_id):
    result = ingest_workbook(path, mapping=mapping, clock=lambda: "T")
    validation = Validator(mapping).validate(result.records)
    identity = IdentityResolver(mapping).resolve(result.records)
    return Reconciler(store).reconcile(
        di_run_id, str(path), result.records, validation, identity, source_hash="h"
    )


def _write_scenario(code, tmp_path, mapping, base=None):
    base = base or []
    scenario = adv.scenario_by_code(code)
    return adv.generate_scenario_copy(base, scenario, tmp_path, seed=1)


# ---------------------------------------------------------------------------
# Migration safety
# ---------------------------------------------------------------------------
def test_migration_v3_present_and_v1_v2_intact(tmp_path):
    store = StateStore(tmp_path / "s.sqlite")
    try:
        assert store.schema_version() == 6
        versions = [
            r[0]
            for r in store._conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5, 6]
        # v1/v2 tables still work
        store.create_run("run-1", controller="none")
        assert store.get_run("run-1")["status"] == "RUNNING"
        # v3 tables exist
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"canonical_jobs", "job_observations", "status_history", "quarantine"} <= tables
    finally:
        store.close()


def test_reopen_does_not_rerun_migrations(tmp_path):
    db = tmp_path / "s.sqlite"
    store = StateStore(db)
    store.upsert_canonical_job("job::x|1", company="X", job_id="1", status="ACTIVE")
    store.close()
    store2 = StateStore(db)
    try:
        assert store2.schema_version() == 6
        assert store2.get_canonical_job("job::x|1") is not None
    finally:
        store2.close()


# ---------------------------------------------------------------------------
# Idempotent transaction support
# ---------------------------------------------------------------------------
def test_transaction_rolls_back_on_error(tmp_path):
    store = StateStore(tmp_path / "s.sqlite")
    try:
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.upsert_canonical_job("job::a|1", company="A", job_id="1")
                assert store.count_canonical_jobs() == 1  # visible mid-transaction
                raise RuntimeError("boom")
        # fully rolled back
        assert store.count_canonical_jobs() == 0
    finally:
        store.close()


def test_transaction_commits_atomically(tmp_path):
    store = StateStore(tmp_path / "s.sqlite")
    try:
        with store.transaction():
            store.upsert_canonical_job("job::a|1", company="A", job_id="1")
            store.upsert_canonical_job("job::b|2", company="B", job_id="2")
        assert store.count_canonical_jobs() == 2
    finally:
        store.close()


def test_operation_idempotency_guard(tmp_path):
    store = StateStore(tmp_path / "s.sqlite")
    try:
        assert store.operation_applied("op-1") is None
        with store.transaction():
            store.mark_operation("op-1", {"n": 1})
        assert store.operation_applied("op-1") == {"n": 1}
        # marking again is a no-op (INSERT OR IGNORE)
        with store.transaction():
            store.mark_operation("op-1", {"n": 999})
        assert store.operation_applied("op-1") == {"n": 1}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Import idempotency (re-ingesting the same data changes nothing)
# ---------------------------------------------------------------------------
def test_reconcile_fixture_is_idempotent(real_fixture, tmp_path):
    m = default_mapping()
    store = StateStore(tmp_path / "s.sqlite")
    try:
        r1 = _reconcile(store, real_fixture, m, "run-1")
        canon1, obs1 = store.count_canonical_jobs(), store.count_observations()
        assert r1.created == 25
        assert canon1 == 25

        r2 = _reconcile(store, real_fixture, m, "run-2")
        # second pass: nothing new
        assert r2.created == 0
        assert r2.updated == 0
        assert r2.observations_added == 0
        assert r2.status_changes == 0
        assert store.count_canonical_jobs() == canon1
        assert store.count_observations() == obs1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Multi-source / repost / closed
# ---------------------------------------------------------------------------
def test_multi_source_relationship(tmp_path):
    m = default_mapping()
    path = _write_scenario("L", tmp_path, m)
    result = ingest_workbook(path, mapping=m, clock=lambda: "T")
    resolution = IdentityResolver(m).resolve(result.records)
    rels = set()
    for c in resolution.clusters:
        rels.update(r.value for r in c.relationships)
    assert "MULTI_SOURCE" in rels


def test_repost_relationship(tmp_path):
    m = default_mapping()
    path = _write_scenario("K", tmp_path, m)
    result = ingest_workbook(path, mapping=m, clock=lambda: "T")
    resolution = IdentityResolver(m).resolve(result.records)
    rels = set()
    for c in resolution.clusters:
        rels.update(r.value for r in c.relationships)
    assert "REPOST" in rels


def test_closed_status_reporting(tmp_path):
    m = default_mapping()
    path = _write_scenario("M", tmp_path, m)
    store = StateStore(tmp_path / "s.sqlite")
    try:
        rc = _reconcile(store, path, m, "closed-run")
        assert rc.closed >= 1
        closed = [j for j in store.list_canonical_jobs() if j["current_status"] == "CLOSED"]
        assert len(closed) == 1
        # status history recorded the closure
        hist = store.list_status_history(closed[0]["canonical_id"])
        assert any(h["to_status"] == "CLOSED" for h in hist)
    finally:
        store.close()


def test_cross_sheet_contradiction_closes_job(tmp_path):
    m = default_mapping()
    path = _write_scenario("AI", tmp_path, m)
    store = StateStore(tmp_path / "s.sqlite")
    try:
        rc = _reconcile(store, path, m, "contradiction")
        assert rc.closed >= 1
        # both sheets contributed observations to one canonical job
        closed = [j for j in store.list_canonical_jobs() if j["current_status"] == "CLOSED"]
        assert closed
        obs = store.list_observations(closed[0]["canonical_id"])
        assert len(obs) >= 2
    finally:
        store.close()


def test_reconcile_writes_observations_and_history(tmp_path, real_fixture):
    m = default_mapping()
    store = StateStore(tmp_path / "s.sqlite")
    try:
        _reconcile(store, real_fixture, m, "run-1")
        assert store.count_observations() >= store.count_canonical_jobs()
        # every canonical job has at least one status-history row
        for job in store.list_canonical_jobs():
            assert store.list_status_history(job["canonical_id"])
    finally:
        store.close()
