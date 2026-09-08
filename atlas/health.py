"""Atlas offline health check ("doctor").

Produces a concise PASS/WARN/FAIL report covering the platform
foundation, entirely offline (no external network access) unless a
caller explicitly opts into a network check. Intended to run before any
future scheduled production execution (see docs/OPERATIONS.md).

Non-critical items WARN rather than FAIL, so a healthy-but-imperfect
machine state does not unnecessarily block Atlas from running.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from atlas.config import ConfigValidationError, Settings, load_settings
from atlas.utils.procutil import is_pid_running

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_REQUIRED_PACKAGES = ("playwright", "langgraph", "pydantic", "yaml", "openpyxl")

# Common installed-Chrome locations on Windows (used only to give a more
# useful WARN message - Playwright's `channel="chrome"` launch is the
# real authority on whether Chrome is usable).
_COMMON_CHROME_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str = ""


@dataclass
class HealthReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if any(r.status == FAIL for r in self.results):
            return FAIL
        if any(r.status == WARN for r in self.results):
            return WARN
        return PASS

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(CheckResult(name=name, status=status, detail=detail))

    def render(self) -> str:
        lines = [f"[{r.status}] {r.name}" + (f" - {r.detail}" if r.detail else "") for r in self.results]
        lines.append(f"\nOVERALL: {self.overall}")
        return "\n".join(lines)


def _check_python(report: HealthReport) -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 12):
        report.add("Python version", PASS, f"{sys.version.split()[0]}")
    else:
        report.add("Python version", WARN, f"{sys.version.split()[0]} (Atlas targets 3.12+)")


def _check_packages(report: HealthReport) -> None:
    for pkg in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
            report.add(f"Package: {pkg}", PASS)
        except ImportError as exc:
            report.add(f"Package: {pkg}", FAIL, str(exc))


def _check_config(report: HealthReport) -> Settings | None:
    try:
        settings = load_settings()
        report.add("Configuration loads and validates", PASS, f"controller={settings.controller}")
        return settings
    except ConfigValidationError as exc:
        report.add("Configuration loads and validates", FAIL, str(exc))
        return None


def _check_paths(report: HealthReport, settings: Settings) -> None:
    for label, path in (
        ("state_db parent", settings.state_db.parent),
        ("checkpoint_db parent", settings.checkpoint_db.parent),
        ("output_dir", settings.output_dir),
        ("logs_dir", settings.logs_dir),
        ("agents_dir", settings.agents_dir),
        ("skills_dir", settings.skills_dir),
        ("browser_profile parent", settings.browser_profile.parent),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".atlas-doctor-write-check.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            report.add(f"Path writable: {label}", PASS, str(path))
        except OSError as exc:
            report.add(f"Path writable: {label}", FAIL, f"{path}: {exc}")


def _check_state_db(report: HealthReport, settings: Settings) -> None:
    try:
        from atlas.persistence.sqlite import StateStore

        with StateStore(settings.state_db) as store:
            report.add("SQLite state DB initializes", PASS, f"schema_version={store.schema_version()}")
    except Exception as exc:  # noqa: BLE001
        report.add("SQLite state DB initializes", FAIL, str(exc))


def _check_checkpoint_db(report: HealthReport, settings: Settings) -> None:
    try:
        from atlas.orchestration.checkpoints import open_checkpointer

        with open_checkpointer(settings.checkpoint_db):
            pass
        report.add("LangGraph checkpoint DB opens", PASS, str(settings.checkpoint_db))
    except Exception as exc:  # noqa: BLE001
        report.add("LangGraph checkpoint DB opens", FAIL, str(exc))


def _check_chrome(report: HealthReport) -> None:
    found = [p for p in _COMMON_CHROME_PATHS if p.exists()]
    if found:
        report.add("Installed Google Chrome detected", PASS, str(found[0]))
    else:
        report.add(
            "Installed Google Chrome detected",
            WARN,
            "Not found at common install paths - channel=\"chrome\" launches will fail "
            "until Chrome is installed (see docs/BROWSER_POLICY.md).",
        )


def _check_playwright_driver(report: HealthReport) -> None:
    try:
        import playwright

        report.add("Playwright package importable", PASS, getattr(playwright, "__version__", "unknown version"))
    except ImportError as exc:
        report.add("Playwright package importable", FAIL, str(exc))


def _check_browser_profile(report: HealthReport, settings: Settings) -> None:
    profile = settings.browser_profile
    if profile.exists():
        report.add("Atlas browser profile exists", PASS, str(profile))
    else:
        report.add(
            "Atlas browser profile exists",
            WARN,
            f"{profile} does not exist yet - will be created on first browser launch.",
        )


def _check_locks(report: HealthReport, settings: Settings) -> None:
    from atlas.utils.pidlock import PidLock

    profile_lock = PidLock(settings.browser_profile / ".atlas-browser-manager.lock")
    info = profile_lock.read_info()
    if info is None:
        report.add("Browser profile lock", PASS, "not held")
    elif is_pid_running(info.pid):
        report.add(
            "Browser profile lock",
            WARN,
            f"held by live PID {info.pid} (another Atlas browser session is active)",
        )
    else:
        report.add(
            "Browser profile lock",
            WARN,
            f"stale lock from dead PID {info.pid} - will be reclaimed automatically",
        )

    run_lock = PidLock(settings.state_db.parent / "atlas_run.lock")
    run_info = run_lock.read_info()
    if run_info is None:
        report.add("Run lock", PASS, "no active run")
    elif is_pid_running(run_info.pid):
        report.add(
            "Run lock",
            WARN,
            f"RUN_ALREADY_ACTIVE - PID {run_info.pid}, run_id={run_info.metadata.get('run_id')}, "
            f"started_at={run_info.acquired_at}",
        )
    else:
        report.add("Run lock", WARN, f"stale run lock from dead PID {run_info.pid} - will be reclaimed automatically")


def _check_runtime_shell(report: HealthReport, settings: Settings) -> None:
    """Phase 0.75 runtime-shell readiness checks: run lock, checkpoint
    backend, SQLite schema, worker registry, report output, controller
    configuration. Never browses the web."""
    try:
        from atlas.orchestration.run_lock import RunLock

        RunLock(settings.state_db.parent)
        report.add("Runtime: run lock backend", PASS, str(settings.state_db.parent / "atlas_run.lock"))
    except Exception as exc:  # noqa: BLE001
        report.add("Runtime: run lock backend", FAIL, str(exc))

    try:
        from atlas.orchestration.checkpoints import open_checkpointer

        with open_checkpointer(settings.checkpoint_db):
            pass
        report.add("Runtime: checkpoint backend", PASS, str(settings.checkpoint_db))
    except Exception as exc:  # noqa: BLE001
        report.add("Runtime: checkpoint backend", FAIL, str(exc))

    try:
        from atlas.persistence.sqlite import StateStore

        with StateStore(settings.state_db) as store:
            version = store.schema_version()
        report.add("Runtime: SQLite state schema", PASS, f"schema_version={version}")
    except Exception as exc:  # noqa: BLE001
        report.add("Runtime: SQLite state schema", FAIL, str(exc))

    try:
        from atlas.runtime.demo_workload import DemoWorker, build_demo_failure_injector
        from atlas.workers.base import BaseWorker

        worker = DemoWorker(build_demo_failure_injector())
        if isinstance(worker, BaseWorker):
            report.add("Runtime: worker registry (demo worker)", PASS, worker.name)
        else:
            report.add("Runtime: worker registry (demo worker)", FAIL, "DemoWorker is not a BaseWorker")
    except Exception as exc:  # noqa: BLE001
        report.add("Runtime: worker registry (demo worker)", FAIL, str(exc))

    try:
        probe = settings.output_dir / ".atlas-doctor-runtime-report-check.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add("Runtime: report output writable", PASS, str(settings.output_dir))
    except OSError as exc:
        report.add("Runtime: report output writable", FAIL, str(exc))

    if settings.controller in ALLOWED_CONTROLLERS_FOR_RUNTIME:
        report.add("Runtime: controller configuration", PASS, settings.controller)
    else:
        report.add("Runtime: controller configuration", WARN, f"unexpected controller value: {settings.controller}")


ALLOWED_CONTROLLERS_FOR_RUNTIME = frozenset({"none", "copilot", "codex"})


def _check_source_framework(report: HealthReport, settings: Settings) -> None:
    """Phase 1A source-engine readiness checks (all offline): registry,
    descriptors, source config validation, rate-limiter config, coverage
    schema, evidence store, and the adapter-contract harness."""
    try:
        from atlas.sources.registry import default_registry

        registry = default_registry()
        types = registry.registered_types()
        descriptors = registry.describe()
        # Duplicate registration is impossible by construction; assert the
        # descriptor list is 1:1 with the registered types (deterministic).
        if len(descriptors) == len(types):
            report.add(
                "Sources: registry + descriptors",
                PASS,
                f"{len(types)} registered adapter(s) (0 real adapters expected in Phase 1A)",
            )
        else:
            report.add("Sources: registry + descriptors", FAIL, "descriptor/type count mismatch")
    except Exception as exc:  # noqa: BLE001
        report.add("Sources: registry + descriptors", FAIL, str(exc))

    try:
        from atlas.sources.config import demo_source_config

        cfg = demo_source_config()
        report.add("Sources: config validation", PASS, f"demo config: {len(cfg.instances)} instance(s)")
    except Exception as exc:  # noqa: BLE001
        report.add("Sources: config validation", FAIL, str(exc))

    try:
        from atlas.sources.rate_limit import RatePolicy

        RatePolicy().validate()
        report.add("Sources: rate limiter config", PASS)
    except Exception as exc:  # noqa: BLE001
        report.add("Sources: rate limiter config", FAIL, str(exc))

    try:
        from atlas.persistence.sqlite import StateStore

        with StateStore(settings.state_db) as store:
            version = store.schema_version()
        if version >= 4:
            report.add("Sources: coverage/health schema", PASS, f"schema_version={version}")
        else:
            report.add("Sources: coverage/health schema", FAIL, f"schema_version={version} (<4)")
    except Exception as exc:  # noqa: BLE001
        report.add("Sources: coverage/health schema", FAIL, str(exc))

    try:
        from atlas.sources.evidence import EvidenceStore

        EvidenceStore()
        report.add("Sources: evidence store", PASS)
    except Exception as exc:  # noqa: BLE001
        report.add("Sources: evidence store", FAIL, str(exc))

    try:
        from atlas.sources.testing.contract import run_contract_checks  # noqa: F401

        report.add("Sources: adapter contract harness", PASS)
    except Exception as exc:  # noqa: BLE001
        report.add("Sources: adapter contract harness", FAIL, str(exc))

    # Phase 1C-A: the four official ATS adapters are importable and register
    # into an explicit registry with NO import-time network and NO enabled
    # instances (doctor stays fully offline).
    try:
        from atlas.sources.ats import ATS_FAMILIES, build_ats_registry, describe_ats_adapters

        registry = build_ats_registry()
        descriptors = describe_ats_adapters()
        families = registry.registered_families()
        if len(descriptors) == 4 and len(families) == 4 and len(ATS_FAMILIES) == 4:
            report.add(
                "Sources: official ATS adapters",
                PASS,
                "4 registered (greenhouse/lever/ashby/workday), read-only, disabled unless configured",
            )
        else:
            report.add("Sources: official ATS adapters", FAIL,
                       f"expected 4 ATS adapters, found {len(descriptors)}")
    except Exception as exc:  # noqa: BLE001
        report.add("Sources: official ATS adapters", FAIL, str(exc))

    # Phase 1C-B: the two generic official-career adapters (HTTP + browser) are
    # importable and register alongside the four ATS adapters in one careers
    # registry, with NO import-time network/browser and NO enabled instances.
    try:
        from atlas.sources.generic import (
            GENERIC_CAREER_FAMILIES,
            build_careers_registry,
            describe_generic_career_adapters,
        )

        registry = build_careers_registry()
        generic = describe_generic_career_adapters()
        families = registry.registered_families()
        if len(generic) == 2 and len(GENERIC_CAREER_FAMILIES) == 2 and len(families) == 6:
            report.add(
                "Sources: generic career adapters",
                PASS,
                "2 registered (company_career HTTP + company_career_browser), read-only, "
                "6 total families in careers registry",
            )
        else:
            report.add("Sources: generic career adapters", FAIL,
                       f"expected 2 generic + 6 total families, found {len(generic)}/{len(families)}")
    except Exception as exc:  # noqa: BLE001
        report.add("Sources: generic career adapters", FAIL, str(exc))


def _check_company_registry(report: HealthReport, settings: Settings) -> None:
    """Phase 1A.5 company/source registry integrity checks (all offline):
    schema, orphan relationships/aliases, duplicate alias/source identities,
    invalid domains, and the fingerprint framework."""
    try:
        from atlas.persistence.sqlite import StateStore

        with StateStore(settings.state_db) as store:
            version = store.schema_version()
            if version >= 5:
                report.add("Company: registry schema", PASS, f"schema_version={version}")
            else:
                report.add("Company: registry schema", FAIL, f"schema_version={version} (<5)")

            conn = store._conn  # read-only integrity queries
            orphan_rels = conn.execute(
                "SELECT COUNT(*) AS n FROM company_source_relationships r "
                "WHERE NOT EXISTS (SELECT 1 FROM company_registry c WHERE c.company_id = r.company_id)"
            ).fetchone()["n"]
            orphan_aliases = conn.execute(
                "SELECT COUNT(*) AS n FROM company_aliases a "
                "WHERE NOT EXISTS (SELECT 1 FROM company_registry c WHERE c.company_id = a.company_id)"
            ).fetchone()["n"]
            if orphan_rels == 0 and orphan_aliases == 0:
                report.add("Company: relationship integrity", PASS, "no orphan relationships/aliases")
            else:
                report.add("Company: relationship integrity", FAIL,
                           f"orphan relationships={orphan_rels}, orphan aliases={orphan_aliases}")

            dup_alias = conn.execute(
                "SELECT COUNT(*) AS n FROM (SELECT alias_key FROM company_aliases "
                "GROUP BY alias_key HAVING COUNT(DISTINCT company_id) > 1)"
            ).fetchone()["n"]
            dup_source = conn.execute(
                "SELECT COUNT(*) AS n FROM (SELECT instance_id FROM company_source_relationships "
                "GROUP BY instance_id HAVING COUNT(DISTINCT company_id) > 1)"
            ).fetchone()["n"]
            if dup_alias == 0 and dup_source == 0:
                report.add("Company: no duplicate alias/source identities", PASS)
            else:
                report.add("Company: no duplicate alias/source identities", WARN,
                           f"ambiguous alias_keys={dup_alias}, shared instance_ids={dup_source}")

            bad_domains = conn.execute(
                "SELECT COUNT(*) AS n FROM company_registry "
                "WHERE official_domain IS NOT NULL AND official_domain NOT LIKE '%.%'"
            ).fetchone()["n"]
            if bad_domains == 0:
                report.add("Company: domains valid", PASS)
            else:
                report.add("Company: domains valid", WARN, f"{bad_domains} company domain(s) look invalid")
    except Exception as exc:  # noqa: BLE001
        report.add("Company: registry schema", FAIL, str(exc))

    try:
        from atlas.sources.fingerprint import fingerprint_ats  # noqa: F401
        from atlas.company.tenant import extract_tenant  # noqa: F401

        report.add("Company: ATS fingerprint framework", PASS)
    except Exception as exc:  # noqa: BLE001
        report.add("Company: ATS fingerprint framework", FAIL, str(exc))


def _check_policy_and_production(report: HealthReport) -> None:
    """Phase 1B: validate the typed policy bundle, the report mapping, and
    that the production runtime imports (no live adapters claimed)."""
    try:
        from atlas.policy import load_policy

        bundle = load_policy()
        report.add(
            "Policy: bundle loads and validates",
            PASS,
            f"{len(bundle.lanes)} lanes, {len(bundle.company_seed.companies)} seed companies, "
            f"fp={bundle.short_fingerprint}",
        )
        if any(e.live_adapter for e in bundle.source_policy.entries):
            report.add("Policy: no live source adapters", FAIL, "a source declares a live adapter")
        else:
            report.add("Policy: no live source adapters", PASS, "Phase 1B ships no live adapters")
    except Exception as exc:  # noqa: BLE001
        report.add("Policy: bundle loads and validates", FAIL, str(exc))

    try:
        from atlas.reporting.mapping import REQUIRED_SHEETS, load_report_mapping

        mapping = load_report_mapping()
        missing = [s for s in REQUIRED_SHEETS if s not in mapping.sheets]
        if missing:
            report.add("Report: 8-sheet mapping", FAIL, f"missing sheets: {missing}")
        else:
            report.add("Report: 8-sheet mapping", PASS, f"{len(mapping.sheets)} sheets mapped (report-only)")
    except Exception as exc:  # noqa: BLE001
        report.add("Report: 8-sheet mapping", FAIL, str(exc))

    try:
        from atlas.orchestration.production_state import PRODUCTION_PHASE_ORDER
        from atlas.runtime.production import ProductionSearchRuntime  # noqa: F401

        report.add(
            "Production: multi-phase graph importable",
            PASS,
            f"{len(PRODUCTION_PHASE_ORDER)} phases (search-first isolation)",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("Production: multi-phase graph importable", FAIL, str(exc))


def run_doctor(check_network: bool = False) -> HealthReport:
    """Run the full offline health check. `check_network` is accepted for
    forward-compatibility but is NOT used to browse external websites in
    this build - Atlas doctor never browses the internet unless a future,
    explicitly-requested real-web check is added."""
    report = HealthReport()
    _check_python(report)
    _check_packages(report)
    _check_playwright_driver(report)
    _check_chrome(report)
    settings = _check_config(report)
    if settings is not None:
        settings.ensure_directories()
        _check_paths(report, settings)
        _check_state_db(report, settings)
        _check_checkpoint_db(report, settings)
        _check_browser_profile(report, settings)
        _check_locks(report, settings)
        _check_runtime_shell(report, settings)
        _check_source_framework(report, settings)
        _check_company_registry(report, settings)
        _check_policy_and_production(report)
    return report
