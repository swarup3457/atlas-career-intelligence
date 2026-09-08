"""Phase 1A.5: persistence — migration v5, reopen safety, idempotency."""

from __future__ import annotations

import pytest

from atlas.persistence.sqlite import SCHEMA_VERSION, StateStore

pytestmark = pytest.mark.unit


def test_migration_v5_present_and_prior_intact(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        assert SCHEMA_VERSION >= 5
        assert store.schema_version() == SCHEMA_VERSION
        versions = [r[0] for r in store._conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        assert versions == list(range(1, SCHEMA_VERSION + 1))
        tables = {r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"company_registry", "company_aliases", "company_source_relationships",
                "source_discovery_observations"} <= tables
        # Phase 1B tables present
        assert {"source_instances", "coverage_attempts", "coverage_plans"} <= tables
        # prior-phase tables still present
        assert {"canonical_jobs", "coverage_records", "companies"} <= tables


def test_reopen_does_not_rerun_migrations(tmp_path):
    db = tmp_path / "s.sqlite"
    store = StateStore(db)
    store.upsert_company("co-x", "Acme", "acme", official_domain="acme.com")
    store.close()
    store2 = StateStore(db)
    try:
        assert store2.schema_version() == 7
        assert store2.get_company("co-x") is not None
    finally:
        store2.close()


def test_upsert_company_idempotent(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        assert store.upsert_company("co-1", "Acme", "acme", official_domain="acme.com") == "created"
        assert store.upsert_company("co-1", "Acme Corp", "acme") == "updated"
        assert store.count_companies() == 1
        # COALESCE keeps a known domain even when a later update omits it
        assert store.get_company("co-1")["official_domain"] == "acme.com"


def test_alias_and_observation_idempotency(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        store.upsert_company("co-1", "Acme", "acme")
        assert store.add_company_alias("al-1", "co-1", "Acme Inc", "acme inc") is True
        assert store.add_company_alias("al-1", "co-1", "Acme Inc", "acme inc") is False
        assert store.add_source_discovery_observation("obs-1", "co-1", "FINGERPRINT") is True
        assert store.add_source_discovery_observation("obs-1", "co-1", "FINGERPRINT") is False


def test_backup_restore_preserves_company_data(tmp_path):
    from atlas.backup.backup import create_backup, backup_dir_for
    from atlas.backup.restore import restore_backup
    from tests._phase095_helpers import make_settings, seed_agents_and_skills

    settings = make_settings(tmp_path / "home")
    settings.ensure_directories()
    seed_agents_and_skills(settings)
    with StateStore(settings.state_db) as store:
        store.upsert_company("co-1", "Acme", "acme", official_domain="acme.com")
        store.upsert_source_relationship("rel-1", "co-1", "co-1--ats_workday--acme", "ATS_WORKDAY", tenant="acme")
    # a checkpoint db must exist for a consistent backup
    from atlas.orchestration.checkpoints import open_checkpointer
    with open_checkpointer(settings.checkpoint_db):
        pass

    backups = tmp_path / "backups"
    manifest = create_backup(settings, backups)
    assert manifest.state_schema_version == 7

    target = tmp_path / "restored"
    result = restore_backup(backup_dir_for(backups, manifest), target)
    assert result.ok
    with StateStore(target / "state" / "atlas_state.sqlite") as restored:
        assert restored.get_company("co-1") is not None
        assert restored.count_source_relationships() == 1
