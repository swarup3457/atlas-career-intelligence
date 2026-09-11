from __future__ import annotations

import argparse
import json
from pathlib import Path

from atlas.config import load_settings
from atlas.persistence.sqlite import StateStore

from .models import Backend, TASK_CONTRACT_VERSION
from .report import build_workbook
from .service import VscodeHuntService


def _service() -> tuple[StateStore, VscodeHuntService]:
    settings = load_settings()
    settings.ensure_directories()
    root = settings.output_dir
    store = StateStore(settings.state_db)
    return store, VscodeHuntService(store, root)


def command(args: argparse.Namespace) -> int:
    store, service = _service()
    try:
        if args.vscode_command == "doctor":
            payload = {"state_db": str(store.db_path), "schema_version": store.schema_version(), "backends": [backend.value for backend in Backend], "browser": True, "run_subagent": True, "persistent_session_tools": False}
        elif args.vscode_command == "create-run":
            data = json.loads(Path(args.companies_file).read_text(encoding="utf-8-sig"))
            payload = {"run_id": service.create_run(data.get("companies", data)), "status": "SEALED"}
        elif args.vscode_command == "next-tasks":
            payload = {"tasks": service.next_tasks(args.run_id, args.limit, args.materialize)}
        elif args.vscode_command == "record-attempt":
            attempt = service.record_attempt(args.run_id, args.task_id, args.attempt_id, Backend(args.backend), args.parent_attempt_id)
            payload = {"attempt_id": attempt.attempt_id, "task_id": attempt.task_id, "attempt_number": attempt.attempt_number}
        elif args.vscode_command == "ingest-result":
            payload = service.ingest(args.run_id, args.task_id, args.attempt_id, Path(args.file))
        elif args.vscode_command == "validate-task":
            row = store._conn.execute("SELECT status FROM vscode_hunt_tasks WHERE run_id=? AND task_id=?", (args.run_id, args.task_id)).fetchone()
            payload = {"run_id": args.run_id, "task_id": args.task_id, "status": row["status"] if row else "NOT_FOUND"}
        elif args.vscode_command == "status":
            payload = service.status(args.run_id)
        elif args.vscode_command == "build-workbook":
            path = build_workbook(service.root, args.run_id, store._conn)
            payload = {"run_id": args.run_id, "path": str(path), "sheets": list(__import__("atlas.vscode_hunt.report", fromlist=["SHEETS"]).SHEETS)}
        elif args.vscode_command == "upgrade-contract":
            payload = service.upgrade_task_contract(args.run_id)
        elif args.vscode_command == "mark-interrupted":
            payload = service.mark_interrupted_uncommitted(args.run_id, args.reason)
        elif args.vscode_command == "get-task-status":
            payload = service.get_task_status(args.run_id, args.task_id)
        elif args.vscode_command == "build-audit-workbook":
            context = {"status": args.status, "note": args.note, "task_contract_version": TASK_CONTRACT_VERSION, "discovery_reused_from_parent_run": True}
            payload = service.build_audit_workbook(args.run_id, context)
        elif args.vscode_command == "resume":
            payload = service.status(args.run_id)
        else:
            return 2
        print(json.dumps(payload, indent=2))
        return 0
    finally:
        store.close()


def register_vscode_hunt_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("vscode-hunt", help="Stateless VS Code company-worker control plane.")
    children = parser.add_subparsers(dest="vscode_command", required=True)
    p = children.add_parser("doctor"); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    p = children.add_parser("create-run"); p.add_argument("--companies-file", required=True); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    p = children.add_parser("next-tasks"); p.add_argument("--run-id", required=True); p.add_argument("--limit", type=int, default=2); p.add_argument("--materialize", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    p = children.add_parser("record-attempt"); p.add_argument("--run-id", required=True); p.add_argument("--task-id", required=True); p.add_argument("--attempt-id", required=True); p.add_argument("--backend", required=True, choices=[b.value for b in Backend]); p.add_argument("--parent-attempt-id"); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    p = children.add_parser("ingest-result"); p.add_argument("--run-id", required=True); p.add_argument("--task-id", required=True); p.add_argument("--attempt-id", required=True); p.add_argument("--file", required=True); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    p = children.add_parser("validate-task"); p.add_argument("--run-id", required=True); p.add_argument("--task-id", required=True); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    for name in ("status", "build-workbook", "resume"):
        p = children.add_parser(name); p.add_argument("--run-id", required=True); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    p = children.add_parser("upgrade-contract"); p.add_argument("--run-id", required=True); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    p = children.add_parser("mark-interrupted"); p.add_argument("--run-id", required=True); p.add_argument("--reason", default="MCP_TOOL_NOT_EXPOSED"); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    p = children.add_parser("get-task-status"); p.add_argument("--run-id", required=True); p.add_argument("--task-id", required=True); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
    p = children.add_parser("build-audit-workbook"); p.add_argument("--run-id", required=True); p.add_argument("--status", default=""); p.add_argument("--note", default=""); p.add_argument("--json", action="store_true"); p.set_defaults(func=command)
