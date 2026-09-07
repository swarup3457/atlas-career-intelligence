"""Atlas LangGraph governor — generic single-node queue-processing graph.

This is the deterministic core proven by tests/langgraph_reliability_graph.py
and tests/real_web_orchestration_graph.py, generalized so any BaseWorker can
be plugged in without duplicating queue/retry/checkpoint logic.

LangGraph/code (this module) owns:
    - what remains, what completed
    - retries and retry budgets (via atlas.orchestration.retry)
    - checkpoints (via atlas.orchestration.checkpoints)
    - continuation / resumption
    - terminal states and deduplication of the completed-items list
    - accounting (attempt_log records EVERY attempt; completed_items
      records each item at most once)

The LLM controller (see atlas/controllers/) is never asked to remember
"how many of N items are done" — that lives here, in durable state.
"""

from __future__ import annotations

from typing import Callable, Optional

from langgraph.graph import END, StateGraph

from atlas.models import TaskStatus
from atlas.orchestration import retry as retry_policy
from atlas.orchestration.events import EventBus, EventType
from atlas.orchestration.state import (
    RUN_STATUS_COMPLETE,
    RUN_STATUS_RUNNING,
    QueueState,
    now_iso,
)
from atlas.workers.base import BaseWorker, WorkerError, make_task_result


def make_process_one_node(
    worker: BaseWorker,
    retry_budget: int,
    event_bus: Optional[EventBus] = None,
) -> Callable[[QueueState], QueueState]:
    """Build the single LangGraph node function bound to `worker`.

    One graph.invoke() call == one processing attempt for the item at the
    front of the remaining-items queue (or a no-op COMPLETE transition
    once the queue is empty).

    If `event_bus` is provided, TASK_STARTED/TASK_RETRY/TASK_COMPLETED/
    TASK_FAILED/ACCESS_LIMITED events are published for observability -
    entirely optional and backward compatible (existing callers that omit
    it see no behavior change).
    """

    def process_one(state: QueueState) -> QueueState:
        remaining = list(state.get("remaining_items", []))
        completed = list(state.get("completed_items", []))
        retry_counts = dict(state.get("retry_counts", {}))
        failed = list(state.get("failed_items", []))
        access_limited = list(state.get("access_limited_items", []))
        human_waiting = list(state.get("human_waiting_items", []))
        item_results = dict(state.get("item_results", {}))
        attempt_log = list(state.get("attempt_log", []))

        if not remaining:
            return {**state, "current_item": None, "run_status": RUN_STATUS_COMPLETE}

        item = remaining.pop(0)
        attempt_number = retry_counts.get(item, 0) + 1
        timestamp = now_iso()
        if event_bus is not None:
            event_bus.publish(EventType.TASK_STARTED, task_id=item, attempt=attempt_number)

        terminal_status: TaskStatus | None = None
        payload: dict = {}
        log_result = "UNKNOWN"
        error_category = None
        error_detail = None
        next_action = "NONE"

        try:
            outcome = worker.attempt(item, attempt_number)
            terminal_status = outcome.status
            payload = outcome.payload
            log_result = terminal_status.value
        except WorkerError as exc:
            decision = retry_policy.evaluate(exc.category, attempt_number, retry_budget)
            log_result = f"ERROR:{exc.category.value}"
            error_category = exc.category
            error_detail = exc.message
            if decision.should_retry:
                terminal_status = None
                payload = {"error": exc.message, "reason": decision.reason}
                next_action = "RETRY"
            else:
                terminal_status = decision.terminal_status
                payload = {"error": exc.message, "reason": decision.reason}
                next_action = "ESCALATE" if terminal_status in (
                    TaskStatus.WAITING_FOR_HUMAN,
                    TaskStatus.LOGIN_REQUIRED,
                ) else "NONE"

        attempt_log.append(
            {
                "item": item,
                "attempt_number": attempt_number,
                "result": log_result,
                "timestamp": timestamp,
            }
        )
        retry_counts[item] = attempt_number

        if terminal_status is None:
            # Still within retry budget - requeue at the back so other
            # items keep making progress in the meantime.
            remaining.append(item)
            if event_bus is not None:
                event_bus.publish(EventType.TASK_RETRY, task_id=item, attempt=attempt_number, detail=payload)
        else:
            task_result = make_task_result(
                task_id=item,
                status=terminal_status,
                started_at=timestamp,
                attempt=attempt_number,
                data=payload,
                error_category=error_category,
                error_detail=error_detail,
                next_action=next_action,
            )
            item_results[item] = task_result.to_dict()
            completed.append(item)
            if terminal_status == TaskStatus.ACCESS_LIMITED:
                access_limited.append(item)
                if event_bus is not None:
                    event_bus.publish(EventType.ACCESS_LIMITED, task_id=item, attempt=attempt_number)
            elif terminal_status == TaskStatus.PERMANENT_FAILURE:
                failed.append(item)
                if event_bus is not None:
                    event_bus.publish(EventType.TASK_FAILED, task_id=item, attempt=attempt_number, detail=payload)
            elif terminal_status in (TaskStatus.WAITING_FOR_HUMAN, TaskStatus.LOGIN_REQUIRED):
                human_waiting.append(item)
                if event_bus is not None:
                    event_bus.publish(EventType.HUMAN_INTERVENTION_REQUIRED, task_id=item, attempt=attempt_number)
            if event_bus is not None and terminal_status not in (
                TaskStatus.ACCESS_LIMITED,
                TaskStatus.PERMANENT_FAILURE,
                TaskStatus.WAITING_FOR_HUMAN,
                TaskStatus.LOGIN_REQUIRED,
            ):
                event_bus.publish(EventType.TASK_COMPLETED, task_id=item, attempt=attempt_number)

        run_status = RUN_STATUS_COMPLETE if not remaining else RUN_STATUS_RUNNING
        if event_bus is not None and run_status == RUN_STATUS_COMPLETE:
            event_bus.publish(EventType.RUN_COMPLETED)

        return {
            "planned_items": state.get("planned_items", []),
            "completed_items": completed,
            "remaining_items": remaining,
            "retry_counts": retry_counts,
            "failed_items": failed,
            "access_limited_items": access_limited,
            "human_waiting_items": human_waiting,
            "item_results": item_results,
            "attempt_log": attempt_log,
            "current_item": item,
            "run_status": run_status,
        }

    return process_one


def build_graph(worker: BaseWorker, retry_budget: int, event_bus: Optional[EventBus] = None):
    """Build (uncompiled) the single-node Atlas governor graph."""
    builder = StateGraph(QueueState)
    builder.add_node("process_one", make_process_one_node(worker, retry_budget, event_bus=event_bus))
    builder.set_entry_point("process_one")
    builder.add_edge("process_one", END)
    return builder


def run_to_completion(graph, config: dict, seed_state: QueueState) -> QueueState:
    """Invoke the compiled graph repeatedly until run_status is COMPLETE.

    Seeds the run on the first invoke, then continues invoking with an
    empty input, which is exactly the pattern proven to support
    checkpoint/restart/resume across separate processes.
    """
    state = graph.invoke(seed_state, config)
    while state.get("run_status") != RUN_STATUS_COMPLETE:
        state = graph.invoke({}, config)
    return state

