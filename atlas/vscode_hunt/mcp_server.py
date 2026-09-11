"""Narrow local atlas-runtime MCP bridge; no external network access."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from atlas.config import load_settings
from atlas.persistence.sqlite import StateStore

from .models import Backend
from .service import VscodeHuntService


def build_runtime_server():
    from mcp.server.fastmcp import FastMCP

    settings = load_settings()
    settings.ensure_directories()
    store = StateStore(settings.state_db)
    service = VscodeHuntService(store, settings.output_dir)
    server = FastMCP("atlas-runtime")

    @server.tool()
    def get_task_context(run_id: str, task_id: str, attempt_id: str) -> dict:
        row = store._conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        if row is None:
            raise ValueError("unknown task")
        return {"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id, "company": row["company_name"], "lanes": json.loads(row["lanes_json"]), "status": row["status"]}

    @server.tool()
    def heartbeat(run_id: str, task_id: str, attempt_id: str, payload: dict | None = None) -> dict:
        return service.heartbeat(run_id, task_id, attempt_id, payload)

    @server.tool()
    def record_lane_checkpoint(run_id: str, task_id: str, attempt_id: str, lane: str, state: str, evidence: dict | None = None) -> dict:
        return service.record_checkpoint(run_id, task_id, attempt_id, "LANE_CHECKPOINT", {"lane": lane, "state": state, "evidence": evidence or {}})

    @server.tool()
    def commit_result(run_id: str, task_id: str, attempt_id: str, result_path: str) -> dict:
        return service.commit_result(run_id, task_id, attempt_id, Path(result_path))

    @server.tool()
    def get_run_status(run_id: str) -> dict:
        return service.status(run_id)

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
