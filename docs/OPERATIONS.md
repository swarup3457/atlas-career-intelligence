# Atlas Operations Guide (Phase 0.5)

This document describes how the generic Atlas PLATFORM is operated
today, and how it is intended to be invoked unattended in the future.
Nothing in this file describes job-search business logic - that remains
out of scope until the Workspace Atlas specification is imported.

Status labels used throughout: **PROVEN** (exercised end-to-end and
verified working), **IMPLEMENTED** (built and unit/integration tested but
not yet exercised in a full production scenario), **SCAFFOLDED**
(interface/contract exists, no real implementation yet), **FUTURE**
(intentionally deferred).

## 1. Health check — `atlas doctor` (IMPLEMENTED)

```powershell
C:\Atlas\.venv\Scripts\atlas.exe doctor
```

Runs a comprehensive **offline** check (no external network access) and
prints one `[PASS]`/`[WARN]`/`[FAIL]` line per item, covering:

- Python version and required packages (playwright, langgraph, pydantic,
  PyYAML, openpyxl)
- Configuration loads and validates (`atlas.config.load_settings()`)
- All managed paths exist and are writable (state/checkpoint DB parents,
  output, logs, agents, skills, browser profile parent)
- SQLite state DB initializes and reports its schema version
- LangGraph checkpoint DB opens
- Installed Google Chrome detection (WARN, not FAIL, if not found at a
  common path - `channel="chrome"` launches will simply fail until
  Chrome is installed)
- Browser profile existence (WARN if not yet created - expected on a
  fresh machine)
- Browser-profile lock and run-lock state (WARN if currently held by a
  live PID, or if a stale lock from a dead PID would be reclaimed)

Exit code is `0` unless any check is `FAIL`. Non-critical items WARN
rather than block, per spec section 20 ("do not make non-critical
warnings stop Atlas unnecessarily").

`atlas doctor` never browses external websites.

## 2. Status — `atlas status` (IMPLEMENTED)

Reports current run-lock holder (if any), state DB path + schema
version, checkpoint DB path, browser profile path, and configured
controller. Read-only; does not acquire or modify any lock.

## 3. Resume — `atlas resume` (SCAFFOLDED)

Reports the most recent `continuation` records found in the state DB
(run_id, thread_id, remaining/completed item lists, updated_at). Does
**not** restart a business-logic run - no production run engine exists
yet (Phase 0.5 is platform hardening only). This command exists so a
future scheduler/operator can inspect resumability before Phase 1 wires
an actual production run loop to it.

## 4. Single-run guard (IMPLEMENTED — `atlas.orchestration.run_lock.RunLock`)

Before any future production run starts, it must call
`RunLock(state_dir).try_acquire(run_id=...)`. If another run is already
active, the result's `.status` is `RUN_ALREADY_ACTIVE` and the caller
MUST NOT start a second production run - see
`docs/FAILURE_RECOVERY.md` for the full crash/stale-lock recovery
semantics.

## 5. Browser profile lock (PROVEN — `atlas.browser.manager.BrowserManager`)

Every `BrowserManager(profile_dir, channel=...)` acquires a PID-aware
lock over the persistent Chrome profile directory before launching, and
releases it on `close()`/context-manager exit. See
`docs/FAILURE_RECOVERY.md` for stale-lock recovery.

## 6. Graceful shutdown (IMPLEMENTED — `atlas.orchestration.shutdown.GracefulShutdown`)

Wraps a future run loop:

```python
from atlas.orchestration.shutdown import GracefulShutdown

shutdown = GracefulShutdown()
shutdown.register(lambda: checkpointer_flush())
shutdown.register(lambda: browser_manager.close())
shutdown.register(lambda: run_lock.release())

with shutdown:
    while not shutdown.requested and remaining_items:
        ...  # process one item, checkpoint after each
```

Ctrl+C (SIGINT) and Ctrl+Break (SIGBREAK) are reliably delivered on
Windows; SIGTERM is registered but Windows cannot reliably deliver it
externally (only via in-process `os.kill`/`signal.raise_signal`).
Cleanup callbacks run in LIFO order and tolerate individual failures.

## 7. Structured logging (PROVEN — `atlas.utils.logging`)

`configure_logging(logs_dir)` writes rotating JSON-lines logs to
`logs/atlas.log` plus a plain console stream. `log_task_event(...)`
emits the standard task-event field set. Sensitive-looking keys
(password, token, cookie, auth, credential, api_key, ...) are redacted
automatically as a safety net - callers must still never pass secrets in.

## 8. Structured event bus + metrics (IMPLEMENTED — spec sections 12/13)

`atlas.orchestration.events.EventBus` publishes generic runtime events
(`RUN_STARTED`, `TASK_STARTED`, `TASK_RETRY`, `TASK_COMPLETED`,
`TASK_FAILED`, `ACCESS_LIMITED`, `HUMAN_INTERVENTION_REQUIRED`,
`HUMAN_INTERVENTION_RESOLVED`, `CHECKPOINT_SAVED`, `RUN_PARTIAL`,
`RUN_COMPLETED`) with an optional JSON-lines file sink.
`atlas.orchestration.metrics.RunMetrics` can subscribe to a bus
(`metrics.observe_event`) to maintain planned/attempted/completed/
remaining/retry/access_limited/failed counts and elapsed time. This is
LOCAL runtime architecture, distinct from the future GitHub persistence
event schema - useful today for logging/metrics, later for GitHub export
and Excel run summaries.

## 9. Human intervention queue (IMPLEMENTED — spec section 17)

`atlas.browser.intervention.InterventionQueue` serializes multiple
pending intervention requests (LOGIN_REQUIRED, MFA_REQUIRED,
CAPTCHA_PRESENT, SESSION_EXPIRED) so **at most one visible Chrome window**
is ever open for human intervention at a time; other blocked tasks remain
checkpointed rather than each popping their own window.

## Windows Task Scheduler preparation (FUTURE — documented only, spec section 21)

No scheduled task is created yet. When the search engine exists, the
stable entry point for Task Scheduler will be:

```
Program:    C:\Atlas\.venv\Scripts\python.exe
Arguments:  -m atlas.cli run        (future subcommand, not yet implemented)
Start in:   C:\Atlas
```

The future scheduled invocation must, in order:

1. Start Atlas (`atlas doctor` first, non-fatal on WARN, fatal on FAIL).
2. Attempt `RunLock.try_acquire(run_id=<timestamp>)`; if
   `RUN_ALREADY_ACTIVE`, exit immediately with a distinct, documented exit
   code and do not start a second run.
3. Resume from the last checkpoint if one exists (`atlas resume` already
   reports what would be resumed).
4. Write logs via `atlas.utils.logging.configure_logging`.
5. Return a meaningful process exit code (0 = success/complete, non-zero
   = failure categories to be defined alongside the real run engine).

Task Scheduler itself is intentionally NOT configured in this phase -
"Do not schedule production before the search engine exists."

## Known limitations

- `atlas resume` is report-only; no production run loop exists to
  actually resume yet.
- `atlas doctor`'s installed-Chrome detection only checks two common
  Windows install paths; a non-standard Chrome install location will WARN
  even though `channel="chrome"` may still work (Playwright's own launch
  is the real authority).
- SIGTERM cannot be reliably delivered to a Windows process externally;
  Ctrl+C/Ctrl+Break are the dependable local signals.
