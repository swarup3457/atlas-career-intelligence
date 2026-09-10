from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from pathlib import Path
from typing import Any

from atlas.persistence.sqlite import StateStore

from .models import Action, Backend, HuntTask, WorkerAttempt
from .validation import missing_obligations, validate_result


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class VscodeHuntService:
    def __init__(self, store: StateStore, root: Path):
        self.store = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = store._conn  # StateStore is the sole owner of this connection.

    def create_run(self, companies: list[dict[str, Any]], run_id: str | None = None) -> str:
        run_id = run_id or f"vscode-{uuid.uuid4().hex}"
        with self.conn:
            self.conn.execute("INSERT INTO vscode_hunt_runs(run_id,status,company_count) VALUES(?,?,?)", (run_id, "SEALED", len(companies)))
            for company in companies:
                task_id = f"{run_id}::{company['company_id']}"
                payload = dict(company)
                payload["lanes"] = list(company.get("lanes", ("JAVA_BACKEND", "DOTNET_BACKEND", "REACT_ENTERPRISE")))
                self.conn.execute("""INSERT INTO vscode_hunt_tasks
                    (task_id,run_id,company_id,company_name,official_domain,careers_url,lanes_json,status,attempt_number)
                    VALUES(?,?,?,?,?,?,?,?,0)""", (task_id, run_id, company["company_id"], company.get("name", company["company_id"]), company["official_domain"], company.get("careers_url"), json.dumps(payload["lanes"]), "PENDING"))
        return run_id

    def next_tasks(self, run_id: str, limit: int = 2, materialize: bool = False) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND status='PENDING' ORDER BY task_id LIMIT ?", (run_id, limit)).fetchall()
        output = []
        for row in rows:
            safe_task_id = "".join("_" if char in '<>:"/\\|?*' else char for char in row["task_id"])
            task_dir = self.root / run_id / safe_task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            package_path = task_dir / "task.json"
            result_path = task_dir / "result.json"
            package = {"run_id": run_id, "task_id": row["task_id"], "company_id": row["company_id"], "company_name": row["company_name"], "official_domain": row["official_domain"], "careers_url": row["careers_url"], "lanes": json.loads(row["lanes_json"]), "allowed_output_path": str(result_path), "safety": ["read-only", "no-login", "no-apply"], "completion_checklist": ["detail evidence", "India eligibility", "lane coverage"]}
            if materialize:
                package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")
            output.append({**package, "task_package_path": str(package_path), "result_path": str(result_path)})
        return output

    def record_attempt(self, run_id: str, task_id: str, attempt_id: str | None, backend: Backend, parent_attempt_id: str | None = None) -> WorkerAttempt:
        row = self.conn.execute("SELECT attempt_number,status FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        if row is None: raise ValueError("unknown task")
        if row["status"] == "COMPLETE": raise ValueError("completed task cannot be rerun")
        attempt_id = attempt_id or f"attempt-{secrets.token_hex(12)}"
        number = int(row["attempt_number"]) + 1
        with self.conn:
            self.conn.execute("UPDATE vscode_hunt_tasks SET status='LEASED',attempt_number=? WHERE task_id=?", (number, task_id))
            self.conn.execute("INSERT INTO vscode_hunt_attempts(attempt_id,task_id,attempt_number,backend,parent_attempt_id,status) VALUES(?,?,?,?,?,?)", (attempt_id, task_id, number, backend.value, parent_attempt_id, "OPEN"))
        return WorkerAttempt(attempt_id, task_id, number, backend, parent_attempt_id)

    def ingest(self, run_id: str, task_id: str, attempt_id: str, path: Path) -> dict[str, Any]:
        result_hash = _hash_file(path)
        result = json.loads(path.read_text(encoding="utf-8"))
        task = self.conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        attempt = self.conn.execute("SELECT attempt_id FROM vscode_hunt_attempts WHERE attempt_id=? AND task_id=?", (attempt_id, task_id)).fetchone()
        errors = ["unknown attempt identity"] if attempt is None else validate_result(result, task)
        if errors:
            action = Action.REJECTED_INVALID_RESULT
            missing = errors
        else:
            missing = missing_obligations(result, task)
            action = Action.FOLLOW_UP_REQUIRED if missing else Action.COMPLETE
        with self.conn:
            self.conn.execute("UPDATE vscode_hunt_attempts SET status=?,result_hash=?,result_path=?,missing_json=? WHERE attempt_id=?", (action.value, result_hash, str(path), json.dumps(missing), attempt_id))
            self.conn.execute("UPDATE vscode_hunt_tasks SET status=? WHERE task_id=?", (action.value, task_id))
        response = {"action": action.value, "run_id": run_id, "task_id": task_id, "attempt_id": attempt_id, "result_path": str(path), "result_hash": result_hash, "missing_obligations": missing}
        if action is Action.FOLLOW_UP_REQUIRED:
            response["new_attempt_id"] = f"attempt-{secrets.token_hex(12)}"
            response["correction_prompt"] = f"Correct the same task {task_id}. Resolve exactly: {', '.join(missing)}. Return one typed JSON object."
        return response

    def status(self, run_id: str) -> dict[str, Any]:
        rows = self.conn.execute("SELECT status,COUNT(*) AS n FROM vscode_hunt_tasks WHERE run_id=? GROUP BY status", (run_id,)).fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        return {"run_id": run_id, "counts": counts, "all_tasks_terminal": not any(k in counts for k in ("PENDING", "LEASED", "FOLLOW_UP_REQUIRED"))}
