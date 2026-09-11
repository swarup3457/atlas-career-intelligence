from __future__ import annotations

import json
from typing import Any


TERMINAL_ITEM_CLASSES = {"VERIFIED_ACCEPTED", "VERIFIED_STRETCH", "VERIFIED_REJECTED", "PORTAL_ONLY_UNVERIFIED", "CLOSED", "FOREIGN", "DUPLICATE", "SOURCE_UNAVAILABLE", "INTERNAL_ERROR"}


def latest_valid_batch_results(conn: Any, run_id: str) -> list[dict[str, Any]]:
    """Read only the latest VALID committed result per batch task."""
    rows = conn.execute(
        """SELECT c.* FROM vscode_result_commits c
           JOIN vscode_hunt_tasks t ON t.task_id=c.task_id
           WHERE c.run_id=? AND t.task_kind='VERIFY_JOB_LEAD_BATCH' AND c.validation_state='VALID'
           AND c.committed_at=(SELECT MAX(c2.committed_at) FROM vscode_result_commits c2 WHERE c2.task_id=c.task_id AND c2.validation_state='VALID')
           ORDER BY c.task_id""",
        (run_id,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            with open(row["result_path"], encoding="utf-8") as handle:
                result = json.load(handle)
        except (OSError, TypeError, ValueError):
            continue
        result["commit_id"] = row["commit_id"]
        result["result_sha256"] = row["result_sha256"]
        results.append(result)
    return results


def outcome_rows(conn: Any, run_id: str) -> list[dict[str, Any]]:
    """Flatten latest valid batch outcomes for reports and history adapters."""
    rows: list[dict[str, Any]] = []
    for result in latest_valid_batch_results(conn, run_id):
        for outcome in result.get("outcomes", []):
            if isinstance(outcome, dict) and str(outcome.get("classification", "")) in TERMINAL_ITEM_CLASSES:
                rows.append({**outcome, "run_id": run_id, "batch_id": result.get("batch_id", ""), "commit_id": result.get("commit_id", ""), "result_sha256": result.get("result_sha256", "")})
    return rows
