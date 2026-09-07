"""Phase 0.95 — THE DISASTER TEST (spec section 6) and the clean-room test
(spec section 26).

Everything is fully offline and uses ONLY tmp_path. A partial demo run is
backed up, its state directory is destroyed, the backup is restored into a
fresh location, and a *new* AtlasRuntime instance (simulating a fresh
Python process) resumes from the restored state to completion — proving no
previously-completed task is re-executed.
"""

from __future__ import annotations

import datetime
from collections import Counter
from pathlib import Path

import pytest

from atlas.backup.backup import backup_dir_for, create_backup
from atlas.backup.clock import FixedClock
from atlas.backup.restore import RESTORED, restore_backup
from atlas.backup.verify import verify_backup
from atlas.health import FAIL, run_doctor
from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.orchestration.graph import build_graph
from atlas.runtime.demo_workload import (
    DEMO_TASK_COUNT,
    DemoWorker,
    build_demo_failure_injector,
    build_demo_tasks,
)
from atlas.runtime.engine import AtlasRuntime, new_run_id
from atlas.workers.base import BaseWorker, WorkerOutcome
from tests._phase095_helpers import make_settings

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_FIXED = datetime.datetime(2026, 9, 6, 18, 0, 0, tzinfo=datetime.timezone.utc)


class CountingDemoWorker(BaseWorker):
    """DemoWorker that records how many times each item is attempted.

    The shared ``counter`` is a test observer that survives across both
    (simulated-process) runtime instances so we can prove that resume does
    NOT re-attempt any task that was already completed before the crash.
    """

    name = "counting-demo-worker"

    def __init__(self, injector, counter: Counter):
        self.injector = injector
        self.counter = counter

    def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
        self.counter[item] += 1
        return self.injector.resolve(item, attempt_number)


def _completed_items(settings, run_id: str) -> set[str]:
    """Read the durable set of completed item ids from the checkpoint."""
    with open_checkpointer(settings.checkpoint_db) as cp:
        graph = build_graph(
            DemoWorker(build_demo_failure_injector()), retry_budget=settings.retry_budget
        ).compile(checkpointer=cp)
        state = graph.get_state(thread_config(run_id)).values
    return set(state.get("completed_items", []))


def test_full_disaster_recovery_resume_without_reexecution(tmp_path):
    # 1) disposable settings entirely under tmp_path
    src = make_settings(tmp_path / "src")
    (src.agents_dir / "agent.md").write_text("agent def", encoding="utf-8")
    (src.skills_dir / "skill.md").write_text("skill def", encoding="utf-8")

    run_id = new_run_id()
    counter: Counter = Counter()

    # 2) deterministic PARTIAL demo run (stop after 15 completed)
    runtime_a = AtlasRuntime(
        src,
        run_id,
        build_demo_tasks(),
        CountingDemoWorker(build_demo_failure_injector(), counter),
        batch_size=5,
        stop_after_completed=15,
    )
    result_a = runtime_a.run()
    assert result_a.status == "PARTIAL"
    assert result_a.progress.completed == 15

    completed_after_partial = _completed_items(src, run_id)
    assert len(completed_after_partial) == 15
    counts_after_partial = dict(counter)

    # 3) create a backup of the partial state
    backups = tmp_path / "backups"
    manifest = create_backup(src, backups, clock=FixedClock(_FIXED))
    bpath = backup_dir_for(backups, manifest)
    assert verify_backup(bpath).ok

    # 4) DESTROY the source state directory (simulate disaster)
    import shutil

    shutil.rmtree(src.state_db.parent)
    assert not src.state_db.exists()
    assert not src.checkpoint_db.exists()

    # 5) restore into a fresh tmp_path target
    target = tmp_path / "restored"
    restore_result = restore_backup(bpath, target)
    assert restore_result.status == RESTORED, restore_result.render()

    # 6) a NEW AtlasRuntime (fresh "process") pointed at the restored state
    restored = make_settings(
        tmp_path / "restored_run",
        state_db=target / "state" / "atlas_state.sqlite",
        checkpoint_db=target / "state" / "atlas_checkpoints.sqlite",
    )
    runtime_b = AtlasRuntime(
        restored,
        run_id,
        build_demo_tasks(),
        CountingDemoWorker(build_demo_failure_injector(), counter),
        batch_size=5,
    )

    # 7) resume to completion
    result_b = runtime_b.resume()
    assert result_b.status == "COMPLETE"
    assert result_b.progress.completed == DEMO_TASK_COUNT
    assert result_b.progress.remaining == 0

    # 8a) completed set only grew, never shrank or duplicated
    completed_final = _completed_items(restored, run_id)
    assert completed_after_partial.issubset(completed_final)
    assert len(completed_final) == DEMO_TASK_COUNT

    # 8b) no previously-completed task was re-executed during resume:
    #     its attempt counter is identical to its pre-crash value.
    counts_after_resume = dict(counter)
    for task_id in completed_after_partial:
        assert counts_after_resume[task_id] == counts_after_partial[task_id], (
            f"task {task_id} was re-executed on resume "
            f"({counts_after_partial[task_id]} -> {counts_after_resume[task_id]})"
        )


