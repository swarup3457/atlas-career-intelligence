# ai-job-search Implementation Matrix (PRODUCTION R1 §1B)

Implementation audit mapping each verified upstream pattern from
[`MadsLorentzen/ai-job-search`](https://github.com/MadsLorentzen/ai-job-search)
(local clone: `C:\Atlas-Research\ai-job-search`) to the Atlas module that
implements it and the Atlas test that proves it. Atlas remains the product; this
is a pattern audit, not a port.

Upstream is a Claude-Code skill framework: `job_scraper/seen_jobs.json` +
`url_or_company_title_key` dedupe, portal-CLI providers under
`.agents/skills/*-search` with a Step-2 search-output contract, a Step-4.75
degraded-portal scan, and a `/scrape` → `/rank` order with `tools/rank_state.py`
persisting scores.

| # | Pattern | Upstream source | Atlas module | Implemented behavior | Test proving it |
|---|---------|-----------------|--------------|----------------------|-----------------|
| 1 | State-first seen-job history | `.claude/skills/job-scraper/SKILL.md` Step 0 (`job_scraper/seen_jobs.json`, append-only) | `atlas/persistence/sqlite.py` (`canonical_jobs`, `job_observations`), `atlas/vscode_hunt/history.py::record_run_jobs` | Every seen job persists across runs in SQLite; observations append-only; re-record idempotent on `content_hash`/`observation_id` | `tests/test_cumulative_history.py::test_prior_job_survives_an_unrelated_later_run` |
| 2 | Profile-driven search queries | `search-queries.md` + SKILL.md Step 1 | `atlas/discovery/service.py::DiscoveryService.run(queries)`; `write_discovery_evidence` | Queries derived from target lanes + India policy, passed to providers, persisted | `tests/test_discovery_phase.py::test_write_evidence_and_plan_batches` |
| 3 | Freehire / portal-CLI provider | `.agents/skills/*-search` (jobnet-search, jobdanmark-search) | `atlas/discovery/freehire.py::FreehireProvider` | Read-only HTTP provider, India `countries=in`, recency `posted_within_days`, `include_description`; returns `DiscoveryBatch` | `tests/test_discovery_phase.py::test_write_evidence_and_plan_batches` |
| 4 | Provider/source fan-out | SKILL.md Step 1b ("run these in parallel") | `atlas/discovery/service.py::DiscoveryService.run` | Multiple providers merged into one lead pool | `tests/test_discovery.py::test_provider_failure_isolated` |
| 5 | Provider failure isolation | SKILL.md Step 4.75 (degraded portal doesn't abort run) | `DiscoveryService.run` per-provider try/except, errors collected | One provider raising never aborts the others; error recorded | `tests/test_discovery.py::test_provider_failure_isolated` |
| 6 | Full-detail hydration | portal `detail.ts` detail fetch | `atlas/browser_backend/validation.py::validate_job_evidence` (`CARD_ONLY` reject) via `atlas/reporting/trust_boundary.py` | A lead with no captured job-detail evidence cannot be validated | `tests/test_report_trust_boundary.py::test_partition_rejects_drivetrain_blank_shape` |
| 7 | Canonical job identity | `url_or_company_title_key` (seen_jobs dedupe) | `atlas/discovery/models.py::JobLead.identity`; `atlas/vscode_hunt/history.py::canonical_job_id` | URL-first identity precedence → requisition/company → hashed fallback | `tests/test_discovery_hardening.py::test_identity_precedence_and_cross_provider_dedup` |
| 8 | Closed-at-source handling | `tools/rank_state.py` rule-6 expiry sweep; `test_outcome_stale.py` | `validate_job_evidence` `CLOSED_POSTING`; `atlas/vscode_hunt/history.py::mark_closed` / `mark_source_unavailable` | CLOSED requires explicit closure evidence; inaccessible source → REVERIFY/SOURCE_UNAVAILABLE, never deleted | `tests/test_cumulative_history.py::test_mark_closed_requires_evidence`, `::test_source_unavailable_moves_not_deletes` |
| 9 | Source health | SKILL.md Step 4.75 degraded scan | `atlas/discovery/service.py::write_discovery_evidence`; `source_health_history` table; Source_Health sheets | Per-provider health persisted and surfaced in run + master workbooks | `tests/test_discovery_phase.py::test_write_evidence_and_plan_batches` |
| 10 | Mass-posting consolidation | cross-portal dedupe (seen_jobs key) | `atlas/discovery/canonicalize.py::deduplicate` | Duplicate leads collapsed, description-rich copy preferred | `tests/test_discovery.py::test_dedup_prefers_description_rich_copy` |
| 11 | Deferred backlog | seen_jobs `skipped` entries (append-only) | `atlas/discovery/prefilter.py` (`DEFERRED` = `NO_TARGET_SIGNAL`) | Unclear leads deferred/queued for verification, never silently dropped | `tests/test_discovery.py::test_prefilter_rejects_support_and_foreign` |
| 12 | Search-before-rank | `/scrape` precedes `/rank` | `atlas/vscode_hunt/pipeline.py::assemble_run_outputs` | Discovery + verification precede Python recommendation/ranking | `tests/test_pipeline_assembly.py::test_assemble_run_outputs_end_to_end` |
| 13 | Rank-state persistence | `tools/rank_state.py` (candidates/sweep/apply, idempotent write-back) | `StateStore.upsert_canonical_job` + `status_history` + recommendation in `payload_json`; `record_run_jobs` | Recommendation/status persisted in SQLite; re-scoring idempotent | `tests/test_cumulative_history.py::test_record_lifecycle_new_unchanged_updated` |
| 14 | Provider extension contract | SKILL.md Step 2 search-output contract (title, company, location, date, url); `/add-portal`; `tests/test_scrape_contract.py` | `atlas/discovery/provider.py::DiscoveryProvider` protocol (`discover(queries) -> DiscoveryBatch`); `JobLead` required fields | New providers implement one duck-typed contract; required lead fields enforced by the dataclass | `tests/test_discovery.py::test_provider_failure_isolated` (custom providers) |

## Deliberately omitted (not ported)

| Upstream behavior | Reason omitted |
|-------------------|----------------|
| Denmark-specific portals/queries (jobnet, jobdanmark) | Atlas is India-only; geography is policy, not a portal list |
| Private candidate data / CV / cover-letter assets | Candidate PII is gitignored and never exposed to discovery workers |
| LaTeX / document generation | Out of scope; Atlas output is report-only Excel |
| Auto-apply / application submission | Hard safety boundary — Atlas never applies, logs in, or submits forms |
| LinkedIn-as-official-truth | Portals/aggregators are leads only; acceptance requires official employer/ATS detail |
| JSON `seen_jobs.json` state file | Replaced by durable SQLite canonical store (concurrent, transactional) |
| Time-based automatic expiry to CLOSED | Atlas requires explicit closure evidence; a stale/inaccessible job becomes REVERIFY_REQUIRED, never silently closed |
