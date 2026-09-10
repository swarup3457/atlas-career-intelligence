"""``atlas browser-backend ...`` CLI commands.

install / doctor / canary / status / review-queue. Canaries write to
``output/production/browser_backend_canaries/<RUN_ID>/`` with unique, immutable
run ids; the ordinary ``latest`` workbook is never touched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from atlas.browser_backend.models import CompanyTask, CliProcessConfig, new_run_id

# General, non-job-specific official entry points. Recipes are inferred live;
# NO specific requisition id is ever seeded.
_COMPANY_SEEDS = {
    "accenture": {
        "official_domain": "accenture.com",
        "career_entry_url": "https://www.accenture.com/in-en/careers/jobsearch",
        "query_terms": ("Java", "Java Developer", "Java Full Stack"),
        "lanes": ("JAVA_BACKEND", "JAVA_FULLSTACK"),
    },
    "infosys": {
        "official_domain": "infosys.com",
        "career_entry_url": "https://career.infosys.com/joblist",
        "query_terms": ("Java", "Java Developer"),
        "lanes": ("JAVA_BACKEND", "JAVA_FULLSTACK"),
    },
}

_CANARY_ROOT = Path("output") / "production" / "browser_backend_canaries"


def _emit(as_json: bool, payload: dict, text_lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        for line in text_lines:
            print(line)


def _cmd_bb_install(args: argparse.Namespace) -> int:
    from atlas.browser_backend.install import InstallError, install
    try:
        report = install(force=getattr(args, "force", False))
    except InstallError as exc:
        _emit(args.json, {"ok": False, "error": str(exc)}, [f"INSTALL FAILED: {exc}"])
        return 1
    _emit(args.json, {"ok": True, **report},
          [f"{report['action']}: {report['package']}@{report['version']} ({report['license']})",
           f"cli_path={report['cli_path']}", f"verified={report['verified']}"])
    return 0 if report.get("verified") else 1


def _cmd_bb_doctor(args: argparse.Namespace) -> int:
    from atlas.browser_backend.doctor import FAIL, run_doctor
    rep = run_doctor()
    _emit(args.json, rep.to_dict(), [rep.render()])
    return 0 if rep.overall != FAIL else 1


def _resolve_task(company: str, run_id: str, headed: bool) -> CompanyTask:
    seed = _COMPANY_SEEDS.get(company.strip().lower(), {})
    return CompanyTask(
        company=company,
        official_domain=seed.get("official_domain", ""),
        career_entry_url=seed.get("career_entry_url", ""),
        run_id=run_id,
        query_terms=tuple(seed.get("query_terms", ("Java",))),
        locations=("India",),
        lanes=tuple(seed.get("lanes", ("JAVA_BACKEND",))),
        headed=headed,
    )


def _cmd_bb_canary(args: argparse.Namespace) -> int:
    run_id = getattr(args, "run_id", None) or new_run_id("canary")
    run_dir = _CANARY_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    task = _resolve_task(args.company, run_id, bool(getattr(args, "headed", False)))

    plan = {
        "run_id": run_id, "company": task.company, "task_id": task.task_id,
        "official_domain": task.official_domain, "career_entry_url": task.career_entry_url,
        "query_terms": list(task.query_terms), "lanes": list(task.lanes),
        "live": bool(getattr(args, "live", False)), "headed": task.headed,
        "run_dir": str(run_dir),
    }
    (run_dir / "canary_plan.json").write_bytes(json.dumps(plan, indent=2).encode("utf-8"))

    if not getattr(args, "live", False):
        _emit(args.json, {"status": "DRY_RUN", **plan},
              ["DRY-RUN (pass --live to spawn the Copilot Playwright backend).",
               f"RUN_ID={run_id} COMPANY={task.company}", f"RUN_DIR={run_dir}"])
        return 0

    from atlas.browser_backend.cli_playwright import CliPlaywrightBackend
    from atlas.browser_backend.hybrid import (
        CompletionLedger, HybridRouter, run_company_with_policy,
    )

    cfg = CliProcessConfig(model=getattr(args, "model", None) or "claude-sonnet-5",
                           max_ai_credits=getattr(args, "max_credits", None) or 250)
    backend = CliPlaywrightBackend(run_dir, process_config=cfg, agent=getattr(args, "agent", "") or "")
    router = HybridRouter(backend)
    ledger = CompletionLedger(run_dir / "completion_ledger.jsonl")
    result = run_company_with_policy(
        router, task, ledger=ledger,
        review_queue_path=Path("output") / "production" / "browser_review_queue.jsonl",
        max_internal_retries=0,  # canary: at most the attempts specified externally
    )
    payload = result.to_dict()
    payload["run_id"] = run_id
    (run_dir / "canary_result.json").write_bytes(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
    _emit(args.json, payload, [
        f"RUN_ID={run_id} COMPANY={task.company}",
        f"STATUS={result.status} VALID={result.valid} BROWSER_EVIDENCE={result.browser_evidence}",
        f"DETAILS_OPENED={result.details_opened} USAGE_CREDITS={result.usage_credits}",
        f"RUN_DIR={run_dir}",
    ])
    return 0 if (result.valid or result.external_block) else 1


def _cmd_bb_status(args: argparse.Namespace) -> int:
    runs = []
    if _CANARY_ROOT.exists():
        for d in sorted(_CANARY_ROOT.iterdir()):
            rp = d / "canary_result.json"
            if rp.exists():
                try:
                    runs.append(json.loads(rp.read_text(encoding="utf-8")))
                except (ValueError, OSError):
                    continue
    _emit(args.json, {"canary_runs": runs, "count": len(runs)},
          [f"CANARY_RUNS={len(runs)}"] + [f"  {r.get('run_id')}: {r.get('company')} -> {r.get('status')}" for r in runs])
    return 0


def _cmd_bb_review_queue(args: argparse.Namespace) -> int:
    from atlas.browser_backend.review_queue import read_queue
    rows = read_queue()
    _emit(args.json, {"review_queue": rows, "count": len(rows)},
          [f"REVIEW_QUEUE={len(rows)} item(s)"] +
          [f"  {r.get('company')}: {r.get('last_known_state')} ({r.get('internal_error','')[:60]})" for r in rows])
    return 0


def register_browser_backend_commands(subparsers) -> None:
    p = subparsers.add_parser(
        "browser-backend",
        help="Copilot-CLI + Playwright-MCP browser backend (install/doctor/canary/status/review-queue).",
    )
    sub = p.add_subparsers(dest="browser_backend_command", required=True)

    p_install = sub.add_parser("install", help="Install the pinned Playwright MCP server (idempotent).")
    p_install.add_argument("--force", action="store_true", help="Reinstall even if the pinned version is present.")
    p_install.add_argument("--json", action="store_true")
    p_install.set_defaults(func=_cmd_bb_install)

    p_doctor = sub.add_parser("doctor", help="Backend readiness diagnostics (offline).")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.set_defaults(func=_cmd_bb_doctor)

    p_canary = sub.add_parser("canary", help="Run one integrated company canary (DRY-RUN unless --live).")
    p_canary.add_argument("--company", required=True, help="Company name, e.g. Accenture or Infosys.")
    p_canary.add_argument("--live", action="store_true", help="Actually spawn the Copilot Playwright backend.")
    p_canary.add_argument("--headed", action="store_true", help="Run the browser headed (debug/canary).")
    p_canary.add_argument("--run-id", default=None, help="Override the immutable run id.")
    p_canary.add_argument("--model", default=None, help="Company-agent model (default claude-sonnet-5).")
    p_canary.add_argument("--max-credits", type=int, default=None, help="Max AI credits per attempt (default 250).")
    p_canary.add_argument("--json", action="store_true")
    p_canary.set_defaults(func=_cmd_bb_canary)

    p_status = sub.add_parser("status", help="Show recorded canary runs (offline).")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=_cmd_bb_status)

    p_rq = sub.add_parser("review-queue", help="Show the internal browser review queue (offline).")
    p_rq.add_argument("--json", action="store_true")
    p_rq.set_defaults(func=_cmd_bb_review_queue)


__all__ = ["register_browser_backend_commands"]
