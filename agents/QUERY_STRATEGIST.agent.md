---
name: QUERY_STRATEGIST
description: Expand ambiguous search queries for a lane/geography into additional credible title/technology variants. Reasoning-only; owns no loops, coverage, pagination, or state.
status: THIN_POINTER
version: 1.0.0
---

# QUERY_STRATEGIST

Optional reasoning helper for **ambiguous query expansion only**.

## Owns
- Suggesting additional credible title/technology/synonym variants for a lane
  when the deterministic query hints in `config/policy/search_lanes.yaml` are
  insufficient.

## Never owns
Loops, retries, budgets, coverage, pagination, cursors, persistence, or state —
those belong to `atlas/planning/` and `atlas/orchestration/`. This agent never
decides completion.

## Contract
Typed via `atlas.controllers.operations.QueryExpansionRequest/Result`. Must run
correctly with `NullController` (returns the deterministic base terms). Portal
URL syntax stays in adapters, never here.
