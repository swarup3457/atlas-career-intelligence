# Atlas Runtime Shell (Phase 0.75)

> **NO PRODUCTION JOB SEARCH EXISTS YET.** This document describes ONLY
> the generic, production-shaped runtime shell that wires together the
> already-proven Phase 0 / Phase 0.5 platform components using
> deterministic fake/demo workers. There is no ATS/portal connector, no
> candidate matching, no production GitHub write path, and no final
> Excel business schema. `atlas run` supports only `--demo` in this
> build.

## Scope

Phase 0.75 wires the following already-proven components into one
cohesive runtime (`atlas.runtime.engine.AtlasRuntime`):

- configuration (`atlas.config`)
- run lock (`atlas.orchestration.run_lock.RunLock`)
- LangGraph governor (`atlas.orchestration.graph.build_graph`)
- SQLite durable state (`atlas.persistence.sqlite.StateStore`)
- LangGraph checkpointing (`atlas.orchestration.checkpoints`)
- retry policy (`atlas.orchestration.retry`)
- worker contract (`atlas.workers.base.BaseWorker`)
- source adapter contract (`atlas.sources.base.BaseSource` - not yet
  used by any real connector)
- event bus (`atlas.orchestration.events.EventBus`)
- metrics (`atlas.orchestration.metrics`)
- structured logging (`atlas.utils.logging`)
- graceful shutdown (`atlas.orchestration.shutdown.GracefulShutdown`)
- intervention queue (`atlas.browser.intervention.InterventionQueue`)
- BrowserManager (`atlas.browser.manager` - NOT invoked by the demo path)
- report scaffold (`atlas.reporting.excel.ExcelReporter`)

New Phase 0.75 modules live entirely under `atlas/runtime/`:

| Module | Purpose |
|---|---|
| `states.py` | Explicit `RunState` enum + deterministic transition table |
| `scheduler.py` | `TaskRecord` + `TaskScheduler` (priority/batch/dependency ordering) |
| `concurrency.py` | `BoundedExecutor` - bounded, duplicate-safe concurrency primitive |
| `failure_injection.py` | Deterministic, reusable failure-injection mechanism |
| `demo_workload.py` | The 50-task deterministic demo workload + `DemoWorker` |
| `progress.py` | `ProgressSnapshot` - machine-readable progress, no LLM involved |
| `manifest.py` | `RunManifest` + non-secret config fingerprint |
| `report.py` | `Atlas_DEMO_Runtime_Report.xlsx` / `.json` writer |
| `engine.py` | `AtlasRuntime` - the runtime lifecycle itself |

## Run lifecycle

```
INITIALIZE
  -> ACQUIRE RUN LOCK            (RunLock.try_acquire)
  -> LOAD OR CREATE RUN          (StateStore.get_run / create_run)
  -> LOAD CHECKPOINT             (LangGraph SqliteSaver via open_checkpointer)
  -> BUILD TASK QUEUE            (TaskScheduler.order -> initial_queue_state)
  -> EXECUTE BATCH                 \
  -> RECORD RESULT                  |  repeated until RUN_STATUS_COMPLETE
  -> RETRY WHEN REQUIRED             >  or a stop condition (budget / --stop-after)
  -> CHECKPOINT                     |  is reached
  -> CONTINUE REMAINING TASKS       /
  -> FINALIZE                    (COMPLETE / WAITING_FOR_HUMAN / PARTIAL)
  -> WRITE DEMO REPORT            (Atlas_DEMO_Runtime_Report.xlsx + .json)
  -> RELEASE RUN LOCK
```

`atlas run --demo` and `atlas resume` both call into
`AtlasRuntime._execute()`; the only difference is whether an existing
LangGraph checkpoint for the same `thread_id` is found (resume path) or
a fresh task queue is seeded (start path). This mirrors the pattern
already proven by `tests/_crash_recovery_worker.py` in Phase 0.5.

## State machine

`atlas.runtime.states.RunState`:

```
INITIALIZING -> RUNNING -> FINALIZING -> COMPLETE
                        \-> WAITING_FOR_HUMAN
                 \-> PARTIAL -> RESUMING -> RUNNING -> ...
                 \-> WAITING_FOR_HUMAN -> RESUMING -> RUNNING -> ...
Any non-terminal state -> FAILED
```

Transitions are validated by `atlas.runtime.states.transition()` against
an explicit `ALLOWED_TRANSITIONS` table - **never** by free-form
LLM/controller output. `COMPLETE` and `FAILED` are terminal (no outgoing
transitions).

## Batch execution & the concurrency boundary

- `batch_size` controls how many LangGraph `graph.invoke()` calls (each
  one processing exactly one queue item, exactly as proven in
  `atlas/orchestration/graph.py`) happen before the runtime checkpoints
  continuation state and re-evaluates the runtime budget / stop
  condition.
