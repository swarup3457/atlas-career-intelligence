"""Atlas Phase 0.75 deterministic demo workload (spec section 7).

50 fake company-like tasks with a required, deterministic outcome
distribution:

    - 42 plain successes (first attempt)
    - 5 transient failures that succeed on retry
    - 1 ACCESS_LIMITED (never retried - security-control category)
    - 1 EXTRACTION_UNRESOLVED (never retried - reported truthfully)
    - 1 LOGIN_REQUIRED (simulated human intervention)

No permanent failure occurs unless a test deliberately injects one via
atlas.runtime.failure_injection. This is demo/test-only workload
metadata - NOT a real job-search source.
"""

from __future__ import annotations

from atlas.runtime.failure_injection import (
    FailureInjector,
    inject_access_limited,
    inject_extraction_unresolved,
    inject_login_required,
    inject_transient_then_success,
)
from atlas.runtime.scheduler import TaskRecord
from atlas.workers.base import BaseWorker, WorkerOutcome

DEMO_TASK_COUNT = 50
TASK_ID_PREFIX = "DEMO-COMPANY"

# Deterministic index assignment (0-indexed within the 50 demo tasks).
ACCESS_LIMITED_INDEX = 0
EXTRACTION_UNRESOLVED_INDEX = 1
LOGIN_REQUIRED_INDEX = 2
TRANSIENT_RETRY_INDICES = (3, 4, 5, 6, 7)  # 5 tasks: fail once, then succeed


def demo_task_id(index: int) -> str:
    return f"{TASK_ID_PREFIX}-{index + 1:03d}"


def build_demo_tasks(n: int = DEMO_TASK_COUNT) -> list[TaskRecord]:
    """Build the deterministic list of demo TaskRecords.

    Priorities are fake/demo metadata only (no job-specific priority
    scheme): the three "interesting" outcome tasks get a slightly higher
    priority so they are scheduled early and are easy to observe in a
    short demo run; everything else is priority 0.
    """
    tasks: list[TaskRecord] = []
    interesting = {ACCESS_LIMITED_INDEX, EXTRACTION_UNRESOLVED_INDEX, LOGIN_REQUIRED_INDEX, *TRANSIENT_RETRY_INDICES}
    for i in range(n):
        priority = 10 if i in interesting else 0
        tasks.append(TaskRecord(task_id=demo_task_id(i), priority=priority, batch="demo", metadata={"index": i}))
    return tasks


def build_demo_failure_injector(n: int = DEMO_TASK_COUNT) -> FailureInjector:
    """Configure a FailureInjector matching the required demo outcome
    distribution described in the module docstring."""
    injector = FailureInjector()
    inject_access_limited(injector, demo_task_id(ACCESS_LIMITED_INDEX))
    inject_extraction_unresolved(injector, demo_task_id(EXTRACTION_UNRESOLVED_INDEX))
    inject_login_required(injector, demo_task_id(LOGIN_REQUIRED_INDEX))
    for idx in TRANSIENT_RETRY_INDICES:
        inject_transient_then_success(injector, demo_task_id(idx), fail_attempts=1)
    return injector


class DemoWorker(BaseWorker):
    """Deterministic, non-web fake worker used ONLY for the Phase 0.75
    runtime-shell demo. Never opens a browser, never makes a network
    call - every outcome comes from the injected FailureInjector script."""

    name = "demo-fake-worker"

    def __init__(self, injector: FailureInjector):
        self.injector = injector

    def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
        return self.injector.resolve(item, attempt_number)


__all__ = [
    "DEMO_TASK_COUNT",
    "ACCESS_LIMITED_INDEX",
    "EXTRACTION_UNRESOLVED_INDEX",
    "LOGIN_REQUIRED_INDEX",
    "TRANSIENT_RETRY_INDICES",
    "demo_task_id",
    "build_demo_tasks",
    "build_demo_failure_injector",
    "DemoWorker",
]
