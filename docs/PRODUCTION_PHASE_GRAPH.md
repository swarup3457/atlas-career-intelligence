# Production Phase Graph

Status: **implemented (Phase 1B).** The production run is an explicit,
checkpointed, multi-phase graph
(`atlas/orchestration/production_graph.py`, `production_state.py`) driven by
`ProductionSearchRuntime` (`atlas/runtime/production.py`). It is the
deterministic implementation of file 13's search-first phase isolation.

## Phase order

```
INITIALIZE
  → LOAD_PRIVATE_PROFILE
  → LOAD_POLICY
  → BUILD_AND_SEAL_PLAN
  → DISCOVER
  → SOURCE_HEALTH_GATE
  → DETAIL_HYDRATION
  → OFFICIAL_VERIFICATION
  → DEDUPE_AND_REPOST_CLASSIFICATION
  → CANDIDATE_MATCH
  → PERSIST_LOCAL
  → BUILD_REPORT
  → OPTIONAL_REMOTE_AUDIT
  → COMPLETE
```

Each `advance_phase` invocation runs exactly one phase handler, checkpoints, and
advances the phase pointer.

## Search-first isolation

Persistence, Excel, and remote-audit side effects are allowed **only** in the
persistence phases (`PERSIST_LOCAL`, `BUILD_REPORT`, `OPTIONAL_REMOTE_AUDIT`) and
**never** during discovery. A `PhaseContext` records side effects and raises
`PhaseIsolationError` if a non-persistence phase attempts one, so discovery can
be *proven* not to have written the report or remote audit.

## Truthful terminal states

`PARTIAL`, `WAITING_FOR_HUMAN`, and `FAILED` are distinct terminal outcomes and
are never confused with `COMPLETE`. A human-waiting or failed phase stops the
run without advancing to `COMPLETE`; `stop_after_phase` yields a resumable
`PARTIAL`.

## Compact checkpoints

Checkpoints carry only: run id, policy/plan fingerprints, current and completed
phases, planned task IDs, counters, cursors, retry references, and short status
summaries — **no job descriptions or payloads**. Bulky-field and size guards
(`assert_compact`) fail fast if a payload leaks in. Detailed observations
(discovered jobs, attempts) persist to SQLite, keeping the checkpoint bounded
regardless of discovery volume.

## Runtime separation

`ProductionSearchRuntime` is separate from the demo `AtlasRuntime`, which is
preserved for regression. The production runtime runs end-to-end under
`NullController` with fake/fixture discovery — no live adapter, no live web
search.

## Coverage planner archetypes

The planner (`atlas/planning/planner.py`) builds a sealed, fingerprinted
manifest from typed archetypes, avoiding a blind
`companies × sources × lanes × cities` Cartesian explosion:

| Archetype | Granularity |
|---|---|
| `COMPANY_DELTA` / `COMPANY_DEEP` | One task per due company (carries the lane bundle + geography group) |
| `PORTAL_DISCOVERY` | Per portal family × lane × geography group (**not** per company) |
| `ATS_CROSS_COMPANY_DISCOVERY` | Per ATS family × geography group |
| `OFFICIAL_VERIFICATION` | Per specific lead |
| `COMPANY_SOURCE_DISCOVERY` | Per unknown/new company lacking a source instance |
| `DETAIL_HYDRATION` | Per lead needing detail |

## Optional remote audit

`OPTIONAL_REMOTE_AUDIT` (`atlas/persistence/remote_audit.py`) exports a
sanitized, immutable run-event record. A failure degrades to
`REMOTE_AUDIT_DEGRADED` **without stopping the local run**; local state and the
report remain valid, and GitHub availability is never a prerequisite. Events are
PII-sanitized before export.

Tests assert the phase order and that search never triggers a persistence side
effect. See [PRODUCTION_SEARCH_ARCHITECTURE.md](PRODUCTION_SEARCH_ARCHITECTURE.md).
