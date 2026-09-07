"""Pytest coverage for the worker (Phase 0.5 spec section 14) and source
adapter (spec section 15) abstract contracts. No concrete workers/sources
are implemented yet - these tests only prove the contracts are well
formed and enforced."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory, TaskStatus
from atlas.workers.base import BaseWorker, WorkerError, WorkerOutcome, make_task_result

pytestmark = pytest.mark.unit


def test_base_worker_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        BaseWorker()  # type: ignore[abstract]


def test_make_task_result_never_communicates_via_free_form_text():
    result = make_task_result(
        task_id="task-1",
        status=TaskStatus.SUCCESS,
        started_at="2026-01-01T00:00:00+00:00",
        attempt=1,
        data={"foo": "bar"},
    )
    as_dict = result.to_dict()
    assert set(as_dict.keys()) == {
        "task_id", "status", "started_at", "completed_at", "attempt",
        "data", "error_category", "error_detail", "next_action",
    }
    assert as_dict["status"] == "SUCCESS"
    assert as_dict["next_action"] == "NONE"


def test_make_task_result_records_error_category_and_detail():
    result = make_task_result(
        task_id="task-2",
        status=TaskStatus.PERMANENT_FAILURE,
        started_at="2026-01-01T00:00:00+00:00",
        attempt=3,
        error_category=ErrorCategory.TIMEOUT,
        error_detail="navigation timed out",
        next_action="ESCALATE",
    )
    assert result.error_category == "TIMEOUT"
    assert result.error_detail == "navigation timed out"
    assert result.next_action == "ESCALATE"


def test_worker_error_carries_category():
    err = WorkerError(ErrorCategory.CAPTCHA, "captcha detected")
    assert err.category == ErrorCategory.CAPTCHA
    assert str(err) == "captcha detected"


def test_worker_outcome_defaults_to_empty_payload():
    outcome = WorkerOutcome(status=TaskStatus.SUCCESS)
    assert outcome.payload == {}


def test_concrete_worker_can_subclass_base_worker():
    class TrivialWorker(BaseWorker):
        name = "trivial"

        def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
            return WorkerOutcome(status=TaskStatus.SUCCESS, payload={"item": item})

    worker = TrivialWorker()
    outcome = worker.attempt("A", 1)
    assert outcome.status == TaskStatus.SUCCESS
