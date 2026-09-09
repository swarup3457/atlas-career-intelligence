"""``atlas hunt`` CLI (build spec 13, 24, 25).

Offline-capable subcommands: plan, validate, requalify, rebuild-report, status,
latest, and run (offline fixture default; ``--live`` uses the bounded official
provider). Live campaigns are official-source-only in the recovery build.
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Optional

from atlas.hunt.artifacts import (
    details_by_company,
    load_campaign,
    load_details,
    load_snapshots,
    snapshots_by_company,
)
from atlas.hunt.campaign import seal_campaign
from atlas.hunt.matching import HuntCandidate, diversify_shortlist
from atlas.hunt.models import RunLineage
from atlas.hunt.pipeline import requalify_details
from atlas.hunt.report import write_hunt_report
from atlas.hunt.role_intent import load_role_intent_policy
from atlas.hunt.validator import validate_run_dir
from atlas.policy.loader import load_policy

DEFAULT_OUTPUT_ROOT = Path("output") / "production"


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


def _new_run_id(prefix: str) -> str:
    return f"{prefix}_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _root(args: argparse.Namespace) -> Path:
    r = getattr(args, "output_root", None)
    return Path(r) if r else DEFAULT_OUTPUT_ROOT


def _default_candidate(lanes) -> HuntCandidate:
    return HuntCandidate.from_skills(
        ["Java", "Spring Boot", "REST APIs", "Hibernate", "MySQL", "React", "JavaScript", "TypeScript", "SQL"],
        total_experience_years=2.0, target_lanes=tuple(lanes), location_group="PRIMARY",
    )


def _hunt_plan(args: argparse.Namespace) -> int:
    policy = load_policy()
    intent = load_role_intent_policy()
    lanes = intent.lane_keys()
    campaign = seal_campaign(
        policy, campaign_id=_new_run_id("HUNTPLAN"), lanes=lanes,
        role_policy_hash=intent.fingerprint, cohort_size=args.cohort, max_batch=args.max_batch,
    )
    out = {
        "campaign_id": campaign.campaign_id,
        "policy_version": intent.policy_version,
        "role_policy_hash": intent.fingerprint[:16],
        "company_plan_hash": campaign.company_plan_hash[:16],
        "batch0_hash": campaign.batches[0].batch_hash[:16],
        "companies_planned": campaign.company_count,
        "lanes": list(lanes),
        "company_lane_obligations": campaign.obligation_count,
        "expected_board_fetches": campaign.company_count,
        "cohort": [f"{c.name} [{c.group}]" for c in campaign.companies],
        "unique_output_path": str(DEFAULT_OUTPUT_ROOT / "runs" / "<RUN_ID>" / "Atlas_Jobs_<YYYYMMDD-HHMMSS>_<RUN_ID>.xlsx"),
    }
    _print(out, args.json)
    return 0


def _hunt_validate(args: argparse.Namespace) -> int:
    run_dir = _root(args) / "runs" / args.run_id
    if not run_dir.exists():
        _print({"error": f"run directory not found: {run_dir}"}, args.json)
        return 2
    report = validate_run_dir(run_dir)
    _print(report.to_dict(), args.json)
    return 0 if report.passed else 1


def _hunt_requalify(args: argparse.Namespace) -> int:
    root = _root(args)
    src = root / "runs" / args.collection_run_id
    if not src.exists():
        _print({"error": f"collection run not found: {src}"}, args.json)
        return 2
    policy = load_policy()
    intent = load_role_intent_policy()
    snaps = load_snapshots(src)
    details = load_details(src)
    campaign = load_campaign(src)
    campaign.campaign_id = args.new_run_id
    candidate = _default_candidate(campaign.lanes)
    result = requalify_details(
        campaign, intent, policy, candidate,
        snapshots_by_company(snaps), details_by_company(snaps, details),
    )
    shortlist = diversify_shortlist(result.matches, intent)
    outcome = "ENGINEERING_PASS_WITH_MATCHES" if shortlist else "ENGINEERING_PASS_COMPLETE_NO_MATCHES"
    lineage = RunLineage(
        run_id=args.new_run_id, run_kind="QUALIFICATION", parent_run_id=args.collection_run_id,
        collection_run_id=args.collection_run_id, reused_snapshot_ids=tuple(s.snapshot_id for s in snaps),
        role_policy_hash=intent.fingerprint, company_plan_hash=campaign.company_plan_hash,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        retry_reason="policy-only requalification (zero network)",
    )
    report = write_hunt_report(result, campaign, shortlist, lineage, intent, policy,
                               run_id=args.new_run_id, output_root=root, outcome=outcome)
    _print({"new_run_id": args.new_run_id, "collection_run_id": args.collection_run_id,
            "network_calls": result.network_calls, "relevant_jobs": len(shortlist),
            "workbook": str(report.workbook_path), "outcome": outcome}, args.json)
    return 0


def _hunt_rebuild(args: argparse.Namespace) -> int:
    args.collection_run_id = args.qualification_run_id
    return _hunt_requalify(args)


def _hunt_status(args: argparse.Namespace) -> int:
    run_dir = _root(args) / "runs" / args.run_id
    manifest = run_dir / "run_manifest.json"
    lineage = run_dir / "run_lineage.json"
    out = {"run_id": args.run_id, "run_dir": str(run_dir), "exists": run_dir.exists()}
    if manifest.exists():
        out["manifest"] = json.loads(manifest.read_text(encoding="utf-8"))
    if lineage.exists():
        out["lineage"] = json.loads(lineage.read_text(encoding="utf-8"))
    _print(out, args.json)
    return 0 if run_dir.exists() else 2


def _hunt_latest(args: argparse.Namespace) -> int:
    index = _root(args) / "run_index.jsonl"
    if not index.exists():
        _print({"error": "no run_index.jsonl"}, args.json)
        return 2
    records = [json.loads(l) for l in index.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.accepted:
        records = [r for r in records if r.get("outcome", "").startswith("ENGINEERING_PASS")]
    if not records:
        _print({"error": "no matching runs"}, args.json)
        return 2
    _print(records[-1], args.json)
    return 0


def _hunt_run(args: argparse.Namespace) -> int:
    from atlas.hunt.graph import HuntRuntime, run_hunt

    policy = load_policy()
    intent = load_role_intent_policy()
    lanes = intent.lane_keys()
    run_id = args.run_id or _new_run_id("HUNT")
    output_root = Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT

    if args.live:
        from atlas.hunt.live import build_live_provider

        provider, note = build_live_provider(policy, intent, max_companies=args.cohort,
                                             max_pages=args.max_pages, timeout=args.timeout)
    else:
        from atlas.hunt.live import build_demo_provider

        provider, note = build_demo_provider(), "offline demo fixture"

    campaign = seal_campaign(policy, campaign_id=run_id, lanes=lanes,
                             role_policy_hash=intent.fingerprint, cohort_size=args.cohort, max_batch=args.max_batch)
    candidate = _default_candidate(lanes)
    lineage = RunLineage(run_id=run_id, run_kind="COLLECTION", role_policy_hash=intent.fingerprint,
                         company_plan_hash=campaign.company_plan_hash,
                         created_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    runtime = HuntRuntime(intent=intent, policy=policy, provider=provider, candidate=candidate,
                          campaign=campaign, output_root=output_root, run_id=run_id, lineage=lineage,
                          candidate_years=2.0, allow_extension=not args.no_extension,
                          max_hydrate_per_company=getattr(args, "max_hydrate", None))
    outcome = run_hunt(runtime)
    _print({"run_id": run_id, "mode": "live" if args.live else "offline", "provider": note,
            "outcome": outcome.outcome, "relevant_jobs": outcome.relevant_jobs,
            "qualified_jobs": outcome.qualified_jobs, "companies": outcome.companies,
            "obligations": outcome.obligations,
            "workbook": str(outcome.report.workbook_path) if outcome.report else None,
            "validation_passed": outcome.validation.passed if outcome.validation else None}, args.json)
    return 0 if outcome.outcome.startswith("ENGINEERING_PASS") else 1


def _hunt_resume(args: argparse.Namespace) -> int:
    _print({"run_id": args.run_id, "note": "resume reuses the collection run; use requalify for policy-only reruns"}, args.json)
    return 0


def _cmd_hunt(args: argparse.Namespace) -> int:
    return {
        "plan": _hunt_plan,
        "run": _hunt_run,
        "resume": _hunt_resume,
        "status": _hunt_status,
        "validate": _hunt_validate,
        "requalify": _hunt_requalify,
        "rebuild-report": _hunt_rebuild,
        "latest": _hunt_latest,
    }[args.hunt_command](args)


def register_hunt_commands(subparsers) -> None:
    p_hunt = subparsers.add_parser("hunt", help="Search Hunt Recovery V2: company-first, official-first, six-lane search.")
    hunt_sub = p_hunt.add_subparsers(dest="hunt_command", required=True)

    p_plan = hunt_sub.add_parser("plan", help="Offline: seal a stratified cohort and print the company/lane matrix.")
    p_plan.add_argument("--cohort", type=int, default=30)
    p_plan.add_argument("--max-batch", type=int, default=60)
    p_plan.add_argument("--json", action="store_true")
    p_plan.set_defaults(func=_cmd_hunt, hunt_command="plan")

    p_run = hunt_sub.add_parser("run", help="Run a hunt campaign (offline demo by default; --live for official sources).")
    p_run.add_argument("--live", action="store_true")
    p_run.add_argument("--cohort", type=int, default=30)
    p_run.add_argument("--max-batch", type=int, default=60)
    p_run.add_argument("--max-pages", type=int, default=2)
    p_run.add_argument("--max-hydrate", type=int, default=None)
    p_run.add_argument("--timeout", type=float, default=15.0)
    p_run.add_argument("--no-extension", action="store_true")
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--output-root", default=None)
    p_run.add_argument("--json", action="store_true")
    p_run.set_defaults(func=_cmd_hunt, hunt_command="run")

    p_resume = hunt_sub.add_parser("resume", help="Resume a PARTIAL hunt run.")
    p_resume.add_argument("--run-id", required=True)
    p_resume.add_argument("--live", action="store_true")
    p_resume.add_argument("--json", action="store_true")
    p_resume.set_defaults(func=_cmd_hunt, hunt_command="resume")

    p_status = hunt_sub.add_parser("status", help="Show a run's manifest/lineage.")
    p_status.add_argument("--run-id", required=True)
    p_status.add_argument("--output-root", default=None)
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=_cmd_hunt, hunt_command="status")

    p_validate = hunt_sub.add_parser("validate", help="Validate a run's search-quality contract.")
    p_validate.add_argument("--run-id", required=True)
    p_validate.add_argument("--output-root", default=None)
    p_validate.add_argument("--json", action="store_true")
    p_validate.set_defaults(func=_cmd_hunt, hunt_command="validate")

    p_requal = hunt_sub.add_parser("requalify", help="Policy-only requalify of a collection run (zero network).")
    p_requal.add_argument("--collection-run-id", required=True)
    p_requal.add_argument("--new-run-id", required=True)
    p_requal.add_argument("--output-root", default=None)
    p_requal.add_argument("--json", action="store_true")
    p_requal.set_defaults(func=_cmd_hunt, hunt_command="requalify")

    p_rebuild = hunt_sub.add_parser("rebuild-report", help="Rebuild a report from stored decisions (zero network).")
    p_rebuild.add_argument("--qualification-run-id", required=True)
    p_rebuild.add_argument("--new-run-id", required=True)
    p_rebuild.add_argument("--output-root", default=None)
    p_rebuild.add_argument("--json", action="store_true")
    p_rebuild.set_defaults(func=_cmd_hunt, hunt_command="rebuild-report")

    p_latest = hunt_sub.add_parser("latest", help="Show the newest (accepted) run from run_index.jsonl.")
    p_latest.add_argument("--accepted", action="store_true")
    p_latest.add_argument("--output-root", default=None)
    p_latest.add_argument("--json", action="store_true")
    p_latest.set_defaults(func=_cmd_hunt, hunt_command="latest")


__all__ = ["register_hunt_commands"]
