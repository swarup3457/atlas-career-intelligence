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
    if sub == "resume-after-human":
        reason = getattr(args, "reason", None)
        if not reason or not str(reason).strip():
            print("ERROR: resume-after-human requires a non-empty --reason (human authorization).")
            return 2
        result = rt.resume_after_human(str(reason), reference=getattr(args, "reference", None))
    elif sub == "resume":
        result = rt.resume()
    else:
        result = rt.run()

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


def _cmd_adapters(args: argparse.Namespace) -> int:
    """List the official ATS adapters, or run a low-volume READ-ONLY canary."""
    import json as _json

    from atlas.runtime.canary import (
        default_canary_config,
        load_canary_config,
        run_all_adapter_canaries,
    )
    from atlas.sources.ats import describe_ats_adapters
    from atlas.sources.models import SourceFamily

    sub = getattr(args, "adapters_command", None)
    if sub == "list":
        descriptors = describe_ats_adapters()
        print(f"Official ATS adapters (Phase 1C-A): {len(descriptors)}")
        for d in descriptors:
            print(f"  [{d['source_family']}] {d['adapter_class']} v{d['adapter_version']}"
                  f"/parser {d['parser_version']} caps={d['capabilities']}")
        if getattr(args, "json", False):
            print("\n" + _json.dumps({"adapters": descriptors}))
        return 0

    # canary
    live = bool(getattr(args, "live", False))
    families = None
    if getattr(args, "family", None) and not getattr(args, "all", False):
        try:
            families = [SourceFamily(args.family)]
        except ValueError:
            print(f"ERROR: unknown family {args.family!r}")
            return 2
    config = load_canary_config(Path(args.config)) if getattr(args, "config", None) else default_canary_config()
    if not live:
        print("NOTE: read-only canary in DRY-RUN mode (no network). Pass --live to hit real boards.")
    results = run_all_adapter_canaries(config, live=live, families=families, limit=int(getattr(args, "limit", 25)))
    ok_any = False
    for r in results:
        ok_any = ok_any or r.ok
        line = (f"[{r.family}] {r.board_identity} status={r.request_status} "
                f"count={r.result_count} more={r.has_more} health={r.health_state}")
        if r.limitation:
            line += f" limitation={r.limitation}"
        print(line)
        for s in r.samples:
            print(f"    - {s.title} | {s.location} | {s.source_job_id}")
    if getattr(args, "json", False):
        print("\n" + _json.dumps({"live": live, "results": [r.to_dict() for r in results]}))
    if not results:
        return 0
    if live:
        return 0 if ok_any else 1
    return 0


def _cmd_production_canary(args: argparse.Namespace) -> int:
    """Run/resume/inspect a low-volume live production canary through the SAME
    production graph with the bounded parallel dispatcher."""
    from atlas.persistence.sqlite import StateStore
    from atlas.runtime.canary import build_canary_runtime, default_canary_config, load_canary_config

    settings = load_settings()
    settings.ensure_directories()
    run_id = args.run_id or "ats-canary-run"
    sub = args.canary_command

    if sub == "status":
        with StateStore(settings.state_db) as store:
            run = store.get_run(run_id)
            plan = store.get_coverage_plan(run_id)
            summary = store.coverage_summary(run_id)
            leases = store.list_coverage_leases(run_id)
            raw = store.count_raw_observations(run_id)
            canon = store.count_canonical_jobs()
        print(f"RUN_ID={run_id}")
        print(f"RUN_STATUS={run['status'] if run else 'NOT_FOUND'}")
        if plan is not None:
            print(f"PLAN_STATE={plan['state']} SEALED_FP={(plan['fingerprint'] or '')[:12]}")
        print(f"COVERAGE_PLANNED={summary.get('_planned', 0)} COVERAGE_TERMINAL={summary.get('_completed', 0)}")
        print(f"LEASES={len(leases)} LEASES_DONE={sum(1 for l in leases if l['terminal'] == 1)}")
        print(f"RAW_OBSERVATIONS={raw} CANONICAL_JOBS={canon}")
        return 0

    live = bool(getattr(args, "live", False))
    if not live:
        print("NOTE: production-canary requires --live to hit real boards; refusing to run without it.")
        return 2
    config = load_canary_config(Path(args.config)) if getattr(args, "config", None) else default_canary_config()
    rt = build_canary_runtime(settings, config, run_id, live=live)
    result = rt.resume() if sub == "resume" else rt.run()

    print(f"RUN_ID={result.run_id}")
    print(f"STATUS={result.terminal_state}")
    print(f"PLANNED_CHILD_TASKS={result.planned_tasks}")
    print(f"TERMINAL_CHILD_TASKS={result.terminal_tasks}")
    print(f"LANES={sorted(result.lane_summary)}")
    print(f"PLAN_FP={result.plan_fingerprint[:12]} POLICY_FP={result.policy_fingerprint[:12]}")
    print(f"COUNTERS={result.counters}")
    if result.report_path:
        print(f"REPORT={result.report_path} VALID={result.report_valid}")
    print(f"STATE_DB={settings.state_db}")
    if getattr(args, "json", False):
        import json as _json
        print("\n" + _json.dumps({"run_id": result.run_id, "status": result.terminal_state,
                                  "planned": result.planned_tasks, "terminal": result.terminal_tasks,
                                  "counters": result.counters}))
    return 0 if result.terminal_state in ("COMPLETE", "PARTIAL", "WAITING_FOR_HUMAN") else 1


