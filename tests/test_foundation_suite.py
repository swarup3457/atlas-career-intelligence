"""Atlas Foundation Test Suite.

Standalone script (run directly with the venv Python, matching the
existing project convention — no pytest dependency) that exercises every
foundation-build requirement from the PHASE 0 spec section 18:

    A. configuration loads
    B. agent/skill definitions can be loaded
    C. SQLite state initializes
    D. retry policy works
    E. terminal states work
    F. BrowserManager launches installed Chrome
    G. background/headless browser test works (or a documented limitation)
    H. visible escalation mechanism can be triggered (simulated LOGIN_REQUIRED)
    I. checkpoint/resume remains functional

Prints one PASS/FAIL line per section plus a final summary. Exits non-zero
if any section fails.

Run:
    C:\\Atlas\\.venv\\Scripts\\python.exe tests\\test_foundation_suite.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def _record(section: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((section, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {section}" + (f" - {detail}" if detail else ""))


def section_a_config() -> None:
    from atlas.config import load_settings, reset_settings_cache

    reset_settings_cache()
    settings = load_settings()
    assert settings.project_root == ROOT, f"unexpected project_root {settings.project_root}"
    assert settings.browser_channel == "chrome"
    assert settings.browser_profile == ROOT / ".browser-profile-chrome"
    assert settings.controller == "none"
    assert settings.retry_budget >= 1
    _record(
        "A. configuration loads",
        True,
        f"project_root={settings.project_root}, channel={settings.browser_channel}, controller={settings.controller}",
    )


def section_b_agents_skills() -> None:
    from atlas.agents_loader import load_agents, load_skills, SpecValidationError

    agents = load_agents(ROOT / "agents")
    skills = load_skills(ROOT / "skills")
    assert len(agents) == 9, f"expected 9 agent specs, found {len(agents)}"
    assert len(skills) >= 1, "expected at least 1 skill spec"
    names = {a.name for a in agents}
    expected = {
        "ORCHESTRATOR", "COMPANY_DISCOVERY", "CAREER_SEARCH", "ATS_SEARCH",
        "PORTAL_SEARCH", "VERIFICATION", "DEDUPLICATION", "CANDIDATE_MATCH", "REPORTING",
    }
    assert names == expected, f"agent name set mismatch: {names ^ expected}"

    # Validation must reject a malformed spec (missing required fields).
    with tempfile.TemporaryDirectory() as tmp:
        bad_dir = Path(tmp) / "agents_bad"
        bad_dir.mkdir()
        (bad_dir / "BAD.agent.md").write_text("---\nstatus: SCAFFOLDED\n---\nbody", encoding="utf-8")
        try:
            load_agents(bad_dir)
            raise AssertionError("expected SpecValidationError for missing required fields")
        except SpecValidationError:
            pass

    _record("B. agent/skill definitions load", True, f"{len(agents)} agents, {len(skills)} skills validated")


def section_c_sqlite_state() -> None:
    from atlas.persistence.sqlite import open_store

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "foundation_test_state.sqlite"
        with open_store(db_path) as store:
            store.create_run("run-foundation-test", controller="none", metadata={"purpose": "foundation-test"})
            store.upsert_task("task-1", "run-foundation-test", "career_page", "PENDING", company="Acme")
            store.record_attempt("attempt-1", "task-1", 1, "SUCCESS")
            store.save_continuation("run-foundation-test", "thread-1", remaining=["b"], completed=["a"])
            store.request_human_intervention("iv-1", "task-1", "LOGIN_REQUIRED")
            store.resolve_human_intervention("iv-1", notes="simulated resolution")
            store.complete_run("run-foundation-test")

            run = store.get_run("run-foundation-test")
            task = store.get_task("task-1")
            attempts = store.list_attempts("task-1")
            continuation = store.get_continuation("run-foundation-test")

            assert run["status"] == "COMPLETE"
            assert task["status"] == "PENDING"
            assert len(attempts) == 1
            assert continuation["remaining"] == ["b"]
            assert continuation["completed"] == ["a"]
        assert db_path.exists()
    _record("C. SQLite state initializes", True, "runs/tasks/attempts/continuation/human_interventions round-tripped")


def section_d_retry_policy() -> None:
    from atlas.models import ErrorCategory, TaskStatus
    from atlas.orchestration.retry import evaluate

    # Transient failure within budget -> retry.
    d1 = evaluate(ErrorCategory.TRANSIENT_NAVIGATION, attempt_number=1, retry_budget=2)
    assert d1.should_retry is True

    # Transient failure exceeding budget -> PERMANENT_FAILURE, no infinite retry.
    d2 = evaluate(ErrorCategory.TRANSIENT_NAVIGATION, attempt_number=3, retry_budget=2)
    assert d2.should_retry is False and d2.terminal_status == TaskStatus.PERMANENT_FAILURE

    # CAPTCHA/MFA/ANTI_BOT/LOGIN_WALL must never be retried.
    for category, expected_status in (
        (ErrorCategory.CAPTCHA, TaskStatus.WAITING_FOR_HUMAN),
        (ErrorCategory.MFA, TaskStatus.WAITING_FOR_HUMAN),
        (ErrorCategory.ANTI_BOT, TaskStatus.ACCESS_LIMITED),
        (ErrorCategory.LOGIN_WALL, TaskStatus.LOGIN_REQUIRED),
    ):
        d = evaluate(category, attempt_number=1, retry_budget=5)
        assert d.should_retry is False, f"{category} must never be retried"
        assert d.terminal_status == expected_status, f"{category} -> {d.terminal_status}"

    # Selector uncertainty -> EXTRACTION_UNRESOLVED, never NO_RELEVANT_RESULTS.
    d3 = evaluate(ErrorCategory.SELECTOR_UNCERTAINTY, attempt_number=1, retry_budget=2)
    assert d3.terminal_status == TaskStatus.EXTRACTION_UNRESOLVED
    assert d3.terminal_status != TaskStatus.NO_RELEVANT_RESULTS

    _record("D. retry policy works", True, "budget enforcement + never-retry categories + EXTRACTION_UNRESOLVED distinction verified")


def section_e_terminal_states() -> None:
    from atlas.models import TaskStatus, is_terminal, requires_human

    assert is_terminal(TaskStatus.SUCCESS)
    assert is_terminal(TaskStatus.NO_RELEVANT_RESULTS)
    assert is_terminal(TaskStatus.EXTRACTION_UNRESOLVED)
    assert is_terminal(TaskStatus.ACCESS_LIMITED)
    assert not is_terminal(TaskStatus.IN_PROGRESS)
    assert not is_terminal(TaskStatus.PENDING)
    assert requires_human(TaskStatus.LOGIN_REQUIRED)
    assert requires_human(TaskStatus.WAITING_FOR_HUMAN)
    assert not requires_human(TaskStatus.SUCCESS)
    # Explicit non-conflation check required by spec section 11.
    assert TaskStatus.EXTRACTION_UNRESOLVED != TaskStatus.NO_RELEVANT_RESULTS
    _record("E. terminal states work", True, "is_terminal/requires_human + EXTRACTION_UNRESOLVED != NO_RELEVANT_RESULTS")


def section_f_g_browser_manager() -> None:
    """F: BrowserManager launches installed Chrome.
    G: background/headless works with a throwaway profile (the
       authenticated-profile case was already proven by
       test_headless_profile_diagnostic.py; here we just confirm the
       BrowserManager class itself launches headless Chrome and can
       navigate/close cleanly, using a disposable profile so we never
       touch the real Atlas profile in this generic test run)."""
    from atlas.browser.manager import BrowserManager

    with tempfile.TemporaryDirectory() as tmp:
        profile_dir = Path(tmp) / "throwaway-profile"
        with BrowserManager(profile_dir, channel="chrome") as manager:
            page = manager.launch(headless=True)
            manager.navigate(page, "https://example.com")
            title = page.title()
            assert "Example" in title, f"unexpected title: {title}"
            assert manager.is_running
            assert manager.is_headless is True
    _record(
        "F. BrowserManager launches installed Chrome",
        True,
        f"headless navigate to example.com succeeded, title='{title}'",
    )
    _record(
        "G. background/headless mode works",
        True,
        "headless=True launch+navigate+close succeeded with a disposable profile "
        "(authenticated-profile reuse already proven separately by "
        "tests/test_headless_profile_diagnostic.py: HEADLESS_PROFILE_DIAGNOSTIC=PASS)",
    )


def section_h_visible_escalation_simulated() -> None:
    """Simulate a LOGIN_REQUIRED condition and drive the escalation
    function with an injected wait_for_user callback (no real human, no
    real credentials) so the mechanism itself is proven callable and
    correct without requiring a visible window for automated test runs."""
    from atlas.browser.intervention import escalate_for_human

    with tempfile.TemporaryDirectory() as tmp:
        profile_dir = Path(tmp) / "throwaway-profile-escalation"
        waited = {"called": False}

        def fake_wait_for_user() -> None:
            waited["called"] = True

        notes: list[str] = []
        result = escalate_for_human(
            profile_dir=profile_dir,
            channel="chrome",
            url="https://example.com",
            reason="LOGIN_REQUIRED (simulated for foundation test)",
            wait_for_user=fake_wait_for_user,
            notify=notes.append,
        )
        assert waited["called"] is True, "wait_for_user callback was not invoked"
        assert result.final_url is not None
        assert result.resolved is True, f"expected resolved=True for example.com, got notes={result.notes}"
    _record(
        "H. visible escalation mechanism triggers",
        True,
        f"simulated LOGIN_REQUIRED escalation completed, resolved={result.resolved}",
    )


def section_i_checkpoint_resume() -> None:
    from atlas.models import ErrorCategory, TaskStatus
    from atlas.orchestration.checkpoints import open_checkpointer, thread_config
    from atlas.orchestration.graph import build_graph, run_to_completion
    from atlas.orchestration.state import initial_queue_state
    from atlas.workers.base import BaseWorker, WorkerError, WorkerOutcome

    class FlakyOnceWorker(BaseWorker):
        """Fails the first attempt for item 'B' only, succeeds otherwise -
        proves retry + checkpoint/resume works with a real graph.invoke
        loop, mirroring the proven Deloitte simulated-failure pattern."""

        name = "flaky-once-test-worker"

        def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
            if item == "B" and attempt_number == 1:
                raise WorkerError(ErrorCategory.TRANSIENT_NAVIGATION, "simulated first-attempt failure")
            return WorkerOutcome(status=TaskStatus.SUCCESS, payload={"item": item})

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "foundation_checkpoint_test.sqlite"
        worker = FlakyOnceWorker()
        builder = build_graph(worker, retry_budget=2)
        seed = initial_queue_state(["A", "B", "C"])
        config = thread_config("foundation-checkpoint-test")

        # First process: run to completion and capture the final state.
        with open_checkpointer(db_path) as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            final_state = run_to_completion(graph, config, seed)

        assert final_state["run_status"] == "COMPLETE"
        assert set(final_state["completed_items"]) == {"A", "B", "C"}
        assert final_state["retry_counts"]["B"] == 2, "B should have needed a retry (2 attempts)"
        assert final_state["item_results"]["B"]["status"] == "SUCCESS"

        # Reopen a NEW checkpointer/graph against the SAME db+thread_id and
        # confirm the completed run's state is still readable (durable
        # resume semantics), matching the proven reliability-test pattern.
        with open_checkpointer(db_path) as checkpointer2:
            graph2 = builder.compile(checkpointer=checkpointer2)
            resumed_state = graph2.get_state(config).values
            assert resumed_state["run_status"] == "COMPLETE"
            assert set(resumed_state["completed_items"]) == {"A", "B", "C"}

    _record(
        "I. checkpoint/resume remains functional",
        True,
        "3-item queue with 1 simulated retry completed and was durably readable after reopening the checkpoint DB",
    )


def main() -> int:
    sections = [
        section_a_config,
        section_b_agents_skills,
        section_c_sqlite_state,
        section_d_retry_policy,
        section_e_terminal_states,
        section_f_g_browser_manager,
        section_h_visible_escalation_simulated,
        section_i_checkpoint_resume,
    ]
    for fn in sections:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            _record(fn.__name__, False, str(exc))

    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} :: {name}")
    print(f"\nFOUNDATION_TEST_SUITE: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
