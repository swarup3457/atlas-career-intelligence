"""Atlas Test 3 — LangGraph + Playwright real-web orchestration harness.

Runs a single controlled Playwright browser context (installed Google
Chrome, dedicated Atlas profile) driven by a LangGraph state machine that
processes 5 real public company career pages, one attempt per graph
invocation, with durable SQLite checkpointing and bounded retries.

Usage:
    C:\\Atlas\\.venv\\Scripts\\python.exe C:\\Atlas\\tests\\real_web_orchestration_run.py
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from playwright.sync_api import sync_playwright

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from real_web_orchestration_graph import (  # noqa: E402
    COMPANIES,
    MAX_RETRIES,
    SIMULATED_FIRST_FAILURE_COMPANY,
    THREAD_ID,
    build_graph,
    initial_state,
)

STATE_DIR = Path(r"C:\Atlas\state")
OUTPUT_DIR = Path(r"C:\Atlas\output")
DB_PATH = STATE_DIR / "atlas_real_web_test.sqlite"
OUTPUT_JSON = OUTPUT_DIR / "real_web_test_results.json"
PROFILE_DIR = r"C:\Atlas\.browser-profile-chrome"


def _clean_checkpoint_db() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _clean_checkpoint_db()

    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    failures: list[str] = []

    with sync_playwright() as p:
        # One controlled browser context for the whole test, using the
        # already-proven installed Chrome + dedicated Atlas profile. We do
        # not clear cookies/session state and do not touch any personal
        # Chrome profile.
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            channel="chrome",
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            with SqliteSaver.from_conn_string(str(DB_PATH)) as checkpointer:
                graph = build_graph(page).compile(checkpointer=checkpointer)
                config = {"configurable": {"thread_id": THREAD_ID}}

                state = graph.invoke(initial_state(), config)
                while state.get("run_status") != "COMPLETE":
                    state = graph.invoke({}, config)

            completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        finally:
            context.close()

    # -----------------------------------------------------------------
    # Build JSON output
    # -----------------------------------------------------------------
    planned = state.get("planned_companies", [])
    completed = state.get("completed_companies", [])
    remaining = state.get("remaining_companies", [])
    failed = state.get("failed_companies", [])
    access_limited = state.get("access_limited_companies", [])
    company_results = state.get("company_results", {})
    attempt_log = state.get("attempt_log", [])
    retry_counts = state.get("retry_counts", {})
    run_status = state.get("run_status")

    retry_summary = {
        company: {"attempts": retry_counts.get(company, 0)} for company in planned
    }

    output_company_results = []
    for company in planned:
        result = company_results.get(company, {})
        output_company_results.append(
            {
                "company": company,
                "status": result.get("status"),
                "attempts": result.get("attempts"),
                "official_careers_url": result.get("official_careers_url"),
                "final_url": result.get("final_url"),
                "page_title": result.get("page_title"),
                "career_platform": result.get("career_platform"),
                "search_interface_accessible": result.get("search_interface_accessible"),
                "visible_job_count": result.get("visible_job_count"),
                "sample_jobs": result.get("sample_jobs", []),
                "access_limitation": result.get("access_limitation"),
                "error": result.get("error"),
            }
        )

    output_doc = {
        "run_id": THREAD_ID,
        "started_at": started_at,
        "completed_at": completed_at,
        "run_status": run_status,
        "planned_count": len(planned),
        "completed_count": len(completed),
        "remaining_count": len(remaining),
        "retry_summary": retry_summary,
        "company_results": output_company_results,
    }

    OUTPUT_JSON.write_text(json.dumps(output_doc, indent=2), encoding="utf-8")

    # -----------------------------------------------------------------
    # Re-parse the JSON independently to prove it is valid.
    # -----------------------------------------------------------------
    try:
        reparsed = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
        json_ok = True
    except Exception as exc:  # noqa: BLE001
        reparsed = None
        json_ok = False
        failures.append(f"JSON output failed to parse: {exc}")

    # -----------------------------------------------------------------
    # Assertions
    # -----------------------------------------------------------------
    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    check(len(set(planned)) == 5, f"planned companies != 5 (got {len(set(planned))})")
    check(len(set(completed)) == 5, f"processed companies != 5 (got {len(set(completed))})")
    check(len(remaining) == 0, f"remaining companies != 0 (got {len(remaining)})")

    deloitte_attempts = retry_counts.get(SIMULATED_FIRST_FAILURE_COMPANY, 0)
    check(
        deloitte_attempts >= 2,
        f"{SIMULATED_FIRST_FAILURE_COMPANY} attempts < 2 (got {deloitte_attempts})",
    )

    for company in planned:
        check(
            retry_counts.get(company, 0) <= MAX_RETRIES + 1,
            f"{company} exceeded retry budget (attempts={retry_counts.get(company, 0)})",
        )

    for company in planned:
        result = company_results.get(company)
        check(result is not None, f"{company} has no terminal result recorded")
        if result is not None:
            check(
                result.get("status") in {
                    "SUCCESS",
                    "NO_RELEVANT_RESULTS",
                    "ACCESS_LIMITED",
                    "PERMANENT_FAILURE_AFTER_RETRIES",
                },
                f"{company} does not have a valid terminal status (got {result.get('status')})",
            )

    check(json_ok, "JSON output does not exist or does not parse")
    check(run_status == "COMPLETE", f"final run_status != COMPLETE (got {run_status})")

    overall_pass = not failures

    # -----------------------------------------------------------------
    # Final report
    # -----------------------------------------------------------------
    success_count = sum(1 for r in company_results.values() if r.get("status") == "SUCCESS")
    no_relevant_count = sum(1 for r in company_results.values() if r.get("status") == "NO_RELEVANT_RESULTS")
    access_limited_count = len(access_limited)
    failed_count = len(failed)

    print("\n=== ATLAS REAL-WEB ORCHESTRATION TEST REPORT ===")
    print(f"ATLAS_REAL_WEB_ORCHESTRATION_TEST={'PASS' if overall_pass else 'FAIL'}")
    print()
    print(f"Companies planned: {len(set(planned))}")
    print(f"Companies processed: {len(set(completed))}")
    print(f"Remaining: {len(remaining)}")
    print(f"Success: {success_count}")
    print(f"No relevant results: {no_relevant_count}")
    print(f"Access limited: {access_limited_count}")
    print(f"Permanent failures: {failed_count}")
    print(f"Deloitte attempts: {deloitte_attempts}")
    print("Checkpoint backend: langgraph-checkpoint-sqlite (SqliteSaver)")
    print(f"Checkpoint DB: {DB_PATH}")
    print("Browser: Google Chrome (channel=\"chrome\")")
    print(f"Profile: {PROFILE_DIR}")
    print(f"JSON output: {OUTPUT_JSON}")
    print(f"Overall result: {'PASS' if overall_pass else 'FAIL'}")

    print("\nCompany | Status | Attempts | Search Interface | Jobs Visible | Limitation")
    print("--------|--------|----------|-------------------|--------------|------------")
    for company in planned:
        result = company_results.get(company, {})
        print(
            f"{company} | {result.get('status')} | {result.get('attempts')} | "
            f"{result.get('search_interface_accessible')} | {result.get('visible_job_count')} | "
            f"{result.get('access_limitation') or result.get('error') or '-'}"
        )

    if failures:
        print("\nFailure details:")
        for f in failures:
            print(f"  - {f}")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
