from __future__ import annotations

import hashlib
import json
import secrets
import uuid
import datetime
import shutil
from pathlib import Path
from typing import Any

from atlas.persistence.sqlite import StateStore

from .models import (
    Action,
    Backend,
    CANONICAL_LANES,
    RESULT_SCHEMA_VERSION,
    TASK_CONTRACT_VERSION,
    TASK_SCHEMA_VERSION,
    HuntTask,
    WorkerAttempt,
    contract_hash,
    normalize_lanes,
)
from .validation import missing_obligations, validate_result
from .completion_evaluator import evaluate_completion


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# A task in any of these statuses still owes live work and blocks completion.
IN_FLIGHT_TASK_STATUSES = ("PENDING", "CLAIMED", "RUNNING", "LEASED", "FOLLOW_UP_REQUIRED", "REJECTED_INVALID_RESULT")
# Terminal states that are acceptable only for a truthful PARTIAL run.
PARTIAL_TERMINAL_STATUSES = ("NEEDS_REPAIR", "EXTERNAL_ACCESS_LIMITED")


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
                payload["lanes"] = list(normalize_lanes(company.get("lanes")))
                self.conn.execute("""INSERT INTO vscode_hunt_tasks
                    (task_id,run_id,company_id,company_name,official_domain,careers_url,lanes_json,status,attempt_number)
                    VALUES(?,?,?,?,?,?,?,?,0)""", (task_id, run_id, company["company_id"], company.get("name", company["company_id"]), company["official_domain"], company.get("careers_url"), json.dumps(payload["lanes"]), "PENDING"))
        return run_id

    def _task_event(self, run_id: str, task_id: str, event_type: str, payload: dict[str, Any]) -> str:
        # vscode_worker_events.attempt_id is NOT NULL; task-level events use "".
        event_id = f"event-{uuid.uuid4().hex}"
        self.conn.execute(
            "INSERT INTO vscode_worker_events(event_id,run_id,task_id,attempt_id,event_type,payload_json) VALUES(?,?,?,?,?,?)",
            (event_id, run_id, task_id, "", event_type, json.dumps(payload)),
        )
        return event_id

    def upgrade_task_contract(self, run_id: str, target_lanes: tuple[str, ...] = CANONICAL_LANES) -> dict[str, Any]:
        """Replace obsolete uncommitted lane obligations with the canonical five.

        A task that already carries a durable result commit is never rewritten; it is
        preserved and reported instead. Each upgrade appends a TASK_CONTRACT_UPGRADED
        event recording the old and new contract hashes.
        """
        tasks = self.conn.execute("SELECT task_id, lanes_json FROM vscode_hunt_tasks WHERE run_id=?", (run_id,)).fetchall()
        upgraded: list[dict[str, Any]] = []
        preserved: list[dict[str, Any]] = []
        new = tuple(target_lanes)
        with self.conn:
            for task in tasks:
                task_id = task["task_id"]
                old = tuple(json.loads(task["lanes_json"]))
                committed = self.conn.execute("SELECT 1 FROM vscode_result_commits WHERE task_id=? LIMIT 1", (task_id,)).fetchone()
                if committed is not None:
                    preserved.append({"task_id": task_id, "reason": "has_durable_commit", "lanes": list(old)})
                    continue
                if old == new:
                    preserved.append({"task_id": task_id, "reason": "already_canonical", "lanes": list(old)})
                    continue
                self.conn.execute("UPDATE vscode_hunt_tasks SET lanes_json=? WHERE task_id=?", (json.dumps(list(new)), task_id))
                self._task_event(run_id, task_id, "TASK_CONTRACT_UPGRADED", {
                    "contract_version": TASK_CONTRACT_VERSION,
                    "from_lanes": list(old), "to_lanes": list(new),
                    "from_hash": contract_hash(old), "to_hash": contract_hash(new),
                })
                upgraded.append({"task_id": task_id, "from": list(old), "to": list(new)})
        return {"run_id": run_id, "contract_version": TASK_CONTRACT_VERSION, "upgraded": upgraded, "preserved": preserved}

    def mark_interrupted_uncommitted(self, run_id: str, reason: str = "MCP_TOOL_NOT_EXPOSED") -> dict[str, Any]:
        """Transition OPEN attempts with no durable result commit to INTERRUPTED_UNCOMMITTED.

        Chat-only worker output is never promoted. Attempt identity is preserved and the
        owning task returns to PENDING so a fresh resume attempt can be created later.
        """
        rows = self.conn.execute(
            """SELECT a.attempt_id AS attempt_id, a.task_id AS task_id
               FROM vscode_hunt_attempts a
               JOIN vscode_hunt_tasks t ON t.task_id = a.task_id
               WHERE t.run_id = ? AND a.status = 'OPEN'""",
            (run_id,),
        ).fetchall()
        transitioned: list[dict[str, Any]] = []
        with self.conn:
            for row in rows:
                attempt_id, task_id = row["attempt_id"], row["task_id"]
                committed = self.conn.execute("SELECT 1 FROM vscode_result_commits WHERE attempt_id=? LIMIT 1", (attempt_id,)).fetchone()
                if committed is not None:
                    continue
                self.conn.execute("UPDATE vscode_hunt_attempts SET status='INTERRUPTED_UNCOMMITTED' WHERE attempt_id=?", (attempt_id,))
                task = self.conn.execute("SELECT status FROM vscode_hunt_tasks WHERE task_id=?", (task_id,)).fetchone()
                if task is not None and task["status"] not in {"COMPLETE", "EXTERNAL_ACCESS_LIMITED"}:
                    self.conn.execute("UPDATE vscode_hunt_tasks SET status='PENDING' WHERE task_id=?", (task_id,))
                self._task_event(run_id, task_id, "ATTEMPT_INTERRUPTED_UNCOMMITTED", {"attempt_id": attempt_id, "reason": reason})
                transitioned.append({"attempt_id": attempt_id, "task_id": task_id, "reason": reason})
        return {"run_id": run_id, "reason": reason, "interrupted": transitioned}

    def get_task_status(self, run_id: str, task_id: str) -> dict[str, Any]:
        task = self.conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        if task is None:
            raise ValueError("unknown task")
        attempts = self.conn.execute("SELECT attempt_id,status,attempt_number FROM vscode_hunt_attempts WHERE task_id=? ORDER BY attempt_number", (task_id,)).fetchall()
        commit = self.conn.execute("SELECT commit_id,completion_action,missing_json FROM vscode_result_commits WHERE task_id=? ORDER BY committed_at DESC LIMIT 1", (task_id,)).fetchone()
        return {
            "run_id": run_id, "task_id": task_id, "status": task["status"],
            "lanes": json.loads(task["lanes_json"]),
            "attempts": [dict(row) for row in attempts],
            "latest_commit": {"commit_id": commit["commit_id"], "completion_action": commit["completion_action"], "missing_obligations": json.loads(commit["missing_json"])} if commit else None,
        }

    def commit_result_payload(self, run_id: str, task_id: str, attempt_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Commit an inline result dict so a worker never needs filesystem-write access."""
        safe_task_id = "".join("_" if char in '<>:"/\\|?*' else char for char in task_id)
        inbound = Path(self.root) / run_id / safe_task_id / "inbound"
        inbound.mkdir(parents=True, exist_ok=True)
        source = inbound / f"{attempt_id}.json"
        source.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return self.commit_result(run_id, task_id, attempt_id, source)

    def build_audit_workbook(self, run_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        from .report import build_v32_workbook
        return {"run_id": run_id, "kind": "Audit", "path": str(build_v32_workbook(self.root, run_id, self.conn, kind="Audit", context=context))}

    def build_checkpoint_workbook(self, run_id: str, sequence: int = 1, context: dict[str, Any] | None = None) -> dict[str, Any]:
        from .report import build_v32_workbook
        return {"run_id": run_id, "kind": "Checkpoint", "path": str(build_v32_workbook(self.root, run_id, self.conn, kind="Checkpoint", sequence=sequence, context=context))}

    def build_final_workbook(self, run_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        from .report import build_v32_workbook
        return {"run_id": run_id, "kind": "Final", "path": str(build_v32_workbook(self.root, run_id, self.conn, kind="Final", context=context))}

    def next_tasks(self, run_id: str, limit: int = 2, materialize: bool = False) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND status='PENDING' ORDER BY task_id LIMIT ?", (run_id, limit)).fetchall()
        output = []
        for row in rows:
            safe_task_id = "".join("_" if char in '<>:"/\\|?*' else char for char in row["task_id"])
            task_dir = self.root / run_id / safe_task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            package_path = task_dir / "task.json"
            result_path = task_dir / "result.json"
            package = {"schema_version": TASK_SCHEMA_VERSION, "run_id": run_id, "task_id": row["task_id"], "company_id": row["company_id"], "company_name": row["company_name"], "official_domain": row["official_domain"], "careers_url": row["careers_url"], "lanes": json.loads(row["lanes_json"]), "query_families": ["Java", "Java Backend", "Java Full Stack", "React Frontend", ".NET", "C#", "enterprise applications", "HCM payroll integration"], "india_policy": "INDIA_ONLY_EXPLICIT_LOCATION", "experience_policy": "HARD_REJECT_MANDATORY_4_PLUS", "candidate_profile": {"redacted": True, "target_lanes": json.loads(row["lanes_json"]), "experience_years": "policy-gated"}, "allowed_output_path": str(result_path), "safety": ["read-only", "no-login", "no-apply", "no-external-sites-other-than-assigned-company"], "completion_checklist": ["official career navigation", "real result state per lane", "canonical detail evidence", "India eligibility", "source health"]}
            if materialize:
                package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")
            output.append({**package, "task_package_path": str(package_path), "result_path": str(result_path)})
        return output

    def record_attempt(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str | None,
        backend: Backend,
        parent_attempt_id: str | None = None,
        *,
        attempt_kind: str = "PRIMARY",
        parent_commit_id: str | None = None,
        browser_backend: str | None = None,
        browser_slot: str | None = None,
        worker_invocation_id: str | None = None,
    ) -> WorkerAttempt:
        row = self.conn.execute("SELECT attempt_number,status FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        if row is None: raise ValueError("unknown task")
        if row["status"] == "COMPLETE": raise ValueError("completed task cannot be rerun")
        attempt_id = attempt_id or f"attempt-{secrets.token_hex(12)}"
        number = int(row["attempt_number"]) + 1
        with self.conn:
            self.conn.execute("UPDATE vscode_hunt_tasks SET status='RUNNING',attempt_number=? WHERE task_id=?", (number, task_id))
            self.conn.execute(
                """INSERT INTO vscode_hunt_attempts(
                    attempt_id,task_id,attempt_number,backend,parent_attempt_id,status,
                    attempt_kind,parent_commit_id,browser_backend,browser_slot,
                    worker_invocation_id,started_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (attempt_id, task_id, number, backend.value, parent_attempt_id, "OPEN",
                 attempt_kind, parent_commit_id, browser_backend, browser_slot,
                 worker_invocation_id),
            )
        return WorkerAttempt(attempt_id, task_id, number, backend, parent_attempt_id)

    def reopen_browser_recovery(
        self,
        run_id: str,
        task_id: str,
        *,
        expected_commit_id: str,
        expected_result_sha256: str,
        browser_backend: str,
        browser_slot: str | None = None,
    ) -> dict[str, Any]:
        """Reopen only a retryable BROWSER_UNAVAILABLE terminal task.

        The prior attempt and commit remain immutable; the new attempt is linked to both.
        Infrastructure recovery is intentionally distinct from content correction.
        """
        task = self.conn.execute(
            "SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
        ).fetchone()
        commit = self.conn.execute(
            """SELECT * FROM vscode_result_commits
               WHERE run_id=? AND task_id=? AND commit_id=? AND result_sha256=?""",
            (run_id, task_id, expected_commit_id, expected_result_sha256),
        ).fetchone()
        if task is None or commit is None:
            raise ValueError("expected task/commit identity or hash does not match")
        if task["status"] != "NEEDS_REPAIR":
            raise ValueError("only NEEDS_REPAIR tasks may be reopened for browser recovery")
        result = json.loads(Path(commit["result_path"]).read_text(encoding="utf-8"))
        errors = result.get("browser_errors") or []
        error_text = json.dumps(errors).upper()
        retryable_browser_failure = (
            "BROWSER_UNAVAILABLE" in error_text
            or ("BROWSER" in error_text and "NOT AVAILABLE" in error_text)
            or ("BUILT-IN BROWSER" in error_text and "NOT EXPOSED" in error_text)
        )
        if not retryable_browser_failure:
            raise ValueError("latest result is not an explicitly retryable BROWSER_UNAVAILABLE failure")
        previous_attempt = self.conn.execute(
            "SELECT attempt_id FROM vscode_hunt_attempts WHERE attempt_id=? AND task_id=?",
            (commit["attempt_id"], task_id),
        ).fetchone()
        if previous_attempt is None:
            raise ValueError("committed attempt is missing")
        new_attempt_id = f"attempt-recovery-{secrets.token_hex(12)}"
        number = int(task["attempt_number"]) + 1
        with self.conn:
            self.conn.execute("UPDATE vscode_hunt_tasks SET status='PENDING',attempt_number=? WHERE task_id=?", (number, task_id))
            self.conn.execute(
                """INSERT INTO vscode_hunt_attempts(
                    attempt_id,task_id,attempt_number,backend,parent_attempt_id,status,
                    attempt_kind,parent_commit_id,browser_backend,browser_slot,
                    worker_invocation_id,started_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (new_attempt_id, task_id, number, Backend.VSCODE_SUBAGENT.value,
                 commit["attempt_id"], "PENDING", "BROWSER_BACKEND_RECOVERY",
                 expected_commit_id, browser_backend, browser_slot, None),
            )
            self._task_event(run_id, task_id, "TASK_REOPENED_FOR_BROWSER_RECOVERY", {
                "previous_attempt_id": commit["attempt_id"],
                "previous_commit_id": expected_commit_id,
                "previous_result_sha256": expected_result_sha256,
                "browser_backend": browser_backend,
                "browser_slot": browser_slot,
                "new_attempt_id": new_attempt_id,
            })
        return {
            "run_id": run_id, "task_id": task_id, "new_attempt_id": new_attempt_id,
            "parent_attempt_id": commit["attempt_id"], "parent_commit_id": expected_commit_id,
            "browser_backend": browser_backend, "browser_slot": browser_slot,
            "attempt_kind": "BROWSER_BACKEND_RECOVERY", "status": "PENDING",
        }

    def start_attempt(self, run_id: str, task_id: str, attempt_id: str) -> dict[str, Any]:
        """Activate a pre-created recovery attempt without creating a duplicate."""
        row = self.conn.execute(
            "SELECT a.*, t.status AS task_status FROM vscode_hunt_attempts a "
            "JOIN vscode_hunt_tasks t ON t.task_id=a.task_id "
            "WHERE a.attempt_id=? AND a.task_id=? AND t.run_id=?",
            (attempt_id, task_id, run_id),
        ).fetchone()
        if row is None:
            raise ValueError("unknown attempt")
        if row["status"] != "PENDING" or row["attempt_kind"] != "BROWSER_BACKEND_RECOVERY":
            raise ValueError("only a pending browser-recovery attempt may be started")
        with self.conn:
            self.conn.execute("UPDATE vscode_hunt_attempts SET status='OPEN',started_at=CURRENT_TIMESTAMP WHERE attempt_id=?", (attempt_id,))
            self.conn.execute("UPDATE vscode_hunt_tasks SET status='RUNNING' WHERE task_id=?", (task_id,))
        return {"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id, "status": "OPEN", "task_status": "RUNNING"}

    def retry_invalid_recovery(self, run_id: str, task_id: str, *, expected_invalid_commit_id: str, browser_backend: str) -> dict[str, Any]:
        """Create one replacement recovery attempt after a rejected payload.

        The rejected commit remains immutable and is linked as the immediate parent;
        this path cannot reopen a successful result or bypass the original recovery.
        """
        task = self.conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        invalid = self.conn.execute("SELECT * FROM vscode_result_commits WHERE run_id=? AND task_id=? AND commit_id=?", (run_id, task_id, expected_invalid_commit_id)).fetchone()
        if task is None or invalid is None or invalid["validation_state"] != "INVALID":
            raise ValueError("expected invalid recovery commit does not match")
        previous = self.conn.execute("SELECT attempt_kind,parent_commit_id FROM vscode_hunt_attempts WHERE attempt_id=?", (invalid["attempt_id"],)).fetchone()
        if previous is None or previous["attempt_kind"] != "BROWSER_BACKEND_RECOVERY":
            raise ValueError("only invalid browser-recovery attempts may be retried")
        active = self.conn.execute("SELECT 1 FROM vscode_hunt_attempts WHERE task_id=? AND status IN ('OPEN','PENDING')", (task_id,)).fetchone()
        if active is not None:
            raise ValueError("task already has an active attempt")
        new_attempt_id = f"attempt-recovery-retry-{secrets.token_hex(12)}"
        number = int(task["attempt_number"]) + 1
        with self.conn:
            self.conn.execute("UPDATE vscode_hunt_tasks SET status='PENDING',attempt_number=? WHERE task_id=?", (number, task_id))
            self.conn.execute(
                """INSERT INTO vscode_hunt_attempts(
                    attempt_id,task_id,attempt_number,backend,parent_attempt_id,status,
                    attempt_kind,parent_commit_id,browser_backend,started_at
                ) VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
                (new_attempt_id, task_id, number, Backend.VSCODE_SUBAGENT.value,
                 invalid["attempt_id"], "PENDING", "BROWSER_BACKEND_RECOVERY",
                 previous["parent_commit_id"], browser_backend),
            )
            self._task_event(run_id, task_id, "TASK_RECOVERY_RETRY_AFTER_INVALID_RESULT", {
                "invalid_commit_id": expected_invalid_commit_id,
                "invalid_attempt_id": invalid["attempt_id"],
                "new_attempt_id": new_attempt_id,
                "browser_backend": browser_backend,
            })
        return {"run_id": run_id, "task_id": task_id, "new_attempt_id": new_attempt_id, "parent_attempt_id": invalid["attempt_id"], "parent_commit_id": previous["parent_commit_id"], "attempt_kind": "BROWSER_BACKEND_RECOVERY", "status": "PENDING"}

    def resume_interrupted_recovery(self, run_id: str, task_id: str, *, parent_attempt_id: str, browser_backend: str) -> dict[str, Any]:
        """Create a linked recovery attempt after an explicit interrupted attempt."""
        task = self.conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        parent = self.conn.execute("SELECT * FROM vscode_hunt_attempts WHERE attempt_id=? AND task_id=? AND status='INTERRUPTED_UNCOMMITTED'", (parent_attempt_id, task_id)).fetchone()
        if task is None or parent is None or task["status"] != "PENDING":
            raise ValueError("task or interrupted recovery parent is not eligible")
        active = self.conn.execute("SELECT 1 FROM vscode_hunt_attempts WHERE task_id=? AND status IN ('OPEN','PENDING')", (task_id,)).fetchone()
        if active is not None:
            raise ValueError("task already has an active attempt")
        attempt_id = f"attempt-recovery-{secrets.token_hex(12)}"
        number = int(task["attempt_number"]) + 1
        with self.conn:
            self.conn.execute("UPDATE vscode_hunt_tasks SET status='PENDING',attempt_number=? WHERE task_id=?", (number, task_id))
            self.conn.execute("""INSERT INTO vscode_hunt_attempts(
                attempt_id,task_id,attempt_number,backend,parent_attempt_id,status,
                attempt_kind,browser_backend,started_at
            ) VALUES(?,?,?,?,?,?,?,?,NULL)""", (attempt_id, task_id, number, Backend.VSCODE_SUBAGENT.value,
                parent_attempt_id, "PENDING", "BROWSER_BACKEND_RECOVERY", browser_backend))
            self._task_event(run_id, task_id, "TASK_RESUMED_AFTER_INTERRUPTED_RECOVERY", {
                "parent_attempt_id": parent_attempt_id, "new_attempt_id": attempt_id,
                "browser_backend": browser_backend,
            })
        return {"run_id": run_id, "task_id": task_id, "new_attempt_id": attempt_id, "parent_attempt_id": parent_attempt_id, "attempt_kind": "BROWSER_BACKEND_RECOVERY", "status": "PENDING"}

    def terminalize_exhausted_recovery(self, run_id: str, task_id: str, *, reason: str) -> dict[str, Any]:
        """Mark an exhausted invalid-recovery task as truthful NEEDS_REPAIR."""
        task = self.conn.execute("SELECT status FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        attempts = self.conn.execute("SELECT status FROM vscode_hunt_attempts WHERE task_id=?", (task_id,)).fetchall()
        latest = self.conn.execute("""SELECT completion_action,validation_state FROM vscode_result_commits
            WHERE run_id=? AND task_id=? ORDER BY committed_at DESC LIMIT 1""", (run_id, task_id)).fetchone()
        if task is None or task["status"] not in {"PENDING", "REJECTED_INVALID_RESULT"}:
            raise ValueError("only a pending or rejected-invalid task may be terminalized")
        if any(row["status"] in {"OPEN", "PENDING", "RUNNING"} for row in attempts):
            raise ValueError("active attempt remains")
        if latest is None or latest["validation_state"] != "INVALID":
            raise ValueError("latest result is not an invalid recovery result")
        with self.conn:
            self.conn.execute("UPDATE vscode_hunt_tasks SET status='NEEDS_REPAIR' WHERE task_id=?", (task_id,))
            self._task_event(run_id, task_id, "TASK_TERMINALIZED_NEEDS_REPAIR", {"reason": reason})
        return {"run_id": run_id, "task_id": task_id, "status": "NEEDS_REPAIR", "reason": reason}

    def heartbeat(self, run_id: str, task_id: str, attempt_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        attempt = self.conn.execute("SELECT attempt_id FROM vscode_hunt_attempts WHERE attempt_id=? AND task_id=?", (attempt_id, task_id)).fetchone()
        if attempt is None:
            raise ValueError("unknown attempt")
        event_id = f"heartbeat-{uuid.uuid4().hex}"
        with self.conn:
            self.conn.execute("INSERT INTO vscode_worker_events(event_id,run_id,task_id,attempt_id,event_type,payload_json) VALUES(?,?,?,?,?,?)", (event_id, run_id, task_id, attempt_id, "HEARTBEAT", json.dumps(payload or {})))
        return {"event_id": event_id, "attempt_id": attempt_id, "status": "RUNNING"}

    def record_checkpoint(self, run_id: str, task_id: str, attempt_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = f"event-{uuid.uuid4().hex}"
        with self.conn:
            self.conn.execute("INSERT INTO vscode_worker_events(event_id,run_id,task_id,attempt_id,event_type,payload_json) VALUES(?,?,?,?,?,?)", (event_id, run_id, task_id, attempt_id, event_type, json.dumps(payload)))
        return {"event_id": event_id, "event_type": event_type}

    def commit_result(self, run_id: str, task_id: str, attempt_id: str, source_path: Path) -> dict[str, Any]:
        task = self.conn.execute("SELECT * FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        attempt = self.conn.execute("SELECT * FROM vscode_hunt_attempts WHERE attempt_id=? AND task_id=?", (attempt_id, task_id)).fetchone()
        if task is None or attempt is None:
            raise ValueError("unknown task or attempt")
        result = json.loads(Path(source_path).read_text(encoding="utf-8"))
        result_hash = _hash_file(source_path)
        existing = self.conn.execute("SELECT * FROM vscode_result_commits WHERE attempt_id=?", (attempt_id,)).fetchone()
        if existing:
            if existing["result_sha256"] != result_hash:
                raise ValueError("commit conflict: attempt already committed with a different hash")
            return {"commit_id": existing["commit_id"], "result_sha256": result_hash, "task_status": task["status"], "completion_action": existing["completion_action"], "missing_obligations": json.loads(existing["missing_json"])}
        errors = validate_result(result, task)
        decision = evaluate_completion(result, tuple(json.loads(task["lanes_json"]))) if not errors else None
        action = "REJECTED_INVALID_RESULT" if errors else decision.action
        missing = errors if errors else list(decision.missing_obligations)
        commit_id = f"commit-{uuid.uuid4().hex}"
        durable = Path(self.root) / run_id / "committed" / f"{attempt_id}.json"
        durable.parent.mkdir(parents=True, exist_ok=True)
        temp = durable.with_suffix(".tmp")
        shutil.copyfile(source_path, temp)
        temp.replace(durable)
        with self.conn:
            self.conn.execute("INSERT INTO vscode_result_commits(commit_id,run_id,task_id,attempt_id,worker_invocation_id,result_path,result_sha256,schema_version,validation_state,completion_action,missing_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (commit_id, run_id, task_id, attempt_id, str(result.get("worker_invocation_id", "")), str(durable), result_hash, int(result.get("schema_version", 0)), "VALID" if not errors else "INVALID", action, json.dumps(missing)))
            self.conn.execute("UPDATE vscode_hunt_attempts SET status=?,result_hash=?,result_path=?,missing_json=? WHERE attempt_id=?", ("RESULT_COMMITTED" if not errors else "INVALID_RESULT", result_hash, str(durable), json.dumps(missing), attempt_id))
            self.conn.execute("UPDATE vscode_hunt_tasks SET status=? WHERE task_id=?", (action, task_id))
        return {"commit_id": commit_id, "result_sha256": result_hash, "task_status": action, "completion_action": action, "missing_obligations": missing, "result_path": str(durable)}

    def finalize_run(self, run_id: str) -> dict[str, Any]:
        task_rows = self.conn.execute("SELECT task_id,status FROM vscode_hunt_tasks WHERE run_id=?", (run_id,)).fetchall()
        attempts = self.conn.execute("SELECT attempt_id,status,task_id FROM vscode_hunt_attempts WHERE task_id IN (SELECT task_id FROM vscode_hunt_tasks WHERE run_id=?)", (run_id,)).fetchall()
        commits = self.conn.execute("SELECT task_id,attempt_id,validation_state FROM vscode_result_commits WHERE run_id=?", (run_id,)).fetchall()

        in_flight = [row["task_id"] for row in task_rows if row["status"] in IN_FLIGHT_TASK_STATUSES]
        open_attempts = [row["attempt_id"] for row in attempts if row["status"] == "OPEN"]
        committed_task_ids = {row["task_id"] for row in commits}
        interrupted_task_ids = {row["task_id"] for row in attempts if row["status"] == "INTERRUPTED_UNCOMMITTED"}
        undurable = [row["task_id"] for row in task_rows if row["task_id"] not in committed_task_ids and row["task_id"] not in interrupted_task_ids]

        failures: list[str] = []
        required_actions: list[str] = []
        if in_flight:
            failures.append(f"nonterminal tasks remain: {in_flight}")
            required_actions.append(f"resolve nonterminal tasks: {in_flight}")
        if open_attempts:
            failures.append(f"open uncommitted attempts remain: {open_attempts}")
            required_actions.append("commit or transition every OPEN attempt to an interrupted/error terminal state")
        if undurable:
            failures.append(f"tasks without a durable commit or interrupted record: {undurable}")
            required_actions.append("ensure each task has a durable commit or an interrupted/error record")

        can_finish = not failures
        overall_status = None
        if can_finish:
            overall_status = "PARTIAL" if any(row["status"] in PARTIAL_TERMINAL_STATUSES for row in task_rows) else "PASS"
        return {
            "can_finish": can_finish,
            "run_id": run_id,
            "overall_status": overall_status,
            "failures": failures,
            "required_actions": required_actions,
            "tasks": len(task_rows),
            "attempts": len(attempts),
            "committed_results": len(commits),
            "open_attempts": len(open_attempts),
        }

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
        response = {"action": action.value, "run_id": run_id, "task_id": task_id, "attempt_id": attempt_id, "result_path": str(path), "result_hash": result_hash, "missing_obligations": missing, "prior_result_reference": {"path": str(path), "sha256": result_hash}}
        if action is Action.FOLLOW_UP_REQUIRED:
            response["new_attempt_id"] = f"attempt-{secrets.token_hex(12)}"
            response["correction_prompt"] = f"Correct the same task {task_id}. Resolve exactly: {', '.join(missing)}. Return one typed JSON object."
        return response

    def status(self, run_id: str) -> dict[str, Any]:
        rows = self.conn.execute("SELECT status,COUNT(*) AS n FROM vscode_hunt_tasks WHERE run_id=? GROUP BY status", (run_id,)).fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        return {"run_id": run_id, "counts": counts, "all_tasks_terminal": not any(status in counts for status in IN_FLIGHT_TASK_STATUSES)}
