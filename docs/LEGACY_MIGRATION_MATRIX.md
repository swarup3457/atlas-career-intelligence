# Legacy Migration Matrix

Status: **implemented (Phase 1B).** This is the human-readable companion to the
machine-readable matrix in `atlas/imports/migration.py` (exported to
`docs/migration/legacy_migration_matrix.json`). Every one of the **44** imported
files carries a deterministic decision and privacy class. The values below are
sourced from that module — read it for the authoritative record.

## Allowed decisions

`AUTHORITATIVE`, `KEEP_AS_POLICY`, `KEEP_AS_SKILL`, `MOVE_TO_PYTHON`,
`MOVE_TO_LANGGRAPH`, `SPLIT`, `REWRITE`, `REGRESSION_ONLY`,
`LEGACY_IMPORT_ONLY`, `BLOCKED_PENDING_POLICY`, `RETIRE`.

## Privacy classes

`PUBLIC_POLICY` (public company/source/search info), `PRIVATE_PII` (candidate
PII — never committed), `PRIVATE_TEMPLATE` (local report fixture — never
committed), `META` (delivery/kit metadata).

## Root files (15)

| File | Purpose | Decision | New home |
|---|---|---|---|
| `00_CURRENT_LIVE_AGENT_INSTRUCTIONS.md` | Objective, request modes, six lanes, India geography, source breadth, dynamic universe | `SPLIT` | `config/policy/*.yaml` + production graph + thin `AGENTS.md` pointer |
| `01_AGENT_EDIT_INSTRUCTIONS_V5.md` | Obsolete single-active-Excel editor map | `RETIRE` | Historical note only (this doc) |
| `02_CORE_OPERATING_POLICY_V5.md` | Mission, safety boundaries, lanes, evidence classes | `KEEP_AS_POLICY` | `config/policy/` + `atlas/policy/` + run rules |
| `03_DAILY_SCHEDULE_PROMPT_V5.md` | Daily delta/deep flow and source sequence | `MOVE_TO_PYTHON` | `atlas/planning` cadence planner + graph phases |
| `04_COMPANY_RECHECK_CADENCE_V5.md` | Tiering, due selection, no permanent Completed | `KEEP_AS_POLICY` | `config/policy/cadence.yaml` + scheduler |
| `05_SOURCE_AND_ATS_REGISTRY_V5.md` | ATS/portal universe, portal→official flow | `KEEP_AS_POLICY` | `config/policy/source_policy.yaml` + adapter registry (`adapter_key`) |
| `06_CANDIDATE_PROFILE_VERIFIED.md` | Conservative candidate evidence baseline (PII) | `LEGACY_IMPORT_ONLY` · `PRIVATE_PII` | `atlas/candidate` importer → gitignored local store |
| `06_COMPANY_SEARCH_UNIVERSE_V5.md` | 108-company Tier A seed + dynamic-universe rule | `KEEP_AS_POLICY` | `config/policy/company_seed.yaml` (public) |
| `07_TRACKER_SCHEMA_V5.md` | Old workbook operational tracker schema | `RETIRE` | Field-mapping reference only; SQLite + report mapper own it |
| `08_COMPANY_SCHEDULER_LIVE_VERIFICATION_STEP2.md` | Fixed ten-company smoke test, apply-path verification | `REGRESSION_ONLY` | Regression fixtures/tests (never production policy) |
| `09_APPEND_ONLY_GITHUB_EVENT_STORE_STEP2C.md` | Immutable GitHub event architecture | `LEGACY_IMPORT_ONLY` | Optional sanitized remote-audit exporter |
| `10_STEP3_HIGH_COVERAGE_INDIA_DISCOVERY.md` | Broad India discovery, dynamic employer growth | `KEEP_AS_POLICY` | Search policy config + `atlas/planning` planner |
| `11_LIVE_CHANNEL_AND_EXCEL_OUTPUT_CONTRACT.md` | Eight-sheet workbook + truthful coverage reporting | `KEEP_AS_POLICY` | `config/policy/report_mapping.yaml` + `atlas/reporting` |
| `12_STEP3_VERIFICATION_HARDENING.md` | Verification/freshness/closure policy | `AUTHORITATIVE` | `config/policy/verification.yaml` + verification rules + status axes |
| `13_PRODUCTION_SEARCH_PHASE_ISOLATION_HOTFIX.md` | Search-first phase isolation | `AUTHORITATIVE` | `atlas/orchestration` production multi-phase graph |

**The two `06_` files must never be collapsed:** one is private PII
(`LEGACY_IMPORT_ONLY`), the other is public policy (`KEEP_AS_POLICY`).

## Skills (10)

| Skill | Decision |
|---|---|
| `application-brief-generator` | `SPLIT` (methodology skill + typed renderer) |
| `application-response-optimizer` | `SPLIT` (deterministic analytics + skill) |
| `application-tracker-deduper` | `RETIRE` (SQLite/Python own dedupe) |
| `company-cadence-orchestrator` | `MOVE_TO_PYTHON` (scheduler + thin skill) |
| `company-career-page-sweeper` | `MOVE_TO_LANGGRAPH` (planner + adapters, Phase 1C) |
| `international-eligibility-check` | `SPLIT` (rules + reviewer skill) |
| `job-discovery-verification` | `SPLIT` (adapters/planner + reviewer skill) |
| `job-market-trend-analyzer` | `REWRITE` (drop hardcoded workbook) |
| `recruiter-outreach-prep` | `BLOCKED_PENDING_POLICY` (missing policy) |
| `resume-job-matcher` | `SPLIT` (matching gates + analyst skill) |

Each skill's `agents/openai.yaml` metadata is `KEEP_AS_SKILL` (recreated
locally), except those retired/blocked with their skill.

## Broken references detected

1. **Missing `05_PUBLIC_RECRUITER_CONTACT_POLICY.md`** — referenced by the
   `recruiter-outreach-prep` skill, which is therefore `BLOCKED_PENDING_POLICY`.
2. **Stale/mismatched resume filename** — file `00` references a resume
   filename that does not match the resume PDF actually shipped in the package.
   Both name-bearing filenames are redacted from the committed matrix for
   privacy; the file is accounted for by its sha256.
3. **Hardcoded `Atlas_MASTER_ACTIVE_20260808.xlsx`** — in the
   `job-market-trend-analyzer` skill; removed by the `REWRITE`.

`tests/test_phase1b_import_matrix.py` enforces all three detections.