def _cmd_careers(args: argparse.Namespace) -> int:
    """Official career-site discovery / routing inspection / controlled pilot.

    Default commands are OFFLINE/dry-run; live network access requires --live.
    """
    import json as _json

    sub = getattr(args, "careers_command", None)

    if sub == "discover":
        from atlas.careers.discovery import CareerSourceDiscoveryService

        live = bool(getattr(args, "live", False))
        client = None
        if live:
            from atlas.sources.http_client import ReadOnlyHttpClient

            client = ReadOnlyHttpClient(request_budget=int(getattr(args, "budget", 20)),
                                        accept="text/html,application/xhtml+xml,*/*;q=0.8")
        svc = CareerSourceDiscoveryService(http_client=client)
        if not live:
            print("NOTE: discover is OFFLINE by default (no homepage/robots fetch). Pass --live to fetch.")
        outcome = svc.discover(
            args.domain, company_id=getattr(args, "company", None) or args.domain,
            name=getattr(args, "company", "") or "", known_careers_url=getattr(args, "known_url", None),
            fetch=live,
        )
        print(f"DOMAIN={outcome.official_domain} STATUS={outcome.status}")
        for e in outcome.trusted_entry_points:
            print(f"  [TRUSTED {e.trust_kind} {e.confidence:.2f}] {e.discovery_method}: {e.url}")
        for e in outcome.rejected:
            print(f"  [REJECTED {e.trust_kind}] {e.url}")
        if getattr(args, "json", False):
            print("\n" + _json.dumps(outcome.to_dict()))
        return 0 if outcome.trusted_entry_points else 0

    if sub == "inspect":
        from atlas.careers.router import CareerSourceRouter
        from atlas.careers.trust import OfficialUrlTrustPolicy

        live = bool(getattr(args, "live", False))
        url = args.url
        html = None
        status = 200
        challenge = login = False
        final_url = url
        if live:
            from atlas.models import ErrorCategory
            from atlas.sources.ats.base import detect_challenge
            from atlas.sources.http_client import HttpError, HttpRequest, ReadOnlyHttpClient

            client = ReadOnlyHttpClient(request_budget=int(getattr(args, "budget", 5)),
                                        accept="text/html,application/xhtml+xml,*/*;q=0.8")
            try:
                resp = client.fetch(HttpRequest(url, headers={"Accept": "text/html,*/*;q=0.8"}))
                status = resp.status
                final_url = resp.url
                html = resp.text() if status == 200 else None
                cat = detect_challenge(resp)
                challenge = cat == ErrorCategory.ANTI_BOT
                login = cat == ErrorCategory.LOGIN_WALL
            except HttpError as exc:
                print(f"FETCH_ERROR {exc.category.value}: {exc.message}")
                return 1
        else:
            print("NOTE: inspect is OFFLINE by default (URL-only fingerprint). Pass --live to fetch + classify.")
        if getattr(args, "domain", None):
            trust = OfficialUrlTrustPolicy(args.domain)
            td = trust.classify(url)
            print(f"TRUST={td.kind.value} trusted={td.trusted} reason={td.reason}")
        decision = CareerSourceRouter().route(
            url, company_id=getattr(args, "company", None), html=html, status=status,
            final_url=final_url, challenge=challenge, login_wall=login,
        )
        print(f"ROUTE={decision.route_kind.value} confidence={decision.confidence:.2f} reason={decision.reason}")
        if decision.source_instance is not None:
            print(f"  FAMILY={decision.source_instance.adapter_key.value} "
                  f"INSTANCE={decision.source_instance.instance_id}")
        if getattr(args, "json", False):
            print("\n" + _json.dumps(decision.to_dict()))
        return 0

    if sub == "pilot":
        from atlas.careers.pilot import CareerPilot, load_pilot_config

        settings = load_settings()
        settings.ensure_directories()
        config = load_pilot_config(Path(args.config))
        live = bool(getattr(args, "live", False))
        if not live:
            print("NOTE: pilot is DRY-RUN by default (routing preview only). Pass --live to execute.")
        pilot = CareerPilot(settings, config)
        result = pilot.run(live=live)
        print(f"PILOT_RUN_ID={result.run_id}")
        print(f"CONFIG_HASH={result.config_hash[:16]}")
        print(f"PILOT_STATUS={result.status}")
        print(f"RUNTIME_TERMINAL={result.runtime_terminal}")
        print(f"SUMMARY={result.summary}")
        for c in result.companies:
            print(f"  [{c.execution}/{c.route_kind}] {c.name} ({c.official_domain}) "
                  f"term={c.terminal_status} extracted={c.extracted} reported={c.reported} "
                  f"pages={c.pages} health={c.health}"
                  + (f" limitation={c.limitation}" if c.limitation else ""))
        if result.report_path:
            print(f"REPORT={result.report_path} VALID={result.report_valid}")
        if getattr(args, "json", False):
            print("\n" + _json.dumps(result.to_dict()))
        return 0 if result.status in ("PASS", "PARTIAL", "DRY_RUN", "WAITING_FOR_HUMAN") else 1

    if sub == "pilot-status":
        from atlas.persistence.sqlite import StateStore

        settings = load_settings()
        run_id = args.run_id
        with StateStore(settings.state_db) as store:
            row = store.get_career_pilot_run(run_id)
            summary = store.coverage_summary(run_id)
            raw = store.count_raw_observations(run_id)
        if row is None:
            print(f"PILOT_RUN {run_id} NOT_FOUND")
            return 1
        print(f"PILOT_RUN_ID={run_id}")
        print(f"CONFIG_HASH={row['config_hash'][:16]}")
        print(f"STATUS={row['status']}")
        print(f"COVERAGE_PLANNED={summary.get('_planned', 0)} COVERAGE_TERMINAL={summary.get('_completed', 0)}")
        print(f"RAW_OBSERVATIONS={raw}")
        if getattr(args, "json", False):
            print("\n" + _json.dumps({"run_id": run_id, "status": row["status"],
                                      "config_hash": row["config_hash"],
                                      "results": _json.loads(row["results_json"] or "{}")}))
        return 0

    if sub == "resume":
        from atlas.careers.pilot import CareerPilot, load_pilot_config

        settings = load_settings()
        settings.ensure_directories()
        live = bool(getattr(args, "live", False))
        if not live:
            print("NOTE: careers resume requires --live to re-attempt pending children.")
            return 2
        config = load_pilot_config(Path(args.config))
        from atlas.runtime.production import ProductionSearchRuntime
        from atlas.sources.generic import build_careers_registry

        pilot = CareerPilot(settings, config)
        prepared = pilot.prepare(live=True)
        instances = {}
        companies = []
        from atlas.planning import PlannedCompany

        for entry in prepared.routable:
            company = entry.company
            inst = entry.decision.source_instance
            instances[inst.instance_id] = inst
            companies.append(PlannedCompany(
                company_id=company.company_id, name=company.name, tier=company.tier,
                mode=company.mode, source_instances=(inst.instance_id,), geography_group=company.geography_group))
        runtime = ProductionSearchRuntime(
            settings, config.run_id, fixture_mode=True, companies=companies, instances=instances,
            registry=build_careers_registry(), parallel_workers=1, lane_override=list(config.lanes),
            max_per_company=1, max_per_instance=1, max_per_tenant=1)
        result = runtime.resume()
        print(f"RUN_ID={result.run_id} STATUS={result.terminal_state}")
        print(f"PLANNED={result.planned_tasks} TERMINAL={result.terminal_tasks}")
        return 0 if result.terminal_state in ("COMPLETE", "PARTIAL", "WAITING_FOR_HUMAN") else 1

    print("ERROR: unknown careers subcommand")
    return 2


