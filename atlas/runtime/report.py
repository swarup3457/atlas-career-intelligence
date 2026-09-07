"""Atlas demo runtime report (Phase 0.75 spec section 14).

Generates `Atlas_DEMO_Runtime_Report.xlsx` and `.json` from a completed
(or partial) demo run's persisted state. This proves the runtime can
transform durable run state into a user-facing artifact - it is NOT the
final Atlas job workbook and must never be mistaken for one; the final
business report schema will be defined later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atlas.reporting.excel import ExcelReporter
from atlas.runtime.manifest import RunManifest
from atlas.runtime.progress import ProgressSnapshot

REPORT_BASENAME = "Atlas_DEMO_Runtime_Report"


def _task_rows(queue_state: dict) -> list[dict[str, Any]]:
    item_results: dict[str, dict[str, Any]] = queue_state.get("item_results", {})
    retry_counts: dict[str, int] = queue_state.get("retry_counts", {})
    rows: list[dict[str, Any]] = []
    for task_id in queue_state.get("planned_items", []):
        result = item_results.get(task_id, {})
        rows.append(
            {
                "task_id": task_id,
                "status": result.get("status", "PENDING" if task_id in queue_state.get("remaining_items", []) else "UNKNOWN"),
                "attempt": result.get("attempt", retry_counts.get(task_id, 0)),
                "error_category": result.get("error_category"),
                "next_action": result.get("next_action"),
            }
        )
    return rows


def write_demo_report(
    output_dir: Path,
    queue_state: dict,
    manifest: RunManifest,
    progress: ProgressSnapshot,
) -> dict[str, Path]:
    """Write both the .xlsx and .json demo report artifacts into
    `output_dir`. Returns {"xlsx": path, "json": path}."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = _task_rows(queue_state)

    xlsx_path = output_dir / f"{REPORT_BASENAME}.xlsx"
    ExcelReporter().write_generic_sheet(
        xlsx_path,
        sheet_name="Atlas Demo Runtime",
        rows=rows,
        columns=["task_id", "status", "attempt", "error_category", "next_action"],
    )

    json_path = output_dir / f"{REPORT_BASENAME}.json"
    json_payload = {
        "_notice": "DEMO ONLY - not the final Atlas job workbook schema.",
        "manifest": manifest.to_dict(),
        "progress": progress.to_dict(),
        "tasks": rows,
    }
    json_path.write_text(json.dumps(json_payload, indent=2, sort_keys=True), encoding="utf-8")

    return {"xlsx": xlsx_path, "json": json_path}
