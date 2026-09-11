"""Narrow local atlas-runtime MCP bridge; no external network access."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from atlas.config import load_settings
from atlas.persistence.sqlite import StateStore

from .models import Backend
from .service import VscodeHuntService


def build_runtime_server(service: VscodeHuntService | None = None):
    from mcp.server.fastmcp import FastMCP

    if service is None:
        settings = load_settings()
        settings.ensure_directories()
        store = StateStore(settings.state_db)
        service = VscodeHuntService(store, settings.output_dir)
    conn = service.conn
    server = FastMCP("atlas-runtime")

    # ---- Worker operations (narrow, attempt-scoped) --------------------------
    @server.tool()
    def get_task_context(run_id: str, task_id: str, attempt_id: str) -> dict:
        row = conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        if row is None:
            raise ValueError("unknown task")
        return {"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id, "company": row["company_name"], "official_domain": row["official_domain"], "careers_url": row["careers_url"], "lanes": json.loads(row["lanes_json"]), "status": row["status"]}

    @server.tool()
    def heartbeat(run_id: str, task_id: str, attempt_id: str, payload: dict | None = None) -> dict:
        return service.heartbeat(run_id, task_id, attempt_id, payload)

    @server.tool()
    def record_query_checkpoint(run_id: str, task_id: str, attempt_id: str, query: str, evidence: dict | None = None) -> dict:
        return service.record_checkpoint(run_id, task_id, attempt_id, "QUERY_CHECKPOINT", {"query": query, "evidence": evidence or {}})

    @server.tool()
    def record_result_state(run_id: str, task_id: str, attempt_id: str, lane: str, state: str, evidence: dict | None = None) -> dict:
        return service.record_checkpoint(run_id, task_id, attempt_id, "RESULT_STATE", {"lane": lane, "state": state, "evidence": evidence or {}})

    @server.tool()
    def record_lane_checkpoint(run_id: str, task_id: str, attempt_id: str, lane: str, state: str, evidence: dict | None = None) -> dict:
        return service.record_checkpoint(run_id, task_id, attempt_id, "LANE_CHECKPOINT", {"lane": lane, "state": state, "evidence": evidence or {}})

    @server.tool()
    def record_job_evidence(run_id: str, task_id: str, attempt_id: str, evidence: dict) -> dict:
        return service.record_checkpoint(run_id, task_id, attempt_id, "JOB_EVIDENCE", {"evidence": evidence})

    @server.tool()
    def record_dialog_event(run_id: str, task_id: str, attempt_id: str, dialog: dict) -> dict:
        return service.record_checkpoint(run_id, task_id, attempt_id, "DIALOG_EVENT", {"dialog": dialog})

    @server.tool()
    def record_worker_error(run_id: str, task_id: str, attempt_id: str, error: dict) -> dict:
        return service.record_checkpoint(run_id, task_id, attempt_id, "WORKER_ERROR", {"error": error})

    @server.tool()
    def commit_result(run_id: str, task_id: str, attempt_id: str, result: dict | None = None, result_path: str | None = None) -> dict:
        if result is not None:
            return service.commit_result_payload(run_id, task_id, attempt_id, result)
        if result_path:
            return service.commit_result(run_id, task_id, attempt_id, Path(result_path))
        raise ValueError("commit_result requires either an inline result payload or a result_path")

    # ---- Root operations (run-scoped) ---------------------------------------
    @server.tool()
    def get_run_status(run_id: str) -> dict:
        return service.status(run_id)

    @server.tool()
    def get_task_status(run_id: str, task_id: str) -> dict:
        return service.get_task_status(run_id, task_id)

    @server.tool()
    def upgrade_task_contract(run_id: str) -> dict:
        return service.upgrade_task_contract(run_id)

    @server.tool()
    def reopen_browser_recovery(run_id: str, task_id: str, expected_commit_id: str, expected_result_sha256: str, browser_backend: str, browser_slot: str | None = None) -> dict:
        return service.reopen_browser_recovery(
            run_id, task_id, expected_commit_id=expected_commit_id,
            expected_result_sha256=expected_result_sha256,
            browser_backend=browser_backend, browser_slot=browser_slot,
        )

    @server.tool()
    def start_attempt(run_id: str, task_id: str, attempt_id: str) -> dict:
        return service.start_attempt(run_id, task_id, attempt_id)

    @server.tool()
    def retry_invalid_recovery(run_id: str, task_id: str, expected_invalid_commit_id: str, browser_backend: str) -> dict:
        return service.retry_invalid_recovery(run_id, task_id, expected_invalid_commit_id=expected_invalid_commit_id, browser_backend=browser_backend)

    @server.tool()
    def resume_interrupted_recovery(run_id: str, task_id: str, parent_attempt_id: str, browser_backend: str) -> dict:
        return service.resume_interrupted_recovery(run_id, task_id, parent_attempt_id=parent_attempt_id, browser_backend=browser_backend)

    @server.tool()
    def mark_interrupted_uncommitted(run_id: str, reason: str = "MCP_TOOL_NOT_EXPOSED") -> dict:
        return service.mark_interrupted_uncommitted(run_id, reason)

    @server.tool()
    def build_audit_workbook(run_id: str, context: dict | None = None) -> dict:
        return service.build_audit_workbook(run_id, context)

    @server.tool()
    def build_checkpoint_workbook(run_id: str, sequence: int = 1, context: dict | None = None) -> dict:
        return service.build_checkpoint_workbook(run_id, sequence, context)

    @server.tool()
    def build_final_workbook(run_id: str, context: dict | None = None) -> dict:
        return service.build_final_workbook(run_id, context)

    @server.tool()
    def finalize_run(run_id: str) -> dict:
        return service.finalize_run(run_id)

    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true")
    parser.parse_args()
    build_runtime_server().run(transport="stdio")


if __name__ == "__main__":
    main()