def _fresh_run_id(prefix: str) -> str:
    import datetime as _dt

    return f"{prefix}-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _cmd_market(args: argparse.Namespace) -> int:
    """Market discovery + adaptive search. Default is DRY-RUN/offline plan; live
    network/browser discovery requires --live."""
    import json as _json

    from atlas.market.campaign import CampaignBudget, MarketCampaign, MarketWave
    from atlas.market.runtime import MarketRunReuseError, MarketSearchRuntime
    from atlas.persistence.sqlite import StateStore

    sub = getattr(args, "market_command", None)
    settings = load_settings()
    settings.ensure_directories()

    def _lanes() -> list[str]:
        from atlas.policy import load_policy

        if getattr(args, "lanes", None):
            return [l.strip() for l in args.lanes.split(",") if l.strip()]
        return list(load_policy(None).lanes.keys())

    def _budget() -> CampaignBudget:
        return CampaignBudget(
            max_waves=int(getattr(args, "max_waves", 3)),
            max_browser_calls=int(getattr(args, "max_browser", 40)),
            max_portal_pages=int(getattr(args, "max_pages", 2)),
        )

    if sub in ("plan", "run"):
        run_id = getattr(args, "run_id", None) or _fresh_run_id("market")
        geos = [g.strip() for g in getattr(args, "geographies", "PRIMARY").split(",") if g.strip()]
        rt = MarketSearchRuntime(
            settings, run_id, lanes=_lanes(), geography_groups=geos,
            portal_families=tuple(f.strip() for f in getattr(args, "portals", "linkedin,naukri").split(",") if f.strip()),
            budget=_budget(), recency_days=int(getattr(args, "recency_days", 7)))
        live = bool(getattr(args, "live", False))
        if sub == "plan" or not live:
            plan = rt.plan()
            print(f"CAMPAIGN={plan['campaign_id']} RUN_ID={plan['run_id']} WAVE0_TASKS={plan['wave0_tasks']}")
            print(f"LANES={plan['lanes']} GEOS={plan['geographies']} PORTALS={plan['portal_families']}")
            print("NOTE: market plan is OFFLINE/dry-run. Pass 'market run --live' to execute.")
            if getattr(args, "json", False):
                print("\n" + _json.dumps(plan))
            return 0
        try:
            res = rt.run(live=True, resume=bool(getattr(args, "resume", False)))
        except MarketRunReuseError as exc:
            print(f"ERROR: {exc}")
            return 2
        print(f"RUN_ID={res.run_id} STATUS={res.status} LEADS={res.portal_leads} "
              f"DYNAMIC_COMPANIES={res.dynamic_companies} VERIFIED_LINKS={res.verified_links}")
        print(f"WAVES={len(res.waves)} REPORT={res.report_path} REPORT_VALID={res.report_valid}")
        print(f"MAX_CONCURRENCY={res.summary.get('max_observed_concurrency')} "
              f"AUTH_PROFILE_OWNERS_MAX={res.summary.get('authenticated_profile_owner_max')}")
        if getattr(args, "json", False):
            print("\n" + _json.dumps(res.to_dict()))
        return 0 if res.status in ("COMPLETE", "PARTIAL_BUDGET") else 1

    if sub == "resume":
        run_id = args.run_id
        with StateStore(settings.state_db) as store:
            row = store.get_market_campaign(f"camp::{run_id}")
        if row is None:
            print(f"ERROR: no campaign for run_id {run_id!r}")
            return 2
        camp = MarketCampaign.from_row(row)
        rt = MarketSearchRuntime(
            settings, run_id, lanes=camp.config.get("lanes") or _lanes(),
            geography_groups=camp.config.get("geographies", ["PRIMARY"]),
            portal_families=tuple(camp.config.get("portal_families", ["linkedin", "naukri"])),
            budget=camp.budget)
        if not bool(getattr(args, "live", False)):
            print("NOTE: market resume requires --live.")
            return 2
        res = rt.run(live=True, resume=True)
        print(f"RUN_ID={res.run_id} STATUS={res.status} LEADS={res.portal_leads}")
        return 0

    if sub == "status":
        run_id = args.run_id
        with StateStore(settings.state_db) as store:
            row = store.get_market_campaign(f"camp::{run_id}")
            if row is None:
                print(f"ERROR: no campaign for run_id {run_id!r}")
                return 2
            camp = MarketCampaign.from_row(row)
            waves = store.list_market_waves(camp.campaign_id)
            leads = store.list_portal_leads(run_id)
            links = store.list_portal_official_links(run_id)
            budget = store.get_campaign_budget(camp.campaign_id)
            deficits = store.list_wave_deficits(camp.campaign_id)
        print(f"CAMPAIGN={camp.campaign_id} STATUS={camp.status.value} CURRENT_WAVE={camp.current_wave}")
        print(f"TERMINAL_REASON={camp.terminal_reason}")
        for w in waves:
            print(f"  WAVE {w['wave_index']}: status={w['status']} tasks={len(_json.loads(w['tasks_json'] or '[]'))} "
                  f"seal={(w['seal_hash'] or '')[:12]} deficit={w['deficit_reason']}")
        verified = sum(1 for l in links if l["verification_state"] in ("LINKED_OFFICIAL_VERIFIED", "CLOSED_POSITIVE_EVIDENCE"))
        print(f"PORTAL_LEADS={len(leads)} VERIFIED_LINKS={verified} DEFICITS={len(deficits)}")
        if budget is not None:
            print(f"BUDGET http={budget['http_calls']}/{budget['max_http']} browser={budget['browser_calls']}/{budget['max_browser']} "
                  f"results={budget['results']}/{budget['max_results']}")
        return 0

    if sub == "discovered":
        with StateStore(settings.state_db) as store:
            run_id = getattr(args, "run_id", None)
            provs = store.list_dynamic_company_provenance(run_id=run_id)
            seen = set()
            print("DYNAMICALLY DISCOVERED COMPANIES:")
            for p in provs:
                cid = p["company_id"]
                if cid in seen:
                    continue
                seen.add(cid)
                co = store.get_company(cid) if cid else None
                name = (co["display_name"] if co else p["normalized_name"]) or "?"
                print(f"  [{p['resolution_status']}] {name} domain={p['resolved_domain'] or '-'} "
                      f"from={p['discovery_source']}")
            if not provs:
                print("  (none)")
        return 0

    print("ERROR: unknown market subcommand")
    return 2


def _cmd_portals(args: argparse.Namespace) -> int:
    """Portal (LinkedIn/Naukri) read-only health + explicit visible auth."""
    from atlas.sources.models import SearchRequest
    from atlas.sources.portals import describe_portal_adapters, make_portal_instance
    from atlas.sources.portals.registry import PORTAL_FAMILIES, adapter_class_for_family
    from atlas.sources.models import SourceFamily

    sub = getattr(args, "portals_command", None)

    if sub == "health":
        live = bool(getattr(args, "live", False))
        print("PORTAL ADAPTERS (READ-ONLY):")
        for d in describe_portal_adapters():
            print(f"  {d['source_family']}: {d['adapter_class']} v{d['adapter_version']} "
                  f"class={d['concurrency_class']} caps={d['capabilities']}")
        if not live:
            print("NOTE: portals health is OFFLINE by default (adapter descriptors). Pass --live for a bounded probe.")
            return 0
        family_names = {"linkedin": SourceFamily.LINKEDIN, "naukri": SourceFamily.NAUKRI}
        want = [f.strip() for f in (getattr(args, "family", None) or "linkedin,naukri").split(",")]
        rc = 0
        for fname in want:
            fam = family_names.get(fname)
            if fam is None:
                continue
            inst = make_portal_instance(fam, f"{fname}-health")
            adapter = adapter_class_for_family(fam)(inst)
            health = adapter.health_check()
            print(f"  LIVE {fname}: {health.state.value} — {health.detail}")
            if health.state.value in ("AUTH_REQUIRED",):
                print(f"    -> run:  atlas portals auth --family {fname}   (opens Chrome for you to sign in)")
        return rc

    if sub == "auth":
        # An EXPLICIT, human-in-the-loop visible auth flow. Atlas NEVER types
        # credentials, solves a CAPTCHA, or automates login — it only opens a
        # visible Chrome window (dedicated profile) so YOU can sign in.
        family = getattr(args, "family", "linkedin")
        url = {"linkedin": "https://www.linkedin.com/login",
               "naukri": "https://www.naukri.com/nlogin/login"}.get(family, "https://www.linkedin.com/login")
        profile = Path(getattr(args, "profile", None) or f".browser-profile-{family}")
        if not bool(getattr(args, "live", False)):
            print(f"NOTE: 'portals auth' opens a VISIBLE Chrome for MANUAL sign-in to {family} ({url}).")
            print("      Atlas never enters credentials or bypasses any check. Pass --live to open the window.")
            print(f"      Dedicated profile: {profile}")
            return 0
        from atlas.browser.manager import BrowserManager

        print(f"Opening a visible Chrome for you to sign in to {family} manually: {url}")
        print("Atlas will NOT type your password or solve any challenge. Close the window when done.")
        try:
            with BrowserManager(profile, channel="chrome") as manager:
                page = manager.launch(headless=False)
                manager.navigate(page, url)
                try:
                    input("Press Enter here AFTER you have finished signing in (or to abort)... ")
                except EOFError:
                    pass
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not open browser: {exc}")
            return 1
        print("Auth window closed. The dedicated profile session is retained locally (never exported).")
        return 0

    print("ERROR: unknown portals subcommand")
    return 2


