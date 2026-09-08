# Workspace Rule Precedence

Status: **implemented.** The imported Workspace files disagree with each other
(and, in places, with the local runtime). This document defines the single
precedence graph used to resolve those conflicts, from highest to lowest
authority. The machine-readable decisions live in `atlas/imports/migration.py`.

## Precedence graph (highest → lowest)

1. **Local Atlas architectural invariants.** These override every imported
   file. SQLite is the operational state of record; LangGraph checkpoints own
   run continuation; Excel is report-only; GitHub holds code/history and an
   *optional* audit, never required high-frequency operational state.
2. **`13_PRODUCTION_SEARCH_PHASE_ISOLATION_HOTFIX.md`** — the production
   execution phase order (search-first isolation). Authoritative for *when*
   side effects may run.
3. **`12_STEP3_VERIFICATION_HARDENING.md`** — verification, freshness, closure,
   blocked-source budget, and the separated status axes. Authoritative for
   *how* a lead is verified and classified.
4. **`00_CURRENT_LIVE_AGENT_INSTRUCTIONS.md`** — the overall objective: source
   breadth, the six lanes, India focus, the dynamic (non-whitelist) universe,
   and the request modes.
5. **`10_STEP3_HIGH_COVERAGE_INDIA_DISCOVERY.md`** — the broad, search-first
   market-coverage intent. Kept for intent only; its older verification and
   persistence vocabulary is **not** authoritative (superseded by 12 and 13).
6. **Policy-evidence files `03`, `04`, `05`, `06_COMPANY_SEARCH_UNIVERSE`,
   `11`** — converted into typed configuration (cadence, source policy,
   company seed, report mapping). They inform config; they do not govern
   execution order.
7. **Fixed-scope files `08`, `09`** — regression and legacy references only.
   Their fixed ten-company scope and GitHub-event model must never narrow or
   redefine production behaviour.
8. **Obsolete files `01`, `07`** — the old single-active-workbook architecture.
   Retired; only field semantics survive as a mapping reference.

## Why this order

- **Invariants first** because they encode non-negotiable data-integrity and
  privacy guarantees that predate the import.
- **13 over 12 over 00** because phase isolation is a correctness fix, hardened
  verification is a correctness policy, and the live instructions describe
  intent that both refine.
- **10 below 00** because it is a high-coverage *search* brief whose
  persistence/verification terms were later superseded.
- **Config-evidence below intent** so a stale value in a policy file can never
  override the objective or the correctness rules.
- **Regression/obsolete last** so historical smoke scopes and the retired
  workbook model never leak into production policy.

## Documented deviations

- **GitHub-first persistence (files 00/02/09) is rejected.** The local SQLite
  invariant wins; GitHub becomes an optional sanitized remote audit.
- **Apply-path completion (files 08/09) is superseded by file 12.** A final
  Apply submission is *not* required for `VERIFIED_OFFICIAL`.
- **Batch hints are not caps.** "25–40" (cadence) and "~60 domains" (sweeper)
  are execution chunk sizes, never completion criteria.
- **The two `06_` files are distinct** and must never be collapsed:
  `06_CANDIDATE_PROFILE_VERIFIED.md` is private PII; `06_COMPANY_SEARCH_UNIVERSE_V5.md`
  is public policy.
- **"Completed" is never permanent** — it is a per-cycle fact, not a terminal
  company state.

See [PRODUCTION_SEARCH_ARCHITECTURE.md](PRODUCTION_SEARCH_ARCHITECTURE.md) and
[STATUS_MODEL.md](STATUS_MODEL.md) for the implementations these files govern.
