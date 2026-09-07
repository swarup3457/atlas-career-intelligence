"""Atlas LangGraph reliability test — Stage 2 (separate process).

Launched as a genuinely new Python process AFTER Stage 1 has abruptly
terminated. Connects to the SAME durable SQLite checkpoint database and
the SAME thread/run identifier, and resumes processing.

Crucially, this script does NOT reconstruct progress from any hardcoded
assumption about what Stage 1 did. It only asks LangGraph for the current
persisted state (``graph.get_state``) and continues invoking the graph
with an empty input, letting the checkpointed state itself determine what
remains to be done.
"""

from __future__ import annotations

import sys

from langgraph.checkpoint.sqlite import SqliteSaver

from langgraph_reliability_graph import NUM_COMPANIES, THREAD_ID, build_graph


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else r"C:\Atlas\state\langgraph_reliability.sqlite"
    config = {"configurable": {"thread_id": THREAD_ID}}

    with SqliteSaver.from_conn_string(db_path) as checkpointer:
        graph = build_graph().compile(checkpointer=checkpointer)

        existing = graph.get_state(config)
        if not existing.values:
            print("[stage2] ERROR: no persisted state found - cannot resume.")
            sys.exit(2)

        resumed_state = existing.values
        completed_at_resume = list(resumed_state.get("completed_companies", []))
        remaining_at_resume = list(resumed_state.get("remaining_companies", []))

        print("RESUMED_FROM_PERSISTED_STATE=YES")
        print(f"[stage2] Completed at resume: {len(completed_at_resume)} of {NUM_COMPANIES}")
        print(f"[stage2] Remaining at resume: {len(remaining_at_resume)}")

        # Continue processing - one attempt per invoke - purely driven by
        # whatever LangGraph reports as the current persisted state.
        state = resumed_state
        while state.get("run_status") != "COMPLETE" and state.get("remaining_companies"):
            state = graph.invoke({}, config)

        # Final invoke once remaining is empty flips run_status to COMPLETE.
        if state.get("run_status") != "COMPLETE":
            state = graph.invoke({}, config)

        completed_final = list(state.get("completed_companies", []))

        # Verify none of the companies completed before the restart were
        # ever reprocessed (i.e. every pre-restart completed company is
        # still present exactly once, and attempt_log only records ONE
        # SUCCESS attempt for each of them).
        attempt_log = state.get("attempt_log", [])
        success_counts: dict[str, int] = {}
        for entry in attempt_log:
            if entry["result"] == "SUCCESS":
                success_counts[entry["company"]] = success_counts.get(entry["company"], 0) + 1

        reprocessed = [c for c in completed_at_resume if success_counts.get(c, 0) != 1]

        print(f"[stage2] Completed final: {len(completed_final)} of {NUM_COMPANIES}")
        print(f"[stage2] Remaining final: {len(state.get('remaining_companies', []))}")
        print(f"[stage2] Failed permanently: {len(state.get('failed_companies', []))}")
        print(f"[stage2] Run status: {state.get('run_status')}")
        print(f"PREVIOUSLY_COMPLETED_REPROCESSED={'YES' if reprocessed else 'NO'}")
        if reprocessed:
            print(f"[stage2] Reprocessed companies (unexpected): {reprocessed}")


if __name__ == "__main__":
    main()
