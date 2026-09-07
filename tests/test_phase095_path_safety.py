"""Phase 0.95 — filesystem path-safety (spec section 16).

Exercises backup/restore/report writers against awkward paths: spaces,
unicode, long names, missing parent directories, and a locked/read-only
destination — proving controlled diagnostics rather than a crash or a
corrupt/partial artifact. Reuses the Phase 0.9 report_writer atomic
locked-file handling approach as a reference. Offline, tmp_path only.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from atlas.backup import backup as backup_mod
from atlas.backup.backup import backup_dir_for, create_backup
from atlas.backup.clock import FixedClock
from atlas.backup.restore import RESTORED, restore_backup
from atlas.backup.verify import verify_backup
from atlas.data_integrity import report_writer as rw
from atlas.persistence.sqlite import StateStore
from tests._phase095_helpers import make_settings, seed_agents_and_skills

pytestmark = [pytest.mark.integration]

_FIXED = datetime.datetime(2026, 9, 6, 18, 0, 0, tzinfo=datetime.timezone.utc)


def _seed(root):
    settings = make_settings(root)
    seed_agents_and_skills(settings)
    with StateStore(settings.state_db) as store:
        store.create_run("run-1", controller="none")
        store.upsert_task(task_id="T-0", run_id="run-1", task_type="demo", status="SUCCESS")
    return settings


@pytest.mark.parametrize(
    "subdir",
    [
        "back ups with spaces",
        "backups_café_测试_Ω",
        "b" + "a" * 80,  # long-ish component
    ],
)
def test_backup_restore_roundtrip_unusual_paths(tmp_path, subdir):
    settings = _seed(tmp_path / "src")
    backups = tmp_path / subdir / "backups"  # missing parents + unusual name
    manifest = create_backup(settings, backups, clock=FixedClock(_FIXED))
    bpath = backup_dir_for(backups, manifest)
    assert verify_backup(bpath).ok

    target = tmp_path / (subdir + " restored")
    result = restore_backup(bpath, target)
    assert result.status == RESTORED, result.render()
    with StateStore(target / "state" / "atlas_state.sqlite") as store:
        assert len(store.list_tasks("run-1")) == 1


def test_backup_auto_creates_deep_missing_parents(tmp_path):
    settings = _seed(tmp_path / "src")
    deep = tmp_path / "a" / "b" / "c" / "d" / "backups"
    assert not deep.exists()
    manifest = create_backup(settings, deep, clock=FixedClock(_FIXED))
    assert backup_dir_for(deep, manifest).exists()


def test_report_writer_atomic_into_unusual_paths(tmp_path):
    """The atomic JSON writer must handle spaces/unicode/missing parents."""
    final = tmp_path / "weird dir" / "réport_测试" / "data.json"
    res = rw.write_json_atomic(final, {"a": 1, "b": [1, 2, 3]})
    assert Path(res.written_path) == final
    assert final.exists()
    import json

    assert json.loads(final.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2, 3]}


def test_report_writer_locked_final_falls_back_to_alternate(tmp_path, monkeypatch):
    """Reference approach from Phase 0.9: a locked final yields a
    deterministic alternate instead of a crash or lost output."""
    final = tmp_path / "space dir" / "report.json"
    final.parent.mkdir(parents=True)
    final.write_text("existing", encoding="utf-8")

    real_replace = rw.os.replace

    def fake_replace(src, dst, *a, **k):
        if Path(dst) == final:
            raise PermissionError("locked by another process")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(rw.os, "replace", fake_replace)
    res = rw.write_json_atomic(final, {"ok": True})
    assert res.locked and res.used_alternate
    assert Path(res.written_path).exists()


def test_backup_locked_publish_is_controlled_no_corruption(tmp_path, monkeypatch):
    """A locked publish destination must fail cleanly: no partial/valid
    backup at the target, no leftover staging, prior backup intact."""
    settings = _seed(tmp_path / "src")
    backups = tmp_path / "backups"

    good = create_backup(settings, backups, clock=FixedClock(_FIXED))
    good_path = backup_dir_for(backups, good)
    assert verify_backup(good_path).ok

    def locked_replace(src, dst, *a, **k):
        raise PermissionError("destination locked")

    monkeypatch.setattr(backup_mod.os, "replace", locked_replace)
    with pytest.raises(PermissionError):
        create_backup(settings, backups, clock=FixedClock(_FIXED.replace(hour=20)))

    # no leftover staging, and the earlier good backup is still valid
    assert [d for d in backups.iterdir() if d.name.startswith(".staging-")] == []
    assert verify_backup(good_path).ok


def test_restore_missing_source_file_reports_cleanly(tmp_path):
    """If a backup file the manifest lists is missing, restore refuses
    (via verify) with a clear diagnostic rather than a crash."""
    settings = _seed(tmp_path / "src")
    backups = tmp_path / "backups"
    manifest = create_backup(settings, backups, clock=FixedClock(_FIXED))
    bpath = backup_dir_for(backups, manifest)
    # remove a listed file
    (bpath / "agents" / "company_researcher.md").unlink()

    result = restore_backup(bpath, tmp_path / "restored")
    assert not result.ok
    assert result.problems
