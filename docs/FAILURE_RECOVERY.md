# Atlas Failure Recovery Guide (Phase 0.5)

This document describes what happens when Atlas crashes, is killed, or is
interrupted, and how the platform recovers - all PROVEN by
`tests/test_crash_recovery.py` and the lock/shutdown unit tests unless
otherwise noted.

## 1. Browser profile lock recovery (PROVEN)

**Mechanism:** `atlas.utils.pidlock.PidLock`, used by
`atlas.browser.manager.BrowserManager`.

- The lock file (`.atlas-browser-manager.lock` inside the profile
  directory) records the owning PID, hostname, and acquisition time.
- On `acquire()`, if an existing lock file is found:
  - If the recorded PID is **still running**, `LockHeldError` is raised
    (wrapped as `ProfileLockedError` by `BrowserManager`) - the lock is
    **never** stolen from a live process.
  - If the recorded PID is **no longer running** (crashed process), the
    lock is safely reclaimed and overwritten.
  - If the lock file itself is corrupt/unreadable, a time-based fallback
    (`corrupt_stale_after_seconds`, default 6h) is used instead.
- On normal `close()`/context-manager exit, the lock is released (deleted)
  - but only if it is still owned by the current process's PID.

**Verified by:** `tests/test_pidlock.py` (`test_second_lock_blocked_while_first_process_is_live`,
`test_stale_lock_from_dead_pid_is_reclaimed`,
`test_release_never_removes_a_lock_owned_by_another_pid`,
`test_corrupt_lock_file_falls_back_to_age_based_staleness`).

## 2. Run lock recovery / single-run guard (PROVEN)

**Mechanism:** `atlas.orchestration.run_lock.RunLock` (same `PidLock`
primitive, different lock file: `atlas_run.lock` in the state directory).

- Before starting a production run, call
  `RunLock(state_dir).try_acquire(run_id=...)`.
- If another run is genuinely active (live PID), the result is
  `RunLockStatus(status=RUN_ALREADY_ACTIVE, run_id=..., pid=..., started_at=...)`
  - the caller **must not** start a second run.
- If the previous run's process crashed (dead PID), `try_acquire()`
  transparently reclaims the lock and returns
  `RunLockStatus(status=RUN_LOCK_ACQUIRED, ...)`.
- `RunLock.current_holder()` is a read-only inspection (used by
  `atlas status`) that never acquires or modifies the lock.

**Verified by:** `tests/test_run_lock.py` and end-to-end by
`tests/test_crash_recovery.py`.

## 3. Checkpoint recovery (PROVEN)

**Mechanism:** `atlas.orchestration.checkpoints.open_checkpointer`
(official `langgraph-checkpoint-sqlite` `SqliteSaver`), keyed by
`thread_config(thread_id)`.

- Every `graph.invoke()` call durably persists the full `QueueState`
  (planned/completed/remaining items, retry counts, item_results,
  attempt_log, run_status) to the checkpoint SQLite DB before returning.
- A fresh process can reopen the SAME checkpoint DB + thread_id and call
  `graph.get_state(config).values` to read back the exact state, or call
  `graph.invoke({}, config)` to continue processing exactly where the
  previous process left off.
- Because `remaining_items` only ever shrinks (an item is popped and
  either requeued at the back on retry or moved to `completed_items` on a
  terminal outcome), a resumed run can never reprocess an already
  `completed_items` entry from scratch.

**Verified by:** `tests/test_checkpoints.py` and
`tests/test_crash_recovery.py`.

## 4. Crash-recovery integration test (PROVEN — `tests/test_crash_recovery.py`)

Deterministic, no LLM, no internet. Sequence:

1. Subprocess A acquires the run lock (`run_id=run-morning`), processes 2
   of 5 fake tasks via the real LangGraph governor graph, then calls
   `os._exit(1)` - simulating an unexpected crash (no lock release, no
   graceful shutdown, no cleanup).
2. The test confirms the run-lock file is left behind and its PID is no
   longer running.
3. Subprocess B (`run_id=run-resume`) starts fresh: `RunLock.try_acquire`
   reclaims the stale lock, `open_checkpointer` reopens the SAME
   checkpoint DB/thread_id, and processing continues from item 3 through
   5 without repeating items 1-2.
4. The final checkpointed state is verified: `run_status == "COMPLETE"`,
   all 5 items present in `completed_items` exactly once each, each
   item's `retry_counts` entry is exactly 1 (no accidental reprocessing).
5. A third invocation (`run_id=run-third`) proves stale-lock recovery
   works repeatedly, not just once (subprocess B also crash-exits via
   `os._exit`, by design, to keep the test's crash simulation realistic).

## 5. Graceful shutdown (IMPLEMENTED — not yet wired into a real run loop)

**Mechanism:** `atlas.orchestration.shutdown.GracefulShutdown`.

- Registers SIGINT (Ctrl+C) and, on Windows, SIGBREAK (Ctrl+Break) as
  reliable interrupt signals; SIGTERM is also registered but Windows
  cannot reliably deliver it externally.
- `trigger()` is idempotent and runs all registered cleanup callbacks in
  LIFO order, tolerating individual callback failures so e.g. a broken
  "close browser" callback never prevents "release run lock" from still
  running.
- Intended usage (once a real run loop exists): register, in order,
  checkpoint-flush, browser-close, and lock-release callbacks, then check
  `shutdown.requested` at the top of each loop iteration and stop pulling
  new work - **never mark an unfinished task as complete**.

**Verified by:** `tests/test_graceful_shutdown.py` (LIFO order,
idempotency, fault tolerance, signal registration/restoration, and a
simulated-SIGINT wiring test).

## 6. Database migration safety (IMPLEMENTED)

**Mechanism:** `atlas.persistence.sqlite._MIGRATIONS` (ordered
`(version, description, statements)` tuples), applied by
`StateStore._migrate()`.

- Each migration runs inside its own `with self._conn:` transaction; a
  `sqlite3.Error` during a migration triggers an automatic ROLLBACK and
  raises `MigrationError` **without** recording that version as applied,
  so a failed migration can be retried safely.
- The `schema_migrations` table records every successfully-applied
  version + description; `StateStore.schema_version()` reports the
  highest applied version.
- No destructive migration exists today (migration 2 only adds
  non-destructive indexes).

**Backup strategy (documented, FUTURE — not implemented yet):** before
any future destructive migration is introduced, Atlas must copy the
SQLite file (`shutil.copy2`) to a timestamped backup path under
`state/backups/` and verify the copy opens successfully before running
the destructive migration. This is intentionally not built yet since no
destructive migration exists.

**Verified by:** `tests/test_state_store.py`
(`test_migrations_table_records_each_version`,
`test_reopening_store_preserves_data_and_does_not_rerun_migrations`,
`test_migration_error_type_is_exposed`).

## 7. What is explicitly NOT recovered/implemented yet (FUTURE)

- There is no production run loop yet to actually wire
  `GracefulShutdown`/`RunLock`/checkpointing together end-to-end outside
  of tests - Phase 0.5 proves each mechanism works in isolation and in
  the deterministic crash-recovery integration test, not inside a real
  business-logic run.
- No destructive-migration backup automation exists yet (documented
  strategy above, not code).
- No Windows Scheduled Task is configured (see `docs/OPERATIONS.md`
  section "Windows Task Scheduler preparation").
