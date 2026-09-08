# Parallel Worker Leasing

Phase 1B.1 executed a sealed coverage plan correctly but sequentially. Phase
1C-A adds a bounded parallel worker pool driven by **atomic task leases** so the
exact coverage child is the unique unit of work, claimed by at most one worker
at a time, reclaimable after a crash, and never re-run once terminal.

## Schema (migration v8, additive)

`coverage_leases` — one row per coverage child:

| Column | Meaning |
|---|---|
| `coverage_id` (PK) | the exact unit of work |
| `run_id` | owning run |
| `worker_id` | current holder (null when available) |
| `status` | `AVAILABLE` \| `LEASED` \| `DONE` \| `HELD_RELEASED` |
| `terminal` | 1 once the child is done — never leasable again |
| `attempt` | incremented on each acquire (reclaim count = attempt − 1) |
| `version` | optimistic version, bumped on each acquire |
| `lease_acquired_at` / `lease_expires_at` / `heartbeat_at` | UTC (fixed-width ISO µs) |
| `company` / `source_instance` / `tenant` | cap dimensions |

`coverage_lease_events` — append-only audit (`ACQUIRE`, `HEARTBEAT`, `RELEASE`,
`COMPLETE`), ordered by `(at, rowid)` so the trail is insertion-stable.

## Atomicity

Every claim runs inside `StateStore.immediate_transaction()` (`BEGIN
IMMEDIATE`), which takes a RESERVED write lock up front. A second writer — on a
separate connection or process — blocks on that lock (up to the busy timeout)
instead of racing, so two workers can never both observe the same lease as free.
The read-decide-write is therefore atomic. UTC-only, fixed-width ISO µs
timestamps make lease-expiry string comparisons in SQL correct.

## Guarantees (each has a regression test)

1. Exact coverage child is the unique unit of work.
2. Claim is atomic — 20 threads contending for one child → exactly one wins.
3. A second worker cannot claim an unexpired lease.
4. An expired lease is reclaimable (attempt increments).
5. A long task can `heartbeat` to extend its lease; a non-owner cannot.
6. A terminal / already-done child can never be leased.
7. Duplicate delivery is harmless (idempotent `complete`; deterministic
   observation/attempt ids → no duplicate side effects on reclaim).
8. Every transition is auditable.
9. UTC only.
10. Unit tests use an injected virtual clock (`ManualUTCClock`).

## Bounded dispatcher

`ParallelExecutionPipeline` (main thread) leases the next eligible child and
submits it to a `ThreadPoolExecutor`. Eligibility respects **per-company**,
**per-source-instance**, and **per-tenant** concurrency caps (all configurable,
default 1). Each worker opens its **own** `StateStore` connection via a store
factory — a sqlite connection is never shared across threads. Workers persist
their coverage row / attempts / observations and mark the lease terminal; the
report is written only by the graph, never by a worker.

**No deadlock:** whenever nothing is in flight, all cap counters are zero, so at
least one remaining child is always eligible.

**Crash recovery:** a worker that crashes between the HTTP response and status
persistence releases its lease (requeue). The dispatcher reclaims it; because
observation/attempt ids are deterministic and staged with `INSERT OR IGNORE`,
re-execution produces no duplicate attempt, observation, or canonical job.

## The executor slot fix (build spec 8)

`RateLimitedExecutor.run_search` previously ignored the boolean returned by
`acquire_slot`: a failed acquisition still called the adapter and then released
a slot it never held, corrupting the active count under contention. It now
acquires the slot in **blocking** mode and calls the adapter **only** when a
slot is genuinely held, releasing **only** a held slot. `RateLimiter.acquire_slot`
gained an optional blocking mode backed by a `Condition`.
