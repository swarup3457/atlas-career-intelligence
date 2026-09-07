"""Atlas LangGraph reliability test — Stage 1 (separate process).

Starts a brand-new run against the durable SQLite checkpoint database and
processes companies one attempt per graph.invoke() call. Once exactly 9
UNIQUE companies have completed successfully, this process intentionally
terminates (simulating a crash/restart) via os._exit, leaving the last
committed checkpoint intact.

This script performs NO reconstruction of progress from hardcoded
assumptions - it seeds the initial state once (only because no checkpoint
exists yet for this thread) and then lets LangGraph's persisted state
drive every subsequent step.
"""

from __future__ import annotations

import os
import sys

from langgraph.checkpoint.sqlite import SqliteSaver

from langgraph_reliability_graph import (
    NUM_COMPANIES,
    THREAD_ID,
    build_graph,
    initial_state,
)

STOP_AFTER_COMPLETED = 9


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else r"C:\Atlas\state\langgraph_reliability.sqlite"
    config = {"configurable": {"thread_id": THREAD_ID}}

    # Enter the checkpointer's context manager manually (without exiting
    # it) so that when we simulate a crash below via os._exit, we do NOT
    # perform any clean connection shutdown. Each checkpoint write is
    # already committed to disk individually (see SqliteSaver.cursor()),
    # so this proves durability survives a genuinely abrupt process stop.
    checkpointer_cm = SqliteSaver.from_conn_string(db_path)
    checkpointer = checkpointer_cm.__enter__()

    graph = build_graph().compile(checkpointer=checkpointer)

    existing = graph.get_state(config)
    if existing.values:
        print(
            "[stage1] ERROR: expected a clean checkpoint database but "
            "found existing state. Aborting to avoid corrupting the test."
        )
        sys.exit(2)

    # First invoke seeds the run (no persisted state exists yet).
    state = graph.invoke(initial_state(), config)

    completed = len(state.get("completed_companies", []))
    while completed < STOP_AFTER_COMPLETED and state.get("run_status") != "COMPLETE":
        state = graph.invoke({}, config)
        completed = len(state.get("completed_companies", []))

    print(f"[stage1] Completed so far: {completed} of {NUM_COMPANIES}")
    print(f"[stage1] Remaining: {len(state.get('remaining_companies', []))}")
    print(f"[stage1] Attempts logged: {len(state.get('attempt_log', []))}")

    print("SIMULATED_PROCESS_STOP=YES")
    print(f"COMPLETED_BEFORE_STOP={completed}")

    # Force an abrupt process termination (simulated crash) rather than a
    # clean interpreter/connection shutdown, to prove the durable
    # checkpoint already committed to disk survives a hard stop.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
