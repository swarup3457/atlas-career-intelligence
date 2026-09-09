"""``atlas llm-pilot`` CLI: run / resume / validate / plan the ten-company India pilot."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Optional

from atlas.candidate.search_profile import (
    CandidateProfileWaitingForHuman,
    load_candidate_search_profile,
)
from atlas.pilot.config import load_pilot_config
from atlas.pilot.governor import PilotRuntime, run_pilot
from atlas.pilot.validator import validate_pilot_run
from atlas.pilot.worker import LlmCompanySearchWorker

DEFAULT_OUTPUT_ROOT = Path("output") / "production"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


def _new_run_id() -> str:
    return f"LLMPILOT_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"


def resolve_runtime_models(preferred: str, escalation_pref: str) -> dict:
    """Query available Copilot models and resolve the runtime search + escalation models."""
    available: list[str] = []
    try:
        from atlas.controllers.copilot import _OfficialCopilotSdkTransport

        available = list(_OfficialCopilotSdkTransport(
            base_directory=str(REPO_ROOT / "state" / "copilot_runtime"), model_list_timeout_s=25.0
        ).available_models())
    except Exception as exc:  # noqa: BLE001
        available = []
    search = preferred if preferred in available else ("auto" if "auto" in available else preferred)
    # escalation: strongest available high-reasoning model (prefer an Opus-class model)
    opus = [m for m in available if "opus" in m.lower()]
    if escalation_pref in available:
        escalation = escalation_pref
    elif opus:
        escalation = sorted(opus)[-1]
    elif search in available:
        escalation = search
    else:
        escalation = search
    return {"available": available, "search_model": search, "escalation_model": escalation,
            "sdk_available": bool(available)}


def _pilot_plan(args: argparse.Namespace) -> int:
    config = load_pilot_config(Path(args.config) if args.config else None)
    _print({
        "pilot": config.raw.get("pilot_name"),
        "companies": list(config.companies),
        "primary_lanes": list(config.primary_lanes),
        "config_sha256": config.source_sha256[:16],
        "concurrency": config.concurrency_company_agents,
        "budgets": {
            "search_rounds": config.max_search_rounds_per_company,
            "escalations": config.max_escalations_per_company,
            "details": config.max_job_details_per_company,
            "pages": config.max_pages_or_load_more_per_query,
        },
        "update_latest": config.update_latest,
    }, args.json)
    return 0


def _build_runtime(args, *, run_id: str, parent_run_id: Optional[str]) -> PilotRuntime:
    config = load_pilot_config(Path(args.config) if args.config else None)
    profile = load_candidate_search_profile(
        live=not args.allow_synthetic, allow_synthetic=args.allow_synthetic,
    )
    models = resolve_runtime_models(config.company_search_preferred_model, "claude-opus-4.8")
    offline = getattr(args, "offline", False) or not models["sdk_available"]
    mode = "deterministic" if offline else "llm"
    worker = LlmCompanySearchWorker(
        config=config, mode=mode, model=models["search_model"],
        base_directory=str(REPO_ROOT / "state" / "copilot_runtime"),
        session_timeout_s=float(getattr(args, "session_timeout", 180.0)),
        profile_summary=profile.redacted_dict(),
    )
    prior_manifest = Path(args.prior_manifest) if getattr(args, "prior_manifest", None) else None
    return PilotRuntime(
        config=config, worker=worker, profile=profile,
        output_root=Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT,
        run_id=run_id, parent_run_id=parent_run_id, escalation_model=models["escalation_model"],
        prior_manifest_path=prior_manifest, repo_root=REPO_ROOT,
    )


def _summarize(runtime: PilotRuntime, outcome, models_note: str) -> dict:
    val = runtime.validation
    return {
        "run_id": outcome.run_id,
        "outcome": outcome.outcome,
        "mode": runtime.worker.mode,
        "search_model": runtime.worker.model,
        "escalation_model": runtime.escalation_model,
        "companies_terminal": outcome.companies_terminal,
        "india_jobs": outcome.india_jobs,
        "foreign_leads": outcome.foreign_leads,
        "workbook": str(outcome.report.workbook_path) if outcome.report else None,
        "run_dir": str(outcome.report.run_dir) if outcome.report else None,
        "validation_passed": val.passed if val else None,
        "validation_failures": (val.failures if val else None),
        "candidate_synthetic": getattr(runtime.profile, "synthetic", None),
        "usage_totals": runtime.usage.snapshot().get("totals", {}),
    }


def _pilot_run(args: argparse.Namespace) -> int:
    try:
        runtime = _build_runtime(args, run_id=args.run_id or _new_run_id(), parent_run_id=None)
    except CandidateProfileWaitingForHuman as exc:
        _print({"outcome": "WAITING_FOR_HUMAN", "reason": str(exc)}, args.json)
        return 3
    outcome = run_pilot(runtime)
    _print(_summarize(runtime, outcome, ""), args.json)
    return 0 if outcome.outcome in ("PASS", "COMPLETE_NO_MATCHES") else 1


def _pilot_resume(args: argparse.Namespace) -> int:
    run_dir = (Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT) / "llm_pilots" / args.run_id
    if not run_dir.exists():
        _print({"error": f"run dir not found: {run_dir}"}, args.json)
        return 2
    try:
        runtime = _build_runtime(args, run_id=args.run_id, parent_run_id=None)
    except CandidateProfileWaitingForHuman as exc:
        _print({"outcome": "WAITING_FOR_HUMAN", "reason": str(exc)}, args.json)
        return 3
    outcome = run_pilot(runtime)
    _print(_summarize(runtime, outcome, ""), args.json)
    return 0 if outcome.outcome in ("PASS", "COMPLETE_NO_MATCHES") else 1


def _pilot_validate(args: argparse.Namespace) -> int:
    config = load_pilot_config(Path(args.config) if args.config else None)
    run_dir = (Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT) / "llm_pilots" / args.run_id
    if not run_dir.exists():
        _print({"error": f"run dir not found: {run_dir}"}, args.json)
        return 2
    prior = Path(args.prior_manifest) if args.prior_manifest else None
    report = validate_pilot_run(run_dir, config, prior_manifest_path=prior, repo_root=REPO_ROOT)
    _print(report.to_dict(), args.json)
    return 0 if report.passed else 1


def _pilot_reevaluate(args: argparse.Namespace) -> int:
    from atlas.pilot.governor import reevaluate_from_partials

    output_root = Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT
    parent_dir = output_root / "llm_pilots" / args.parent_run_id
    if not parent_dir.exists():
        _print({"error": f"parent run dir not found: {parent_dir}"}, args.json)
        return 2
    config = load_pilot_config(Path(args.config) if args.config else None)
    try:
        profile = load_candidate_search_profile(
            live=not args.allow_synthetic, allow_synthetic=args.allow_synthetic)
    except CandidateProfileWaitingForHuman as exc:
        _print({"outcome": "WAITING_FOR_HUMAN", "reason": str(exc)}, args.json)
        return 3
    new_run_id = args.run_id or (_new_run_id() + "_REEVAL")
    prior = Path(args.prior_manifest) if args.prior_manifest else None
    outcome = reevaluate_from_partials(
        config, profile, parent_run_dir=parent_dir, output_root=output_root,
        new_run_id=new_run_id, prior_manifest_path=prior, repo_root=REPO_ROOT,
    )
    _print({
        "run_id": outcome.run_id, "parent_run_id": args.parent_run_id, "outcome": outcome.outcome,
        "companies_terminal": outcome.companies_terminal, "india_jobs": outcome.india_jobs,
        "foreign_leads": outcome.foreign_leads,
        "workbook": str(outcome.report.workbook_path) if outcome.report else None,
        "run_dir": str(outcome.report.run_dir) if outcome.report else None,
        "validation_passed": outcome.validation.passed if outcome.validation else None,
        "validation_failures": outcome.validation.failures if outcome.validation else None,
    }, args.json)
    return 0 if outcome.outcome in ("PASS", "COMPLETE_NO_MATCHES") else 1


def _cmd_pilot(args: argparse.Namespace) -> int:
    return {
        "plan": _pilot_plan,
        "run": _pilot_run,
        "resume": _pilot_resume,
        "validate": _pilot_validate,
        "reevaluate": _pilot_reevaluate,
    }[args.pilot_command](args)


def register_llm_pilot_commands(subparsers) -> None:
    p = subparsers.add_parser("llm-pilot", help="LLM-directed India official-career ten-company pilot.")
    sub = p.add_subparsers(dest="pilot_command", required=True)

    def _common(sp):
        sp.add_argument("--config", default=None, help="Pilot config YAML (defaults to V3 import).")
        sp.add_argument("--output-root", default=None)
        sp.add_argument("--json", action="store_true")

    p_plan = sub.add_parser("plan", help="Print the sealed cohort, lanes, and budgets.")
    _common(p_plan)
    p_plan.set_defaults(func=_cmd_pilot, pilot_command="plan")

    p_run = sub.add_parser("run", help="Run the fixed ten-company official-only pilot.")
    _common(p_run)
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--offline", action="store_true", help="Deterministic tool-driver (no live LLM).")
    p_run.add_argument("--allow-synthetic", action="store_true", help="Permit synthetic candidate (NOT for live).")
    p_run.add_argument("--session-timeout", type=float, default=180.0)
    p_run.add_argument("--prior-manifest", default=None, help="Prior-runs hash manifest for the history gate.")
    p_run.set_defaults(func=_cmd_pilot, pilot_command="run")

    p_res = sub.add_parser("resume", help="Resume a pilot run (skips completed companies).")
    _common(p_res)
    p_res.add_argument("--run-id", required=True)
    p_res.add_argument("--offline", action="store_true")
    p_res.add_argument("--allow-synthetic", action="store_true")
    p_res.add_argument("--session-timeout", type=float, default=180.0)
    p_res.add_argument("--prior-manifest", default=None)
    p_res.set_defaults(func=_cmd_pilot, pilot_command="resume")

    p_val = sub.add_parser("validate", help="Validate a pilot run's usefulness contract.")
    _common(p_val)
    p_val.add_argument("--run-id", required=True)
    p_val.add_argument("--prior-manifest", default=None)
    p_val.set_defaults(func=_cmd_pilot, pilot_command="validate")

    p_re = sub.add_parser("reevaluate", help="Re-run the deterministic evaluation over a completed "
                                             "run's searched results (child run, zero LLM/network cost).")
    _common(p_re)
    p_re.add_argument("--parent-run-id", required=True)
    p_re.add_argument("--run-id", default=None)
    p_re.add_argument("--allow-synthetic", action="store_true")
    p_re.add_argument("--prior-manifest", default=None)
    p_re.set_defaults(func=_cmd_pilot, pilot_command="reevaluate")


__all__ = ["register_llm_pilot_commands", "resolve_runtime_models"]
