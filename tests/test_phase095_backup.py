"""Phase 0.95 — backup creation, consistency, config redaction, verification,
and atomicity/crash-safety. Fully offline, tmp_path only.
"""

from __future__ import annotations

import sqlite3

import pytest

from atlas.backup import backup as backup_mod
from atlas.backup.backup import BackupError, backup_dir_for, create_backup
from atlas.backup.clock import FixedClock
from atlas.backup.config_export import (
    REDACTED,
    build_config_export,
    export_config_files,
    export_settings,
    is_sensitive_key,
    redact_structure,
)
from atlas.backup.manifest import MANIFEST_FILENAME, BackupManifest
from atlas.backup.sqlite_backup import backup_sqlite_database, integrity_check
from atlas.backup.verify import HASH_MISMATCH, MANIFEST_MALFORMED, MISSING_FILE, verify_backup
from atlas.persistence.sqlite import StateStore
from tests._phase095_helpers import make_settings, seed_agents_and_skills

pytestmark = [pytest.mark.integration]

_FIXED = __import__("datetime").datetime(2026, 9, 6, 18, 0, 0, tzinfo=__import__("datetime").timezone.utc)


def _clock():
    return FixedClock(_FIXED)


def _seed_state(settings) -> int:
    """Create a small amount of committed state; return task count."""
    with StateStore(settings.state_db) as store:
        store.create_run("run-1", controller="none", metadata={"demo": True})
        for i in range(7):
            store.upsert_task(task_id=f"T-{i}", run_id="run-1", task_type="demo", status="SUCCESS")
        return len(store.list_tasks("run-1"))


# ---------------------------------------------------------------------------
# Consistent SQLite backup (WAL-mode) — the core correctness proof
# ---------------------------------------------------------------------------
def test_consistent_sqlite_backup_captures_committed_rows_with_wal_open(tmp_path):
    settings = make_settings(tmp_path)
    # Open the StateStore and keep it open so WAL is active and a -wal file
    # exists alongside the main db while we take the backup.
    with StateStore(settings.state_db) as store:
        store.create_run("run-1", controller="none")
        for i in range(20):
            store.upsert_task(task_id=f"T-{i}", run_id="run-1", task_type="demo", status="SUCCESS")

        # WAL journal mode must actually be in effect for this to be a
        # meaningful test.
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

        dest = tmp_path / "snapshot" / "state.sqlite"
        backup_sqlite_database(settings.state_db, dest)

        # The snapshot must be a transactionally consistent, self-contained
        # database (no -wal needed) that returns exactly the committed rows.
        assert not (dest.parent / (dest.name + "-wal")).exists()
        snap = sqlite3.connect(str(dest))
        try:
            count = snap.execute("SELECT COUNT(*) FROM tasks WHERE run_id='run-1'").fetchone()[0]
        finally:
            snap.close()
        assert count == 20
        assert integrity_check(dest) == []


def test_sqlite_backup_missing_source_raises(tmp_path):
    from atlas.backup.sqlite_backup import SqliteBackupError

    with pytest.raises(SqliteBackupError):
        backup_sqlite_database(tmp_path / "nope.sqlite", tmp_path / "out.sqlite")


