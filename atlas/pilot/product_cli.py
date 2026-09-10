"""``atlas product-companies`` and ``atlas product-pilot`` CLI (prompt s.3/s.4).

- ``product-companies pool``   : dump the product-company metadata pool.
- ``product-companies select`` : deterministic stratified daily selection (count/seed).
- ``product-pilot plan``       : show the sealed fixed cohort + selection audit (no network).
- ``product-pilot run``        : run the fixed five-company product pilot (``--live`` for browser).
- ``product-pilot resume``     : resume a run, skipping completed companies.
- ``product-pilot validate``   : reopen + hash-verify a run's workbook/artifacts.
- ``product-pilot status``     : list published product-pilot runs.

The selection manifest is SEALED to disk before the first browser/network action. The live
five-company list is fixed by config and never substituted mid-run.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Optional

from atlas.candidate.search_profile import (
    CandidateProfileWaitingForHuman,
    load_candidate_search_profile,
)
from atlas.company.product_pool import (
    load_product_pool,
    plan_daily_buckets,
    select_fixed_cohort,
    select_product_companies,
)
from atlas.pilot.cli import resolve_runtime_models
from atlas.pilot.governor import PilotRuntime, run_pilot
from atlas.pilot.product_config import load_product_pilot_config
from atlas.pilot.product_report import (
    REQUIRED_SHEETS,
    build_product_report_fn,
    seal_selection_manifest,
)
from atlas.pilot.worker import LlmCompanySearchWorker

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = Path("output") / "production"
IMPORT_DIR = Path(r"C:\Atlas-Agent-Import")
DEFAULT_POOL = IMPORT_DIR / "Atlas_Product_Company_Pool_Seed_20260910.yaml"
DEFAULT_CONFIG = IMPORT_DIR / "Atlas_Product_Company_5_Pilot_Config_20260910.yaml"

__all__ = ["register_product_commands"]


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


def _pool_path(args) -> Path:
    return Path(args.pool) if getattr(args, "pool", None) else DEFAULT_POOL


def _policy_hash() -> str:
    try:
        from atlas.policy.loader import load_policy
        pol = load_policy()
        return getattr(pol, "policy_sha256", "") or getattr(pol, "version", "") or "policy"
    except Exception:  # noqa: BLE001
        return "policy"


def _load_profile(allow_synthetic: bool):
    """Try the live candidate profile; on WAITING fall back to synthetic (conservative:
    the 4+ experience exception then cannot apply, so 4+ roles are safely rejected)."""
    note = ""
    try:
        profile = load_candidate_search_profile(live=not allow_synthetic, allow_synthetic=allow_synthetic)
    except CandidateProfileWaitingForHuman:
        profile = load_candidate_search_profile(live=False, allow_synthetic=True)
        note = "candidate profile waiting-for-human; used synthetic (4+ exception disabled)"
    return profile, note


class _TimingWorker(LlmCompanySearchWorker):
    """Records per-company wall-clock so Company_Coverage can report elapsed seconds."""

    def __init__(self, *a, elapsed: dict, **kw):
        super().__init__(*a, **kw)
        object.__setattr__(self, "_elapsed", elapsed)
        object.__setattr__(self, "_elapsed_lock", threading.Lock())

    def search_company(self, task, usage):
        t0 = time.time()
        try:
            return super().search_company(task, usage)
        finally:
            with self._elapsed_lock:
                self._elapsed[task.company] = self._elapsed.get(task.company, 0.0) + (time.time() - t0)


# --------------------------------------------------------------------------- pool / select

def _cmd_pool(args) -> int:
    pool = load_product_pool(_pool_path(args))
    _print({
        "selection_policy": pool.selection_policy,
        "source": pool.source_path,
        "source_sha256": pool.source_sha256[:16],
        "count": len(pool.companies),
        "companies": [
            {"name": c.name, "category": c.category, "product_company": c.product_company,
             "official_domain": c.official_domain, "career_entry_url": c.career_entry_url,
             "india_presence": c.india_presence, "lane_affinity": list(c.lane_affinity),
             "source_family": c.resolved_source_family, "disabled": c.disabled}
            for c in pool.companies
        ],
    }, args.json)
    return 0


def _cmd_select(args) -> int:
    pool = load_product_pool(_pool_path(args))
    today = datetime.date.fromisoformat(args.today) if getattr(args, "today", None) else datetime.date.today()
    if getattr(args, "buckets", False):
        selection = plan_daily_buckets(
            pool, seed=args.seed, today=today, target_lanes=tuple(args.lanes or ()),
            run_id=args.run_id or "", )
    else:
        selection = select_product_companies(
            pool, count=args.count, seed=args.seed, today=today,
            target_lanes=tuple(args.lanes or ()), policy_hash=_policy_hash(),
            candidate_profile_hash="", run_id=args.run_id or "", )
    _print(selection.to_dict(), args.json)
    return 0


# --------------------------------------------------------------------------- product-pilot

def _seal(product_config, *, run_id: str, output_root: Path, pool_path: Path):
    """Build the sealed FIXED cohort selection + persist selection_manifest.json."""
    pool = load_product_pool(pool_path)
    selection = select_fixed_cohort(
        pool, product_config.companies, seed=product_config.selection_seed,
        target_lanes=product_config.primary_lanes, run_id=run_id,
    )
    run_dir = output_root / "product_pilots" / run_id
    manifest = seal_selection_manifest(run_dir, selection, product_config, run_id=run_id)
    hints_domain = {c.company.name.lower(): c.company.official_domain for c in selection.selected}
    hints_entry = {c.company.name.lower(): c.company.career_entry_url for c in selection.selected}
    return pool, selection, manifest, hints_domain, hints_entry


def _build_runtime(product_config, selection, hints_domain, hints_entry, *, run_id: str,
                   output_root: Path, live: bool, allow_synthetic: bool, elapsed: dict):
    profile, profile_note = _load_profile(allow_synthetic)
    models = resolve_runtime_models(product_config.company_search_model, "claude-opus-4.8")
    offline = (not live) or (not models["sdk_available"])
    mode = "deterministic" if offline else "llm"
    worker = _TimingWorker(
        config=product_config.pilot_config, mode=mode, model=models["search_model"],
        base_directory=str(REPO_ROOT / "state" / "copilot_runtime"),
        session_timeout_s=float(product_config.wall_clock_timeout_seconds),
        profile_summary=profile.redacted_dict(), elapsed=elapsed,
    )
    runtime = PilotRuntime(
        config=product_config.pilot_config, worker=worker, profile=profile,
        output_root=output_root, run_id=run_id, parent_run_id=None,
        escalation_model=models["escalation_model"], repo_root=REPO_ROOT,
        subdir="product_pilots",
        report_fn=build_product_report_fn(product_config, selection, run_id=run_id,
                                          elapsed_by_company=elapsed),
        domain_hints=hints_domain, entry_hints=hints_entry,
    )
    return runtime, profile, profile_note, mode


def _summarize(runtime, outcome, product_config, selection, mode, profile_note) -> dict:
    report = outcome.report
    val = runtime.validation
    return {
        "run_id": outcome.run_id,
        "pilot": product_config.pilot_name,
        "selection_mode": selection.mode,
        "selection_seed": selection.seed,
        "cohort": list(selection.names()),
        "outcome": outcome.outcome,
        "mode": mode,
        "search_model": runtime.worker.model,
        "companies_terminal": outcome.companies_terminal,
        "validated_jobs": outcome.india_jobs,
        "foreign_leads": outcome.foreign_leads,
        "workbook": (report.workbook_path.resolve().as_posix() if report else None),
        "run_dir": (report.run_dir.resolve().as_posix() if report else None),
        "validation_passed": (val.passed if val else None),
        "validation_failures": (val.failures if val else None),
        "profile_note": profile_note,
        "candidate_synthetic": getattr(runtime.profile, "synthetic", None),
        "usage_totals": runtime.usage.snapshot().get("totals", {}),
    }


def _cmd_plan(args) -> int:
    product_config = load_product_pilot_config(Path(args.config) if args.config else DEFAULT_CONFIG)
    pool = load_product_pool(_pool_path(args))
    selection = select_fixed_cohort(
        pool, product_config.companies, seed=product_config.selection_seed,
        target_lanes=product_config.primary_lanes, run_id=getattr(args, "run_id", "") or "",
    )
    _print({
        "pilot": product_config.pilot_name,
        "selection_mode": selection.mode,
        "selection_seed": selection.seed,
        "companies": list(selection.names()),
        "primary_lanes": list(product_config.primary_lanes),
        "audit_only_lanes": list(product_config.audit_only_lanes),
        "concurrency": product_config.pilot_config.concurrency_company_agents,
        "max_internal_retries_per_company": product_config.max_internal_retries_per_company,
        "browser_headed": product_config.browser_headed,
        "config_sha256": product_config.source_sha256[:16],
        "selection_audit": selection.to_dict(),
        "update_latest": product_config.update_latest,
    }, args.json)
    return 0


def _cmd_run(args) -> int:
    product_config = load_product_pilot_config(Path(args.config) if args.config else DEFAULT_CONFIG)
    output_root = Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT
    run_id = args.run_id or f"PRODUCT5_{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    # SEAL the fixed cohort manifest BEFORE any browser/network action (prompt s.3)
    pool, selection, manifest, hints_domain, hints_entry = _seal(
        product_config, run_id=run_id, output_root=output_root, pool_path=_pool_path(args))
    elapsed: dict = {}
    runtime, profile, profile_note, mode = _build_runtime(
        product_config, selection, hints_domain, hints_entry, run_id=run_id,
        output_root=output_root, live=bool(args.live), allow_synthetic=bool(args.allow_synthetic),
        elapsed=elapsed)
    outcome = run_pilot(runtime)
    summary = _summarize(runtime, outcome, product_config, selection, mode, profile_note)
    summary["selection_manifest"] = manifest.resolve().as_posix()
    _print(summary, args.json)
    return 0 if outcome.outcome in ("PASS", "COMPLETE_NO_MATCHES") else 1


def _cmd_resume(args) -> int:
    product_config = load_product_pilot_config(Path(args.config) if args.config else DEFAULT_CONFIG)
    output_root = Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT
    run_dir = output_root / "product_pilots" / args.run_id
    if not run_dir.exists():
        _print({"error": f"run dir not found: {run_dir}"}, args.json)
        return 2
    # reuse the sealed manifest cohort (never re-seal, never substitute)
    pool, selection, manifest, hints_domain, hints_entry = _seal(
        product_config, run_id=args.run_id, output_root=output_root, pool_path=_pool_path(args))
    elapsed: dict = {}
    runtime, profile, profile_note, mode = _build_runtime(
        product_config, selection, hints_domain, hints_entry, run_id=args.run_id,
        output_root=output_root, live=bool(args.live), allow_synthetic=bool(args.allow_synthetic),
        elapsed=elapsed)
    outcome = run_pilot(runtime)
    _print(_summarize(runtime, outcome, product_config, selection, mode, profile_note), args.json)
    return 0 if outcome.outcome in ("PASS", "COMPLETE_NO_MATCHES") else 1


def _cmd_validate(args) -> int:
    from openpyxl import load_workbook

    output_root = Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT
    run_dir = output_root / "product_pilots" / args.run_id
    if not run_dir.exists():
        _print({"error": f"run dir not found: {run_dir}"}, args.json)
        return 2
    workbooks = sorted(run_dir.glob("Atlas_Product_Company_5_Pilot_*.xlsx"))
    checks: list[dict] = []

    def _check(name, ok, detail=""):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    _check("workbook_present", bool(workbooks), f"{len(workbooks)} workbook(s)")
    sheets_ok = False
    if workbooks:
        wb = load_workbook(workbooks[-1], read_only=True)
        missing = [s for s in REQUIRED_SHEETS if s not in wb.sheetnames]
        wb.close()
        sheets_ok = not missing
        _check("eight_required_sheets", sheets_ok, f"missing={missing}")
    manifest_p = run_dir / "selection_manifest.json"
    _check("selection_manifest_sealed", manifest_p.exists())
    hashes_p = run_dir / "artifact_hashes.json"
    hashes_ok = False
    if hashes_p.exists():
        recorded = json.loads(hashes_p.read_text(encoding="utf-8"))
        mism = []
        for rel, want in recorded.items():
            f = run_dir / rel
            if not f.exists() or hashlib.sha256(f.read_bytes()).hexdigest() != want:
                mism.append(rel)
        hashes_ok = not mism
        _check("artifact_hashes_match", hashes_ok, f"mismatched={mism[:5]}")
    else:
        _check("artifact_hashes_match", False, "artifact_hashes.json missing")
    passed = all(c["passed"] for c in checks)
    _print({"run_id": args.run_id, "run_dir": run_dir.resolve().as_posix(),
            "passed": passed, "checks": checks}, args.json)
    return 0 if passed else 1


def _cmd_status(args) -> int:
    output_root = Path(args.output_root) if args.output_root else DEFAULT_OUTPUT_ROOT
    base = output_root / "product_pilots"
    runs = []
    if base.exists():
        for run_dir in sorted(base.iterdir()):
            man = run_dir / "run_manifest.json"
            sel = run_dir / "selection_manifest.json"
            entry = {"run_id": run_dir.name, "sealed": sel.exists()}
            if man.exists():
                m = json.loads(man.read_text(encoding="utf-8"))
                entry.update({"outcome": m.get("outcome"), "validated_jobs": m.get("validated_jobs"),
                              "companies_terminal": m.get("companies_terminal"),
                              "workbook": m.get("workbook")})
            runs.append(entry)
    _print({"product_pilots_root": base.resolve().as_posix(), "runs": runs}, args.json)
    return 0


def register_product_commands(subparsers) -> None:
    # ---- product-companies ----
    pc = subparsers.add_parser("product-companies", help="Product-company pool + stratified selector.")
    pc_sub = pc.add_subparsers(dest="product_companies_command", required=True)

    p_pool = pc_sub.add_parser("pool", help="Dump the product-company metadata pool.")
    p_pool.add_argument("--pool", default=None)
    p_pool.add_argument("--json", action="store_true")
    p_pool.set_defaults(func=_cmd_pool)

    p_sel = pc_sub.add_parser("select", help="Deterministic stratified selection (count/seed).")
    p_sel.add_argument("--pool", default=None)
    p_sel.add_argument("--count", type=int, default=20)
    p_sel.add_argument("--seed", default="product-company")
    p_sel.add_argument("--today", default=None, help="ISO date override (deterministic tests).")
    p_sel.add_argument("--run-id", default=None)
    p_sel.add_argument("--lanes", nargs="*", default=None)
    p_sel.add_argument("--buckets", action="store_true", help="Plan the 20-company bucket split.")
    p_sel.add_argument("--json", action="store_true")
    p_sel.set_defaults(func=_cmd_select)

    # ---- product-pilot ----
    pp = subparsers.add_parser("product-pilot", help="Fixed five-company product-company pilot.")
    pp_sub = pp.add_subparsers(dest="product_pilot_command", required=True)

    def _common(sp):
        sp.add_argument("--config", default=None)
        sp.add_argument("--pool", default=None)
        sp.add_argument("--output-root", default=None)
        sp.add_argument("--json", action="store_true")

    p_plan = pp_sub.add_parser("plan", help="Show the sealed fixed cohort + selection audit.")
    _common(p_plan)
    p_plan.add_argument("--run-id", default=None)
    p_plan.set_defaults(func=_cmd_plan)

    p_run = pp_sub.add_parser("run", help="Run the fixed five-company product pilot.")
    _common(p_run)
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--live", action="store_true", help="Use the live Copilot/Playwright browser backend.")
    p_run.add_argument("--allow-synthetic", action="store_true")
    p_run.set_defaults(func=_cmd_run)

    p_res = pp_sub.add_parser("resume", help="Resume a run, skipping completed companies.")
    _common(p_res)
    p_res.add_argument("--run-id", required=True)
    p_res.add_argument("--live", action="store_true")
    p_res.add_argument("--allow-synthetic", action="store_true")
    p_res.set_defaults(func=_cmd_resume)

    p_val = pp_sub.add_parser("validate", help="Reopen + hash-verify a run's workbook/artifacts.")
    _common(p_val)
    p_val.add_argument("--run-id", required=True)
    p_val.set_defaults(func=_cmd_validate)

    p_stat = pp_sub.add_parser("status", help="List published product-pilot runs.")
    p_stat.add_argument("--output-root", default=None)
    p_stat.add_argument("--json", action="store_true")
    p_stat.set_defaults(func=_cmd_status)
