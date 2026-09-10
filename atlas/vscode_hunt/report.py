from __future__ import annotations

import json
import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

SHEETS = ("Validated_Jobs", "Rejected_Jobs", "Foreign_Leads", "Company_Coverage", "Lane_Coverage", "Evidence_Audit", "Session_Audit", "Run_Summary")


def build_workbook(root: Path, run_id: str, conn: Any) -> Path:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(root) / run_id / f"Atlas_VSCode_Live_Product3_{timestamp}_{run_id}.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet in SHEETS:
        workbook.create_sheet(sheet)
    tasks = conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? ORDER BY task_id", (run_id,)).fetchall()
    attempts = conn.execute("SELECT * FROM vscode_hunt_attempts WHERE task_id IN (SELECT task_id FROM vscode_hunt_tasks WHERE run_id=?) ORDER BY created_at", (run_id,)).fetchall()
    workbook["Validated_Jobs"].append(["company", "title", "location", "official_url", "requisition", "role_lane", "stack", "mandatory_experience", "posted_date", "recommendation", "strengths", "gaps", "verification_status"])
    workbook["Rejected_Jobs"].append(["company", "title", "location", "url", "lane", "reason_code", "detail"])
    workbook["Foreign_Leads"].append(["company", "title", "location", "url", "reason"])
    workbook["Company_Coverage"].append(["task_id", "company_id", "company_name", "official_domain", "status", "attempts", "details_opened", "browser_errors"])
    for task in tasks:
        task_attempts = [a for a in attempts if a["task_id"] == task["task_id"]]
        results = []
        for attempt in task_attempts:
            if attempt["result_path"] and Path(attempt["result_path"]).exists():
                results.append(json.loads(Path(attempt["result_path"]).read_text(encoding="utf-8")))
        final = results[-1] if results else {}
        workbook["Company_Coverage"].append([task["task_id"], task["company_id"], task["company_name"], task["official_domain"], task["status"], len(task_attempts), len(final.get("detail_urls", [])), len(final.get("browser_errors", []))])
        for job in final.get("jobs", []):
            if str(job.get("proposed_decision", "accept")).lower() not in {"accept", "accepted", "validate"}:
                continue
            workbook["Validated_Jobs"].append([task["company_name"], job.get("title", ""), job.get("location", ""), job.get("canonical_url", job.get("official_url", "")), job.get("requisition_id", ""), job.get("lane", ""), ", ".join(job.get("stack", [])) if isinstance(job.get("stack"), list) else job.get("stack", ""), job.get("experience", job.get("experience_text", "")), job.get("posted_date", ""), "MANUAL_REVIEW", "", "", "PYTHON_VALIDATED"])
        for rejection in final.get("rejections", []):
            workbook["Rejected_Jobs"].append([task["company_name"], rejection.get("title", ""), rejection.get("location", ""), rejection.get("url", ""), rejection.get("lane", ""), rejection.get("reason_code", ""), rejection.get("detail", rejection.get("reason", ""))])
        for foreign in final.get("foreign_leads", []):
            workbook["Foreign_Leads"].append([task["company_name"], foreign.get("title", ""), foreign.get("location", ""), foreign.get("url", ""), foreign.get("reason", "NON_INDIA_LOCATION")])
    workbook["Lane_Coverage"].append(["task_id", "company", "lane", "result_state", "terminal"])
    for task in tasks:
        paths = [a["result_path"] for a in attempts if a["task_id"] == task["task_id"] and a["result_path"] and Path(a["result_path"]).exists()]
        final = json.loads(Path(paths[-1]).read_text(encoding="utf-8")) if paths else {}
        states = {str(s.get("lane")): str(s.get("state")) for s in final.get("result_states", []) if isinstance(s, dict)}
        for lane in json.loads(task["lanes_json"]):
            workbook["Lane_Coverage"].append([task["task_id"], task["company_name"], lane, states.get(lane, "NOT_OBSERVED"), task["status"] == "COMPLETE"])
    workbook["Evidence_Audit"].append(["task_id", "attempt_id", "result_hash", "detail_urls", "quotes", "source_health", "browser_errors"])
    for attempt in attempts:
        path = attempt["result_path"]
        result = json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).exists() else {}
        workbook["Evidence_Audit"].append([attempt["task_id"], attempt["attempt_id"], attempt["result_hash"], len(result.get("detail_urls", [])), len(result.get("evidence_quotes", [])), json.dumps(result.get("source_health", {})), json.dumps(result.get("browser_errors", []))])
    workbook["Session_Audit"].append(["attempt_id", "task_id", "backend", "attempt_number", "action", "model", "browser_used", "error"])
    for attempt in attempts:
        path = attempt["result_path"]
        result = json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).exists() else {}
        errors = result.get("browser_errors", [])
        error_text = "; ".join(error if isinstance(error, str) else json.dumps(error, sort_keys=True) for error in errors)
        workbook["Session_Audit"].append([attempt["attempt_id"], attempt["task_id"], attempt["backend"], attempt["attempt_number"], attempt["status"], result.get("model", "sonnet"), bool(result.get("browser_evidence", True)), error_text])
    workbook["Run_Summary"].append(["run_id", "tasks", "terminal_tasks"])
    terminal = sum(1 for task in tasks if task["status"] == "COMPLETE")
    workbook["Run_Summary"].append([run_id, len(tasks), terminal])
    workbook.save(output)
    load_workbook(output).close()
    return output
