# Atlas Career Intelligence

Local, deterministic career-intelligence platform. The orchestration spine
(Python / LangGraph / SQLite / Playwright, durable checkpoints, source & company
registries, source health, dedupe, report-only Excel, backup/recovery) is
extended in Phase 1B with a typed business-policy layer imported from the
Workspace Agent specification.

**Phase 1B status:** architecture corrections + typed search policy + candidate
evidence ledger + sealed coverage planner + an explicit multi-phase production
search graph, all exercised offline with a `NullController`. **No live source
adapter and no live web search exist yet** — those are deferred to Phase 1C.

Start with [AGENTS.md](AGENTS.md) (the thin canonical pointer), then see `docs/`:

- `docs/PHASE1B_WORKSPACE_IMPORT_AUDIT.md` — what was imported and why
- `docs/WORKSPACE_RULE_PRECEDENCE.md` — legacy conflict precedence
- `docs/PRODUCTION_SEARCH_ARCHITECTURE.md` — source taxonomy/instances/signatures
- `docs/PRODUCTION_PHASE_GRAPH.md` — the multi-phase production graph
- `docs/SEARCH_POLICY.md` — six lanes, geography, experience, exclusions, cadence, seed
- `docs/STATUS_MODEL.md` — separated status axes + verification/freshness/closure
- `docs/CANDIDATE_EVIDENCE_LEDGER.md` — private candidate evidence model
- `docs/LEGACY_MIGRATION_MATRIX.md` — per-file migration decisions
- `docs/PHASE1B_PRIVACY.md` — the public-repository privacy gate

Plus the platform docs: architecture, browser policy, state model, and operations.

