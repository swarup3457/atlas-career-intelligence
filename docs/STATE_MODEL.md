# Atlas State Model

## Two separate SQLite databases, on purpose

1. **LangGraph checkpoint DB** (`atlas/orchestration/checkpoints.py`,
   backed by `langgraph-checkpoint-sqlite`'s `SqliteSaver`) — durable
   graph-execution state (which queue items remain, retry counts,
   attempt log) keyed by `thread_id`. This is the exact mechanism already
   PROVEN in `tests/langgraph_reliability_graph.py` and
   `tests/real_web_orchestration_graph.py`, and generalized (not
   rewritten) in `atlas/orchestration/graph.py`.
2. **Atlas business state DB** (`atlas/persistence/sqlite.py`,
   `StateStore`) — Atlas's own bookkeeping tables that the future
   Workspace Agent business logic will read/write: `runs`, `tasks`,
   `companies`, `company_checks`, `sources`, `jobs`, `job_sources`,
   `attempts`, `failures`, `continuation`, `human_interventions`, plus a
   `schema_migrations` table for versioning (`SCHEMA_VERSION` constant in
   the module).

These are intentionally not merged: LangGraph owns *execution*
continuation; `StateStore` owns *business* continuation (the
`continuation` table duplicates remaining/completed lists in a
business-readable form independent of LangGraph's internal checkpoint
format, so tooling/reports never need to parse LangGraph's checkpoint
blobs directly).

GitHub is explicitly NOT used as a high-frequency checkpoint database —
see `docs/CONTROLLER_ABSTRACTION.md`/`atlas/persistence/github.py` — it
is durable history/export only.

## Terminal status model

Defined in `atlas/models/__init__.py` (`TaskStatus` enum):

| Status | Terminal? | Meaning |
|---|---|---|
| `SUCCESS` | yes | Task completed and produced results. |
| `NO_RELEVANT_RESULTS` | yes | Task completed; extraction succeeded but found zero relevant items. |
| `EXTRACTION_UNRESOLVED` | yes | Page/task was reachable, but extraction logic itself failed/raised — **NOT** the same as `NO_RELEVANT_RESULTS`. |
| `ACCESS_LIMITED` | yes | A deliberate access-limitation control was detected (CAPTCHA/anti-bot/policy block); counts as processed for queue completion. |
| `PERMANENT_FAILURE` | yes | Retry budget exhausted on a transient failure category. |
| `CLOSED` / `SKIPPED` | yes | Task intentionally not processed further. |
| `LOGIN_REQUIRED` / `WAITING_FOR_HUMAN` | requires human | Needs `atlas.browser.intervention.escalate_for_human`. |
| `TRANSIENT_FAILURE` / `IN_PROGRESS` / `PENDING` | no | In-flight / not yet resolved. |

### Why `EXTRACTION_UNRESOLVED` exists as its own status

An earlier ad-hoc Accenture career-page test found the search interface
but could not determine, from the page content alone, whether job results
existed — and an earlier version of that test incorrectly/ambiguously
reported this as "no relevant results." That is wrong: "we could not tell"
and "we could tell and there were none" are different facts with
different next actions (the first should perhaps be retried with an
alternate extraction strategy later; the second should not be). The
foundation build makes this an explicit, structurally distinct status
(`atlas/models/__init__.py`), enforced by the retry policy
(`atlas/orchestration/retry.py`: `ErrorCategory.SELECTOR_UNCERTAINTY` maps
only to `EXTRACTION_UNRESOLVED`, never to `NO_RELEVANT_RESULTS`), and
covered by an explicit regression assertion in
`tests/test_foundation_suite.py` (section D and E).

`CareerPageWorker` (`atlas/workers/career_page.py`) implements this
distinction concretely: if reading the title or extracting job links
itself raises an exception, the outcome is `EXTRACTION_UNRESOLVED`; only
if extraction succeeds and yields zero job links is the outcome
`NO_RELEVANT_RESULTS`.

## Retry policy summary

Centralized in `atlas/orchestration/retry.py::evaluate()`. See
`docs/ARCHITECTURE.md` and the module's own docstring for full detail.
Key invariants (each covered by `tests/test_foundation_suite.py` section
D):

- Retry budget is a hard integer; exceeding it always yields
  `PERMANENT_FAILURE`, never another retry.
- `CAPTCHA`, `MFA`, `ANTI_BOT`, `LOGIN_WALL` are **never** retried — they
  map directly to an escalation/limitation status instead, because
  retrying against a security control would look like an attempted
  bypass.
- `SELECTOR_UNCERTAINTY` always yields `EXTRACTION_UNRESOLVED`.

## Deduplication

`StateStore.jobs.dedupe_key` exists as a schema column today. The actual
deduplication *rule* (what constitutes a duplicate job posting across
sources) is intentionally NOT implemented yet — it depends on business
rules that will arrive with the Workspace Atlas Agent import (see
`agents/DEDUPLICATION.agent.md`).
