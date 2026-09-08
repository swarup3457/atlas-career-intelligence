"""Phase 0.95 — restore verification, schema-compatibility gating, and the
target-directory safety guard. Fully offline, tmp_path only.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import pytest

import atlas
from atlas.backup.backup import backup_dir_for, create_backup
from atlas.backup.clock import FixedClock
from atlas.backup.manifest import MANIFEST_FILENAME, BackupManifest
from atlas.backup.restore import (
    FAILED_VALIDATION,
    REFUSED_INCOMPATIBLE_SCHEMA,
    REFUSED_INVALID_BACKUP,
    RESTORE_MARKER_NAME,
    RESTORED,
    RestoreSafetyError,
    restore_backup,
)
from atlas.persistence.sqlite import StateStore
from tests._phase095_helpers import make_settings, seed_agents_and_skills

pytestmark = [pytest.mark.integration]

_FIXED = datetime.datetime(2026, 9, 6, 18, 0, 0, tzinfo=datetime.timezone.utc)


def _make_backup(tmp_path):
    settings = make_settings(tmp_path / "src")
    seed_agents_and_skills(settings)
    with StateStore(settings.state_db) as store:
        store.create_run("run-1", controller="none")
        for i in range(5):
            store.upsert_task(task_id=f"T-{i}", run_id="run-1", task_type="demo", status="SUCCESS")
    with StateStore(settings.checkpoint_db):
        pass
    backups = tmp_path / "backups"
    manifest = create_backup(settings, backups, clock=FixedClock(_FIXED))
    return settings, backups, manifest, backup_dir_for(backups, manifest)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_restore_happy_path_writes_marker_and_validates(tmp_path):
    _settings, _backups, manifest, bpath = _make_backup(tmp_path)
    target = tmp_path / "restored"

    result = restore_backup(bpath, target)

    assert result.status == RESTORED, result.render()
    assert result.ok
    assert result.backup_id == manifest.backup_id
    assert result.state_schema_version == 9
    assert result.checkpoint_ok is True
    assert result.config_ok is True
    assert (target / RESTORE_MARKER_NAME).exists()

    # restored state DB actually has the rows
    with StateStore(target / "state" / "atlas_state.sqlite") as store:
        assert len(store.list_tasks("run-1")) == 5


# ---------------------------------------------------------------------------
# Refuse an invalid backup
# ---------------------------------------------------------------------------
def test_restore_refuses_invalid_backup(tmp_path):
    _settings, _backups, _manifest, bpath = _make_backup(tmp_path)
    # corrupt one file so verify fails
    victim = bpath / "skills" / "extract_jobs.md"
    victim.write_text("tampered", encoding="utf-8")

    target = tmp_path / "restored"
    result = restore_backup(bpath, target)
    assert result.status == REFUSED_INVALID_BACKUP
    assert not result.ok
    assert result.problems
    # nothing meaningful should have been written
    assert not (target / RESTORE_MARKER_NAME).exists()


# ---------------------------------------------------------------------------
# Schema compatibility
# ---------------------------------------------------------------------------
def _rewrite_manifest_schema(bpath: Path, new_version: int) -> None:
    manifest = BackupManifest.from_json((bpath / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    manifest.state_schema_version = new_version
    (bpath / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")


def test_restore_refuses_newer_schema(tmp_path):
    _settings, _backups, _manifest, bpath = _make_backup(tmp_path)
    _rewrite_manifest_schema(bpath, 999)  # a future Atlas
    result = restore_backup(bpath, tmp_path / "restored")
    assert result.status == REFUSED_INCOMPATIBLE_SCHEMA
    assert not result.ok


def test_restore_allows_equal_or_older_schema(tmp_path):
    _settings, _backups, _manifest, bpath = _make_backup(tmp_path)
    _rewrite_manifest_schema(bpath, 2)  # older; forward migrations can run
    result = restore_backup(bpath, tmp_path / "restored")
    assert result.status == RESTORED, result.render()


# ---------------------------------------------------------------------------
# Target-directory safety guard
# ---------------------------------------------------------------------------
def test_restore_refuses_real_project_paths(tmp_path):
    _settings, _backups, _manifest, bpath = _make_backup(tmp_path)
    real_root = Path(atlas.__file__).resolve().parents[1]

    for unsafe in (real_root, real_root / "state", real_root / "fixtures" / "real",
                   real_root / ".browser-profile-chrome", real_root / ".venv"):
        with pytest.raises(RestoreSafetyError):
            restore_backup(bpath, unsafe)

    # sanity: a genuinely disposable target is allowed
    assert restore_backup(bpath, tmp_path / "ok").ok


# ---------------------------------------------------------------------------
# FAILED_VALIDATION: a backup that VERIFIES (all hashes match, no corrupt
# SQLite) but whose config export is not parseable JSON. verify_backup does
# not parse config content, so this reaches restore's own post-copy
# validation and fails there.
# ---------------------------------------------------------------------------
def test_restore_reports_failed_validation_on_bad_config(tmp_path):
    bpath = tmp_path / "fake_backup"
    (bpath / "config").mkdir(parents=True)
    bad_json = b"{ this is not valid json"
    cfg_file = bpath / "config" / "config_export.json"
    cfg_file.write_bytes(bad_json)

    manifest = BackupManifest(
        backup_id="20260906T180000Z-FAKE",
        created_at="2026-09-06T18:00:00+00:00",
        atlas_version="0.1.0-foundation",
        python_version="3.12.0",
        langgraph_version="x",
        playwright_version="x",
        state_schema_version=3,
        checkpoint_schema_info="fake",
        platform="test",
        config_fingerprint="0" * 64,
    )
    from atlas.backup.manifest import IncludedComponent

    manifest.included_components = [
        IncludedComponent(
            relative_path="config/config_export.json",
            sha256=hashlib.sha256(bad_json).hexdigest(),
            size=len(bad_json),
            kind="config",
        )
    ]
    (bpath / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")

    # verify passes (config content is not parsed at verify time)...
    from atlas.backup.verify import verify_backup

    assert verify_backup(bpath).ok
    # ...but restore's own validation catches the bad config.
    result = restore_backup(bpath, tmp_path / "restored")
    assert result.status == FAILED_VALIDATION
    assert result.config_ok is False
    assert any("config_export.json" in p for p in result.problems)