def _cmd_outputs(args: argparse.Namespace) -> int:
    """Inspect the stable production output directory (runs + latest pointer)."""
    import json as _json

    from atlas.reporting.production_output import (
        LatestPaths,
        ProductionOutputPublisher,
        ProductionRunPaths,
    )

    settings = load_settings()
    pub = ProductionOutputPublisher(settings)
    sub = getattr(args, "outputs_command", None)
    as_json = bool(getattr(args, "json", False))

    if sub == "latest":
        latest = pub.latest()
        wb = LatestPaths(pub.root).workbook
        if latest is None:
            print(_json.dumps({"latest": None}) if as_json else "No latest production run yet.")
            return 0
        if as_json:
            print(_json.dumps({"latest": latest, "workbook": str(wb)}, indent=2))
        else:
            print(f"Latest run: {latest['run_id']} ({latest.get('status')})")
            print(f"Workbook:   {wb}")
            print(f"Run dir:    {latest.get('run_dir')}")
        return 0

    if sub == "list":
        runs = pub.list_runs()
        if as_json:
            print(_json.dumps({"runs": runs, "production_root": str(pub.root)}, indent=2))
        else:
            if not runs:
                print("No production runs published yet.")
            for r in runs:
                print(f"  {r['run_id']:<28} {str(r.get('status')):<14} {r.get('published_at')}")
            print(f"Production root: {pub.root}")
        return 0

    if sub == "show":
        run_id = getattr(args, "run_id", None)
        manifest = pub.show_run(run_id)
        if manifest is None:
            print(f"ERROR: no such run: {run_id}")
            return 1
        if as_json:
            print(_json.dumps(manifest, indent=2))
        else:
            paths = ProductionRunPaths(pub.root, run_id)
            print(f"Run: {manifest['run_id']} ({manifest.get('status')})")
            print(f"Published: {manifest.get('published_at')}  report_valid={manifest.get('report_valid')}")
            print(f"Run dir:  {paths.run_dir}")
            print(f"Workbook: {paths.workbook}")
            print("Files (sha256):")
            for rel, digest in sorted(manifest.get("files", {}).items()):
                print(f"  {rel:<40} {str(digest)[:16]}")
        return 0

    if sub == "open-latest":
        latest = pub.latest()
        if latest is None:
            print("No latest run to open.")
            return 1
        wb = LatestPaths(pub.root).workbook
        if not bool(getattr(args, "live", False)):
            print(f"NOTE: would open {wb}. Pass --live to actually open it.")
            return 0
        try:
            import os as _os

            _os.startfile(str(wb))  # noqa: S606 - explicit, operator-initiated open (Windows)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not open workbook: {exc}")
            return 1
        print(f"Opened {wb}")
        return 0

    print("ERROR: unknown outputs subcommand")
    return 2


def _synthetic_daily_candidate():
    from atlas.candidate.eligibility import CandidateProfile
    from atlas.candidate.importer import build_synthetic_ledger

    return CandidateProfile.from_ledger(
        build_synthetic_ledger(), total_experience_years=4.5,
        target_lanes=("JAVA_BACKEND", "GENERAL_SOFTWARE"),
    )


def _cmd_copilot(args: argparse.Namespace) -> int:
    """Optional Copilot SDK reasoning controller: info + a synthetic canary.

    The canary uses SYNTHETIC data only (no candidate PII). Real private data is
    NEVER sent unless the operator supplies both the consent flag AND an
    account-type acknowledgement (verified elsewhere)."""
    import json as _json

    from atlas.controllers.copilot import (
        OFFICIAL_SDK_DISTRIBUTION,
        OFFICIAL_SDK_LICENSE,
        OFFICIAL_SDK_PINNED_VERSION,
        CopilotSdkController,
    )

    settings = load_settings()
    sub = getattr(args, "copilot_command", None)
    as_json = bool(getattr(args, "json", False))

    if sub == "info":
        info = {
            "official_sdk": OFFICIAL_SDK_DISTRIBUTION,
            "pinned_version": OFFICIAL_SDK_PINNED_VERSION,
            "license": OFFICIAL_SDK_LICENSE,
            "controller_default": settings.controller,
            "controller_model": settings.controller_model,
            "private_candidate_consent": bool(settings.allow_private_candidate_to_copilot),
            "account_type": settings.copilot_account_type,
        }
        print(_json.dumps(info, indent=2) if as_json else
              "\n".join(f"{k}: {v}" for k, v in info.items()))
        return 0

    if sub == "canary":
        # SYNTHETIC candidate + jobs — never real PII.
        payload = {
            "jobs": [
                {"job_key": "synth_1", "title": "Java Backend Engineer",
                 "mandatory": ["Java", "Spring Boot"]},
                {"job_key": "synth_2", "title": "React Frontend Engineer",
                 "mandatory": ["React", "TypeScript"]},
            ],
            "candidate_evidence_tokens": ["java", "spring boot", "rest", "mysql"],
            "instruction": "Return strict JSON {\"scores\":[{\"job_key\":..,\"score\":0-100,\"strengths\":[..]}]}",
        }
        ctrl = CopilotSdkController(
            model=settings.controller_model,
            session_timeout_s=int(getattr(args, "timeout", 90) or 90),
            max_session_credits=settings.controller_max_session_credits,
        )
        if not bool(getattr(args, "live", False)):
            print("NOTE: 'copilot canary' makes a REAL Copilot model call with SYNTHETIC data.")
            print(f"      Model: {settings.controller_model}. Pass --live to run it.")
            return 0
        res = ctrl.run_agent(
            "triage-ranker",
            "Rank these synthetic jobs for this synthetic candidate. Output only strict JSON.",
            payload)
        out = {
            "quarantined": res.quarantined, "model": res.model, "session_id": res.session_id,
            "latency_ms": round(res.latency_ms, 1), "content": res.content[:1000],
            "usage": ctrl.usage(),
        }
        if as_json:
            print(_json.dumps(out, indent=2))
        else:
            print(f"Copilot synthetic canary: quarantined={res.quarantined} model={res.model} "
                  f"session={res.session_id} latency_ms={round(res.latency_ms,1)}")
            print(f"  content: {res.content[:300]}")
        return 0 if not res.quarantined else 1

    print("ERROR: unknown copilot subcommand")
    return 2


def _synthetic_daily_jobs():
    """A small PII-free synthetic job set so `atlas daily` demonstrates the full
    durable pipeline end-to-end offline. Live source wiring replaces this set."""
    import datetime

    from atlas.candidate.eligibility import RankableJob

    today = datetime.date.today()
    specs = [
        ("Co-Alpha", ("Java", "Spring Boot"), ("MySQL",), "VERIFIED_OFFICIAL"),
        ("Co-Bravo", ("Java", "REST"), ("AWS",), "VERIFIED_OFFICIAL"),
        ("Co-Charlie", ("Java", "Kubernetes"), ("Docker",), "PORTAL_CURRENT_LEAD"),
    ]
    jobs = []
    for i, (company, mand, pref, verif) in enumerate(specs):
        jobs.append(RankableJob(
            job_key=f"job_daily_synth_{i}", company=company, title="Java Backend Engineer",
            location="Bengaluru, India", lane="JAVA_BACKEND",
            mandatory_requirements=mand, preferred_requirements=pref,
            experience_text="2+ years", eligibility_text="Bengaluru, India",
            posted_date=today, verification_state=verif, has_live_official_page=(verif == "VERIFIED_OFFICIAL"),
            url=f"https://{company.lower()}.example/jobs/{i}", source_family="synthetic",
        ))
    return jobs


