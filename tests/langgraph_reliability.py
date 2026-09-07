"""Atlas LangGraph reliability test — parent harness.

Proves, using genuinely separate Python processes, that the Atlas
LangGraph reliability graph:

    1. Durably checkpoints progress (SQLite-backed checkpointer).
    2. Handles retries deterministically within a fixed budget.
    3. Restarts/resumes correctly after an abrupt process stop.
    4. Loses no completed work across the restart.
    5. Does not reprocess already-completed companies after restart.
    6. Produces correct completion accounting (attempts vs. completions).

Usage:
    C:\\Atlas\\.venv\\Scripts\\python.exe C:\\Atlas\\tests\\langgraph_reliability.py

No web access, no LLM calls, no real company data - this test exists only
to validate LangGraph orchestration mechanics.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(r"C:\Atlas\state")
DB_PATH = STATE_DIR / "langgraph_reliability.sqlite"
PYTHON = r"C:\Atlas\.venv\Scripts\python.exe"

sys.path.insert(0, str(TESTS_DIR))

from langgraph_reliability_graph import (  # noqa: E402
    COMPANIES,
    INTENTIONAL_FIRST_ATTEMPT_FAILURES,
    MAX_RETRIES,
    NUM_COMPANIES,
    THREAD_ID,
)


def _clean_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def _run_subprocess(script_name: str) -> tuple[int, str]:
    script_path = TESTS_DIR / script_name
    proc = subprocess.run(
        [PYTHON, str(script_path), str(DB_PATH)],
        cwd=str(TESTS_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    print(output)
    return proc.returncode, output


def _extract(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def main() -> int:
    failures: list[str] = []

    print("=== Cleaning test state ===")
    _clean_state()

    print("\n=== Launching Stage 1 (new process) ===")
    rc1, out1 = _run_subprocess("langgraph_reliability_stage1.py")
    if rc1 != 0:
        print(f"[harness] Stage 1 exited with code {rc1}")
        failures.append(f"Stage 1 process failed with exit code {rc1}")

    simulated_stop = _extract(r"SIMULATED_PROCESS_STOP=(\w+)", out1)
    completed_before_stop = _extract(r"COMPLETED_BEFORE_STOP=(\d+)", out1)

    print("\n=== Launching Stage 2 (new, separate process) ===")
    rc2, out2 = _run_subprocess("langgraph_reliability_stage2.py")
    if rc2 != 0:
        print(f"[harness] Stage 2 exited with code {rc2}")
        failures.append(f"Stage 2 process failed with exit code {rc2}")

    resumed = _extract(r"RESUMED_FROM_PERSISTED_STATE=(\w+)", out2)
    reprocessed = _extract(r"PREVIOUSLY_COMPLETED_REPROCESSED=(\w+)", out2)
    run_status_final = _extract(r"Run status: (\w+)", out2)

    # ------------------------------------------------------------------
    # Independently re-open the checkpoint DB to verify final state
    # directly, rather than trusting subprocess stdout alone.
    # ------------------------------------------------------------------
    from langgraph.checkpoint.sqlite import SqliteSaver

    from langgraph_reliability_graph import build_graph

    with SqliteSaver.from_conn_string(str(DB_PATH)) as checkpointer:
        graph = build_graph().compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": THREAD_ID}}
        final_state = graph.get_state(config).values

    planned = final_state.get("planned_companies", [])
    completed = final_state.get("completed_companies", [])
    remaining = final_state.get("remaining_companies", [])
    failed = final_state.get("failed_companies", [])
    retry_counts = final_state.get("retry_counts", {})
    attempt_log = final_state.get("attempt_log", [])
    run_status = final_state.get("run_status")

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------
    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    check(len(set(planned)) == NUM_COMPANIES, f"planned unique companies != {NUM_COMPANIES} (got {len(set(planned))})")
    check(len(set(completed)) == NUM_COMPANIES, f"completed unique companies != {NUM_COMPANIES} (got {len(set(completed))})")
    check(len(completed) == len(set(completed)), "a company was counted as completed more than once")
    check(len(remaining) == 0, f"remaining companies != 0 (got {len(remaining)})")
    check(len(failed) == 0, f"permanently failed companies != 0 (got {len(failed)})")
    check(run_status == "COMPLETE", f"final run_status != COMPLETE (got {run_status})")

    def attempts_for(company: str) -> list[dict]:
        return [e for e in attempt_log if e["company"] == company]

    for special in INTENTIONAL_FIRST_ATTEMPT_FAILURES:
        entries = attempts_for(special)
        results = [e["result"] for e in entries]
        check(
            results == ["FAILURE", "SUCCESS"],
            f"{special} attempt history != [FAILURE, SUCCESS] (got {results})",
        )
        check(
            retry_counts.get(special, 0) <= MAX_RETRIES,
            f"{special} exceeded retry budget (retry_counts={retry_counts.get(special)})",
        )

    check(resumed == "YES", "Stage 2 did not report resuming from persisted state")
    check(reprocessed == "NO", "Stage 2 reprocessed one or more already-completed companies")
    check(simulated_stop == "YES", "Stage 1 did not report a simulated process stop")
    check(completed_before_stop == "9", f"Stage 1 did not stop at exactly 9 completions (got {completed_before_stop})")

    # No successful company counted twice in the attempt log.
    success_counts: dict[str, int] = {}
    for e in attempt_log:
        if e["result"] == "SUCCESS":
            success_counts[e["company"]] = success_counts.get(e["company"], 0) + 1
    duplicated_success = [c for c, n in success_counts.items() if n > 1]
    check(not duplicated_success, f"companies with duplicated SUCCESS attempts: {duplicated_success}")

    total_attempts = len(attempt_log)
    total_successes = sum(1 for e in attempt_log if e["result"] == "SUCCESS")
    total_failures = sum(1 for e in attempt_log if e["result"] == "FAILURE")

    company_07_attempts = len(attempts_for("COMPANY_07"))
    company_14_attempts = len(attempts_for("COMPANY_14"))

    overall_pass = not failures

    print("\n=== LANGGRAPH RELIABILITY TEST REPORT ===")
    print(f"LANGGRAPH_RELIABILITY_TEST={'PASS' if overall_pass else 'FAIL'}")
    print()
    print("LangGraph version: 1.2.11")
    print("Checkpoint backend: langgraph-checkpoint-sqlite (SqliteSaver)")
    print(f"Checkpoint DB: {DB_PATH}")
    print("Processes used: 2 (Stage 1 subprocess + Stage 2 subprocess), independent verification in parent process")
    print(f"Planned companies: {len(set(planned))}")
    print(f"Completed companies: {len(set(completed))}")
    print(f"Remaining companies: {len(remaining)}")
    print(f"Permanent failures: {len(failed)}")
    print(f"COMPANY_07 attempts: {company_07_attempts} ({[e['result'] for e in attempts_for('COMPANY_07')]})")
    print(f"COMPANY_14 attempts: {company_14_attempts} ({[e['result'] for e in attempts_for('COMPANY_14')]})")
    print(f"Completed before simulated stop: {completed_before_stop}")
    print(f"Previously completed companies reprocessed after restart: {reprocessed}")
    print(f"Restart/resume: {'YES' if resumed == 'YES' else 'NO'}")
    print(f"Retry logic: total attempts={total_attempts}, successes={total_successes}, failures={total_failures}, max retries observed={max(retry_counts.values()) if retry_counts else 0} (budget={MAX_RETRIES})")
    print(f"Overall result: {'PASS' if overall_pass else 'FAIL'}")

    if failures:
        print("\nFailure details:")
        for f in failures:
            print(f"  - {f}")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
