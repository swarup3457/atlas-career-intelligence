from __future__ import annotations

import re
from pathlib import Path

import pytest

from atlas.persistence.sqlite import StateStore
from atlas.vscode_hunt.models import Backend
from atlas.vscode_hunt.service import VscodeHuntService

AGENTS_DIR = Path(__file__).resolve().parents[1] / ".github" / "agents"
WORKER_AGENTS = [
    "atlas-company-search-vscode.agent.md",
    "atlas-company-correction-vscode.agent.md",
    "atlas-company-discovery-vscode.agent.md",
    "atlas-company-verification-vscode.agent.md",
]
CAMEL_BROWSER = [
    "openBrowserPage", "navigatePage", "readPage", "clickElement", "typeInPage",
    "handleDialog", "hoverElement", "dragElement", "screenshotPage", "runPlaywrightCode",
]
OBSOLETE_SNAKE = [
    "open_browser_page", "navigate_page", "read_page", "click_element", "type_in_page",
    "handle_dialog", "hover_element", "drag_element", "screenshot_page", "run_playwright_code",
]
PROHIBITED = [
    "run_in_terminal", "runInTerminal", "create_file", "replace_string_in_file",
    "multi_replace_string_in_file", "runCommands", "runTasks", "git", "terminal",
]


def _tools(agent_file: str) -> list[str]:
    text = (AGENTS_DIR / agent_file).read_text(encoding="utf-8")
    match = re.search(r"^tools:\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert match, f"{agent_file} has no explicit tools list"
    return [tool.strip() for tool in match.group(1).split(",") if tool.strip()]


@pytest.mark.parametrize("agent", WORKER_AGENTS)
def test_worker_uses_current_camelcase_browser_ids(agent: str) -> None:
    tools = _tools(agent)
    for tool_id in CAMEL_BROWSER:
        assert tool_id in tools, f"{agent} is missing camelCase Browser id {tool_id}"


@pytest.mark.parametrize("agent", WORKER_AGENTS)
def test_worker_has_no_obsolete_snakecase_browser_ids(agent: str) -> None:
    tools = _tools(agent)
    for tool_id in OBSOLETE_SNAKE:
        assert tool_id not in tools, f"{agent} still lists obsolete snake_case Browser id {tool_id}"


@pytest.mark.parametrize("agent", WORKER_AGENTS)
def test_worker_grants_atlas_runtime(agent: str) -> None:
    tools = _tools(agent)
    assert any(tool == "atlas-runtime" or tool.startswith("atlas-runtime/") for tool in tools), (
        f"{agent} must grant the atlas-runtime tool set"
    )


@pytest.mark.parametrize("agent", WORKER_AGENTS)
def test_worker_has_no_prohibited_tools(agent: str) -> None:
    tools = _tools(agent)
    for tool_id in PROHIBITED:
        assert tool_id not in tools, f"{agent} must not grant prohibited tool {tool_id}"


def _browser_failure(run_id: str, task_id: str, attempt_id: str) -> dict:
    lanes = ["JAVA_BACKEND", "JAVA_FULLSTACK", "REACT_FRONTEND", "DOTNET", "ENTERPRISE_HR_PAYROLL_INTEGRATION"]
    return {
        "schema_version": 2, "run_id": run_id, "task_id": task_id, "attempt_id": attempt_id,
        "company_id": "co", "worker_invocation_id": "worker-v32",
        "official_domain": "co.example", "career_url": "https://co.example/careers",
        "queries": [], "lanes_attempted": lanes,
        "result_states": [{"lane": lane, "state": "INTERNAL_FAILURE"} for lane in lanes],
        "detail_urls": [], "jobs": [], "rejections": [], "foreign_leads": [],
        "browser_errors": ["BROWSER_UNAVAILABLE: native Browser tools missing"],
        "evidence_quotes": [], "source_health": {"status": "unusable"},
        "external_block_evidence": "", "completion_claim": False,
    }


def test_browser_recovery_preserves_parent_and_requires_matching_commit(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        service = VscodeHuntService(store, tmp_path / "out")
        run_id = service.create_run([{"company_id": "co", "name": "Co", "official_domain": "co.example"}])
        task = service.next_tasks(run_id, materialize=True)[0]
        attempt = service.record_attempt(run_id, task["task_id"], "attempt-v32", Backend.VSCODE_SUBAGENT)
        ack = service.commit_result_payload(run_id, task["task_id"], attempt.attempt_id, _browser_failure(run_id, task["task_id"], attempt.attempt_id))
        assert ack["task_status"] == "NEEDS_REPAIR"

        with pytest.raises(ValueError, match="identity or hash"):
            service.reopen_browser_recovery(
                run_id, task["task_id"], expected_commit_id=ack["commit_id"],
                expected_result_sha256="wrong", browser_backend="VSCODE_NATIVE_BROWSER",
            )

        reopened = service.reopen_browser_recovery(
            run_id, task["task_id"], expected_commit_id=ack["commit_id"],
            expected_result_sha256=ack["result_sha256"], browser_backend="VSCODE_NATIVE_BROWSER",
        )
        assert reopened["attempt_kind"] == "BROWSER_BACKEND_RECOVERY"
        assert reopened["parent_attempt_id"] == attempt.attempt_id
        assert reopened["parent_commit_id"] == ack["commit_id"]
        assert store._conn.execute("SELECT status FROM vscode_hunt_tasks WHERE task_id=?", (task["task_id"],)).fetchone()["status"] == "PENDING"

        started = service.start_attempt(run_id, task["task_id"], reopened["new_attempt_id"])
        assert started["status"] == "OPEN"
        assert service.get_task_status(run_id, task["task_id"])["status"] == "RUNNING"

        parent = store._conn.execute("SELECT status,result_hash FROM vscode_hunt_attempts WHERE attempt_id=?", (attempt.attempt_id,)).fetchone()
        assert parent["status"] == "RESULT_COMMITTED"
        assert parent["result_hash"] == ack["result_sha256"]
        event = store._conn.execute("SELECT payload_json FROM vscode_worker_events WHERE event_type='TASK_REOPENED_FOR_BROWSER_RECOVERY'").fetchone()
        assert event is not None
        assert reopened["new_attempt_id"] in event["payload_json"]


def test_browser_recovery_rejects_non_retryable_terminal_result(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        service = VscodeHuntService(store, tmp_path / "out")
        run_id = service.create_run([{"company_id": "co", "name": "Co", "official_domain": "co.example"}])
        task = service.next_tasks(run_id, materialize=True)[0]
        attempt = service.record_attempt(run_id, task["task_id"], "attempt-real", Backend.VSCODE_SUBAGENT)
        result = _browser_failure(run_id, task["task_id"], attempt.attempt_id)
        result["browser_errors"] = []
        result["completion_claim"] = True
        result["source_health"] = {"status": "healthy"}
        ack = service.commit_result_payload(run_id, task["task_id"], attempt.attempt_id, result)
        assert ack["task_status"] == "COMPLETE"
        with pytest.raises(ValueError, match="NEEDS_REPAIR"):
            service.reopen_browser_recovery(
                run_id, task["task_id"], expected_commit_id=ack["commit_id"],
                expected_result_sha256=ack["result_sha256"], browser_backend="VSCODE_NATIVE_BROWSER",
            )


def test_browser_recovery_accepts_historical_unavailable_error_text(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        service = VscodeHuntService(store, tmp_path / "out")
        run_id = service.create_run([{"company_id": "co", "name": "Co", "official_domain": "co.example"}])
        task = service.next_tasks(run_id, materialize=True)[0]
        attempt = service.record_attempt(run_id, task["task_id"], "attempt-v32", Backend.VSCODE_SUBAGENT)
        result = _browser_failure(run_id, task["task_id"], attempt.attempt_id)
        result["browser_errors"] = ["VS Code built-in Browser tools not available in this session"]
        ack = service.commit_result_payload(run_id, task["task_id"], attempt.attempt_id, result)
        reopened = service.reopen_browser_recovery(
            run_id, task["task_id"], expected_commit_id=ack["commit_id"],
            expected_result_sha256=ack["result_sha256"], browser_backend="VSCODE_NATIVE_BROWSER",
        )
        assert reopened["attempt_kind"] == "BROWSER_BACKEND_RECOVERY"


def test_invalid_recovery_retry_preserves_invalid_commit_and_parent(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        service = VscodeHuntService(store, tmp_path / "out")
        run_id = service.create_run([{"company_id": "co", "name": "Co", "official_domain": "co.example"}])
        task = service.next_tasks(run_id, materialize=True)[0]
        first = service.record_attempt(run_id, task["task_id"], "attempt-v32", Backend.VSCODE_SUBAGENT)
        parent = service.commit_result_payload(run_id, task["task_id"], first.attempt_id, _browser_failure(run_id, task["task_id"], first.attempt_id))
        recovered = service.reopen_browser_recovery(
            run_id, task["task_id"], expected_commit_id=parent["commit_id"],
            expected_result_sha256=parent["result_sha256"], browser_backend="VSCODE_NATIVE_BROWSER",
        )
        service.start_attempt(run_id, task["task_id"], recovered["new_attempt_id"])
        invalid = dict(_browser_failure(run_id, task["task_id"], recovered["new_attempt_id"]))
        invalid["worker_invocation_id"] = "worker-invalid"
        invalid["lanes_attempted"] = []
        invalid["completion_claim"] = True
        invalid["browser_errors"] = ["internal browser failure"]
        rejected = service.commit_result_payload(run_id, task["task_id"], recovered["new_attempt_id"], invalid)
        assert rejected["task_status"] == "REJECTED_INVALID_RESULT"

        retry = service.retry_invalid_recovery(
            run_id, task["task_id"], expected_invalid_commit_id=rejected["commit_id"],
            browser_backend="VSCODE_NATIVE_BROWSER",
        )
        assert retry["parent_attempt_id"] == recovered["new_attempt_id"]
        assert retry["parent_commit_id"] == parent["commit_id"]
        preserved = store._conn.execute("SELECT validation_state FROM vscode_result_commits WHERE commit_id=?", (rejected["commit_id"],)).fetchone()
        assert preserved["validation_state"] == "INVALID"
