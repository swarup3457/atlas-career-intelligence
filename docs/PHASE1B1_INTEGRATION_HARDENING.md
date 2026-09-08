# Phase 1B.1 — Production Integration Hardening

Status: **fixture production path PROVEN**. Real adapters still **absent**; live
production still **disabled**. This phase made the sealed coverage plan execute
through the real `SourceRegistry → adapter → executor → worker → attempts → raw
staging → coverage → canonicalize → report` path (fixture/fake only), fixing the
Phase 1B gap where the runtime emitted synthetic canonical jobs directly.

## What changed (by blocker)

| ID | Fix | Where |
|---|---|---|
| P0-1 | Sealed plan executed through the real pipeline, not a synthetic count | `atlas/runtime/production.py`, `atlas/runtime/fixture_pipeline.py` |
| P0-2 | First LangGraph invoke seeds `initial_production_state` | `production.py:_run_graph` |
| P0-3 | Fresh-process resume rehydrates policy/ledger/plan/started_at | `production.py`, `tests/test_production_crash_resume.py` |
| P0-4 | No `task_ids[:2000]` truncation; pending ids streamed from SQLite | `production.py`, `production_state.py` |
| P0-5 | Local persistence failure → FAILED, never silent COMPLETE | `production.py:_h_persist_local`, `resolve_terminal` |
| P0-6 | Production RunLock around the run | `production.py:run` |
| P0-7 | Private candidate snapshot in production; synthetic only in fixture mode | `production.py:_ensure_ledger` |
| P0-8 | Evidence transition matrix; parser preserves parent class | `atlas/candidate/models.py`, `importer.py` |
| P0-9 | Apply control is corroboration, not required for VERIFIED_OFFICIAL | `atlas/policy/rules.py` |
| P0-10 | LIVE_DATE_UNKNOWN vs DATE_UNKNOWN vs DATA_CONFLICT | `rules.py:freshness_band` |
| P0-11 | Per-lane child coverage accountability | `atlas/planning/planner.py`, `coverage.py:lane_summary` |
| P0-12 | QuerySignature wired into worker health/yield history | `atlas/sources/worker.py`, `fixture_pipeline.py` |
| P0-13 | Shared `RateLimitedExecutor` in the worker | `atlas/sources/executor.py` |
| P0-14 | Company discovery: family + tenant + site identity, persisted | `atlas/company/discovery.py`, `identity.py` |
| P0-15 | Raw observation staging (schema v7) before canonicalization | `sqlite.py`, `canonicalize.py` |
| P0-16 | Actual checkpoint DB inspected + bounded | `tests/test_production_crash_resume.py` |
| P0-17 | Real terminality gate; missing handler fails closed | `production_graph.py`, `production.py:resolve_terminal` |
| P0-18 | Atomic Excel via `write_workbook_atomic` | `atlas/reporting/mapping.py` |
| P0-19 | Strict typed controller validation + deterministic ceilings | `atlas/controllers/operations.py` |
| P0-20 | Direct read-only PDF review; redacted summary only | `docs/CANDIDATE_PRIVATE_IMPORT.md` |
| P1-9 | Remote audit explicit allowlist | `atlas/persistence/remote_audit.py` |

## Safety invariants (unchanged, enforced)

No real adapter, no live web request, no browser navigation, no auto-apply, no
CAPTCHA/MFA/anti-bot bypass. Fixture discovery uses `FakeAdapter`/`FixtureAdapter`
and committed synthetic fixtures only. Candidate PII never enters the public Git
tree; production evidence is a gitignored snapshot referenced by hash.

See also: `docs/PRODUCTION_RUNTIME.md`, `docs/COVERAGE_EXECUTION.md`,
`docs/VERIFICATION_POLICY.md`, `docs/REPORT_RELIABILITY.md`,
`docs/LEGACY_TABLE_AUTHORITY.md`, `docs/CANDIDATE_PRIVATE_IMPORT.md`.