- `atlas.runtime.concurrency.BoundedExecutor` is a standalone, tested,
  reusable concurrency primitive that bounds `max_parallel_tasks` and
  guarantees the same task id is never in flight twice. **Future browser
  concurrency constraint**: real browser/worker tasks must NEVER share
  one Playwright `Page`/browser context, and Atlas will never open many
  real visible browser windows at once - `InterventionQueue` already
  enforces "at most one visible browser session at a time" for human
  escalation, and any future parallel browser worker pool must adopt an
  equivalent one-page-per-worker (or serialized-browser) model. Phase
  0.75's demo workload intentionally uses deterministic, non-web fake
  workers and drives them through the existing, proven **sequential**
  LangGraph governor for state-mutation safety - `BoundedExecutor` is
  provided and unit-tested today so future non-browser-parallelizable
  work (or a future worker pool with its own per-worker browser context)
  has a ready, safe primitive to build on.

## Human intervention (simulated in demo mode)

When a task reaches `LOGIN_REQUIRED`/`WAITING_FOR_HUMAN`, the runtime:

1. checkpoints the task (already terminal in `QueueState` bookkeeping,
   tracked in `human_waiting_items`)
2. records a durable `human_interventions` row via
   `StateStore.request_human_intervention`
3. enqueues an `InterventionRequest` into `InterventionQueue`
4. in demo mode (`simulate_intervention=True`, the default), resolves it
   immediately via a simulated resolver that **never opens a real
   browser** (`atlas.runtime.engine._simulated_escalate`), and records
   the resolution via `StateStore.resolve_human_intervention`
5. all other tasks continue independently - the run does not stop
   merely because one task needs a human

At finalization, if any `human_waiting_items` remain unresolved (checked
durably via `StateStore.intervention_status`, not merely in-memory - so
this is correct even across separate process runs), the run reaches
`WAITING_FOR_HUMAN` instead of `COMPLETE`. A real (non-demo) run would
instead call `atlas.browser.intervention.escalate_for_human` (unchanged,
still opens a real visible Chrome window one at a time).

## Runtime budget & partial runs

`max_runtime_minutes` (optional) computes a deadline at the start of a
run/resume. Before scheduling each new batch, the runtime checks the
deadline (and `--stop-after N` for deterministic demo/test partial
stops); if reached, it checkpoints continuation state, marks the run
`PARTIAL`, and exits cleanly - **no state is lost**. `atlas resume`
(or a second `AtlasRuntime.resume()` call) picks the SAME run back up
from its last checkpoint.

## Idempotency

- Re-running `AtlasRuntime.run()`/`resume()` on an already-`COMPLETE` run
  is a no-op: the LangGraph state already has `run_status == COMPLETE`
  and an empty `remaining_items`, so the batch loop never executes and
  no attempt/completion records are duplicated.
- Resuming twice never double-processes completed tasks (LangGraph's
  proven queue-pop semantics: an item is removed from `remaining_items`
  the moment it is popped, and only appended to `completed_items` once).
- Human-intervention resolution state lives in the durable
  `human_interventions` table (not per-process memory), so finalization
  correctly reaches `COMPLETE` even when a resolved intervention was
  recorded by an earlier process (see `tests/test_runtime_restart.py`
  and `tests/test_runtime_engine.py::test_double_resume_is_idempotent_and_still_complete`).

## Progress, manifest, and config fingerprint

- `atlas status --run-id <id> --json` prints a `ProgressSnapshot`
  (`atlas.runtime.progress.compute_progress`) derived purely from
  durable `QueueState` counts - no LLM computes these numbers.
- Every finalized run writes a `RunManifest`
  (`run_manifest_<run_id>.json` in `output_dir`) containing run
  id/timestamps/status/config fingerprint/controller/task counts/retry
  counts/interventions/software versions.
- The config fingerprint (`atlas.runtime.manifest.compute_config_fingerprint`)
  is a deterministic sha256 hash of an explicit **allow-list** of
  non-secret `Settings` fields. It never includes credentials, cookies,
  tokens, or authorization data - the allow-list is defensively checked
  against a sensitive-name-fragment list at hash-build time.

## Demo report

`Atlas_DEMO_Runtime_Report.xlsx` / `.json` (written to `output_dir`) are
demonstration-only artifacts proving the runtime can transform durable
run state into a user-facing report. **This is not the final Atlas job
workbook** - the final business report schema will be defined once the
Workspace Atlas Agent specification is imported.

## Failure injection

`atlas.runtime.failure_injection.FailureInjector` lets tests script
exactly what a worker attempt should do for a given `(task_id,
attempt_number)` pair - timeout, navigation failure, access limited,
login required, extraction unresolved, or worker crash - without ever
modifying production worker code. The Phase 0.75 demo workload (50
tasks) is itself built entirely on this mechanism.

## CLI

```
atlas run --demo [--run-id ID] [--batch-size N] [--stop-after N]
                 [--max-runtime-minutes M] [--no-auto-resolve]
atlas resume [--run-id ID] [--batch-size N] [--stop-after N]
             [--max-runtime-minutes M] [--no-auto-resolve]
atlas status [--run-id ID] [--json]
atlas version --verbose
atlas doctor   # now also checks run lock / checkpoint backend / SQLite
               # schema / worker registry / report output / controller
```

`--run-id` defaults to `atlas-demo-run` for all three commands so a
plain `atlas run --demo` followed by `atlas resume` "just works" without
requiring the caller to track an id by hand.
