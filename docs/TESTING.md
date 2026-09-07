# Atlas Testing Guide (Phase 0.5)

## Two kinds of tests in this repository

1. **Standalone regression scripts** (Phase 0, preserved as-is) - run
   directly with the venv Python, print PASS/FAIL lines, exit non-zero on
   failure. These remain the authoritative full end-to-end regression
   harnesses:
   - `tests/test_foundation_suite.py`
   - `tests/langgraph_reliability.py` (+ `_stage1`/`_stage2`/`_graph` helpers)
   - `tests/real_web_orchestration_run.py` (+ `_graph` helper)
   - `tests/test_headless_profile_diagnostic.py`
   - `tests/playwright_smoke.py`, `tests/playwright_chrome_linkedin_diag.py`,
     `tests/playwright_linkedin_session_persistence.py` (manual/diagnostic,
     opens a visible browser - not run by CI/automated suites)

2. **pytest modules** (Phase 0.5, new) - `tests/test_*.py`, run via
   `python -m pytest`. These provide granular, markered coverage of the
   individual platform components.

## Running the pytest suite

```powershell
# Default: offline only (real_web tests are excluded automatically by
# pyproject.toml's addopts = -m "not real_web")
C:\Atlas\.venv\Scripts\python.exe -m pytest

# Explicitly run only the real_web-marked tests (opens a real headless
# Chrome, navigates to https://example.com over the real network):
C:\Atlas\.venv\Scripts\python.exe -m pytest -m real_web

# Run a single file:
C:\Atlas\.venv\Scripts\python.exe -m pytest tests/test_config.py -v
```

Or via the Atlas CLI: `atlas test` / `atlas test --real-web`.

## Markers (defined in `pyproject.toml`)

| Marker        | Meaning                                                              |
|---------------|-----------------------------------------------------------------------|
| `unit`        | Fast, fully offline, no subprocess, no real browser.                  |
| `integration` | Offline but exercises multiple components together (e.g. subprocess). |
| `real_web`    | Opens a real browser and/or makes a real network request. Excluded by default. |
| `browser`     | Launches a real Playwright/Chrome browser (always combined with `real_web` today). |
| `slow`        | Takes noticeably longer than the rest of the suite.                   |

`--strict-markers` is enabled, so an unregistered marker name is a hard
error - this prevents marker typos from silently being ignored.

## Test inventory (pytest modules)

| File                                  | Marker(s)             | Covers |
|----------------------------------------|------------------------|--------|
| `test_config.py`                       | unit                   | Settings loading, override precedence, `validate()` fail-fast with ALL problems listed |
| `test_models.py`                       | unit                   | Terminal states, human-intervention states, EXTRACTION_UNRESOLVED != NO_RELEVANT_RESULTS |
| `test_retry.py`                        | unit                   | Retry budget enforcement, never-retry categories, selector-uncertainty handling |
| `test_state_store.py`                  | unit                   | StateStore migrations/schema version, WAL/busy_timeout pragmas, run/task/attempt/continuation round-trip |
| `test_checkpoints.py`                  | unit                   | LangGraph SqliteSaver open/close, thread_config shape, checkpoint persists across reopen |
| `test_agents_loader.py`                | unit                   | Agent/skill spec loading + malformed-spec rejection |
| `test_browser_manager.py`              | real_web, browser, slow| BrowserManager headless launch + navigation observability (`NavigationRecord`) |
| `test_pidlock.py`                      | unit                   | PidLock acquire/release, live-lock blocking, stale-lock reclaim, corrupt-file fallback, no cross-PID deletion |
| `test_run_lock.py`                     | unit                   | RunLock RUN_ALREADY_ACTIVE / RUN_LOCK_ACQUIRED, stale-run recovery, read-only current_holder() |
| `test_graceful_shutdown.py`            | unit                   | LIFO cleanup ordering, idempotent trigger, one broken callback doesn't block others, signal wiring |
| `test_events_metrics.py`               | unit                   | EventBus publish/subscribe/sink, unknown event type rejection, RunMetrics counters |
| `test_intervention.py`                 | unit                   | InterventionQueue dedup, serialized single-visible-session guarantee (monkeypatched, no real browser) |
| `test_worker_contract.py`              | unit                   | BaseWorker ABC enforcement, TaskResult typed shape (no free-form completion) |
| `test_source_adapter_contract.py`      | unit                   | BaseSource ABC enforcement (discover/search/fetch_detail/health_check) |
| `test_controller_boundary.py`          | unit                   | NullController-only deterministic graph run - no Copilot/Codex/OpenAI dependency |
| `test_logging.py`                      | unit                   | Structured JSON-lines log output, secret redaction |
| `test_secret_hygiene.py`               | unit                   | `.gitignore` covers every known sensitive runtime path |
| `test_crash_recovery.py`               | integration            | Full crash/restart cycle: run-lock recovery, checkpoint recovery, no repeated tasks, resumed completion |

## What "no internet required" means here

The default `python -m pytest` run (82 tests as of this writing) never
opens a browser, never makes an outbound HTTP request, and never depends
on Chrome being reachable on the network. Only `test_browser_manager.py`
(explicitly `real_web`) does that, and it is excluded unless
`-m real_web` is passed.

## Adding a new pytest test

1. Put it in `tests/test_<topic>.py`.
2. Mark it with the narrowest applicable marker(s)
   (`pytestmark = pytest.mark.unit` at module level, or per-test
   `@pytest.mark.integration`).
3. Never touch `C:\Atlas\.browser-profile-chrome` (the real authenticated
   profile) or `C:\Atlas\state\atlas_state.sqlite` (the real state DB) -
   always use `tmp_path`.
4. If it needs a real browser or network, mark it `real_web` (+ `browser`
   and/or `slow` as appropriate) so it stays out of the default run.
