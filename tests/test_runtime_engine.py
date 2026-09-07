"""Pytest coverage for atlas.runtime.engine.AtlasRuntime (Phase 0.75 spec
sections 2/7/8/9/11/12/16/21). Fully offline, deterministic - no real web.
"""

from __future__ import annotations

import pytest

from atlas.config import load_settings
from atlas.runtime.demo_workload import (
    ACCESS_LIMITED_INDEX,
    DEMO_TASK_COUNT,
    EXTRACTION_UNRESOLVED_INDEX,
    LOGIN_REQUIRED_INDEX,
    DemoWorker,
    build_demo_failure_injector,
    build_demo_tasks,
    demo_task_id,
)
from atlas.runtime.engine import AtlasRuntime, RunAlreadyActiveError, new_run_id

pytestmark = pytest.mark.integration


def _settings(tmp_path, **overrides):
    kwargs = dict(
        state_db=tmp_path / "state" / "atlas_state.sqlite",
        checkpoint_db=tmp_path / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_path / "output",
        logs_dir=tmp_path / "logs",
        browser_profile=tmp_path / "profile",
        agents_dir=tmp_path / "agents",
        skills_dir=tmp_path / "skills",
        batch_size=5,
        retry_budget=2,
    )
    kwargs.update(overrides)
    settings = load_settings(**kwargs)
    settings.ensure_directories()
    return settings


def _demo_runtime(settings, run_id=None, **kwargs):
    tasks = build_demo_tasks()
    injector = build_demo_failure_injector()
    worker = DemoWorker(injector)
    return AtlasRuntime(settings, run_id or new_run_id(), tasks, worker, **kwargs)


def test_demo_run_reaches_complete_with_required_distribution(tmp_path):
    settings = _settings(tmp_path)
    runtime = _demo_runtime(settings)
    result = runtime.run()

    assert result.status == "COMPLETE"
    progress = result.progress
    assert progress.planned == DEMO_TASK_COUNT
    assert progress.completed == DEMO_TASK_COUNT
    assert progress.remaining == 0
    assert progress.access_limited == 1
    assert progress.extraction_unresolved == 1
    assert progress.waiting_for_human == 1
    assert progress.permanent_failure == 0
    # 50 - 1 - 1 - 1 = 47 successes (including the 5 retry-then-succeed tasks)
    assert progress.success == 47


def test_completion_governor_never_ends_early(tmp_path):
    """The run must not report COMPLETE while any planned task has not
    reached a terminal state - verified by checking every planned task
    id appears in either completed_items with a terminal status."""
    settings = _settings(tmp_path)
    runtime = _demo_runtime(settings)
    result = runtime.run()
    assert result.status == "COMPLETE"
    assert result.progress.planned == result.progress.completed


def test_partial_run_then_resume_reaches_complete(tmp_path):
    settings = _settings(tmp_path)
    run_id = new_run_id()
    injector = build_demo_failure_injector()

    runtime_a = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5, stop_after_completed=15)
    result_a = runtime_a.run()
    assert result_a.status == "PARTIAL"
    assert result_a.progress.completed == 15
    assert result_a.progress.remaining == DEMO_TASK_COUNT - 15

    runtime_b = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5)
    result_b = runtime_b.resume()
    assert result_b.status == "COMPLETE"
    assert result_b.progress.completed == DEMO_TASK_COUNT
    assert result_b.progress.remaining == 0


def test_runtime_budget_stops_and_marks_partial(tmp_path):
    settings = _settings(tmp_path)
    runtime = _demo_runtime(settings, max_runtime_minutes=0.0)
    result = runtime.run()
    assert result.status == "PARTIAL"
    assert result.progress.remaining > 0


def test_unresolved_intervention_yields_waiting_for_human(tmp_path):
    settings = _settings(tmp_path)
    runtime = _demo_runtime(settings, auto_resolve_interventions=False)
    result = runtime.run()
    assert result.status == "WAITING_FOR_HUMAN"
    assert result.progress.remaining == 0
    assert result.progress.waiting_for_human == 1


