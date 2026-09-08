"""Atlas CLI.

Generic platform commands PLUS the offline production-fixture runtime. Live
production (`atlas run` against real sources) remains intentionally DISABLED —
no real adapter exists yet — but `atlas production-fixture` executes the SAME
sealed-plan production integration path with synthetic, zero-network fixtures.

Commands:
    atlas doctor              - offline health check (PASS/WARN/FAIL)
    atlas status              - report current run/lock/checkpoint state
    atlas resume              - resume a PARTIAL demo runtime run
    atlas production-fixture  - offline (synthetic, zero-network) production
                                runtime: run / resume / status
    atlas test                - run the offline pytest suite
    atlas version             - print Atlas + key dependency versions
    atlas backup              - create a verified, consistent local backup
    atlas backup verify       - verify an existing backup directory
    atlas restore             - restore a verified backup into a target directory
    atlas support-bundle      - write a sanitized diagnostic support bundle (zip)
    atlas add-source          - scaffold a new source adapter (dry-run default)

See docs/PRODUCTION_RUNTIME.md and docs/OPERATIONS.md.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import atlas
from atlas.config import load_settings
from atlas.health import FAIL, WARN, run_doctor


def _cmd_version(args: argparse.Namespace) -> int:
    print(f"atlas {atlas.__version__}")
    verbose = getattr(args, "verbose", False)
    if not verbose:
        try:
            import playwright

            print(f"playwright {getattr(playwright, '__version__', 'unknown')}")
        except ImportError:
            pass
        try:
            import langgraph

            print(f"langgraph {getattr(langgraph, '__version__', 'unknown')}")
        except ImportError:
            pass
        return 0

    from atlas.runtime.manifest import software_versions

    versions = software_versions()
    for key in ("atlas", "python", "langgraph", "playwright", "schema_version"):
        if key in versions:
            print(f"{key}: {versions[key]}")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    report = run_doctor()
    print(report.render())
    return 0 if report.overall != FAIL else 1


def _cmd_status(args: argparse.Namespace) -> int:
    import json as _json

    from atlas.orchestration.run_lock import RunLock
    from atlas.persistence.sqlite import StateStore

    settings = load_settings()
    settings.ensure_directories()

    run_id = getattr(args, "run_id", None) or "atlas-demo-run"
    snapshot = None
    try:
        from atlas.runtime.demo_workload import DemoWorker, build_demo_failure_injector
        from atlas.runtime.engine import AtlasRuntime

        runtime = AtlasRuntime(settings, run_id, tasks=[], worker=DemoWorker(build_demo_failure_injector()))
        snapshot = runtime.snapshot()
    except Exception:  # noqa: BLE001
        snapshot = None

    if getattr(args, "json", False):
        payload = snapshot.to_dict() if snapshot is not None else {"run_id": run_id, "status": "UNKNOWN"}
        print(_json.dumps(payload))
        return 0

    run_lock = RunLock(settings.state_db.parent)
    holder = run_lock.current_holder()
    if holder is None:
        print("Run lock: no active run.")
    else:
        print(
            f"Run lock: ACTIVE (or stale) - PID={holder.pid}, run_id={holder.run_id}, "
            f"started_at={holder.started_at}. Run `atlas doctor` for a liveness check."
        )

    try:
        with StateStore(settings.state_db) as store:
            print(f"State DB: {settings.state_db} (schema_version={store.schema_version()})")
    except Exception as exc:  # noqa: BLE001
        print(f"State DB: ERROR opening {settings.state_db}: {exc}")

    print(f"Checkpoint DB: {settings.checkpoint_db}")
    print(f"Browser profile: {settings.browser_profile}")
    print(f"Controller: {settings.controller}")

    if snapshot is not None:
        print(f"\nProgress snapshot (run_id={run_id}):")
        print(_json.dumps(snapshot.to_dict(), indent=2))
    else:
        print(f"\nNo runtime progress found for run_id={run_id!r} (nothing has run yet, or a different --run-id was used).")
    return 0


def _build_demo_runtime(args: argparse.Namespace):
    from atlas.runtime.demo_workload import DemoWorker, build_demo_failure_injector, build_demo_tasks
    from atlas.runtime.engine import AtlasRuntime

    settings = load_settings()
    settings.ensure_directories()

    run_id = getattr(args, "run_id", None) or "atlas-demo-run"
    tasks = build_demo_tasks()
    injector = build_demo_failure_injector()
    worker = DemoWorker(injector)

    runtime = AtlasRuntime(
        settings,
        run_id,
        tasks,
        worker,
        max_runtime_minutes=getattr(args, "max_runtime_minutes", None),
        stop_after_completed=getattr(args, "stop_after", None),
        batch_size=getattr(args, "batch_size", None),
        simulate_intervention=True,
        auto_resolve_interventions=not getattr(args, "no_auto_resolve", False),
    )
    return runtime


def _cmd_run(args: argparse.Namespace) -> int:
    if not args.demo:
        print("ERROR: `atlas run` only supports --demo in this build (Phase 0.75). "
              "No real job-search source exists yet.")
        return 2

    from atlas.runtime.engine import RunAlreadyActiveError

    runtime = _build_demo_runtime(args)
    try:
        result = runtime.run()
    except RunAlreadyActiveError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"RUN_ID={result.run_id}")
    print(f"STATUS={result.status}")
    print(f"PROGRESS={result.progress.to_dict()}")
    if result.report_paths:
        for kind, path in result.report_paths.items():
            print(f"REPORT[{kind}]={path}")
    return 0 if result.status in ("COMPLETE", "PARTIAL", "WAITING_FOR_HUMAN") else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    from atlas.runtime.engine import RunAlreadyActiveError

    runtime = _build_demo_runtime(args)
    try:
        result = runtime.resume()
    except RunAlreadyActiveError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"RUN_ID={result.run_id}")
    print(f"STATUS={result.status}")
    print(f"PROGRESS={result.progress.to_dict()}")
    if result.report_paths:
        for kind, path in result.report_paths.items():
            print(f"REPORT[{kind}]={path}")
    return 0 if result.status in ("COMPLETE", "PARTIAL", "WAITING_FOR_HUMAN") else 1


def _cmd_test(args: argparse.Namespace) -> int:
    project_root = Path(atlas.__file__).resolve().parent.parent
    cmd = [sys.executable, "-m", "pytest"]
    if args.real_web:
        cmd += ["-m", "real_web"]
    completed = subprocess.run(cmd, cwd=str(project_root))
    return completed.returncode


_FIXTURE_BANNER = (
    "================ ATLAS OFFLINE PRODUCTION-FIXTURE RUN ================\n"
    " SYNTHETIC DATA ONLY. Zero network. No real adapter, no live search,\n"
    " no browser navigation, no application submission. This exercises the\n"
    " SAME production integration path (SourceRegistry -> adapter -> rate-\n"
    " limited executor -> worker -> attempts -> raw staging -> coverage ->\n"
    " canonicalize -> report) that real adapters will use in Phase 1C.\n"
    "====================================================================="
)


def _cmd_production_fixture(args: argparse.Namespace) -> int:
    from atlas.orchestration.production_state import ProductionPhase
    from atlas.persistence.sqlite import StateStore
    from atlas.runtime.production import ProductionSearchRuntime, default_fixture_topology

    print(_FIXTURE_BANNER)
    settings = load_settings()
    settings.ensure_directories()
    run_id = args.run_id or "production-fixture-demo"
    sub = args.fixture_command

    if sub == "status":
        with StateStore(settings.state_db) as store:
            run = store.get_run(run_id)
            plan = store.get_coverage_plan(run_id)
            summary = store.coverage_summary(run_id)
            raw = store.count_raw_observations(run_id)
        print(f"RUN_ID={run_id}")
        print(f"RUN_STATUS={run['status'] if run else 'NOT_FOUND'}")
        if plan is not None:
            print(f"PLAN_STATE={plan['state']} SEALED_FP={(plan['fingerprint'] or '')[:12]}")
        print(f"COVERAGE_PLANNED={summary.get('_planned', 0)} COVERAGE_TERMINAL={summary.get('_completed', 0)}")
        print(f"RAW_OBSERVATIONS={raw}")
        return 0

    n = int(args.companies)
    instances, companies = default_fixture_topology(n)
    stop_after = ProductionPhase.DISCOVER if getattr(args, "stop_after_discover", False) else None
    rt = ProductionSearchRuntime(
        settings, run_id, instances=instances, companies=companies,
        stop_after_phase=stop_after,
    )
    result = rt.resume() if sub == "resume" else rt.run()

    print(f"RUN_ID={result.run_id}")
    print(f"STATUS={result.terminal_state}")
    print(f"PLANNED_CHILD_TASKS={result.planned_tasks}")
    print(f"TERMINAL_CHILD_TASKS={result.terminal_tasks}")
    print(f"LANES={sorted(result.lane_summary)}")
    print(f"POLICY_FP={result.policy_fingerprint[:12]} PLAN_FP={result.plan_fingerprint[:12]}")
    print(f"CANDIDATE_SNAPSHOT={result.candidate_snapshot} SYNTHETIC={result.manifest.get('synthetic_candidate_evidence')}")
    print(f"CHECKPOINT_BYTES={result.checkpoint_bytes}")
    if result.report_path:
        print(f"REPORT={result.report_path} VALID={result.report_valid}")
    print(f"STATE_DB={settings.state_db}")
    print(f"CHECKPOINT_DB={settings.checkpoint_db}")
    print("NOTE: `atlas run` live production remains DISABLED (no real adapters).")
    return 0 if result.terminal_state in ("COMPLETE", "PARTIAL", "WAITING_FOR_HUMAN") else 1


def _cmd_backup(args: argparse.Namespace) -> int:
    from atlas.backup.backup import BackupError, backup_dir_for, create_backup
    from atlas.backup.retention import apply_retention

    settings = load_settings()
    settings.ensure_directories()

    backups_dir = Path(args.output) if getattr(args, "output", None) else settings.output_dir / "backups"
    try:
        manifest = create_backup(settings, backups_dir)
    except BackupError as exc:
        print(f"ERROR: backup failed: {exc}")
        return 1

    backup_path = backup_dir_for(backups_dir, manifest)
    print(f"BACKUP_ID={manifest.backup_id}")
    print(f"CREATED_AT={manifest.created_at}")
    print(f"PATH={backup_path}")
    print(f"STATE_SCHEMA_VERSION={manifest.state_schema_version}")
    print(f"ATLAS_VERSION={manifest.atlas_version}")
    print(f"TOTAL_SIZE_BYTES={manifest.total_size()}")
    print(f"INCLUDED ({len(manifest.included_components)}):")
    for comp in manifest.included_components:
        print(f"  [{comp.kind}] {comp.relative_path}  sha256={comp.sha256[:16]}...  {comp.size}B")
    print(f"EXCLUDED ({len(manifest.excluded_components)}):")
    for exc in manifest.excluded_components:
        print(f"  {exc.name}: {exc.reason}")

    if getattr(args, "keep_latest", None) is not None:
        deleted = apply_retention(backups_dir, args.keep_latest)
        print(f"RETENTION: kept latest {args.keep_latest}, deleted {len(deleted)} old backup(s).")
        for bid in deleted:
            print(f"  deleted: {bid}")
    return 0


def _cmd_backup_verify(args: argparse.Namespace) -> int:
    from atlas.backup.verify import verify_backup

    result = verify_backup(Path(args.backup))
    print(result.render())
    return 0 if result.ok else 1


def _cmd_restore(args: argparse.Namespace) -> int:
    from atlas.backup.restore import RestoreSafetyError, restore_backup

    try:
        result = restore_backup(Path(args.backup), Path(args.target))
    except RestoreSafetyError as exc:
        print(f"ERROR: refusing to restore: {exc}")
        return 2
    print(result.render())
    return 0 if result.ok else 1


def _cmd_support_bundle(args: argparse.Namespace) -> int:
    from atlas.backup.support_bundle import create_support_bundle

    settings = load_settings()
    settings.ensure_directories()
    output_dir = Path(args.output) if getattr(args, "output", None) else settings.output_dir / "support_bundles"
    path = create_support_bundle(settings, output_dir)
    print(f"SUPPORT_BUNDLE={path}")
    return 0


def _cmd_sources(args: argparse.Namespace) -> int:
    """List the registered source adapters and the demo source config
    (Phase 1A ships zero real adapters — the framework only)."""
    import json as _json

    from atlas.sources.config import SourceConfigError, demo_source_config
    from atlas.sources.registry import default_registry

    registry = default_registry()
    descriptors = registry.describe()
    print(f"Registered source adapters: {len(descriptors)}")
    for desc in descriptors:
        print(f"  [{desc['source_type']}] {desc['adapter_class']} "
              f"v{desc['adapter_version']}/parser {desc['parser_version']} caps={desc['capabilities']}")
    if not descriptors:
        print("  (none — Phase 1A foundation ships the framework, not real adapters)")

    try:
        cfg = demo_source_config()
        print(f"\nDemo source config: {len(cfg.instances)} instance(s)")
        for inst in cfg.instances:
            print(f"  {inst.instance_id} ({inst.source_type.value}) enabled={inst.enabled}")
    except SourceConfigError as exc:
        print(f"Demo source config INVALID: {exc}")
        return 1

    if getattr(args, "json", False):
        print("\n" + _json.dumps({"registered": descriptors}))
    return 0


def _cmd_companies(args: argparse.Namespace) -> int:
    """List registered companies (read-only diagnostic)."""
    from atlas.company.registry import CompanyRegistry
    from atlas.persistence.sqlite import StateStore

    settings = load_settings()
    settings.ensure_directories()
    with StateStore(settings.state_db) as store:
        registry = CompanyRegistry(store)
        companies = registry.list_companies(limit=getattr(args, "limit", 200))
        print(f"Companies: {store.count_companies()} (source relationships: {store.count_source_relationships()})")
        for c in companies:
            rels = store.list_relationships_for_company(c.company_id)
            current = sum(1 for r in rels if r["is_current"])
            print(f"  {c.company_id}  {c.canonical_name!r}  domain={c.official_domain or '-'}  "
                  f"sources={len(rels)} (current={current})  aliases={len(c.aliases)}")
        if not companies:
            print("  (none registered yet)")
    return 0


def _cmd_company_show(args: argparse.Namespace) -> int:
    """Show one company's identity, aliases, sources, and discovery history."""
    from atlas.company.registry import CompanyRegistry
    from atlas.persistence.sqlite import StateStore

    settings = load_settings()
    settings.ensure_directories()
    with StateStore(settings.state_db) as store:
        registry = CompanyRegistry(store)
        company = registry.get_company(args.company_id)
        if company is None:
            print(f"No company with id {args.company_id!r}.")
            return 1
        print(f"company_id:     {company.company_id}")
        print(f"canonical_name: {company.canonical_name}")
        print(f"display_name:   {company.display_name}")
        print(f"identity_key:   {company.identity_key}")
        print(f"official_domain:{company.official_domain}")
        print(f"country:        {company.country}")
        print(f"status:         {company.status.value}")
        print(f"aliases:        {list(company.aliases)}")
        print("sources:")
        for rel in registry.list_relationships(company.company_id):
            print(f"  [{rel.state.value}] {rel.source_type} instance={rel.instance_id} "
                  f"tenant={rel.tenant or '-'} confidence={rel.confidence.value} current={rel.is_current}")
        print("discovery observations:")
        for row in store.list_source_discovery_observations(company.company_id):
            print(f"  {row['method']} ats={row['detected_ats'] or '-'} conf={row['confidence']} "
                  f"state={row['verification_state']}")
    return 0