def _private_daily_candidate(settings, *, target_lanes=("JAVA_BACKEND", "GENERAL_SOFTWARE"),
                             experience_years: float = 2.0, import_source=None):
    """Import the REAL private candidate (PRIVATE_LOCAL) into the gitignored
    private store and build a CandidateProfile from it. Records ONLY a mode +
    source hash + counts (never PII). Falls back to synthetic ONLY when the
    private source is absent; production callers should require PRIVATE_LOCAL."""
    from atlas.candidate.eligibility import CandidateProfile
    from atlas.candidate.importer import import_candidate_evidence

    res = import_candidate_evidence(source=import_source, write=True)
    candidate = CandidateProfile.from_ledger(
        res.ledger, total_experience_years=float(experience_years),
        target_lanes=tuple(target_lanes), strong_overall=True,
    )
    mode = "PRIVATE_LOCAL" if not res.used_synthetic else "SYNTHETIC_FALLBACK"
    info = {
        "mode": mode,                    # machine-readable contract key
        "candidate_mode": mode,          # Section 12 wording
        "synthetic": bool(res.used_synthetic),
        "candidate_source_sha256": res.source_sha256,
        "claim_count": res.claim_count,
        "private_store": res.out_path,
    }
    return candidate, info


def _load_official_targets(path):
    """Load sealed official company targets (verified domains + optional known
    ATS boards) from a YAML/JSON file. Domains are VERIFIED facts, never guessed."""
    import json as _json
    from pathlib import Path as _Path

    from atlas.runtime.official_universe import OfficialCompanyTarget

    text = _Path(path).read_text(encoding="utf-8")
    if str(path).lower().endswith((".yaml", ".yml")):
        import yaml
        data = yaml.safe_load(text) or {}
    else:
        data = _json.loads(text)
    targets = []
    for c in data.get("companies", []):
        targets.append(OfficialCompanyTarget(
            company_id=str(c["company_id"]), name=str(c.get("name", c["company_id"])),
            official_domain=str(c["official_domain"]),
            known_careers_url=c.get("known_careers_url"),
            tier=str(c.get("tier", "A")), mode=str(c.get("mode", "DELTA")),
            geography_group=str(c.get("geography_group", "PRIMARY")),
            board_token=c.get("board_token"), family=c.get("family"),
            expected_route=c.get("expected_route"),
        ))
    return targets, data


def _official_targets_from_registry(settings, *, limit: int):
    """Select up to ``limit`` due official companies from the company registry
    (those with a VERIFIED official domain). The runtime is generic over any
    target list; this is the production selection path."""
    from atlas.company.registry import CompanyRegistry
    from atlas.persistence.sqlite import StateStore
    from atlas.runtime.official_universe import OfficialCompanyTarget

    targets = []
    with StateStore(settings.state_db) as store:
        registry = CompanyRegistry(store)
        for company in registry.list_companies(limit=max(limit * 4, limit)):
            if not company.official_domain:
                continue
            targets.append(OfficialCompanyTarget(
                company_id=company.company_id, name=company.canonical_name,
                official_domain=company.official_domain,
                known_careers_url=company.careers_url,
            ))
            if len(targets) >= limit:
                break
    return targets


