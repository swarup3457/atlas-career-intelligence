"""Pytest coverage for atlas.runtime.progress and atlas.runtime.manifest
(Phase 0.75 spec sections 13/17/18)."""

from __future__ import annotations

import pytest

from atlas.config import load_settings
from atlas.orchestration.state import initial_queue_state
from atlas.runtime.manifest import RunManifest, compute_config_fingerprint, software_versions
from atlas.runtime.progress import compute_progress
from atlas.runtime.states import RunState

pytestmark = pytest.mark.unit


def test_compute_progress_counts_from_queue_state():
    state = initial_queue_state(["a", "b", "c"])
    state["completed_items"] = ["a"]
    state["remaining_items"] = ["b", "c"]
    state["retry_counts"] = {"a": 1, "b": 1}
    state["item_results"] = {"a": {"status": "SUCCESS"}}

    snapshot = compute_progress("run-1", RunState.RUNNING, state)
    assert snapshot.planned == 3
    assert snapshot.completed == 1
    assert snapshot.remaining == 2
    assert snapshot.success == 1
    assert snapshot.retry_pending == 1  # "b" is remaining and already attempted once


def test_progress_snapshot_to_dict_is_json_shape():
    state = initial_queue_state(["a"])
    snapshot = compute_progress("run-1", RunState.COMPLETE, state)
    payload = snapshot.to_dict()
    assert payload["run_id"] == "run-1"
    assert payload["status"] == "COMPLETE"
    assert set(payload.keys()) >= {"planned", "completed", "remaining", "retry_pending", "access_limited", "waiting_for_human"}


def test_config_fingerprint_is_deterministic(tmp_path):
    settings = load_settings(
        state_db=tmp_path / "state" / "atlas_state.sqlite",
        checkpoint_db=tmp_path / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_path / "output",
        logs_dir=tmp_path / "logs",
        browser_profile=tmp_path / "profile",
        agents_dir=tmp_path / "agents",
        skills_dir=tmp_path / "skills",
    )
    fp1 = compute_config_fingerprint(settings)
    fp2 = compute_config_fingerprint(settings)
    assert fp1 == fp2
    assert len(fp1) == 64  # sha256 hex digest


def test_config_fingerprint_never_includes_sensitive_field_names():
    from atlas.runtime import manifest as manifest_module

    for name in manifest_module._FINGERPRINT_FIELDS:
        lowered = name.lower()
        assert not any(fragment in lowered for fragment in manifest_module._SENSITIVE_NAME_FRAGMENTS)


def test_config_fingerprint_changes_when_config_changes(tmp_path):
    settings_a = load_settings(
        state_db=tmp_path / "state" / "atlas_state.sqlite",
        checkpoint_db=tmp_path / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_path / "output",
        logs_dir=tmp_path / "logs",
        browser_profile=tmp_path / "profile",
        agents_dir=tmp_path / "agents",
        skills_dir=tmp_path / "skills",
        batch_size=10,
    )
    settings_b = load_settings(
        state_db=tmp_path / "state" / "atlas_state.sqlite",
        checkpoint_db=tmp_path / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_path / "output",
        logs_dir=tmp_path / "logs",
        browser_profile=tmp_path / "profile",
        agents_dir=tmp_path / "agents",
        skills_dir=tmp_path / "skills",
        batch_size=25,
    )
    assert compute_config_fingerprint(settings_a) != compute_config_fingerprint(settings_b)


def test_run_manifest_roundtrip(tmp_path):
    manifest = RunManifest(
        run_id="run-1",
        created_at="2026-01-01T00:00:00Z",
        status="COMPLETE",
        config_fingerprint="deadbeef",
        planned_tasks=5,
        completed_tasks=5,
        software_version=software_versions(),
    )
    path = manifest.write(tmp_path / "manifest.json")
    loaded = RunManifest.read(path)
    assert loaded.run_id == "run-1"
    assert loaded.planned_tasks == 5
    assert loaded.software_version["atlas"] == software_versions()["atlas"]


def test_software_versions_includes_atlas_and_python():
    versions = software_versions()
    assert "atlas" in versions
    assert "python" in versions
    assert "schema_version" in versions
