# Atlas Architecture (Foundation Build — Phase 0)

Status legend used throughout this document:

- **PROVEN** — exercised against real systems (real browser, real LangGraph
  checkpoint restart) and verified to work.
- **IMPLEMENTED** — real, working code exists and has automated test
  coverage, but has not been exercised against production-scale/real-world
  conditions beyond the foundation tests.
- **SCAFFOLDED** — an interface/class/file exists to hold future logic, but
  contains no real business behavior yet (calling it does something safe
  and explicit — e.g. raises `NotImplementedError`, or returns a no-op
  result — never something that silently pretends to work).
- **NOT YET BUILT** — nothing exists yet beyond a mention in this doc.

## Layered architecture

```
LLM CONTROLLER ADAPTER        atlas/controllers/          SCAFFOLDED
        |
        v
LANGGRAPH GOVERNOR             atlas/orchestration/        PROVEN (generic engine)
        |
        +-- company/source queue        (QueueState)        PROVEN
        +-- retries                     (retry.py)          PROVEN
        +-- checkpoints                 (checkpoints.py)    PROVEN
        +-- continuation                (graph.py)          PROVEN
        +-- failure/completion accounting                   PROVEN
        |
        v
WORKER LAYER                   atlas/workers/
        +-- career_page.py                                  IMPLEMENTED (mechanics proven; no business rules)
        +-- company.py                                      SCAFFOLDED (placeholder)
        +-- verification.py                                 SCAFFOLDED (placeholder)
        |
        v
LOCAL SQLITE STATE             atlas/persistence/sqlite.py  IMPLEMENTED
        |
        +------------------+
        |                  |
        v                  v
      GitHub              Excel
   atlas/persistence/   atlas/reporting/
   github.py            excel.py
   SCAFFOLDED (dry-run  SCAFFOLDED (generic
   only, no production  sheet writer, no
   writes)              final business schema)
```

## What "PROVEN" means concretely in this build

The following have been exercised end-to-end with real systems, not just
unit-level assertions:

1. **Playwright + installed Google Chrome** (`channel="chrome"`) against a
   dedicated persistent profile (`C:\Atlas\.browser-profile-chrome`) — this
   profile already carries an authenticated LinkedIn session from an
   earlier session's manual sign-in.
2. **headless=True reuse of that authenticated profile** —
   `tests/test_headless_profile_diagnostic.py` proved the session survives
   headless launch + close + relaunch, and that the profile lock correctly
   refuses a second concurrent launch.
3. **LangGraph 1.2.11 + langgraph-checkpoint-sqlite** durable
   checkpoint/restart/resume across separate OS processes
   (`tests/langgraph_reliability.py`, re-run as part of this build,
   still passes: `LANGGRAPH_RELIABILITY_TEST=PASS`).
4. **Real Playwright + LangGraph orchestration** against 5 real employer
   career sites, including a simulated first-attempt failure and a genuine
   Cloudflare-style access-limitation classification
   (`tests/real_web_orchestration_run.py`, re-run as part of this build,
   still passes: `ATLAS_REAL_WEB_ORCHESTRATION_TEST=PASS`).
5. The foundation's own generalized versions of the above
   (`atlas/orchestration/graph.py` + `atlas/browser/manager.py`) were
   proven against a synthetic 3-item, 1-simulated-retry queue with a real
   Chrome browser and a real reopened SQLite checkpoint DB — see
   `tests/test_foundation_suite.py`, section I.

## Deliberate separation of concerns (spec section 3)

`atlas/orchestration/*` (LangGraph + code) owns every piece of "what
remains / what completed / how many retries / is this permanent" state.
The `Controller` protocol (`atlas/controllers/base.py`) is never asked to
remember progress — it only ever receives an already-scoped piece of work
and returns an answer. `NullController` demonstrates the system runs with
`controller=none` for fully deterministic tests, satisfying the
requirement that deterministic workers must not depend on any LLM.

## Directory map

See the project tree under `C:\Atlas\atlas\` for the concrete
implementation of the layers above. Each subpackage's module docstring
states its own PROVEN/IMPLEMENTED/SCAFFOLDED/NOT YET BUILT status.
