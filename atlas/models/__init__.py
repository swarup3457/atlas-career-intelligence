"""Atlas shared models: terminal task statuses and error categories.

These are deliberately generic and reusable across every worker/source —
they are NOT specific to career-page checks. Business-specific models
(jobs, companies, candidate profile) will be added when the Workspace
Atlas Agent specification is imported.
"""

from __future__ import annotations

import enum


class TaskStatus(str, enum.Enum):
    """Explicit terminal (and non-terminal) outcomes for any Atlas task.

    IMPORTANT: a selector/extraction failure is NEVER the same thing as
    "no relevant results". If a page was reachable but the extraction
    logic could not determine whether results existed, the correct status
    is EXTRACTION_UNRESOLVED, not NO_RELEVANT_RESULTS. See docs/STATE_MODEL.md.
    """

    # --- Terminal, successful/neutral outcomes ---
    SUCCESS = "SUCCESS"
    NO_RELEVANT_RESULTS = "NO_RELEVANT_RESULTS"
    CLOSED = "CLOSED"
    SKIPPED = "SKIPPED"

    # --- Terminal, unresolved/limited outcomes ---
    EXTRACTION_UNRESOLVED = "EXTRACTION_UNRESOLVED"
    ACCESS_LIMITED = "ACCESS_LIMITED"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"

    # --- Non-terminal / in-flight outcomes ---
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING = "PENDING"


# Statuses that mean "this task is done, remove it from the active queue".
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.SUCCESS,
        TaskStatus.NO_RELEVANT_RESULTS,
        TaskStatus.EXTRACTION_UNRESOLVED,
        TaskStatus.ACCESS_LIMITED,
        TaskStatus.PERMANENT_FAILURE,
        TaskStatus.CLOSED,
        TaskStatus.SKIPPED,
    }
)

# Statuses that require a human before the task can proceed further.
HUMAN_INTERVENTION_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.LOGIN_REQUIRED,
        TaskStatus.WAITING_FOR_HUMAN,
    }
)


class ErrorCategory(str, enum.Enum):
    """Classification of failures, used to drive retry policy."""

    TRANSIENT_NAVIGATION = "TRANSIENT_NAVIGATION"
    TIMEOUT = "TIMEOUT"
    INTENTIONAL_TEST_FAILURE = "INTENTIONAL_TEST_FAILURE"
    CAPTCHA = "CAPTCHA"
    MFA = "MFA"
    ANTI_BOT = "ANTI_BOT"
    LOGIN_WALL = "LOGIN_WALL"
    SELECTOR_UNCERTAINTY = "SELECTOR_UNCERTAINTY"
    UNKNOWN = "UNKNOWN"


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES


def requires_human(status: TaskStatus) -> bool:
    return status in HUMAN_INTERVENTION_STATUSES