# ---------------------------------------------------------------------------
# Config export redaction
# ---------------------------------------------------------------------------
class TestConfigRedaction:
    def test_is_sensitive_key(self):
        for key in ("password", "API_TOKEN", "session_cookie", "Authorization", "client_secret", "db_credential"):
            assert is_sensitive_key(key), key
        for key in ("browser_channel", "batch_size", "controller", "state_db"):
            assert not is_sensitive_key(key), key

    def test_redact_structure_nested_and_lists(self):
        payload = {
            "api_token": "abc123",
            "keep": "visible",
            "nested": {"password": "hunter2", "fine": 1},
            "items": [{"secret": "s"}, {"ok": 2}],
        }
        red = redact_structure(payload)
        assert red["api_token"] == REDACTED
        assert red["keep"] == "visible"
        assert red["nested"]["password"] == REDACTED
        assert red["nested"]["fine"] == 1
        assert red["items"][0]["secret"] == REDACTED
        assert red["items"][1]["ok"] == 2

    def test_export_config_files_redacts_synthetic_secrets(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "default.yaml").write_text(
            "browser_channel: chrome\nbatch_size: 10\n", encoding="utf-8"
        )
        # A future/local config key that happens to be sensitively named.
        (cfg / "local.yaml").write_text(
            "linkedin_password: hunter2\napi_token: zzz\nharmless: 5\n", encoding="utf-8"
        )
        exported = export_config_files(cfg)
        assert exported["default.yaml"]["browser_channel"] == "chrome"
        assert exported["local.yaml"]["linkedin_password"] == REDACTED
        assert exported["local.yaml"]["api_token"] == REDACTED
        assert exported["local.yaml"]["harmless"] == 5

    def test_export_settings_stringifies_paths(self, tmp_path):
        settings = make_settings(tmp_path)
        exported = export_settings(settings)
        # No Path objects survive (JSON-safe); known path field is a string.
        assert isinstance(exported["state_db"], str)
        assert all(not isinstance(v, __import__("pathlib").Path) for v in exported.values())

    def test_build_config_export_shape(self, tmp_path):
        settings = make_settings(tmp_path)
        export = build_config_export(settings, settings.project_root / "config")
        assert "settings" in export and "config_files" in export


