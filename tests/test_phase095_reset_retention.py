"""Phase 0.95 — backup retention policy and developer-only safe_reset.

All destructive behaviour is exercised ONLY against disposable tmp_path
directories that mimic the project layout — never the real C:\\Atlas tree.
"""

from __future__ import annotations

import datetime

import pytest

from atlas.backup import retention as retention_mod
from atlas.backup.manifest import BackupManifest, MANIFEST_FILENAME
from atlas.backup.retention import RetentionSafetyError, apply_retention
from atlas.backup.reset import RESET_PURPOSES, ResetResult, ResetSafetyError, safe_reset

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
def _fake_backup(backups_dir, backup_id: str, created_at: str) -> None:
    d = backups_dir / backup_id
    d.mkdir(parents=True)
    manifest = BackupManifest(
        backup_id=backup_id,
        created_at=created_at,
        atlas_version="0.1.0",
        python_version="3.12",
        langgraph_version="x",
        playwright_version="x",
        state_schema_version=3,
        checkpoint_schema_info="x",
        platform="test",
        config_fingerprint="0" * 64,
    )
    (d / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")


def _make_four(backups_dir):
    base = datetime.datetime(2026, 9, 6, 10, 0, 0, tzinfo=datetime.timezone.utc)
    ids = []
    for i in range(4):
        ts = (base + datetime.timedelta(hours=i)).isoformat()
        bid = f"2026090{6}T{10 + i:02d}0000Z-B{i}"
        _fake_backup(backups_dir, bid, ts)
        ids.append(bid)
    return ids  # oldest -> newest


def test_retention_keeps_latest_n(tmp_path):
    backups = tmp_path / "backups"
    ids = _make_four(backups)
    deleted = apply_retention(backups, keep_latest_n=2)
    assert set(deleted) == {ids[0], ids[1]}  # two oldest removed
    remaining = {p.name for p in backups.iterdir() if p.is_dir()}
    assert remaining == {ids[2], ids[3]}


def test_retention_keep_all_when_n_large(tmp_path):
    backups = tmp_path / "backups"
    ids = _make_four(backups)
    assert apply_retention(backups, keep_latest_n=10) == []
    assert len({p.name for p in backups.iterdir()}) == len(ids)


def test_retention_keep_zero_deletes_all(tmp_path):
    backups = tmp_path / "backups"
    ids = _make_four(backups)
    deleted = apply_retention(backups, keep_latest_n=0)
    assert set(deleted) == set(ids)
    assert list(backups.iterdir()) == []


def test_retention_ignores_non_backup_and_staging_dirs(tmp_path):
    backups = tmp_path / "backups"
    ids = _make_four(backups)
    (backups / "not-a-backup").mkdir()  # no manifest.json
    (backups / ".staging-inflight").mkdir()
    (backups / ".staging-inflight" / MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
    deleted = apply_retention(backups, keep_latest_n=1)
    assert set(deleted) == set(ids[:3])
    # non-backup and staging dirs are left completely alone
    assert (backups / "not-a-backup").exists()
    assert (backups / ".staging-inflight").exists()


def test_retention_negative_raises(tmp_path):
    with pytest.raises(ValueError):
        apply_retention(tmp_path / "backups", keep_latest_n=-1)


def test_retention_missing_dir_returns_empty(tmp_path):
    assert apply_retention(tmp_path / "nope", keep_latest_n=2) == []


def test_retention_refuses_candidate_outside_backups_dir(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    outside = tmp_path / "outside" / "evil"
    outside.mkdir(parents=True)

    fake = retention_mod._Candidate(path=outside, backup_id="EVIL", created_at="2026-09-06T10:00:00+00:00")
    monkeypatch.setattr(retention_mod, "_discover", lambda d: [fake])
    with pytest.raises(RetentionSafetyError):
        apply_retention(backups, keep_latest_n=0)
    # the outside dir must NOT have been deleted
    assert outside.exists()


# ---------------------------------------------------------------------------
# safe_reset
# ---------------------------------------------------------------------------
def _mimic_project_root(root):
    """Create a disposable tree that mimics the real project layout."""
    for rel in (
        "fixtures/generated",
        "fixtures/real",
        "output/phase09",
        ".browser-profile-chrome",
        ".browser-profile",
        ".venv",
        "state",
        "atlas",
        "tests",
        "docs",
        "config",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)
    # marker files so we can assert survival
    (root / "fixtures" / "real" / "immutable.xlsx").write_text("DO NOT DELETE", encoding="utf-8")
    (root / ".browser-profile-chrome" / "Cookies").write_text("SECRET", encoding="utf-8")
    (root / "state" / "atlas_state.sqlite").write_text("STATE", encoding="utf-8")
    (root / "fixtures" / "generated" / "gen.xlsx").write_text("disposable", encoding="utf-8")
    (root / "output" / "phase09" / "report.xlsx").write_text("disposable", encoding="utf-8")


def test_safe_reset_deletes_only_disposable_dirs(tmp_path):
    root = tmp_path / "root"
    _mimic_project_root(root)

    result = safe_reset(["generated_fixtures", "phase09_output"], root)
    assert isinstance(result, ResetResult)
    assert len(result.deleted) == 2

    # disposable dirs are gone
    assert not (root / "fixtures" / "generated").exists()
    assert not (root / "output" / "phase09").exists()
    # protected dirs survive untouched
    assert (root / "fixtures" / "real" / "immutable.xlsx").read_text(encoding="utf-8") == "DO NOT DELETE"
    assert (root / ".browser-profile-chrome" / "Cookies").exists()
    assert (root / ".browser-profile").exists()
    assert (root / ".venv").exists()
    assert (root / "state" / "atlas_state.sqlite").exists()


@pytest.mark.parametrize("bad_purpose", ["browser_profile", "fixtures_real", "state", "venv", "atlas", "everything", ""])
def test_safe_reset_refuses_unknown_purposes(tmp_path, bad_purpose):
    root = tmp_path / "root"
    _mimic_project_root(root)
    with pytest.raises(ResetSafetyError):
        safe_reset([bad_purpose], root)
    # nothing deleted
    assert (root / "fixtures" / "generated").exists()


def test_safe_reset_defense_in_depth_blocks_malicious_purpose(tmp_path, monkeypatch):
    """Even if a purpose somehow mapped to a protected path, the second
    safety layer must refuse it."""
    root = tmp_path / "root"
    _mimic_project_root(root)
    monkeypatch.setitem(RESET_PURPOSES, "evil_profile", ".browser-profile-chrome")
    with pytest.raises(ResetSafetyError):
        safe_reset(["evil_profile"], root)
    assert (root / ".browser-profile-chrome").exists()


def test_safe_reset_refuses_purpose_resolving_outside_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    _mimic_project_root(root)
    monkeypatch.setitem(RESET_PURPOSES, "escape", "../evil")
    (tmp_path / "evil").mkdir(exist_ok=True)
    with pytest.raises(ResetSafetyError):
        safe_reset(["escape"], root)
    assert (tmp_path / "evil").exists()


def test_safe_reset_all_or_nothing_on_bad_entry(tmp_path):
    """A bad purpose aborts before ANY deletion happens."""
    root = tmp_path / "root"
    _mimic_project_root(root)
    with pytest.raises(ResetSafetyError):
        safe_reset(["generated_fixtures", "state"], root)  # second is invalid
    # the valid one must NOT have been deleted (all-or-nothing)
    assert (root / "fixtures" / "generated").exists()


def test_safe_reset_skips_absent_targets(tmp_path):
    root = tmp_path / "root"
    (root / "atlas").mkdir(parents=True)  # only a protected dir exists
    result = safe_reset(["generated_fixtures"], root)
    assert result.deleted == []
    assert len(result.skipped_absent) == 1
