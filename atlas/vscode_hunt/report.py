from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

SHEETS = ("Validated_Jobs", "Rejected_Jobs", "Foreign_Leads", "Company_Coverage", "Attempt_Audit", "Run_Summary")


def build_workbook(root: Path, run_id: str, conn: Any) -> Path:
    output = Path(root) / run_id / f"{run_id}.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet in SHEETS:
        workbook.create_sheet(sheet)
    tasks = conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? ORDER BY task_id", (run_id,)).fetchall()
    attempts = conn.execute("SELECT * FROM vscode_hunt_attempts WHERE task_id IN (SELECT task_id FROM vscode_hunt_tasks WHERE run_id=?) ORDER BY created_at", (run_id,)).fetchall()
    workbook["Company_Coverage"].append(["task_id", "company_id", "company_name", "status"])
    for task in tasks:
        workbook["Company_Coverage"].append([task["task_id"], task["company_id"], task["company_name"], task["status"]])
    workbook["Attempt_Audit"].append(["attempt_id", "task_id", "attempt_number", "backend", "status", "result_hash"])
    for attempt in attempts:
        workbook["Attempt_Audit"].append([attempt[k] for k in ("attempt_id", "task_id", "attempt_number", "backend", "status", "result_hash")])
    workbook["Run_Summary"].append(["run_id", "tasks", "terminal_tasks"])
    terminal = sum(1 for task in tasks if task["status"] == "COMPLETE")
    workbook["Run_Summary"].append([run_id, len(tasks), terminal])
    for sheet in ("Validated_Jobs", "Rejected_Jobs", "Foreign_Leads"):
        workbook[sheet].append(["task_id", "payload"])
    workbook.save(output)
    load_workbook(output).close()
    return output
