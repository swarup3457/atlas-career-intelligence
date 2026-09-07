"""Atlas orchestration state schema.

A generic, reusable "queue processing" state shape used by the LangGraph
governor. This is intentionally generic (keyed by an opaque "item" id such
as a company name or task id) so it works for the proven reliability test,
the proven real-web orchestration test, and future business workflows
without change.

Business-specific fields (jobs, candidate profile, ATS rules, etc.) are
NOT part of this generic state — they belong in the `company_results` /
task payload dict values, defined by the caller.
"""

from __future__ import annotations

import datetime
from typing import Any, Optional, TypedDict

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPLETE = "COMPLETE"


class QueueState(TypedDict):
    planned_items: list[str]
    completed_items: list[str]
    remaining_items: list[str]
    retry_counts: dict[str, int]
    failed_items: list[str]
    access_limited_items: list[str]
    human_waiting_items: list[str]
    item_results: dict[str, dict[str, Any]]
    attempt_log: list[dict[str, Any]]
    current_item: Optional[str]
    run_status: str


def initial_queue_state(items: list[str]) -> QueueState:
    return QueueState(
        planned_items=list(items),
        completed_items=[],
        remaining_items=list(items),
        retry_counts={},
        failed_items=[],
        access_limited_items=[],
        human_waiting_items=[],
        item_results={},
        attempt_log=[],
        current_item=None,
        run_status=RUN_STATUS_RUNNING,
    )


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
