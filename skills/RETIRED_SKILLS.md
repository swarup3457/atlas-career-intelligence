# Retired Workspace skills (Phase 1B)

These legacy Workspace skills are **retired as skills**; their core moved into
deterministic Python/LangGraph. They are documented here (and in
`atlas/imports/migration.py`) so the migration is auditable, but they are not
active skills and carry no workbook mechanics.

| Legacy skill | Decision | New home |
|---|---|---|
| `application-tracker-deduper` | RETIRE | `atlas/persistence` + `atlas/data_integrity` own canonicalization/dedupe/state (SQLite). The old single active `Atlas_MASTER_ACTIVE_` workbook model is gone; Excel is report-only. |
| `company-cadence-orchestrator` | MOVE_TO_PYTHON | Tier/due/queue selection is `atlas/planning` (cadence policy in `config/policy/cadence.yaml`). Only a thin policy explanation remains, if any. |
| `company-career-page-sweeper` | MOVE_TO_LANGGRAPH | Career-page/ATS execution and coverage are owned by `atlas/planning` + source adapters (Phase 1C). "About 60 domains" is a batch hint, never completion. |

No retired skill requires an active workbook, and none owns loops, retries,
coverage, pagination, or persistence.
