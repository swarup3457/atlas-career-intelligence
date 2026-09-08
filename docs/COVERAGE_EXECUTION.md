# Coverage Execution

Canonical code: `atlas/runtime/fixture_pipeline.py`, `atlas/runtime/canonicalize.py`,
`atlas/sources/worker.py`, `atlas/sources/executor.py`, `atlas/sources/coverage.py`,
`atlas/planning/planner.py`. This is the ONE production execution path a sealed
coverage plan runs through — the same path real adapters will use in Phase 1C.

## The chain (one adapter call == one attempt)

```
sealed plan child (CoverageTask)
  → SourceInstance (loaded from self.instances / SQLite)
  → SourceRegistry.create(instance)              # real adapter construction
  → RateLimitedExecutor.run_search(...)          # slot + interval + Retry-After + backoff, released in finally
  → SourceSearchWorker.attempt(...)              # typed WorkerOutcome / WorkerError
  → atlas.orchestration.retry.evaluate(...)      # centralized retry authority
  → append-only coverage_attempts row            # every attempt, never overwritten
  → source_health_history keyed by QuerySignature
  → raw_discovery_observations staging (schema v7)
  → coverage_records terminal/human-blocked update
```

Retry authority stays centralized; the adapter never sleeps or retries. Raw
discoveries are STAGED — never written to `canonical_jobs` during DISCOVER.

## Per-lane accountability (P0-11)

The planner emits ONE child coverage row per `company × source_instance × lane`
(a shared `parent_batch` key allows compatible lane queries to share transport).
Geography is a GROUP, never per-city, so there is no blind
`companies × sources × lanes × cities` explosion. A failed React lane can never
be hidden by a successful Java lane; `CoverageManifest.lane_summary()` reports
planned vs terminal per lane.

## Query signatures & false zero (P0-12)

`SourceTask` carries a `QuerySignature` (source/lane/geo/mode/keywords/policy).
Health/yield history is read and written by the signature fingerprint, so a zero
for `.NET/Mumbai` is never compared against `Java/Bengaluru` history. An untrusted
zero triggers AT MOST ONE bounded sentinel probe (persisted and auditable).

## Rate limiting (P0-13)

`RateLimitedExecutor` wraps the shared `RateLimiter`: acquire a per-source
concurrency slot, enforce the min interval / not-before, honor an explicit
`Retry-After` from a 429, apply a bounded adaptive backoff otherwise, note a
clean success, and release the slot in `finally`. Deterministic (virtual-clock
testable); no random human-like timing.

## Canonicalization (DEDUPE, P0-15)

`canonicalize_run` resolves STAGED observations into canonical jobs by a
source-INDEPENDENT dedupe key `(company, title, location, posted_at, url)`:
cross-source duplicates collapse to one canonical job with multiple
observations; a duplicate same-source observation is idempotent; a probable
repost (same role, different posted date) keeps a distinct key and is never
silently merged. Idempotent across resume.

## Coverage completion

Completion means "every planned required child reached a terminal state", not
"we found N jobs". `BLOCKED_HUMAN` is non-terminal and awaits human action;
`NOT_ATTEMPTED`/`IN_PROGRESS` block COMPLETE. See `atlas/sources/coverage.py`.