# ---------------------------------------------------------------------------
# Backup creation + manifest + verification
# ---------------------------------------------------------------------------
def test_create_backup_produces_verified_manifest(tmp_path):
    settings = make_settings(tmp_path)
    seed_agents_and_skills(settings)
    _seed_state(settings)
    # also create a checkpoint db + a run manifest so those components exist
    with StateStore(settings.checkpoint_db):
        pass
    (settings.output_dir / "run_manifest_run-1.json").write_text('{"run_id": "run-1"}', encoding="utf-8")

    backups = tmp_path / "backups"
    manifest = create_backup(settings, backups, clock=_clock())
    bpath = backup_dir_for(backups, manifest)

    assert bpath.exists()
    assert manifest.state_schema_version == 7
    assert manifest.backup_id.startswith("20260906T180000Z-")

    rels = {c.relative_path for c in manifest.included_components}
    assert f"state/{settings.state_db.name}" in rels
    assert f"state/{settings.checkpoint_db.name}" in rels
    assert "config/config_export.json" in rels
    assert "agents/company_researcher.md" in rels
    assert "agents/nested/helper.md" in rels
    assert "skills/extract_jobs.md" in rels
    assert "run_manifests/run_manifest_run-1.json" in rels

    # every included file has a non-empty sha256 and a real size
    for comp in manifest.included_components:
        assert len(comp.sha256) == 64
        assert comp.size >= 0

    # verification passes on the published backup
    vr = verify_backup(bpath)
    assert vr.ok, vr.render()
    assert vr.checked_files == len(manifest.included_components)

    # manifest round-trips through JSON
    reloaded = BackupManifest.from_json((bpath / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert reloaded.to_dict() == manifest.to_dict()


def test_backup_never_includes_secrets_or_profile(tmp_path):
    settings = make_settings(tmp_path)
    seed_agents_and_skills(settings)
    _seed_state(settings)
    # put a fake "secret" file inside the browser profile — it must NOT be captured
    (settings.browser_profile / "Cookies").write_text("SECRET-COOKIE", encoding="utf-8")

    backups = tmp_path / "backups"
    manifest = create_backup(settings, backups, clock=_clock())

    for comp in manifest.included_components:
        low = comp.relative_path.lower()
        assert "profile" not in low
        assert "cookie" not in low
        assert ".venv" not in low
    excluded_names = {e.name for e in manifest.excluded_components}
    assert "browser_profile" in excluded_names
    assert ".venv" in excluded_names
    assert "secrets" in excluded_names


# ---------------------------------------------------------------------------
# Corrupted backup detection (section 7)
# ---------------------------------------------------------------------------
def test_corrupted_backup_flagged_by_verify(tmp_path):
    settings = make_settings(tmp_path)
    seed_agents_and_skills(settings)
    _seed_state(settings)
    backups = tmp_path / "backups"
    manifest = create_backup(settings, backups, clock=_clock())
    bpath = backup_dir_for(backups, manifest)

    # flip some bytes in one backed-up file (the agents markdown)
    victim = bpath / "agents" / "company_researcher.md"
    data = bytearray(victim.read_bytes())
    data[0] ^= 0xFF
    victim.write_bytes(bytes(data))

    vr = verify_backup(bpath)
    assert not vr.ok
    kinds = {(p.kind, p.path) for p in vr.problems}
    assert (HASH_MISMATCH, "agents/company_researcher.md") in kinds


def test_corrupted_state_db_detected(tmp_path):
    settings = make_settings(tmp_path)
    _seed_state(settings)
    backups = tmp_path / "backups"
    manifest = create_backup(settings, backups, clock=_clock())
    bpath = backup_dir_for(backups, manifest)

    victim = bpath / "state" / settings.state_db.name
    blob = bytearray(victim.read_bytes())
    # corrupt the SQLite header region
    for i in range(100, 200):
        blob[i] ^= 0xAA
    victim.write_bytes(bytes(blob))

    vr = verify_backup(bpath)
    assert not vr.ok
    # a byte change trips the hash first (still a failure that flags the file)
    assert any(p.path == f"state/{settings.state_db.name}" for p in vr.problems)


# ---------------------------------------------------------------------------
# Incomplete backup detection (section 8)
# ---------------------------------------------------------------------------
def test_incomplete_backup_missing_file_flagged(tmp_path):
    settings = make_settings(tmp_path)
    seed_agents_and_skills(settings)
    _seed_state(settings)
    backups = tmp_path / "backups"
    manifest = create_backup(settings, backups, clock=_clock())
    bpath = backup_dir_for(backups, manifest)

    removed = bpath / "skills" / "extract_jobs.md"
    removed.unlink()

    vr = verify_backup(bpath)
    assert not vr.ok
    assert any(p.kind == MISSING_FILE and p.path == "skills/extract_jobs.md" for p in vr.problems)


def test_malformed_manifest_flagged(tmp_path):
    settings = make_settings(tmp_path)
    _seed_state(settings)
    backups = tmp_path / "backups"
    manifest = create_backup(settings, backups, clock=_clock())
    bpath = backup_dir_for(backups, manifest)
    (bpath / MANIFEST_FILENAME).write_text("{not valid json", encoding="utf-8")
    vr = verify_backup(bpath)
    assert not vr.ok
    assert any(p.kind == MANIFEST_MALFORMED for p in vr.problems)


# ---------------------------------------------------------------------------
# Atomicity / crash-safety (section 5)
# ---------------------------------------------------------------------------
def test_interrupted_backup_leaves_no_valid_backup_and_prior_untouched(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    seed_agents_and_skills(settings)
    _seed_state(settings)
    backups = tmp_path / "backups"

    # 1) a prior good backup
    good = create_backup(settings, backups, clock=FixedClock(_FIXED))
    good_path = backup_dir_for(backups, good)
    assert verify_backup(good_path).ok

    # 2) a second backup that "crashes" mid-way: patch the atomic publish
    #    (os.replace) to raise, simulating a kill right before publish.
    later = FixedClock(_FIXED.replace(hour=19))

    real_replace = backup_mod.os.replace

    def boom(src, dst, *a, **k):
        raise RuntimeError("simulated crash during publish")

    monkeypatch.setattr(backup_mod.os, "replace", boom)
    with pytest.raises(RuntimeError):
        create_backup(settings, backups, clock=later)
    monkeypatch.setattr(backup_mod.os, "replace", real_replace)

    # No valid backup at the (would-be) target path of the crashed attempt.
    from atlas.backup.manifest import generate_backup_id

    crashed_id_prefix = "20260906T190000Z-"
    crashed_dirs = [d for d in backups.iterdir() if d.name.startswith(crashed_id_prefix)]
    assert crashed_dirs == []  # nothing published
    # No leftover staging dirs either.
    assert [d for d in backups.iterdir() if d.name.startswith(".staging-")] == []

    # The prior good backup is completely untouched and still verifies.
    assert verify_backup(good_path).ok


def test_backup_id_collision_refused(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    _seed_state(settings)
    backups = tmp_path / "backups"
    # Force generate_backup_id to a constant so the second call collides.
    monkeypatch.setattr(backup_mod, "generate_backup_id", lambda moment: "COLLIDE-ID")
    create_backup(settings, backups, clock=_clock())
    with pytest.raises(BackupError):
        create_backup(settings, backups, clock=_clock())
