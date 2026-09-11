from __future__ import annotations

import asyncio
import json
from pathlib import Path

from openpyxl import load_workbook

from atlas.persistence.sqlite import StateStore
from atlas.vscode_hunt.completion_evaluator import evaluate_completion
from atlas.vscode_hunt.mcp_server import build_runtime_server
from atlas.vscode_hunt.models import (
    CANONICAL_LANES,
    LEGACY_LANE_ALIASES,
    TASK_CONTRACT_VERSION,
    Backend,
    normalize_lanes,
)
from atlas.vscode_hunt.report import V32_SHEETS, build_v32_workbook
from atlas.vscode_hunt.service import VscodeHuntService

OLD_THREE_LANES = ["JAVA_BACKEND", "DOTNET_BACKEND", "REACT_ENTERPRISE"]


def _service(tmp_path: Path) -> tuple[StateStore, VscodeHuntService]:
    store = StateStore(tmp_path / "state.sqlite")
    return store, VscodeHuntService(store, tmp_path / "out")


def _company(company_id: str = "fixture-a", name: str = "Fixture A") -> dict:
    return {"company_id": company_id, "name": name, "official_domain": "fixture.example", "careers_url": "https://fixture.example/careers"}


def _valid_result(run_id: str, task_id: str, attempt_id: str, company_id: str = "fixture-a") -> dict:
    return {
        "schema_version": 2, "run_id": run_id, "task_id": task_id, "attempt_id": attempt_id,
        "company_id": company_id, "worker_invocation_id": "worker-1",
        "official_domain": "fixture.example", "career_url": "https://fixture.example/careers",
        "queries": ["java India"], "lanes_attempted": list(CANONICAL_LANES),
        "result_states": [{"lane": lane, "state": "SEARCHED_NO_MATCH"} for lane in CANONICAL_LANES],
        "detail_urls": [], "jobs": [], "rejections": [], "foreign_leads": [],
        "browser_errors": [], "evidence_quotes": [], "source_health": {"status": "healthy"},
        "external_block_evidence": "", "completion_claim": True,
    }


# --------------------------------------------------------------------------- #
# Canonical five-lane contract
# --------------------------------------------------------------------------- #
def test_canonical_lanes_are_the_current_five() -> None:
    assert CANONICAL_LANES == (
        "JAVA_BACKEND", "JAVA_FULLSTACK", "REACT_FRONTEND", "DOTNET",
        "ENTERPRISE_HR_PAYROLL_INTEGRATION",
    )
    assert set(LEGACY_LANE_ALIASES) == {"DOTNET_BACKEND", "REACT_ENTERPRISE"}
    assert normalize_lanes(None) == CANONICAL_LANES
    assert normalize_lanes(OLD_THREE_LANES) == ("JAVA_BACKEND", "REACT_FRONTEND", "DOTNET")