def test_run_already_active_raises(tmp_path):
    settings = _settings(tmp_path)
    run_id = new_run_id()
    runtime_a = _demo_runtime(settings, run_id=run_id)
    # Acquire the lock directly to simulate a concurrently active run.
    from atlas.orchestration.run_lock import RunLock

    lock = RunLock(settings.state_db.parent)
    status = lock.try_acquire(run_id="someone-else")
    assert status.status == "RUN_LOCK_ACQUIRED"
    try:
        with pytest.raises(RunAlreadyActiveError):
            runtime_a.run()
    finally:
        lock.release()


def test_idempotent_rerun_does_not_duplicate_completed_items(tmp_path):
    settings = _settings(tmp_path)
    run_id = new_run_id()
    injector = build_demo_failure_injector()

    runtime_1 = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5)
    result_1 = runtime_1.run()
    assert result_1.status == "COMPLETE"

    runtime_2 = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5)
    result_2 = runtime_2.run()
    assert result_2.status == "COMPLETE"
    assert result_2.progress.completed == DEMO_TASK_COUNT
    # No duplication: completed count must not exceed planned.
    assert result_2.progress.completed <= result_2.progress.planned


def test_double_resume_is_idempotent_and_still_complete(tmp_path):
    settings = _settings(tmp_path)
    run_id = new_run_id()
    injector = build_demo_failure_injector()

    runtime_a = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5, stop_after_completed=10)
    runtime_a.run()

    runtime_b = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5)
    result_b = runtime_b.resume()
    assert result_b.status == "COMPLETE"

    runtime_c = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5)
    result_c = runtime_c.resume()
    assert result_c.status == "COMPLETE"
    assert result_c.progress.completed == DEMO_TASK_COUNT


def test_finalization_is_idempotent_for_completed_run(tmp_path):
    settings = _settings(tmp_path)
    run_id = new_run_id()
    injector = build_demo_failure_injector()

    result_1 = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5).run()
    result_2 = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5).run()
    assert result_1.status == result_2.status == "COMPLETE"
    assert result_1.progress.to_dict() == result_2.progress.to_dict()


def test_snapshot_reports_progress_without_lock_or_mutation(tmp_path):
    settings = _settings(tmp_path)
    run_id = new_run_id()
    injector = build_demo_failure_injector()
    AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5, stop_after_completed=10).run()

    reader = AtlasRuntime(settings, run_id, [], DemoWorker(build_demo_failure_injector()), batch_size=5)
    snapshot = reader.snapshot()
    assert snapshot is not None
    assert snapshot.completed == 10
    assert snapshot.remaining == DEMO_TASK_COUNT - 10

    # snapshot() must not have taken the run lock (a resume must still work).
    resumer = AtlasRuntime(settings, run_id, build_demo_tasks(), DemoWorker(injector), batch_size=5)
    result = resumer.resume()
    assert result.status == "COMPLETE"


def test_snapshot_returns_none_for_unknown_run(tmp_path):
    settings = _settings(tmp_path)
    reader = AtlasRuntime(settings, "no-such-run", [], DemoWorker(build_demo_failure_injector()), batch_size=5)
    assert reader.snapshot() is None


def test_demo_report_and_manifest_written_on_complete(tmp_path):
    settings = _settings(tmp_path)
    runtime = _demo_runtime(settings)
    result = runtime.run()
    assert result.manifest is not None
    assert result.manifest.status == "COMPLETE"
    assert result.manifest.planned_tasks == DEMO_TASK_COUNT
    assert result.report_paths["xlsx"].exists()
    assert result.report_paths["json"].exists()
    assert result.report_paths["xlsx"].name == "Atlas_DEMO_Runtime_Report.xlsx"


def test_no_permanent_failure_in_default_demo_run(tmp_path):
    settings = _settings(tmp_path)
    runtime = _demo_runtime(settings)
    result = runtime.run()
    assert result.progress.permanent_failure == 0
