"""Atlas Phase 0.75 production runtime shell - AtlasRuntime engine.

Wires together every already-proven generic component into one
production-shaped runtime lifecycle:

    INITIALIZE -> ACQUIRE RUN LOCK -> LOAD OR CREATE RUN -> LOAD CHECKPOINT
    -> BUILD TASK QUEUE -> EXECUTE BATCH -> RECORD RESULT -> RETRY WHEN
    REQUIRED -> CHECKPOINT -> CONTINUE REMAINING TASKS -> FINALIZE ->
    WRITE DEMO REPORT -> RELEASE LOCK

For Phase 0.75 only deterministic demo/fake workers are supported - no
real job-search source exists yet (see docs/RUNTIME.md).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from atlas.browser.intervention import InterventionQueue, InterventionRequest, InterventionResult
from atlas.config import Settings
from atlas.models import TaskStatus
from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.orchestration.events import EventBus, EventType
from atlas.orchestration.graph import build_graph
from atlas.orchestration.run_lock import RUN_ALREADY_ACTIVE, RunLock
from atlas.orchestration.state import RUN_STATUS_COMPLETE, initial_queue_state
from atlas.persistence.sqlite import StateStore
from atlas.runtime.manifest import RunManifest, compute_config_fingerprint, software_versions
from atlas.runtime.progress import ProgressSnapshot, compute_progress
from atlas.runtime.report import write_demo_report
from atlas.runtime.scheduler import TaskRecord, TaskScheduler
from atlas.runtime.states import RunState, transition
from atlas.workers.base import BaseWorker


class RunAlreadyActiveError(RuntimeError):
    """Raised when another Atlas run already holds the run lock."""


def _simulated_escalate(
    *,
    profile_dir: Path,
    channel: str,
    url: str,
    reason: str,
    wait_for_user,
    recheck_url: Optional[str] = None,
    notify=print,
) -> InterventionResult:
    """Demo-mode intervention resolver: NEVER opens a real browser.

    Used only when AtlasRuntime is constructed with simulate_intervention
    =True (the default for `atlas run --demo`). Immediately "resolves"
    the request, proving the checkpoint/queue/continue-other-tasks
    mechanism works without a visible Chrome window.
    """
    notify(f"[demo] Simulated human intervention for reason={reason!r} url={url!r} (no browser opened).")
    wait_for_user()
    return InterventionResult(resolved=True, final_url=url, title=None, notes="simulated resolution (demo mode) - no real browser opened")


@dataclass
class RunResult:
    run_id: str
    status: str
    progress: ProgressSnapshot
    manifest: Optional[RunManifest] = None
    report_paths: dict[str, Path] = field(default_factory=dict)


class AtlasRuntime:
    """The Phase 0.75 production-shaped runtime engine."""

    def __init__(
        self,
        settings: Settings,
        run_id: str,
        tasks: list[TaskRecord],
        worker: BaseWorker,
        thread_id: Optional[str] = None,
        max_runtime_minutes: Optional[float] = None,
        stop_after_completed: Optional[int] = None,
        batch_size: Optional[int] = None,
        max_parallel_tasks: int = 1,
        simulate_intervention: bool = True,
        auto_resolve_interventions: bool = True,
        write_report: bool = True,
        notify=print,
    ):
        self.settings = settings
        self.run_id = run_id
        self.thread_id = thread_id or run_id
        self.tasks = tasks
        self.worker = worker
        self.max_runtime_minutes = max_runtime_minutes
        self.stop_after_completed = stop_after_completed
        self.batch_size = batch_size or settings.batch_size
        self.max_parallel_tasks = max_parallel_tasks
        self.write_report = write_report
        self.notify = notify

        self.event_bus = EventBus(run_id)
        self.run_lock = RunLock(settings.state_db.parent)

        escalate_fn = _simulated_escalate if simulate_intervention else None
        self.intervention_queue = InterventionQueue(
            profile_dir=settings.browser_profile,
            channel=settings.browser_channel,
            wait_for_user=lambda req: None,
            notify=notify,
            escalate_fn=escalate_fn,
        )
        self.auto_resolve_interventions = auto_resolve_interventions
        self._resolved_intervention_ids: set[str] = set()
        self._enqueued_intervention_ids: set[str] = set()

    # ------------------------------------------------------------------
    def run(self) -> RunResult:
        """Start (or transparently continue) the run identified by
        run_id. Safe to call again on an already-COMPLETE run (idempotent
        - no duplicate work, no duplicate completion records)."""
        return self._execute()

    def resume(self) -> RunResult:
        """Resume a previously PARTIAL/interrupted run. Idempotent: safe
        to call multiple times, including after the run has already
        reached COMPLETE."""
        return self._execute(resuming=True)

    # ------------------------------------------------------------------
    def _handle_new_human_waiting(self, state: dict, store: StateStore) -> None:
        for item in state.get("human_waiting_items", []):
            if item in self._enqueued_intervention_ids:
                continue
            self._enqueued_intervention_ids.add(item)
            status = state.get("item_results", {}).get(item, {}).get("status", "LOGIN_REQUIRED")
            store.upsert_task(task_id=item, run_id=self.run_id, task_type="demo", status=status)
            store.request_human_intervention(intervention_id=item, task_id=item, reason=status)
            self.intervention_queue.enqueue(
                InterventionRequest(request_id=item, reason=status, url="atlas-demo://simulated-intervention")
            )
            self.event_bus.publish(EventType.HUMAN_INTERVENTION_REQUIRED, task_id=item, detail={"reason": status})
            if self.auto_resolve_interventions:
                processed = self.intervention_queue.process_next()
                if processed is not None:
                    _request, result = processed
                    store.resolve_human_intervention(item, notes=result.notes)
                    self._resolved_intervention_ids.add(item)
                    self.event_bus.publish(EventType.HUMAN_INTERVENTION_RESOLVED, task_id=item)

    def _execute(self, resuming: bool = False) -> RunResult:
        lock_status = self.run_lock.try_acquire(run_id=self.run_id)
        if lock_status.status == RUN_ALREADY_ACTIVE:
            raise RunAlreadyActiveError(
                f"{RUN_ALREADY_ACTIVE}: run {lock_status.run_id!r} already active "
                f"(pid={lock_status.pid}, started_at={lock_status.started_at})."
            )
        try:
            return self._execute_locked(resuming=resuming)
        finally:
            self.run_lock.release()

    def _execute_locked(self, resuming: bool) -> RunResult:
        run_state = RunState.INITIALIZING
        with StateStore(self.settings.state_db) as store:
            existing_run = store.get_run(self.run_id)
            if existing_run is None:
                store.create_run(self.run_id, controller=self.settings.controller, metadata={"demo": True})
                existing_run = store.get_run(self.run_id)

            with open_checkpointer(self.settings.checkpoint_db) as checkpointer:
                graph_builder = build_graph(self.worker, retry_budget=self.settings.retry_budget, event_bus=self.event_bus)
                graph = graph_builder.compile(checkpointer=checkpointer)
                config = thread_config(self.thread_id)

                existing_state = graph.get_state(config).values
                if existing_state:
                    state = existing_state
                    if resuming:
                        run_state = transition(RunState.PARTIAL, RunState.RESUMING)
                        run_state = transition(run_state, RunState.RUNNING)
                    else:
                        run_state = transition(RunState.INITIALIZING, RunState.RUNNING)
                    self.event_bus.publish(EventType.RUN_RESUMED)
                else:
                    ordered_ids = TaskScheduler().order(self.tasks)
                    seed = initial_queue_state(ordered_ids)
                    run_state = transition(RunState.INITIALIZING, RunState.RUNNING)
                    self.event_bus.publish(EventType.RUN_STARTED)
                    state = graph.invoke(seed, config)
                    self._handle_new_human_waiting(state, store)
                    store.save_continuation(self.run_id, self.thread_id, state["remaining_items"], state["completed_items"])

                deadline = (time.monotonic() + self.max_runtime_minutes * 60) if self.max_runtime_minutes is not None else None
                partial = False

                while state.get("run_status") != RUN_STATUS_COMPLETE:
                    if deadline is not None and time.monotonic() >= deadline:
                        partial = True
                        break
                    if self.stop_after_completed is not None and len(state.get("completed_items", [])) >= self.stop_after_completed:
                        partial = True
                        break

                    for _ in range(self.batch_size):
                        if state.get("run_status") == RUN_STATUS_COMPLETE:
                            break
                        if self.stop_after_completed is not None and len(state.get("completed_items", [])) >= self.stop_after_completed:
                            partial = True
                            break
                        state = graph.invoke({}, config)
                        self._handle_new_human_waiting(state, store)

                    store.save_continuation(self.run_id, self.thread_id, state["remaining_items"], state["completed_items"])
                    self.event_bus.publish(EventType.CHECKPOINT_SAVED, detail={"remaining": len(state.get("remaining_items", []))})

                    if partial:
                        break

                if partial:
                    run_state = transition(run_state, RunState.PARTIAL)
                    store.complete_run(self.run_id, status=RunState.PARTIAL.value)
                    self.event_bus.publish(EventType.RUN_PARTIAL)
                    progress = compute_progress(self.run_id, run_state, state)
                    return RunResult(run_id=self.run_id, status=run_state.value, progress=progress)

                run_state = transition(run_state, RunState.FINALIZING)
                unresolved = {
                    item
                    for item in state.get("human_waiting_items", [])
                    if store.intervention_status(item) != "RESOLVED"
                }
                if unresolved:
                    run_state = transition(run_state, RunState.WAITING_FOR_HUMAN)
                else:
                    run_state = transition(run_state, RunState.COMPLETE)

                store.complete_run(self.run_id, status=run_state.value)
                progress = compute_progress(self.run_id, run_state, state)

                manifest = RunManifest(
                    run_id=self.run_id,
                    created_at=existing_run["started_at"],
                    started_at=existing_run["started_at"],
                    completed_at=None,
                    status=run_state.value,
                    config_fingerprint=compute_config_fingerprint(self.settings),
                    controller=self.settings.controller,
                    planned_tasks=len(state.get("planned_items", [])),
                    completed_tasks=len(state.get("completed_items", [])),
                    remaining_tasks=len(state.get("remaining_items", [])),
                    retry_counts=dict(state.get("retry_counts", {})),
                    interventions=len(state.get("human_waiting_items", [])),
                    software_version=software_versions(),
                )
                manifest_path = self.settings.output_dir / f"run_manifest_{self.run_id}.json"
                manifest.write(manifest_path)

                report_paths: dict[str, Path] = {}
                if self.write_report:
                    report_paths = write_demo_report(self.settings.output_dir, state, manifest, progress)

                return RunResult(run_id=self.run_id, status=run_state.value, progress=progress, manifest=manifest, report_paths=report_paths)

    # ------------------------------------------------------------------
    def snapshot(self) -> Optional[ProgressSnapshot]:
        """Read-only progress snapshot for `atlas status` - does NOT
        acquire the run lock and never mutates state."""
        with open_checkpointer(self.settings.checkpoint_db) as checkpointer:
            graph_builder = build_graph(self.worker, retry_budget=self.settings.retry_budget)
            graph = graph_builder.compile(checkpointer=checkpointer)
            config = thread_config(self.thread_id)
            state = graph.get_state(config).values
        if not state:
            return None
        with StateStore(self.settings.state_db) as store:
            row = store.get_run(self.run_id)
        status = row["status"] if row is not None else ("COMPLETE" if state.get("run_status") == RUN_STATUS_COMPLETE else "RUNNING")
        try:
            run_state = RunState(status)
        except ValueError:
            run_state = RunState.RUNNING
        return compute_progress(self.run_id, run_state, state)


def new_run_id(prefix: str = "atlas-run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
