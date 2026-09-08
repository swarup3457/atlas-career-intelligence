# Production Runtime

Canonical code: `atlas/runtime/production.py`. The runtime is DISTINCT from the
demo `atlas.runtime.engine.AtlasRuntime`. It drives the explicit multi-phase
production graph (`atlas/orchestration/production_graph.py`, see
`docs/PRODUCTION_PHASE_GRAPH.md`) and executes the SEALED coverage plan through
the real source pipeline. Discovery is fixture/fake only — there is no real
adapter and no live web request.

## Phases

`INITIALIZE → LOAD_PRIVATE_PROFILE → LOAD_POLICY → BUILD_AND_SEAL_PLAN →
DISCOVER → SOURCE_HEALTH_GATE → DETAIL_HYDRATION → OFFICIAL_VERIFICATION →
DEDUPE_AND_REPOST_CLASSIFICATION → CANDIDATE_MATCH → PERSIST_LOCAL →
BUILD_REPORT → OPTIONAL_REMOTE_AUDIT → COMPLETE`

Each `advance_phase` invoke runs one handler and checkpoints a COMPACT state
(run/plan ids, counters, cursors, phase). Job descriptions, HTML, and raw
payloads never enter the checkpoint. `build_production_graph` validates that
every mandatory phase has a handler (a missing handler fails closed).

## Seeding & resume (P0-2, P0-3)

- The FIRST invoke passes `initial_production_state(run_id)`; later invokes pass
  an empty update.
- `started_at`, policy fingerprint, plan fingerprint, and candidate snapshot
  hash are durable. A fresh process rehydrates policy, candidate ledger, and the
  sealed plan from SQLite (`_ensure_policy/_ensure_ledger/_ensure_plan`).
- DISCOVER is batched and resumable: each child is persisted immediately, so an
  abrupt exit leaves a durable partial subset. Resume re-attempts only pending
  children; completed attempts/observations/report are never duplicated.

## Terminality gate (P0-13/17)

Before COMPLETE, `resolve_terminal()` asserts: local persistence ok; a SEALED
plan whose fingerprint matches the persisted plan; no required child
`NOT_ATTEMPTED`/`IN_PROGRESS`; no `BLOCKED_HUMAN` child; report valid. It fails
closed to `WAITING_FOR_HUMAN` / `PARTIAL` / `FAILED` — never a false COMPLETE.

## Reliability

- **RunLock** (`atlas/orchestration/run_lock.py`) guards the run; a second
  concurrent process gets `RUN_ALREADY_ACTIVE`; the lock is released on every
  outcome (success/partial/waiting/failure/exception).
- **Local persistence** is mandatory and classified — a failure returns FAILED,
  never a silent COMPLETE. Only the OPTIONAL remote audit may degrade
  (`REMOTE_AUDIT_DEGRADED`) without failing the local run.
- **Report** publication is atomic (`docs/REPORT_RELIABILITY.md`) and reopened
  for validation; the run is not COMPLETE until the workbook reopens with all
  eight sheets.

## Modes

- **Fixture mode** (default / `atlas production-fixture`): synthetic PII-free
  ledger, `FakeAdapter`/`FixtureAdapter`, zero network. Result manifests state
  `synthetic_candidate_evidence: true`.
- **Production mode** (`fixture_mode=False`): requires a private, versioned
  candidate snapshot loaded from a gitignored path (only its hash is recorded);
  a missing/invalid snapshot yields `WAITING_FOR_HUMAN`. Live `atlas run`
  remains disabled until real adapters pass the Phase 1C gates.