def test_create_run_defaults_to_canonical_five(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        run_id = service.create_run([_company()])
        task = service.next_tasks(run_id)[0]
        assert task["lanes"] == list(CANONICAL_LANES)


def test_create_run_maps_legacy_aliases(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        company = _company()
        company["lanes"] = OLD_THREE_LANES
        run_id = service.create_run([company])
        task = service.next_tasks(run_id)[0]
        assert "DOTNET_BACKEND" not in task["lanes"]
        assert "REACT_ENTERPRISE" not in task["lanes"]
        assert "DOTNET" in task["lanes"] and "REACT_FRONTEND" in task["lanes"]


# --------------------------------------------------------------------------- #
# Task-contract upgrade (§8)
# --------------------------------------------------------------------------- #
def test_upgrade_task_contract_rewrites_uncommitted_but_preserves_committed(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        run_id = service.create_run([_company("co-a", "A"), _company("co-b", "B")])
        tasks = {t["company_id"]: t for t in service.next_tasks(run_id, limit=5, materialize=True)}
        task_a, task_b = tasks["co-a"], tasks["co-b"]

        # Task B gets a durable commit, THEN both are forced to the obsolete contract.
        attempt = service.record_attempt(run_id, task_b["task_id"], "attempt-b", Backend.VSCODE_SUBAGENT)
        service.commit_result_payload(run_id, task_b["task_id"], attempt.attempt_id, _valid_result(run_id, task_b["task_id"], attempt.attempt_id, "co-b"))
        for task_id in (task_a["task_id"], task_b["task_id"]):
            with store._conn:
                store._conn.execute("UPDATE vscode_hunt_tasks SET lanes_json=? WHERE task_id=?", (json.dumps(OLD_THREE_LANES), task_id))

        report = service.upgrade_task_contract(run_id)
        assert report["contract_version"] == TASK_CONTRACT_VERSION
        upgraded_ids = {row["task_id"] for row in report["upgraded"]}
        preserved = {row["task_id"]: row["reason"] for row in report["preserved"]}
        assert task_a["task_id"] in upgraded_ids
        assert preserved.get(task_b["task_id"]) == "has_durable_commit"

        lanes_a = json.loads(store._conn.execute("SELECT lanes_json FROM vscode_hunt_tasks WHERE task_id=?", (task_a["task_id"],)).fetchone()["lanes_json"])
        lanes_b = json.loads(store._conn.execute("SELECT lanes_json FROM vscode_hunt_tasks WHERE task_id=?", (task_b["task_id"],)).fetchone()["lanes_json"])
        assert lanes_a == list(CANONICAL_LANES)
        assert lanes_b == OLD_THREE_LANES  # committed task never rewritten

        events = store._conn.execute("SELECT event_type FROM vscode_worker_events WHERE run_id=? AND event_type='TASK_CONTRACT_UPGRADED'", (run_id,)).fetchall()
        assert len(events) == 1


# --------------------------------------------------------------------------- #
# Interrupted-uncommitted recovery (§9)
# --------------------------------------------------------------------------- #
def test_mark_interrupted_uncommitted_transitions_open_attempts(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        run_id = service.create_run([_company("co-open", "Open"), _company("co-done", "Done")])
        tasks = {t["company_id"]: t for t in service.next_tasks(run_id, limit=5, materialize=True)}
        open_task, done_task = tasks["co-open"], tasks["co-done"]

        open_attempt = service.record_attempt(run_id, open_task["task_id"], "attempt-open", Backend.VSCODE_SUBAGENT)
        done_attempt = service.record_attempt(run_id, done_task["task_id"], "attempt-done", Backend.VSCODE_SUBAGENT)
        service.commit_result_payload(run_id, done_task["task_id"], done_attempt.attempt_id, _valid_result(run_id, done_task["task_id"], done_attempt.attempt_id, "co-done"))

        report = service.mark_interrupted_uncommitted(run_id, reason="MCP_TOOL_NOT_EXPOSED")
        interrupted_ids = {row["attempt_id"] for row in report["interrupted"]}
        assert open_attempt.attempt_id in interrupted_ids
        assert done_attempt.attempt_id not in interrupted_ids

        open_status = store._conn.execute("SELECT status FROM vscode_hunt_attempts WHERE attempt_id=?", (open_attempt.attempt_id,)).fetchone()["status"]
        done_status = store._conn.execute("SELECT status FROM vscode_hunt_attempts WHERE attempt_id=?", (done_attempt.attempt_id,)).fetchone()["status"]
        assert open_status == "INTERRUPTED_UNCOMMITTED"
        assert done_status == "RESULT_COMMITTED"
        # The interrupted task returns to PENDING so a fresh resume attempt can be created.
        assert store._conn.execute("SELECT status FROM vscode_hunt_tasks WHERE task_id=?", (open_task["task_id"],)).fetchone()["status"] == "PENDING"


# --------------------------------------------------------------------------- #
# Atomic inline-payload commit + idempotency + conflict (§11)
# --------------------------------------------------------------------------- #
def test_commit_payload_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        run_id = service.create_run([_company()])
        task = service.next_tasks(run_id, materialize=True)[0]
        attempt = service.record_attempt(run_id, task["task_id"], "attempt-1", Backend.VSCODE_SUBAGENT)
        payload = _valid_result(run_id, task["task_id"], attempt.attempt_id)

        first = service.commit_result_payload(run_id, task["task_id"], attempt.attempt_id, payload)
        assert first["task_status"] == "COMPLETE"
        again = service.commit_result_payload(run_id, task["task_id"], attempt.attempt_id, payload)
        assert again["commit_id"] == first["commit_id"]  # idempotent same-hash resubmit

        conflicting = dict(payload)
        conflicting["queries"] = ["different query"]
        try:
            service.commit_result_payload(run_id, task["task_id"], attempt.attempt_id, conflicting)
        except ValueError as exc:
            assert "conflict" in str(exc).lower()
        else:  # pragma: no cover
            raise AssertionError("conflicting resubmit must raise")


# --------------------------------------------------------------------------- #
# 16-sheet immutable V3.2 workbook (§19)
# --------------------------------------------------------------------------- #
def test_v32_audit_workbook_has_sixteen_sheets_and_is_immutable(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        run_id = service.create_run([_company("co-a", "A"), _company("co-b", "B")])
        service.next_tasks(run_id, limit=5, materialize=True)

        first = build_v32_workbook(service.root, run_id, store._conn, kind="Audit", context={"status": "WAITING_FOR_MCP_TRUST", "task_contract_version": TASK_CONTRACT_VERSION})
        second = build_v32_workbook(service.root, run_id, store._conn, kind="Audit", context={"status": "WAITING_FOR_MCP_TRUST"})
        assert first != second  # immutable: never overwrites

        workbook = load_workbook(first, read_only=True)
        assert workbook.sheetnames == list(V32_SHEETS)
        assert len(V32_SHEETS) == 20
        coverage = list(workbook["Company_Coverage"].iter_rows(values_only=True))
        lanes = list(workbook["Lane_Coverage"].iter_rows(values_only=True))
        workbook.close()
        assert len(coverage) == 1 + 2  # header + two companies
        assert len(lanes) == 1 + 2 * len(CANONICAL_LANES)  # header + 5 lanes per company


# --------------------------------------------------------------------------- #
# MCP server exposes the full §10 tool contract
# --------------------------------------------------------------------------- #
def test_mcp_server_exposes_expected_tools(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        server = build_runtime_server(service)
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
    expected = {
        "get_task_context", "heartbeat", "record_query_checkpoint", "record_result_state",
        "record_lane_checkpoint", "record_job_evidence", "record_dialog_event",
        "record_worker_error", "commit_result", "get_run_status", "get_task_status",
        "upgrade_task_contract", "mark_interrupted_uncommitted", "build_audit_workbook",
        "build_checkpoint_workbook", "build_final_workbook", "finalize_run",
    }
    assert expected <= names


# --------------------------------------------------------------------------- #
# Completion evaluator honours the canonical five lanes
# --------------------------------------------------------------------------- #
def test_completion_requires_every_canonical_lane() -> None:
    complete = {
        "lanes_attempted": list(CANONICAL_LANES),
        "result_states": [{"lane": lane, "state": "SEARCHED_NO_MATCH"} for lane in CANONICAL_LANES],
        "completion_claim": True, "source_health": {"status": "healthy"},
    }
    assert evaluate_completion(complete, CANONICAL_LANES).action == "COMPLETE"

    missing_one = dict(complete)
    missing_one["lanes_attempted"] = list(CANONICAL_LANES[:-1])
    missing_one["result_states"] = [{"lane": lane, "state": "SEARCHED_NO_MATCH"} for lane in CANONICAL_LANES[:-1]]
    decision = evaluate_completion(missing_one, CANONICAL_LANES)
    assert decision.action == "FOLLOW_UP_REQUIRED"
    assert f"lane:{CANONICAL_LANES[-1]}" in decision.missing_obligations


# --------------------------------------------------------------------------- #
# Terminality correctness: status() and finalize_run() (§2B)
# --------------------------------------------------------------------------- #
def test_status_and_finalize_block_while_running(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        run_id = service.create_run([_company()])
        task = service.next_tasks(run_id, materialize=True)[0]
        service.record_attempt(run_id, task["task_id"], "attempt-1", Backend.VSCODE_SUBAGENT)
        # RUNNING task with an OPEN attempt: not terminal, cannot finish.
        assert service.status(run_id)["all_tasks_terminal"] is False
        final = service.finalize_run(run_id)
        assert final["can_finish"] is False
        assert final["overall_status"] is None
        assert final["open_attempts"] == 1
        assert final["required_actions"]


def test_finalize_pass_when_all_complete(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        run_id = service.create_run([_company()])
        task = service.next_tasks(run_id, materialize=True)[0]
        attempt = service.record_attempt(run_id, task["task_id"], "attempt-1", Backend.VSCODE_SUBAGENT)
        service.commit_result_payload(run_id, task["task_id"], attempt.attempt_id, _valid_result(run_id, task["task_id"], attempt.attempt_id))
        assert service.status(run_id)["all_tasks_terminal"] is True
        final = service.finalize_run(run_id)
        assert final["can_finish"] is True
        assert final["overall_status"] == "PASS"


def test_finalize_partial_on_external_access_limited(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    with store:
        run_id = service.create_run([_company()])
        task = service.next_tasks(run_id, materialize=True)[0]
        attempt = service.record_attempt(run_id, task["task_id"], "attempt-1", Backend.VSCODE_SUBAGENT)
        result = _valid_result(run_id, task["task_id"], attempt.attempt_id)
        result["external_block_evidence"] = "HTTP 429 sustained across retries"
        ack = service.commit_result_payload(run_id, task["task_id"], attempt.attempt_id, result)
        assert ack["completion_action"] == "EXTERNAL_ACCESS_LIMITED"
        final = service.finalize_run(run_id)
        assert final["can_finish"] is True
        assert final["overall_status"] == "PARTIAL"
