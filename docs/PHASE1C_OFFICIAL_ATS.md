# Phase 1C-A — Official ATS Adapters + Bounded Parallel Execution

Phase 1C-A begins **real, read-only** job discovery through the four official
Applicant Tracking System (ATS) job-board interfaces, executed through the same
sealed-coverage production graph Phase 1B.1 established — now with a bounded
parallel worker pool and atomic task leasing.

## What shipped

| Component | Module |
|---|---|
| Read-only HTTP transport | `atlas/sources/http_client.py` |
| Shared ATS helpers (date provenance, URL identity, sanitize, challenge detect) | `atlas/sources/ats/base.py` |
| Greenhouse adapter | `atlas/sources/ats/greenhouse.py` |
| Lever adapter | `atlas/sources/ats/lever.py` |
| Ashby adapter | `atlas/sources/ats/ashby.py` |
| Workday adapter (highest risk) | `atlas/sources/ats/workday.py` |
| Adapter registry factory | `atlas/sources/ats/__init__.py` |
| Atomic coverage leases | `atlas/persistence/sqlite.py` (schema v8), `atlas/sources/leasing.py` |
| Shared per-child executor | `atlas/sources/child_executor.py` |
| Bounded parallel dispatcher | `atlas/runtime/parallel_pipeline.py` |
| Live canary runtime + config | `atlas/runtime/canary.py`, `config/canary/*.yaml` |
| CLI | `atlas adapters …`, `atlas production-canary …` |

## Architecture (one governor, many bounded workers)

```
sealed coverage child
  → LeaseManager.acquire (atomic BEGIN IMMEDIATE)
  → ParallelExecutionPipeline (ThreadPool, per-company/instance/tenant caps)
  → CoverageChildExecutor  ── the SAME code the sequential path runs
      → SourceRegistry.create(adapter)
      → shared RateLimitedExecutor (per-instance pacing + concurrency slot)
      → SourceSearchWorker → centralized retry (atlas.orchestration.retry)
      → append-only coverage attempt
      → QuerySignature-keyed source health / yield
      → raw discovery observation staging (NOT canonical jobs)
      → terminal coverage status + lease COMPLETE
  → DEDUPE canonicalization → one Excel report (written by the graph, never a worker)
```

The production graph is unchanged in shape; `DISCOVER` simply selects the
parallel dispatcher when `parallel_workers > 1`. There is **no second live
orchestrator**. Concurrency 1 and concurrency N produce identical canonical
results because both run `CoverageChildExecutor.execute` per child.

## Capabilities per family

| Family | SEARCH | DETAIL | PAGINATION | DESCRIPTION | POSTED_DATE | Notes |
|---|---|---|---|---|---|---|
| Greenhouse | ✅ | ✅ | ❌ (returns all) | ✅ (detail) | ✅ (detail `first_published`) | list has only `updated_at` |
| Lever | ✅ | ✅ | ✅ (skip/limit) | ✅ | ✅ (`createdAt` when present) | array response |
| Ashby | ✅ | ❌ (no detail endpoint) | ❌ | ✅ (in list) | ✅ (`publishedAt`) | filter `isListed:true` |
| Workday | ✅ | ✅ | ✅ (offset/limit ≤20) | ✅ (detail) | ❌ (relative text only) | public CXS search |

## Date provenance

Every date carries one `DateProvenance`: `EMPLOYER_POSTED_AT`,
`EMPLOYER_UPDATED_AT`, `RELATIVE_POSTED_TEXT`, `DISCOVERED_AT`, or `UNKNOWN`. A
crawl/first-seen time is never presented as an employer posted date, and
Workday's relative `postedOn` never becomes a fabricated absolute date.

## Safety boundary (non-negotiable)

Read-only GET only, plus the single public Workday CXS **search** POST that an
employer's own careers page issues (facets/keyword; no candidate data). No
application submission, no login/account, no credentials/cookies, no
CAPTCHA/MFA/anti-bot/Cloudflare/Zscaler bypass, no proxy/IP rotation, no
stealth/UA rotation, no fetching links found inside posting text. Posting text
is untrusted data. If blocked, classify `ACCESS_LIMITED`/`SOURCE_UNAVAILABLE`,
record evidence, and continue other work — never bypass.

## Not built in 1C-A

LinkedIn/Naukri/Foundit/Indeed/Wellfound adapters, a full 108-company sweep, a
production scheduler, auto-apply, candidate submission, recruiter outreach,
resume scoring. Those are later phases (1C-B onward).

See also: `PARALLEL_WORKER_LEASING.md`, `sources/GREENHOUSE.md`,
`sources/LEVER.md`, `sources/ASHBY.md`, `sources/WORKDAY.md`,
`LIVE_CANARY_OPERATIONS.md`, `EXTERNAL_RESEARCH_DECISIONS.md`.