def test_clean_room_end_to_end(tmp_path, monkeypatch):
    """Section 26 clean-room: doctor -> partial run -> backup -> loss ->
    restore -> resume -> COMPLETE -> report artifact -> verify passes.
    Fully offline, deterministic, no real_web."""
    root = tmp_path / "atlasroot"
    # Point the doctor (which calls load_settings() from the environment)
    # at a fully-disposable layout so the check is hermetic.
    layout = {
        "ATLAS_STATE_DB": root / "state" / "atlas_state.sqlite",
        "ATLAS_CHECKPOINT_DB": root / "state" / "atlas_checkpoints.sqlite",
        "ATLAS_OUTPUT_DIR": root / "output",
        "ATLAS_LOGS_DIR": root / "logs",
        "ATLAS_BROWSER_PROFILE": root / "profile",
        "ATLAS_AGENTS_DIR": root / "agents",
        "ATLAS_SKILLS_DIR": root / "skills",
    }
    for key, val in layout.items():
        monkeypatch.setenv(key, str(val))

    settings = make_settings(
        root,
        state_db=layout["ATLAS_STATE_DB"],
        checkpoint_db=layout["ATLAS_CHECKPOINT_DB"],
        output_dir=layout["ATLAS_OUTPUT_DIR"],
        logs_dir=layout["ATLAS_LOGS_DIR"],
        browser_profile=layout["ATLAS_BROWSER_PROFILE"],
        agents_dir=layout["ATLAS_AGENTS_DIR"],
        skills_dir=layout["ATLAS_SKILLS_DIR"],
    )
    (settings.agents_dir / "agent.md").write_text("agent", encoding="utf-8")

    # doctor: no FAIL allowed (WARNs are fine on a bare test machine)
    report = run_doctor()
    assert report.overall != FAIL, report.render()

    # partial run
    run_id = new_run_id()
    runtime = AtlasRuntime(
        settings, run_id, build_demo_tasks(),
        DemoWorker(build_demo_failure_injector()),
        batch_size=5, stop_after_completed=20,
    )
    assert runtime.run().status == "PARTIAL"

    # backup
    backups = root / "output" / "backups"
    manifest = create_backup(settings, backups, clock=FixedClock(_FIXED))
    bpath = backup_dir_for(backups, manifest)

    # simulate loss
    import shutil

    shutil.rmtree(settings.state_db.parent)

    # restore
    target = tmp_path / "restored"
    assert restore_backup(bpath, target).status == RESTORED

    # resume to completion, writing a real report artifact
    restored = make_settings(
        tmp_path / "restored_run",
        state_db=target / "state" / "atlas_state.sqlite",
        checkpoint_db=target / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_path / "restored_output",
    )
    result = AtlasRuntime(
        restored, run_id, build_demo_tasks(),
        DemoWorker(build_demo_failure_injector()), batch_size=5,
    ).resume()
    assert result.status == "COMPLETE"
    assert result.progress.completed == DEMO_TASK_COUNT

    # a small report artifact exists
    report_json = restored.output_dir / "Atlas_DEMO_Runtime_Report.json"
    assert report_json.exists()

    # backup still verifies after everything
    assert verify_backup(bpath).ok