def _cmd_add_source(args: argparse.Namespace) -> int:
    """Developer scaffold for a new source adapter. Dry-run by default —
    prints a plan and writes nothing. Never generates a real adapter."""
    from atlas.sources.scaffold import render_plan, write_scaffold

    try:
        if getattr(args, "out", None) and not args.dry_run:
            written = write_scaffold(args.name, args.type, Path(args.out), base_url=args.base_url)
            print(f"Wrote {len(written)} template file(s) under {args.out}:")
            for path in written:
                print(f"  {path}")
        else:
            print(render_plan(args.name, args.type, base_url=args.base_url))
            print("(dry-run — nothing written; pass --out DIR --write to emit templates)")
    except (ValueError, FileExistsError) as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas", description="Atlas Career Intelligence platform CLI (foundation build).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_doctor = subparsers.add_parser("doctor", help="Run the offline health check.")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_status = subparsers.add_parser("status", help="Report current run/lock/checkpoint state.")
    p_status.add_argument("--run-id", default=None, help="Runtime run_id to report progress for (default: atlas-demo-run).")
    p_status.add_argument("--json", action="store_true", help="Print only the machine-readable JSON progress snapshot.")
    p_status.set_defaults(func=_cmd_status)

    p_run = subparsers.add_parser("run", help="Run the Atlas production runtime shell (demo mode only in this build).")
    p_run.add_argument("--demo", action="store_true", help="Run the deterministic demo workload (required in this build).")
    p_run.add_argument("--run-id", default=None, help="Run id (default: atlas-demo-run).")
    p_run.add_argument("--batch-size", type=int, default=None, help="Override configured batch_size.")
    p_run.add_argument("--stop-after", type=int, default=None, help="Stop after N completed tasks (produces a PARTIAL run, for testing resume).")
    p_run.add_argument("--max-runtime-minutes", type=float, default=None, help="Runtime budget in minutes before stopping and marking PARTIAL.")
    p_run.add_argument("--no-auto-resolve", action="store_true", help="Do not auto-resolve simulated human interventions (demo/testing only).")
    p_run.set_defaults(func=_cmd_run)

    p_resume = subparsers.add_parser("resume", help="Resume a previously PARTIAL Atlas runtime run.")
    p_resume.add_argument("--run-id", default=None, help="Run id to resume (default: atlas-demo-run).")
    p_resume.add_argument("--batch-size", type=int, default=None, help="Override configured batch_size.")
    p_resume.add_argument("--stop-after", type=int, default=None, help="Stop again after N completed tasks.")
    p_resume.add_argument("--max-runtime-minutes", type=float, default=None, help="Runtime budget in minutes.")
    p_resume.add_argument("--no-auto-resolve", action="store_true", help="Do not auto-resolve simulated human interventions (demo/testing only).")
    p_resume.set_defaults(func=_cmd_resume)

    p_test = subparsers.add_parser("test", help="Run the offline pytest suite.")
    p_test.add_argument(
        "--real-web",
        action="store_true",
        help="Run the real_web-marked tests instead of the offline suite (opens real browsers/network).",
    )
    p_test.set_defaults(func=_cmd_test)

    p_version = subparsers.add_parser("version", help="Print Atlas and dependency versions.")
    p_version.add_argument("--verbose", action="store_true", help="Print Atlas/Python/LangGraph/Playwright/schema versions.")
    p_version.set_defaults(func=_cmd_version)

    # --- Phase 0.95: backup / restore / support-bundle --------------------
    p_backup = subparsers.add_parser(
        "backup",
        help="Create a verified, consistent local backup (or `backup verify <dir>`).",
    )
    p_backup.add_argument("--output", default=None, help="Backups root directory (default: <output_dir>/backups).")
    p_backup.add_argument(
        "--keep-latest",
        type=int,
        default=None,
        help="After the backup, prune old backups keeping only the newest N.",
    )
    p_backup.set_defaults(func=_cmd_backup)
    backup_sub = p_backup.add_subparsers(dest="backup_command")
    p_backup_verify = backup_sub.add_parser("verify", help="Verify an existing backup directory.")
    p_backup_verify.add_argument("backup", help="Path to a backup directory (containing manifest.json).")
    p_backup_verify.set_defaults(func=_cmd_backup_verify)

    p_restore = subparsers.add_parser("restore", help="Restore a verified backup into a target directory.")
    p_restore.add_argument("backup", help="Path to a backup directory (containing manifest.json).")
    p_restore.add_argument("--target", required=True, help="Disposable target directory to restore into.")
    p_restore.set_defaults(func=_cmd_restore)

    p_support = subparsers.add_parser("support-bundle", help="Write a sanitized diagnostic support bundle (zip).")
    p_support.add_argument("--output", default=None, help="Directory to write the bundle into (default: <output_dir>/support_bundles).")
    p_support.set_defaults(func=_cmd_support_bundle)

    # --- Phase 1A: source framework introspection + scaffold --------------
    p_sources = subparsers.add_parser("sources", help="List registered source adapters and demo config.")
    p_sources.add_argument("--json", action="store_true", help="Also print machine-readable JSON.")
    p_sources.set_defaults(func=_cmd_sources)

    p_companies = subparsers.add_parser("companies", help="List registered companies (read-only).")
    p_companies.add_argument("--limit", type=int, default=200, help="Max companies to list.")
    p_companies.set_defaults(func=_cmd_companies)

    p_company_show = subparsers.add_parser("company", help="Show one company (`company show <id>`).")
    company_sub = p_company_show.add_subparsers(dest="company_command", required=True)
    p_company_show_show = company_sub.add_parser("show", help="Show a company's identity, sources, and history.")
    p_company_show_show.add_argument("company_id", help="The company_id to show.")
    p_company_show_show.set_defaults(func=_cmd_company_show)

    p_add_source = subparsers.add_parser(
        "add-source",
        help="Scaffold a new source adapter (dry-run by default; writes nothing).",
    )
    p_add_source.add_argument("name", help="Human name of the new source, e.g. 'Acme Board'.")
    p_add_source.add_argument("--type", required=True, help="SourceType value, e.g. ATS_GREENHOUSE, PORTAL_LARGE.")
    p_add_source.add_argument("--base-url", default=None, help="Optional base URL for the descriptor template.")
    p_add_source.add_argument("--out", default=None, help="Disposable output directory to write templates into.")
    p_add_source.add_argument("--dry-run", action="store_true", default=True, help="Print the plan only (default).")
    p_add_source.add_argument("--write", dest="dry_run", action="store_false", help="Actually write templates to --out.")
    p_add_source.set_defaults(func=_cmd_add_source)

    # Offline production-fixture runtime (build spec 24): fixture-only, zero
    # network, exercises the real production integration path. `atlas run` live
    # production stays disabled until real adapters pass Phase 1C gates.
    p_prodfix = subparsers.add_parser(
        "production-fixture",
        help="Run the offline (synthetic, zero-network) production-fixture runtime.",
    )
    prodfix_sub = p_prodfix.add_subparsers(dest="fixture_command", required=True)
    p_pf_run = prodfix_sub.add_parser("run", help="Execute a sealed fixture plan through the production path.")
    p_pf_run.add_argument("--run-id", default=None)
    p_pf_run.add_argument("--companies", default=2, help="Number of synthetic companies (per-lane children = N×6).")
    p_pf_run.add_argument("--stop-after-discover", action="store_true", help="Deliberate partial stop after DISCOVER.")
    p_pf_run.set_defaults(func=_cmd_production_fixture)
    p_pf_resume = prodfix_sub.add_parser("resume", help="Resume a PARTIAL/WAITING production-fixture run.")
    p_pf_resume.add_argument("--run-id", default=None)
    p_pf_resume.add_argument("--companies", default=2)
    p_pf_resume.set_defaults(func=_cmd_production_fixture)
    p_pf_status = prodfix_sub.add_parser("status", help="Show sealed plan / coverage / report references.")
    p_pf_status.add_argument("--run-id", default=None)
    p_pf_status.add_argument("--companies", default=2)
    p_pf_status.set_defaults(func=_cmd_production_fixture)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
