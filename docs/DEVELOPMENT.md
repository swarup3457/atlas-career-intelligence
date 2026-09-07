# Atlas Development Guide (Foundation Build + Phase 0.5 Hardening)

## Environment

- Project root: `C:\Atlas`
- Python virtual environment: `C:\Atlas\.venv` (Python 3.12)
- Activate for interactive use: `C:\Atlas\.venv\Scripts\Activate.ps1`
- Or invoke directly without activating:
  `C:\Atlas\.venv\Scripts\python.exe <script>`

## Key installed packages

- `playwright` 1.62.0 (+ installed Google Chrome, `channel="chrome"`)
- `langgraph` 1.2.11
- `langgraph-checkpoint-sqlite` 3.1.1
- `pydantic` 2.13.5
- `PyYAML` 6.0.3 (used by `atlas/agents_loader.py` and `atlas/config`)
- `openpyxl` 3.1.5 (used by `atlas/reporting/excel.py`)
- `pytest` 9.1.1 **[PHASE 0.5]** (offline unit/integration test runner)

## Import convention **[PHASE 0.5: CHANGED]**

Atlas is now installed as an editable package. From `C:\Atlas`, run:

```powershell
C:\Atlas\.venv\Scripts\python.exe -m pip install -e .
```

After that, `import atlas` works from ANY working directory without any
`sys.path` manipulation - this is verified by the pytest suite and by the
`atlas` console-script entry point (`atlas version`, `atlas doctor`, ...).
The old `sys.path.insert(0, str(Path(r"C:\Atlas")))` convention used by
the original `tests/*.py` standalone scripts still works and those files
are unchanged, but new code should rely on the installed package instead.

## Running tests **[PHASE 0.5: CHANGED]**

Atlas now uses **pytest** for all new coverage, configured in
`pyproject.toml`. The default `python -m pytest` invocation excludes
`real_web`-marked tests (see `addopts = -m "not real_web"`), so the
normal offline suite never opens a browser or touches the network.

```powershell
# Offline suite (config, models, retry, state store, checkpoints,
# agents/skills loader, pidlock, run lock, graceful shutdown, events,
# metrics, worker/source contracts, intervention queue, controller
# boundary, secret hygiene, crash recovery) - no internet required.
C:\Atlas\.venv\Scripts\python.exe -m pytest

# Real-web tests only (opens a real headless Chrome, navigates to
# https://example.com) - must be explicitly requested.
C:\Atlas\.venv\Scripts\python.exe -m pytest -m real_web

# Equivalent via the Atlas CLI:
C:\Atlas\.venv\Scripts\atlas.exe test
C:\Atlas\.venv\Scripts\atlas.exe test --real-web
```

See `docs/TESTING.md` for the full marker reference and test inventory.

The ORIGINAL Phase 0 standalone regression scripts are preserved as-is
(not pytest-based, run directly with the venv Python) and remain the
authoritative end-to-end regression harnesses:

```powershell
# Foundation test suite (config, agents/skills, sqlite, retry, terminal
# states, BrowserManager, headless mode, visible escalation, checkpoint/resume)
C:\Atlas\.venv\Scripts\python.exe tests\test_foundation_suite.py

# Regression: prior LangGraph checkpoint/restart/retry reliability test
C:\Atlas\.venv\Scripts\python.exe tests\langgraph_reliability.py

# Regression: prior real-web LangGraph+Playwright orchestration test
C:\Atlas\.venv\Scripts\python.exe tests\real_web_orchestration_run.py

# Diagnostic: headless mode safety with the real authenticated profile
C:\Atlas\.venv\Scripts\python.exe tests\test_headless_profile_diagnostic.py
```

## Atlas CLI **[PHASE 0.5: NEW]**

```powershell
atlas doctor    # offline health check (PASS/WARN/FAIL), see docs/OPERATIONS.md
atlas status    # report current run/lock/checkpoint state
atlas resume    # report what a resume would continue (reports only - no
                # production run engine exists yet)
atlas test      # run the offline pytest suite (add --real-web for real_web tests)
atlas version   # print Atlas + key dependency versions
```

`atlas search` is intentionally NOT implemented - business-logic search
is out of scope until the Workspace Atlas specification is imported.

## Configuration

`atlas/config/__init__.py` resolves settings with this precedence (lowest
to highest):

1. Hardcoded defaults in `Settings`
2. `config/default.yaml` (checked into git — safe defaults for this
   machine)
3. `config/local.yaml` (git-ignored; copy from `config/local.yaml.example`
   to override locally)
4. Environment variables named `ATLAS_<FIELD_NAME_UPPER>`
   (e.g. `ATLAS_BROWSER_CHANNEL=chrome`)
5. Explicit keyword overrides passed to `load_settings(**overrides)`

Call `atlas.config.get_settings()` for a cached singleton, or
`atlas.config.reset_settings_cache()` in tests that need to reload after
changing environment variables.

## Project layout

```
C:\Atlas\
  atlas\            Python package: config, models, orchestration,
                    controllers, browser, workers, sources, persistence,
                    reporting, utils, cli.py, health.py
  agents\           *.agent.md placeholder specs (metadata only)
  skills\           <name>/SKILL.md placeholder specs (metadata only)
  config\           default.yaml (checked in), local.yaml.example
  state\            SQLite databases + run lock file (git-ignored)
  output\           JSON/report artifacts (git-ignored)
  logs\             structured log files (git-ignored)
  tests\            standalone regression scripts + pytest test_*.py modules
  docs\             this file + ARCHITECTURE/BROWSER_POLICY/STATE_MODEL/
                    CONTROLLER_ABSTRACTION/AGENT_SKILL_MIGRATION/
                    OPERATIONS/TESTING/FAILURE_RECOVERY
  pyproject.toml    packaging + pytest configuration [PHASE 0.5]
  .browser-profile-chrome\   dedicated persistent Chrome profile (git-ignored)
```

## Adding a new worker

1. Subclass `atlas.workers.base.BaseWorker`, implement
   `attempt(self, item, attempt_number) -> WorkerOutcome`.
2. Never implement retry loops inside the worker — raise
   `atlas.workers.base.WorkerError(category, message)` on failure and let
   the governor apply `atlas.orchestration.retry.evaluate()`.
3. Only use `atlas.browser.manager.BrowserManager` for browser access —
   never launch Playwright/Chrome directly inside a worker.
4. Add the worker to `atlas/orchestration/graph.py`'s
   `build_graph(worker, retry_budget)` caller, or create a dedicated graph
   builder call site for it.
5. **[PHASE 0.5]** The governor now assembles a formal
   `atlas.workers.base.TaskResult` (task_id/status/started_at/completed_at/
   attempt/data/error_category/error_detail/next_action) for every
   completed item - never communicate completion via free-form text.

## Safety invariants to preserve in all future work

- No credentials in source code or logs (`atlas/utils/logging.py`
  redacts common secret-like keys as a safety net, but never rely on
  that alone — do not log secrets in the first place).
- No production GitHub writes from `atlas/persistence/github.py` (it
  raises `NotImplementedError` unless `dry_run=True`).
- No CAPTCHA/MFA/anti-bot bypass logic anywhere.
- `EXTRACTION_UNRESOLVED` must never be silently reported as
  `NO_RELEVANT_RESULTS`.
- Retry budgets are always finite; `CAPTCHA`/`MFA`/`ANTI_BOT`/`LOGIN_WALL`
  are never retried.
- **[PHASE 0.5]** Never delete a browser-profile lock or run lock that is
  owned by a live PID (see `atlas/utils/pidlock.py`); only stale
  (dead-PID) locks are ever reclaimed.
