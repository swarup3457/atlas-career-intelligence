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
        return service.get_task_context(run_id, task_id, attempt_id)

    @server.tool()
    def create_verification_run(manifest: dict, run_id: str | None = None) -> dict:
        """Create or verify an idempotent sealed four-batch verification run."""
        return {"run_id": service.create_verification_run(manifest, run_id)}

    @server.tool()
    def create_or_start_attempt(run_id: str, task_id: str, worker_invocation_id: str, backend: str = "VSCODE_SUBAGENT", model: str | None = None, correction: bool = False) -> dict:
        """Create one primary or bounded correction attempt for a verification batch."""
        attempt = service.create_or_start_attempt(run_id, task_id, worker_invocation_id=worker_invocation_id, backend=Backend(backend), model=model, correction=correction)
        return {"run_id": run_id, "task_id": task_id, "attempt_id": attempt.attempt_id, "attempt_number": attempt.attempt_number, "status": "OPEN", "worker_invocation_id": worker_invocation_id}

    @server.tool()
    def record_lead_checkpoint(run_id: str, task_id: str, attempt_id: str, lead_id: str, payload: dict) -> dict:
        """Record one attempt-scoped verification lead checkpoint."""
        return service.record_lead_checkpoint(run_id, task_id, attempt_id, lead_id, payload)

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

    @server.tool()
    def get_ready_tasks(run_id: str, limit: int = 4) -> dict:
        """Return sealed verification batches ready for host-side dispatch."""
        return {"run_id": run_id, "tasks": service.get_ready_tasks(run_id, limit)}

    @server.tool()
    def advance_verification_run(run_id: str) -> dict:
        """Return the deterministic fan-out handoff; this tool never invokes workers."""
        return service.advance_verification_run(run_id)

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
    def terminalize_exhausted_recovery(run_id: str, task_id: str, reason: str) -> dict:
        return service.terminalize_exhausted_recovery(run_id, task_id, reason=reason)

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