def _cmd_daily(args: argparse.Namespace) -> int:
    """Daily operation family: plan / run / resume / status + Windows scheduler."""
    import json as _json

    from atlas.runtime.scheduler_install import WindowsDailyScheduler

    settings = load_settings()
    sub = getattr(args, "daily_command", None)
    as_json = bool(getattr(args, "json", False))

    # -- scheduler (dry-run default; enable is explicit) --------------------
    if sub in ("install-task", "disable-task", "remove-task"):
        sched = WindowsDailyScheduler()
        if sub == "install-task":
            try:
                action = sched.install(getattr(args, "time", ""), enable=bool(getattr(args, "enable", False)))
            except ValueError as exc:
                print(f"ERROR: {exc}")
                return 2
            if as_json:
                print(_json.dumps(action.to_dict(), indent=2))
            elif action.dry_run:
                print("DRY-RUN — no scheduled task was created. Re-run with --enable to install it.")
                print(f"Task: {action.task}")
                print(f"Command:\n  {action.command}")
            else:
                print(f"Task {action.task}: {'ENABLED' if action.enabled else 'FAILED'} (rc={action.returncode}).")
                if action.stderr.strip():
                    print(action.stderr.strip())
            return 0 if (action.dry_run or action.enabled) else 1
        action = sched.disable() if sub == "disable-task" else sched.remove()
        if as_json:
            print(_json.dumps(action.to_dict(), indent=2))
        else:
            print(f"{action.action} {action.task}: rc={action.returncode}")
        return 0 if action.returncode == 0 else 1

    # -- pipeline commands --------------------------------------------------
    from atlas.orchestration.run_lock import RUN_ALREADY_ACTIVE, RunLock
    from atlas.runtime.daily import DailyRunner

    jobs = _synthetic_daily_jobs()

    # Supplemental READ-ONLY live portals (LinkedIn/Naukri). --include-portals is
    # the official-first name; --sources remains accepted for back-compat.
    portals_arg = (getattr(args, "include_portals", None) or getattr(args, "sources", None) or "").strip()
    live_families = tuple(s.strip() for s in portals_arg.split(",") if s.strip()) if portals_arg else ()

    portal_only_debug = bool(getattr(args, "portal_only_debug", False))
    official_config_path = getattr(args, "official_config", None)
    official_companies_n = int(getattr(args, "official_companies", 0) or 0)
    official_workers = int(getattr(args, "official_workers", 1) or 1)
    official_lanes = ("JAVA_BACKEND", "GENERAL_SOFTWARE")

    def _build_candidate(live):
        # A LIVE run uses the REAL PRIVATE_LOCAL candidate (§12) unless the
        # operator explicitly forces synthetic. plan/status stay synthetic (no
        # private import side effects).
        if live and not bool(getattr(args, "synthetic_candidate", False)):
            return _private_daily_candidate(settings, target_lanes=official_lanes)
        return _synthetic_daily_candidate(), {"mode": "SYNTHETIC", "candidate_mode": "SYNTHETIC", "synthetic": True}

    def _make_runner(run_id, *, live=False):
        candidate, cand_info = _build_candidate(live)
        market_exec = None
        source_health_provider = None
        official_followup = None
        official_exec = None
        official_result_provider = None
        eligible_for_latest = True
        seed_jobs = [] if live else list(jobs)

        # OFFICIAL-FIRST: begin from the company universe. --live ALWAYS executes
        # official company coverage unless --portal-only-debug is explicitly set.
        if live and not portal_only_debug:
            targets = []
            if official_config_path:
                targets, _ = _load_official_targets(official_config_path)
            elif official_companies_n:
                targets = _official_targets_from_registry(settings, limit=official_companies_n)
            if official_companies_n and len(targets) > official_companies_n:
                targets = targets[:official_companies_n]
            if targets:
                from atlas.runtime.official_universe import OfficialCompanyUniverseRunner

                off_runner = OfficialCompanyUniverseRunner(
                    settings, run_id, targets=targets, lanes=official_lanes,
                    parallel_workers=official_workers,
                    max_pages=int(getattr(args, "max_pages", 2) or 2), live=True)
                _off_holder: dict = {}

                def official_exec():
                    res = off_runner.run()
                    _off_holder["res"] = res
                    return res.rankable_jobs()

                def official_result_provider():
                    return _off_holder.get("res")

        # Supplemental live portal discovery (PORTAL_ONLY leads, never All_Jobs).
        if live and live_families:
            from atlas.runtime.live_sources import LivePortalDiscovery

            disco = LivePortalDiscovery(
                lane=getattr(args, "lane", None) or "JAVA_BACKEND",
                location=getattr(args, "location", None) or "India",
                recency_days=int(getattr(args, "recency_days", 7) or 7),
                max_pages=min(2, int(getattr(args, "max_pages", 2) or 2)),
            )
            producer = disco.as_market_exec(live_families)
            market_exec = producer

            def source_health_provider():
                outcome = getattr(producer, "holder", {}).get("outcome")
                base = {"mode": "live", "families": list(live_families)}
                if outcome is not None:
                    base.update(outcome.health_dict())
                return base

        # Optional REAL live portal->official follow-up + linkage (PORTAL_OFFICIAL_LINKED).
        if live and bool(getattr(args, "official_followup", False)) and not portal_only_debug:
            from atlas.runtime.official_followup import DEFAULT_KNOWN_SOURCES, LiveOfficialFollowup

            followup = LiveOfficialFollowup(
                settings, run_id, known_sources=DEFAULT_KNOWN_SOURCES,
                recency_days=int(getattr(args, "recency_days", 30) or 30),
                max_portal_pages=min(2, int(getattr(args, "max_pages", 2) or 2)), max_official=300)
            official_followup = followup.run

        # --portal-only-debug is DIAGNOSTIC ONLY: it can never update latest.
        if portal_only_debug:
            eligible_for_latest = False

        return DailyRunner(settings, run_id, jobs=seed_jobs, candidate=candidate, live=live,
                           official_exec=official_exec, official_result_provider=official_result_provider,
                           market_exec=market_exec, official_followup=official_followup,
                           source_health_provider=source_health_provider,
                           candidate_mode_info=cand_info, eligible_for_latest=eligible_for_latest)

    if sub == "plan":
        run_id = getattr(args, "run_id", None) or _fresh_run_id("daily")
        plan = _make_runner(run_id).plan()
        if as_json:
            print(_json.dumps(plan, indent=2))
        else:
            print(f"Daily plan for run {run_id}:")
            print(f"  jobs={plan['jobs']} triage_limit={plan['triage_limit']} deep_limit={plan['deep_limit']}")
            print(f"  live sources: {', '.join(live_families) if live_families else '(none; synthetic demo set)'}")
            print(f"  run dir:   {plan['run_dir']}")
            print(f"  workbook:  {plan['workbook_path']}")
        return 0

    if sub in ("run", "resume"):
        if not bool(getattr(args, "live", False)):
            print("NOTE: 'daily run/resume' requires --live to execute the durable pipeline.")
            return 0
        run_id = getattr(args, "run_id", None) or _fresh_run_id("daily")
        lock = RunLock(settings.state_db.parent, filename="atlas_daily_run.lock")
        acquired = lock.try_acquire(run_id)
        if acquired.status == RUN_ALREADY_ACTIVE:
            print(f"ERROR: another daily run is already active (run_id={acquired.run_id}). No concurrent runs.")
            return 2
        try:
            runner = _make_runner(run_id, live=True)
            result = runner.resume() if sub == "resume" else runner.run()
        finally:
            lock.release()
        payload = result.to_dict()
        if as_json:
            print(_json.dumps(payload, indent=2))
        else:
            print(f"Daily {sub}: {payload['terminal_state']} (report_valid={payload['report_valid']}, "
                  f"latest_updated={payload['latest_updated']})")
            print(f"  discovered={payload['jobs_discovered']} ranked={payload['jobs_ranked']} "
                  f"selected={payload['selected']} packs={payload['packs_built']}")
            print(f"  run dir:  {payload['run_dir']}")
            print(f"  workbook: {payload['workbook_path']}")
        return 0 if payload["terminal_state"] == "COMPLETE" else (
            2 if payload["terminal_state"] == "WAITING_FOR_HUMAN" else 1)

    if sub == "status":
        run_id = getattr(args, "run_id", None)
        status = _make_runner(run_id).status()
        if as_json:
            print(_json.dumps(status, indent=2))
        else:
            print(f"Daily run {run_id}: phase={status['phase']} terminal={status['terminal_state']} "
                  f"manifest={status['manifest_status']}")
            print(f"  run dir: {status['run_dir']}")
        return 0

    print("ERROR: unknown daily subcommand")
    return 2


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
    p_pf_reopen = prodfix_sub.add_parser(
        "resume-after-human",
        help="EXPLICIT, authorized human resolution: reopen BLOCKED_HUMAN children then resume.",
    )
    p_pf_reopen.add_argument("--run-id", default=None)
    p_pf_reopen.add_argument("--companies", default=2)
    p_pf_reopen.add_argument("--reason", required=True, help="Non-empty human authorization reason (no secrets).")
    p_pf_reopen.add_argument("--reference", default=None, help="Optional ticket/reference (no secrets).")
    p_pf_reopen.set_defaults(func=_cmd_production_fixture)
    p_pf_status = prodfix_sub.add_parser("status", help="Show sealed plan / coverage / report references.")
    p_pf_status.add_argument("--run-id", default=None)
    p_pf_status.add_argument("--companies", default=2)
    p_pf_status.set_defaults(func=_cmd_production_fixture)

    # Phase 1C-A: official ATS adapters + low-volume read-only live canary.
    p_adapters = subparsers.add_parser("adapters", help="List official ATS adapters or run a read-only canary.")
    adapters_sub = p_adapters.add_subparsers(dest="adapters_command", required=True)
    p_ad_list = adapters_sub.add_parser("list", help="List the registered official ATS adapters.")
    p_ad_list.add_argument("--json", action="store_true")
    p_ad_list.set_defaults(func=_cmd_adapters)
    p_ad_can = adapters_sub.add_parser("canary", help="Per-family read-only canary (DRY-RUN unless --live).")
    p_ad_can.add_argument("--family", default=None, help="greenhouse|lever|ashby|workday")
    p_ad_can.add_argument("--all", action="store_true", help="Canary every configured board.")
    p_ad_can.add_argument("--config", default=None, help="Path to a canary config YAML (defaults to leverdemo+Ashby).")
    p_ad_can.add_argument("--live", action="store_true", help="Explicitly hit real public boards (opt-in).")
    p_ad_can.add_argument("--limit", type=int, default=25, help="Max jobs to request per board (bounded).")
    p_ad_can.add_argument("--json", action="store_true")
    p_ad_can.set_defaults(func=_cmd_adapters)

    p_canary = subparsers.add_parser(
        "production-canary", help="Low-volume live production canary through the parallel dispatcher (opt-in)."
    )
    canary_sub = p_canary.add_subparsers(dest="canary_command", required=True)
    p_cn_run = canary_sub.add_parser("run", help="Run the production canary (requires --live).")
    p_cn_run.add_argument("--run-id", default=None)
    p_cn_run.add_argument("--config", default=None, help="Canary config YAML (defaults to leverdemo+Ashby).")
    p_cn_run.add_argument("--live", action="store_true")
    p_cn_run.add_argument("--json", action="store_true")
    p_cn_run.set_defaults(func=_cmd_production_canary)
    p_cn_resume = canary_sub.add_parser("resume", help="Resume a PARTIAL/WAITING production canary (requires --live).")
    p_cn_resume.add_argument("--run-id", default=None)
    p_cn_resume.add_argument("--config", default=None)
    p_cn_resume.add_argument("--live", action="store_true")
    p_cn_resume.add_argument("--json", action="store_true")
    p_cn_resume.set_defaults(func=_cmd_production_canary)
    p_cn_status = canary_sub.add_parser("status", help="Show canary run plan/coverage/leases (offline).")
    p_cn_status.add_argument("--run-id", default=None)
    p_cn_status.add_argument("--json", action="store_true")
    p_cn_status.set_defaults(func=_cmd_production_canary)

    # -- careers: official career-site discovery / routing / controlled pilot --
    p_careers = subparsers.add_parser(
        "careers",
        help="Official career-site discovery, routing inspection, and the controlled pilot (offline/dry-run by default).",
    )
    careers_sub = p_careers.add_subparsers(dest="careers_command", required=True)

    p_ca_disc = careers_sub.add_parser("discover", help="Discover trusted career entry points for a company domain.")
    p_ca_disc.add_argument("--domain", required=True, help="Verified official domain (e.g. acme.com).")
    p_ca_disc.add_argument("--company", default=None, help="Company id/name (optional).")
    p_ca_disc.add_argument("--known-url", dest="known_url", default=None, help="A known careers URL (optional).")
    p_ca_disc.add_argument("--budget", type=int, default=20, help="Max HTTP requests when --live.")
    p_ca_disc.add_argument("--live", action="store_true", help="Fetch homepage/robots/sitemap (network).")
    p_ca_disc.add_argument("--json", action="store_true")
    p_ca_disc.set_defaults(func=_cmd_careers)

    p_ca_insp = careers_sub.add_parser("inspect", help="Classify the execution route for a career URL.")
    p_ca_insp.add_argument("--url", required=True, help="A trusted official career URL.")
    p_ca_insp.add_argument("--domain", default=None, help="Official domain for trust classification (optional).")
    p_ca_insp.add_argument("--company", default=None)
    p_ca_insp.add_argument("--budget", type=int, default=5)
    p_ca_insp.add_argument("--live", action="store_true", help="Fetch + classify (network).")
    p_ca_insp.add_argument("--json", action="store_true")
    p_ca_insp.set_defaults(func=_cmd_careers)

    p_ca_pilot = careers_sub.add_parser("pilot", help="Run the sealed controlled pilot (DRY-RUN unless --live).")
    p_ca_pilot.add_argument("--config", required=True, help="Pilot config file (YAML/JSON).")
    p_ca_pilot.add_argument("--live", action="store_true", help="Execute the pilot against real official sites.")
    p_ca_pilot.add_argument("--json", action="store_true")
    p_ca_pilot.set_defaults(func=_cmd_careers)

    p_ca_pstat = careers_sub.add_parser("pilot-status", help="Show a sealed pilot run's status/coverage.")
    p_ca_pstat.add_argument("--run-id", required=True)
    p_ca_pstat.add_argument("--json", action="store_true")
    p_ca_pstat.set_defaults(func=_cmd_careers)

    p_ca_resume = careers_sub.add_parser("resume", help="Resume a PARTIAL pilot's pending children (requires --live).")
    p_ca_resume.add_argument("--config", required=True, help="The same sealed pilot config file.")
    p_ca_resume.add_argument("--live", action="store_true")
    p_ca_resume.add_argument("--json", action="store_true")
    p_ca_resume.set_defaults(func=_cmd_careers)

    # -- market: portal discovery + adaptive search campaign (Phase 1D) -------
    p_market = subparsers.add_parser(
        "market",
        help="Market discovery + adaptive search (portals + campaign/waves). Offline/dry-run by default.",
    )
    market_sub = p_market.add_subparsers(dest="market_command", required=True)

    def _add_market_common(p):
        p.add_argument("--lanes", default=None, help="Comma-separated lane keys (default: all policy lanes).")
        p.add_argument("--geographies", default="PRIMARY", help="Comma-separated geography groups.")
        p.add_argument("--portals", default="linkedin,naukri", help="Comma-separated portal families.")
        p.add_argument("--max-waves", dest="max_waves", type=int, default=3)
        p.add_argument("--max-browser", dest="max_browser", type=int, default=40)
        p.add_argument("--max-pages", dest="max_pages", type=int, default=2)
        p.add_argument("--recency-days", dest="recency_days", type=int, default=7)
        p.add_argument("--json", action="store_true")

    p_mk_plan = market_sub.add_parser("plan", help="Seal a campaign + Wave 0 baseline (offline, no network).")
    p_mk_plan.add_argument("--run-id", default=None)
    _add_market_common(p_mk_plan)
    p_mk_plan.set_defaults(func=_cmd_market)

    p_mk_run = market_sub.add_parser("run", help="Run the adaptive market campaign (requires --live).")
    p_mk_run.add_argument("--run-id", default=None)
    p_mk_run.add_argument("--live", action="store_true", help="Perform live portal discovery (network/browser).")
    p_mk_run.add_argument("--resume", action="store_true")
    _add_market_common(p_mk_run)
    p_mk_run.set_defaults(func=_cmd_market)

    p_mk_resume = market_sub.add_parser("resume", help="Resume a PARTIAL market campaign (requires --live).")
    p_mk_resume.add_argument("--run-id", required=True)
    p_mk_resume.add_argument("--live", action="store_true")
    p_mk_resume.add_argument("--json", action="store_true")
    p_mk_resume.set_defaults(func=_cmd_market)

    p_mk_status = market_sub.add_parser("status", help="Show a campaign's waves / leads / deficits / budget.")
    p_mk_status.add_argument("--run-id", required=True)
    p_mk_status.add_argument("--json", action="store_true")
    p_mk_status.set_defaults(func=_cmd_market)

    p_mk_disc = market_sub.add_parser("discovered", help="List dynamically discovered companies (equiv. `companies discovered`).")
    p_mk_disc.add_argument("--run-id", default=None, help="Scope to one run (default: all).")
    p_mk_disc.set_defaults(func=_cmd_market)

    # -- portals: read-only portal health + explicit visible auth ------------
    p_portals = subparsers.add_parser(
        "portals", help="Read-only portal (LinkedIn/Naukri) health + explicit visible sign-in.")
    portals_sub = p_portals.add_subparsers(dest="portals_command", required=True)
    p_po_health = portals_sub.add_parser("health", help="Portal adapter descriptors (offline) or a bounded live probe.")
    p_po_health.add_argument("--family", default=None, help="Comma-separated: linkedin,naukri (live probe).")
    p_po_health.add_argument("--live", action="store_true", help="Bounded live read-only health probe (opt-in).")
    p_po_health.set_defaults(func=_cmd_portals)
    p_po_auth = portals_sub.add_parser("auth", help="Open a VISIBLE Chrome for MANUAL portal sign-in (never automated).")
    p_po_auth.add_argument("--family", default="linkedin", help="linkedin|naukri")
    p_po_auth.add_argument("--profile", default=None, help="Dedicated browser profile dir (optional).")
    p_po_auth.add_argument("--live", action="store_true", help="Actually open the visible window.")
    p_po_auth.set_defaults(func=_cmd_portals)

    # -- outputs: stable production output directory (Phase 1E/F §10) --------
    p_outputs = subparsers.add_parser(
        "outputs", help="Inspect the stable production output directory (runs + latest).")
    outputs_sub = p_outputs.add_subparsers(dest="outputs_command", required=True)
    p_out_latest = outputs_sub.add_parser("latest", help="Show the latest published run pointer.")
    p_out_latest.add_argument("--json", action="store_true")
    p_out_latest.set_defaults(func=_cmd_outputs)
    p_out_list = outputs_sub.add_parser("list", help="List published production runs.")
    p_out_list.add_argument("--json", action="store_true")
    p_out_list.set_defaults(func=_cmd_outputs)
    p_out_show = outputs_sub.add_parser("show", help="Show one run manifest (--run-id).")
    p_out_show.add_argument("--run-id", required=True)
    p_out_show.add_argument("--json", action="store_true")
    p_out_show.set_defaults(func=_cmd_outputs)
    p_out_open = outputs_sub.add_parser("open-latest", help="Open the latest workbook (explicit; requires --live).")
    p_out_open.add_argument("--live", action="store_true")
    p_out_open.set_defaults(func=_cmd_outputs)

    # -- daily: root daily LangGraph run + Windows scheduler (Phase 1E/F §11) -
    p_daily = subparsers.add_parser(
        "daily", help="Daily operation: plan/run/resume/status + safe Windows scheduling.")
    daily_sub = p_daily.add_subparsers(dest="daily_command", required=True)

    p_da_plan = daily_sub.add_parser("plan", help="Plan a daily run (offline, no side effects).")
    p_da_plan.add_argument("--run-id", default=None)
    p_da_plan.add_argument("--json", action="store_true")
    p_da_plan.set_defaults(func=_cmd_daily)

    p_da_run = daily_sub.add_parser("run", help="Run the durable daily graph (requires --live).")
    p_da_run.add_argument("--run-id", default=None)
    p_da_run.add_argument("--live", action="store_true")
    p_da_run.add_argument("--background", action="store_true", help="Headless/background (no interactive prompts).")
    p_da_run.add_argument("--sources", default=None,
                          help="Comma-separated READ-ONLY live portals to seed real leads (e.g. linkedin,naukri).")
    p_da_run.add_argument("--lane", default="JAVA_BACKEND", help="Search lane for live discovery.")
    p_da_run.add_argument("--location", default="India", help="Location filter for live discovery.")
    p_da_run.add_argument("--max-pages", dest="max_pages", type=int, default=2)
    p_da_run.add_argument("--recency-days", dest="recency_days", type=int, default=7)
    p_da_run.add_argument("--official-followup", dest="official_followup", action="store_true",
                          help="Run REAL live portal->official follow-up + linkage for known-source companies.")
    p_da_run.add_argument("--official-config", dest="official_config", default=None,
                          help="Sealed official company config (YAML/JSON) with VERIFIED domains + optional ATS boards.")
    p_da_run.add_argument("--official-companies", dest="official_companies", type=int, default=0,
                          help="Bounded official company subset to cover this run (from registry or capping the config).")
    p_da_run.add_argument("--official-mode", dest="official_mode", default="due",
                          help="Official cadence mode (e.g. 'due'). Informational.")
    p_da_run.add_argument("--official-workers", dest="official_workers", type=int, default=1,
                          help="Bounded parallel official workers (concurrency 1==N).")
    p_da_run.add_argument("--include-portals", dest="include_portals", default=None,
                          help="Comma-separated READ-ONLY supplemental portals (e.g. linkedin,naukri). PORTAL_ONLY.")
    p_da_run.add_argument("--portal-only-debug", dest="portal_only_debug", action="store_true",
                          help="DIAGNOSTIC ONLY: skip official coverage; can NEVER update latest or return PASS.")
    p_da_run.add_argument("--synthetic-candidate", dest="synthetic_candidate", action="store_true",
                          help="Force the synthetic (PII-free) candidate instead of the real PRIVATE_LOCAL candidate.")
    p_da_run.add_argument("--allow-private-candidate-to-copilot", dest="allow_private", action="store_true",
                          help="Explicit consent to send real candidate data to Copilot (default OFF).")
    p_da_run.add_argument("--json", action="store_true")
    p_da_run.set_defaults(func=_cmd_daily)

    p_da_resume = daily_sub.add_parser("resume", help="Resume a PARTIAL/interrupted daily run (requires --live).")
    p_da_resume.add_argument("--run-id", required=True)
    p_da_resume.add_argument("--live", action="store_true")
    p_da_resume.add_argument("--background", action="store_true")
    p_da_resume.add_argument("--sources", default=None)
    p_da_resume.add_argument("--lane", default="JAVA_BACKEND")
    p_da_resume.add_argument("--location", default="India")
    p_da_resume.add_argument("--max-pages", dest="max_pages", type=int, default=2)
    p_da_resume.add_argument("--recency-days", dest="recency_days", type=int, default=7)
    p_da_resume.add_argument("--official-followup", dest="official_followup", action="store_true")
    p_da_resume.add_argument("--official-config", dest="official_config", default=None)
    p_da_resume.add_argument("--official-companies", dest="official_companies", type=int, default=0)
    p_da_resume.add_argument("--official-mode", dest="official_mode", default="due")
    p_da_resume.add_argument("--official-workers", dest="official_workers", type=int, default=1)
    p_da_resume.add_argument("--include-portals", dest="include_portals", default=None)
    p_da_resume.add_argument("--portal-only-debug", dest="portal_only_debug", action="store_true")
    p_da_resume.add_argument("--synthetic-candidate", dest="synthetic_candidate", action="store_true")
    p_da_resume.add_argument("--json", action="store_true")
    p_da_resume.set_defaults(func=_cmd_daily)

    p_da_status = daily_sub.add_parser("status", help="Show a daily run's phase/terminal/manifest status.")
    p_da_status.add_argument("--run-id", required=True)
    p_da_status.add_argument("--json", action="store_true")
    p_da_status.set_defaults(func=_cmd_daily)

    p_da_install = daily_sub.add_parser("install-task", help="Generate/install the Windows daily task (DRY-RUN default).")
    p_da_install.add_argument("--time", required=True, help="Daily start time HH:mm (24h).")
    p_da_install.add_argument("--dry-run", action="store_true", help="Only print the command (default behavior).")
    p_da_install.add_argument("--enable", action="store_true", help="Actually create the scheduled task (explicit).")
    p_da_install.add_argument("--json", action="store_true")
    p_da_install.set_defaults(func=_cmd_daily)

    p_da_disable = daily_sub.add_parser("disable-task", help="Disable the scheduled daily task.")
    p_da_disable.add_argument("--json", action="store_true")
    p_da_disable.set_defaults(func=_cmd_daily)

    p_da_remove = daily_sub.add_parser("remove-task", help="Delete the scheduled daily task.")
    p_da_remove.add_argument("--json", action="store_true")
    p_da_remove.set_defaults(func=_cmd_daily)

    # -- copilot: optional reasoning controller info + synthetic canary -------
    p_copilot = subparsers.add_parser(
        "copilot", help="Optional Copilot SDK reasoning controller: info + synthetic canary.")
    copilot_sub = p_copilot.add_subparsers(dest="copilot_command", required=True)
    p_cp_info = copilot_sub.add_parser("info", help="Show the official SDK pin, license, and consent state.")
    p_cp_info.add_argument("--json", action="store_true")
    p_cp_info.set_defaults(func=_cmd_copilot)
    p_cp_canary = copilot_sub.add_parser("canary", help="Run a SYNTHETIC-data Copilot reasoning canary (requires --live).")
    p_cp_canary.add_argument("--live", action="store_true", help="Actually make the real (synthetic-data) model call.")
    p_cp_canary.add_argument("--timeout", type=int, default=90)
    p_cp_canary.add_argument("--json", action="store_true")
    p_cp_canary.set_defaults(func=_cmd_copilot)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
