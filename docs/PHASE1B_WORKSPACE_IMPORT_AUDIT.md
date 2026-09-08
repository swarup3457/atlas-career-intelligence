# Phase 1B — Workspace Import Audit

Status: **implemented.** Phase 1B reconciles the private Workspace Agent
package with the local Atlas runtime. It imports the *business intelligence*
of the Workspace product while keeping the local Python/LangGraph runtime as
the execution spine. No candidate PII is committed (see
[PHASE1B_PRIVACY.md](PHASE1B_PRIVACY.md)).

## What was imported

The read-only import package (`C:\Atlas-Agent-Import`) contains **44 files**,
each accounted for — with a deterministic decision and privacy class — in the
machine-readable matrix `atlas/imports/migration.py` and its exported JSON
`docs/migration/legacy_migration_matrix.json`.

| Group | Count | Notes |
|---|---|---|
| Root policy / specification Markdown | 15 | Files `00`–`13` (two distinct `06_` files) |
| Report + candidate artifacts | 3 | 1 Excel template, 2 candidate PDFs (PII, local-only) |
| Skills | 21 | 10 × `SKILL.md` + 10 × `agents/openai.yaml` + 1 reference |
| Kit / delivery metadata | 5 | Manifest, build prompt, deep audit, readme, launcher |

The import is accounting-only: the module stores relative paths, SHA-256
digests, sizes, and decisions — never the imported content or any PII.

## Executive decision

**Keep the local Atlas runtime spine.** Python, LangGraph, SQLite operational
state, Playwright, durable checkpoints, the company + source registry, health
classification, dedupe/identity resolution, fail-safe report-only Excel, and
backup/restore remain the authoritative execution platform.

**Import the Workspace business intelligence.** The six search lanes, India
geography model, experience policy, exclusion families, the 108-company Tier A
seed, recheck cadence, official-first verification, candidate evidence classes,
and the report contract are adopted as typed policy and rules.

**Adopt the `ai-job-search` architecture concepts.** A thin-pointer,
single-source-of-truth layout; self-describing adapters; a search/detail split;
typed results; dynamic adapter discovery keyed by adapter identity; first-class
source health; explicit untrusted-content handling; and a test-per-fix
discipline.

**Reject** four legacy patterns that conflict with the runtime invariants:

- prose/Markdown as the completion governor (loops belong in code);
- flat-file (workbook / GitHub) operational state as the source of truth;
- auto-apply / automated form submission;
- stealth, anti-bot bypass, or CAPTCHA circumvention;
- plaintext credentials in configuration or state.

## Where the decisions live

- `atlas/imports/migration.py` — the PII-free migration matrix (44 records).
- `docs/migration/legacy_migration_matrix.json` — the exported machine copy.
- [LEGACY_MIGRATION_MATRIX.md](LEGACY_MIGRATION_MATRIX.md) — the human-readable
  summary of every decision and the detected broken references.
- [WORKSPACE_RULE_PRECEDENCE.md](WORKSPACE_RULE_PRECEDENCE.md) — how conflicts
  among legacy files are resolved.

## Guardrails preserved

- Excel is **report-only**; SQLite owns operational state.
- LangGraph checkpoints own run continuation.
- GitHub is code/history and an *optional* sanitized audit sink — never a
  high-frequency operational store.
- Candidate evidence is private and never enters the public tree.

Tests: `tests/test_phase1b_import_matrix.py` enforces that every imported file
has a decision, the two `06_` files never collapse, and the known broken
references are flagged.
