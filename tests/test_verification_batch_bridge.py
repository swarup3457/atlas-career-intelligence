from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from atlas.persistence.sqlite import StateStore
from atlas.vscode_hunt.models import Backend
from atlas.vscode_hunt.service import VscodeHuntService
from atlas.vscode_hunt.verification_batch import (
    LeadClassification,
    evaluate_verification_batch_completion,
    manifest_hash,
    validate_verification_batch_result,
)
from atlas.vscode_hunt.mcp_server import build_runtime_server
import asyncio


def _manifest() -> dict:
    return {"batches": [{"batch_id": f"batch-{index}", "leads": [{"lead_id": f"lead-{index}", "company": f"Company {index}", "source_url": "https://example.com/lead"}]} for index in range(1, 5)]}


def _result(run_id: str, task: dict, attempt_id: str, classification: str = LeadClassification.SOURCE_UNAVAILABLE.value) -> dict:
    return {
        "schema_version": 1, "run_id": run_id, "task_id": task["task_id"], "attempt_id": attempt_id,
        "batch_id": task["batch_id"], "manifest_hash": task["manifest_hash"],
        "assigned_lead_ids": task["assigned_lead_ids"],
        "outcomes": [{"lead_id": lead_id, "classification": classification, "company": "Company", "error_details": "sanitized test outcome"} for lead_id in task["assigned_lead_ids"]],
        "queries": [], "navigation_summary": [], "browser_used": False,
        "source_health": {"status": "unavailable"}, "errors": [], "completion_claim": True,
    }


def test_four_batch_creation_is_idempotent_and_persistent(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        service = VscodeHuntService(store, tmp_path / "out")
        manifest = _manifest()
        run_id = service.create_verification_run(manifest, "run-four")
        assert service.create_verification_run(manifest, "run-four") == run_id
        assert len(service.get_ready_tasks(run_id)) == 4
        assert store.schema_version() >= 16
        assert store._conn.execute("SELECT COUNT(*) AS n FROM vscode_verification_items").fetchone()["n"] == 4
        with pytest.raises(ValueError, match="conflict"):
            different = {"batches": [{"batch_id": f"other-{index}", "leads": [{"lead_id": f"other-lead-{index}"}]} for index in range(1, 5)]}
            service.create_verification_run(different, run_id)


def test_batch_commit_tracks_each_terminal_lead_and_finalize(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        service = VscodeHuntService(store, tmp_path / "out")
        run_id = service.create_verification_run(_manifest(), "run-commit")
        task = service.get_ready_tasks(run_id)[0]
        attempt = service.create_or_start_attempt(run_id, task["task_id"], worker_invocation_id="worker-1")
        for lead_id in task["assigned_lead_ids"]:
            service.record_lead_checkpoint(run_id, task["task_id"], attempt.attempt_id, lead_id, {"classification": "SOURCE_UNAVAILABLE"})
        ack = service.commit_result_payload(run_id, task["task_id"], attempt.attempt_id, _result(run_id, task, attempt.attempt_id))
        assert ack["task_status"] == "COMPLETE"
        assert store._conn.execute("SELECT COUNT(*) AS n FROM vscode_verification_items WHERE status='TERMINAL'").fetchone()["n"] == 1
        assert service.finalize_run(run_id)["can_finish"] is False


def test_missing_lead_is_follow_up_and_unknown_identity_is_invalid() -> None:
    task = {"run_id": "run", "task_id": "task", "attempt_id": "attempt", "manifest_hash": "hash", "assigned_lead_ids": ["a", "b"]}
    result = {"schema_version": 1, "run_id": "run", "task_id": "task", "attempt_id": "attempt", "batch_id": "b", "manifest_hash": "hash", "assigned_lead_ids": ["a", "b"], "outcomes": [{"lead_id": "a", "classification": "SOURCE_UNAVAILABLE"}], "completion_claim": True}
    decision = evaluate_verification_batch_completion(result, task)
    assert decision["action"] == "FOLLOW_UP_REQUIRED"
    assert decision["missing_lead_ids"] == ["b"]
    result["outcomes"].append({"lead_id": "c", "classification": "SOURCE_UNAVAILABLE"})
    assert any("unknown lead" in error for error in validate_verification_batch_result(result, task))


def test_agent_contracts_are_fail_closed() -> None:
    root = Path(__file__).resolve().parents[1]
    verifier = (root / ".github/agents/atlas-job-lead-verifier-vscode.agent.md").read_text(encoding="utf-8")
    coordinator = (root / ".github/agents/atlas-r1-verification-coordinator.agent.md").read_text(encoding="utf-8")
    assert "name: atlas-job-lead-verifier-vscode" in verifier
    assert "name: atlas-r1-verification-coordinator" in coordinator
    assert "runSubagent" in coordinator and "atlas-runtime" in coordinator
    assert "openBrowserPage" not in coordinator and "navigatePage" not in coordinator
    assert "agents: [atlas-job-lead-verifier-vscode]" in coordinator


def test_mcp_exposes_verification_control_plane(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        service = VscodeHuntService(store, tmp_path / "out")
        names = {tool.name for tool in asyncio.run(build_runtime_server(service).list_tools())}
    assert {"create_verification_run", "create_or_start_attempt", "record_lead_checkpoint", "get_ready_tasks", "advance_verification_run"} <= names