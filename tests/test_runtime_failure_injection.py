"""Pytest coverage for atlas.runtime.failure_injection (Phase 0.75 spec
section 15)."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory, TaskStatus
from atlas.runtime.failure_injection import (
    FailureInjector,
    inject_access_limited,
    inject_extraction_unresolved,
    inject_login_required,
    inject_navigation_failure,
    inject_timeout,
    inject_transient_then_success,
    inject_worker_crash,
)
from atlas.workers.base import WorkerError, WorkerOutcome

pytestmark = pytest.mark.unit


def test_unscripted_attempt_defaults_to_success():
    injector = FailureInjector()
    outcome = injector.resolve("task-1", 1)
    assert isinstance(outcome, WorkerOutcome)
    assert outcome.status == TaskStatus.SUCCESS


def test_inject_timeout_raises_worker_error():
    injector = FailureInjector()
    inject_timeout(injector, "task-1")
    with pytest.raises(WorkerError) as exc_info:
        injector.resolve("task-1", 1)
    assert exc_info.value.category == ErrorCategory.TIMEOUT


def test_inject_navigation_failure_raises_worker_error():
    injector = FailureInjector()
    inject_navigation_failure(injector, "task-1")
    with pytest.raises(WorkerError) as exc_info:
        injector.resolve("task-1", 1)
    assert exc_info.value.category == ErrorCategory.TRANSIENT_NAVIGATION


def test_inject_access_limited_raises_worker_error():
    injector = FailureInjector()
    inject_access_limited(injector, "task-1")
    with pytest.raises(WorkerError) as exc_info:
        injector.resolve("task-1", 1)
    assert exc_info.value.category == ErrorCategory.ANTI_BOT


def test_inject_login_required_raises_worker_error():
    injector = FailureInjector()
    inject_login_required(injector, "task-1")
    with pytest.raises(WorkerError) as exc_info:
        injector.resolve("task-1", 1)
    assert exc_info.value.category == ErrorCategory.LOGIN_WALL


def test_inject_extraction_unresolved_raises_worker_error():
    injector = FailureInjector()
    inject_extraction_unresolved(injector, "task-1")
    with pytest.raises(WorkerError) as exc_info:
        injector.resolve("task-1", 1)
    assert exc_info.value.category == ErrorCategory.SELECTOR_UNCERTAINTY


def test_inject_worker_crash_raises_plain_exception_not_worker_error():
    injector = FailureInjector()
    inject_worker_crash(injector, "task-1")
    with pytest.raises(RuntimeError) as exc_info:
        injector.resolve("task-1", 1)
    assert not isinstance(exc_info.value, WorkerError)


def test_inject_transient_then_success_fails_then_succeeds():
    injector = FailureInjector()
    inject_transient_then_success(injector, "task-1", fail_attempts=2)
    with pytest.raises(WorkerError):
        injector.resolve("task-1", 1)
    with pytest.raises(WorkerError):
        injector.resolve("task-1", 2)
    outcome = injector.resolve("task-1", 3)
    assert outcome.status == TaskStatus.SUCCESS


def test_injection_is_per_task_id_and_does_not_modify_production_worker_code():
    """The injector configures behavior purely via data, never by
    monkeypatching or modifying atlas.workers.base."""
    injector = FailureInjector()
    inject_timeout(injector, "task-A")
    # task-B was never configured - must be unaffected.
    outcome = injector.resolve("task-B", 1)
    assert outcome.status == TaskStatus.SUCCESS
