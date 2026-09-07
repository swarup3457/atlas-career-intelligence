"""Atlas LangGraph reliability test — shared graph definition.

This module defines a small, deterministic LangGraph graph used ONLY to
prove durable checkpointing / retry handling / restart-resume reliability.

It intentionally contains NO real job-searching logic, NO web access, and
NO LLM/Copilot calls. "Processing a company" is a pure, deterministic,
in-memory function keyed only on the company name and how many times it
has previously been attempted (tracked in persisted graph state).

Deterministic worker rules:
    - COMPANY_07: fails on attempt 1, succeeds on attempt 2.
    - COMPANY_14: fails on attempt 1, succeeds on attempt 2.
    - All other companies succeed on attempt 1.

Retry budget: maximum 2 retries per company (i.e. up to 3 total attempts).
If a company exceeds the retry budget it is moved to ``failed_companies``
permanently; otherwise a failed attempt just re-queues the company at the
back of ``remaining_companies`` so other companies keep making progress.

The graph is intentionally structured so that ONE graph.invoke() call
performs exactly ONE company processing attempt (or, once no companies
remain, marks the run COMPLETE). This lets a driver script stop a process
after an exact number of completions, and lets a fresh process resume by
simply re-invoking the graph against the same durable checkpoint thread —
LangGraph loads the last persisted state automatically, so the driver
never hardcodes or reconstructs progress itself.
"""

from __future__ import annotations

import datetime
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUM_COMPANIES = 20
COMPANIES = [f"COMPANY_{i:02d}" for i in range(1, NUM_COMPANIES + 1)]

# Companies that must deliberately fail on their first attempt.
INTENTIONAL_FIRST_ATTEMPT_FAILURES = {"COMPANY_07", "COMPANY_14"}

MAX_RETRIES = 2  # up to 3 total attempts per company (1 + 2 retries)

THREAD_ID = "atlas-langgraph-reliability-test"

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPLETE = "COMPLETE"


class AtlasReliabilityState(TypedDict):
    planned_companies: list[str]
    completed_companies: list[str]
    remaining_companies: list[str]
    retry_counts: dict[str, int]
    failed_companies: list[str]
    attempt_log: list[dict[str, Any]]
    current_company: Optional[str]
    run_status: str


def initial_state() -> AtlasReliabilityState:
    """Return the seed state for a brand-new run (first invoke only)."""
    return AtlasReliabilityState(
        planned_companies=list(COMPANIES),
        completed_companies=[],
        remaining_companies=list(COMPANIES),
        retry_counts={},
        failed_companies=[],
        attempt_log=[],
        current_company=None,
        run_status=RUN_STATUS_RUNNING,
    )


def _deterministic_worker(company: str, attempt_number: int) -> bool:
    """Pure, deterministic, offline "processing" of a single company.

    No web access, no LLM calls - just a fixed rule used to prove retry
    handling.
    """
    if company in INTENTIONAL_FIRST_ATTEMPT_FAILURES and attempt_number == 1:
        return False
    return True


def process_one(state: AtlasReliabilityState) -> AtlasReliabilityState:
    """Process exactly one company attempt (or finish the run).

    Reads persisted state, performs one deterministic unit of work, and
    returns a full updated state dict. LangGraph checkpoints the result
    after this node completes.
    """
    remaining = list(state.get("remaining_companies", []))
    completed = list(state.get("completed_companies", []))
    retry_counts = dict(state.get("retry_counts", {}))
    failed = list(state.get("failed_companies", []))
    attempt_log = list(state.get("attempt_log", []))

    if not remaining:
        # Nothing left to do - mark the run complete (idempotent).
        return {
            **state,
            "current_company": None,
            "run_status": RUN_STATUS_COMPLETE,
        }

    company = remaining.pop(0)
    attempt_number = retry_counts.get(company, 0) + 1
    success = _deterministic_worker(company, attempt_number)

    attempt_log.append(
        {
            "company": company,
            "attempt_number": attempt_number,
            "result": "SUCCESS" if success else "FAILURE",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )

    if success:
        completed.append(company)
    else:
        retry_counts[company] = attempt_number
        if attempt_number > MAX_RETRIES:
            # Retry budget exhausted - permanently failed.
            failed.append(company)
        else:
            # Still within retry budget - requeue at the back so other
            # companies keep making progress in the meantime.
            remaining.append(company)

    run_status = RUN_STATUS_COMPLETE if not remaining else RUN_STATUS_RUNNING

    return {
        "planned_companies": state.get("planned_companies", list(COMPANIES)),
        "completed_companies": completed,
        "remaining_companies": remaining,
        "retry_counts": retry_counts,
        "failed_companies": failed,
        "attempt_log": attempt_log,
        "current_company": company,
        "run_status": run_status,
    }


def build_graph():
    """Build (uncompiled) the single-node reliability graph."""
    builder = StateGraph(AtlasReliabilityState)
    builder.add_node("process_one", process_one)
    builder.set_entry_point("process_one")
    builder.add_edge("process_one", END)
    return builder
