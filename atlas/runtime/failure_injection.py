"""Atlas reusable, deterministic failure-injection mechanism (Phase 0.75
spec section 15).

Lets tests (and the Phase 0.75 demo workload) script exactly what a
worker's `attempt()` call should do on a given attempt number for a given
task id, WITHOUT modifying any production worker code. Future connector
tests can reuse this same mechanism to simulate: timeout, navigation
failure, access limited, login required, extraction unresolved, and
worker crash.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.models import ErrorCategory, TaskStatus
from atlas.workers.base import WorkerError, WorkerOutcome


class ScriptedKind(str, enum.Enum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"  # raises WorkerError(category) - governor applies retry policy
    CRASH = "CRASH"  # raises an unexpected, non-WorkerError exception


@dataclass
class ScriptedStep:
    kind: ScriptedKind
    category: Optional[ErrorCategory] = None
    status: TaskStatus = TaskStatus.SUCCESS
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class FailureInjector:
    """Deterministic, reusable failure-injection script.

    `configure(task_id, attempt_number, step)` schedules exactly what
    should happen for that (task_id, attempt_number) pair. Any
    (task_id, attempt_number) that was never configured defaults to a
    plain SUCCESS - so callers only need to script the interesting
    attempts, e.g. "fail on attempt 1, succeed on attempt 2".
    """

    def __init__(self) -> None:
        self._script: dict[tuple[str, int], ScriptedStep] = {}

    def configure(self, task_id: str, attempt_number: int, step: ScriptedStep) -> None:
        self._script[(task_id, attempt_number)] = step

    def step_for(self, task_id: str, attempt_number: int) -> ScriptedStep:
        return self._script.get((task_id, attempt_number), ScriptedStep(kind=ScriptedKind.SUCCESS))

    def resolve(self, task_id: str, attempt_number: int) -> WorkerOutcome:
        """Apply the scripted step for (task_id, attempt_number).

        Returns a WorkerOutcome on success, raises WorkerError for a
        scripted ERROR step (letting the caller's centralized retry
        policy decide retry vs. escalate, exactly like a real worker),
        or raises a plain RuntimeError for a scripted CRASH step
        (simulating an unhandled worker crash).
        """
        step = self.step_for(task_id, attempt_number)
        if step.kind == ScriptedKind.SUCCESS:
            return WorkerOutcome(status=step.status, payload=dict(step.payload) or {"task_id": task_id})
        if step.kind == ScriptedKind.ERROR:
            assert step.category is not None
            raise WorkerError(step.category, step.message or f"scripted failure for {task_id}")
        if step.kind == ScriptedKind.CRASH:
            raise RuntimeError(step.message or f"simulated worker crash for {task_id}")
        raise AssertionError(f"Unknown ScriptedKind: {step.kind}")  # pragma: no cover


# --- Convenience factory helpers (spec section 15: timeout, navigation
# failure, access limited, login required, extraction unresolved, worker
# crash) - each configures a FailureInjector in place and returns it so
# calls can be chained. ---


def inject_timeout(injector: FailureInjector, task_id: str, attempt_number: int = 1, message: str = "simulated timeout") -> FailureInjector:
    injector.configure(task_id, attempt_number, ScriptedStep(kind=ScriptedKind.ERROR, category=ErrorCategory.TIMEOUT, message=message))
    return injector


def inject_navigation_failure(injector: FailureInjector, task_id: str, attempt_number: int = 1, message: str = "simulated navigation failure") -> FailureInjector:
    injector.configure(
        task_id, attempt_number, ScriptedStep(kind=ScriptedKind.ERROR, category=ErrorCategory.TRANSIENT_NAVIGATION, message=message)
    )
    return injector


def inject_access_limited(injector: FailureInjector, task_id: str, attempt_number: int = 1, message: str = "simulated anti-bot access limitation") -> FailureInjector:
    injector.configure(task_id, attempt_number, ScriptedStep(kind=ScriptedKind.ERROR, category=ErrorCategory.ANTI_BOT, message=message))
    return injector


def inject_login_required(injector: FailureInjector, task_id: str, attempt_number: int = 1, message: str = "simulated login wall") -> FailureInjector:
    injector.configure(task_id, attempt_number, ScriptedStep(kind=ScriptedKind.ERROR, category=ErrorCategory.LOGIN_WALL, message=message))
    return injector


def inject_extraction_unresolved(injector: FailureInjector, task_id: str, attempt_number: int = 1, message: str = "simulated selector uncertainty") -> FailureInjector:
    injector.configure(
        task_id, attempt_number, ScriptedStep(kind=ScriptedKind.ERROR, category=ErrorCategory.SELECTOR_UNCERTAINTY, message=message)
    )
    return injector


def inject_worker_crash(injector: FailureInjector, task_id: str, attempt_number: int = 1, message: str = "simulated worker crash") -> FailureInjector:
    injector.configure(task_id, attempt_number, ScriptedStep(kind=ScriptedKind.CRASH, message=message))
    return injector


def inject_transient_then_success(injector: FailureInjector, task_id: str, fail_attempts: int = 1, category: ErrorCategory = ErrorCategory.TRANSIENT_NAVIGATION) -> FailureInjector:
    """Fail on attempts 1..fail_attempts, succeed on attempt fail_attempts+1."""
    for attempt_number in range(1, fail_attempts + 1):
        injector.configure(
            task_id,
            attempt_number,
            ScriptedStep(kind=ScriptedKind.ERROR, category=category, message=f"simulated transient failure (attempt {attempt_number})"),
        )
    return injector
