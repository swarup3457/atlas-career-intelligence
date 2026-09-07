"""Atlas centralized retry policy.

Every worker/source must funnel its failure handling through this module
rather than inventing ad-hoc retry logic. This guarantees:
    - a single, auditable place that defines what is retryable
    - a hard, non-negotiable retry budget (never infinite retries)
    - correct separation between "retry" and "escalate to human"
    - correct separation between "no results" and "extraction uncertain"
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas.models import ErrorCategory, TaskStatus

# Error categories that are safe to retry automatically, within budget.
RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.TRANSIENT_NAVIGATION,
        ErrorCategory.TIMEOUT,
        ErrorCategory.INTENTIONAL_TEST_FAILURE,
        ErrorCategory.HTTP_429,
        ErrorCategory.HTTP_5XX,
    }
)

# Error categories that must NEVER be retried automatically - retrying
# against a security control would be an attempted bypass, which Atlas
# must never do.
NEVER_RETRY_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.CAPTCHA,
        ErrorCategory.MFA,
        ErrorCategory.ANTI_BOT,
        ErrorCategory.LOGIN_WALL,
    }
)

# Extraction-uncertainty categories: reported truthfully, never retried
# blindly (see docs/STATE_MODEL.md and docs/SOURCE_HEALTH.md). A malformed
# parse is never silently downgraded to "no results".
REPORT_NOT_RETRY_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.SELECTOR_UNCERTAINTY,
        ErrorCategory.PARSE_FAILURE,
        ErrorCategory.INVALID_RESPONSE,
    }
)

# Maps a non-retryable error category to the terminal/escalation status it
# should produce.
CATEGORY_TERMINAL_STATUS: dict[ErrorCategory, TaskStatus] = {
    ErrorCategory.CAPTCHA: TaskStatus.WAITING_FOR_HUMAN,
    ErrorCategory.MFA: TaskStatus.WAITING_FOR_HUMAN,
    ErrorCategory.LOGIN_WALL: TaskStatus.LOGIN_REQUIRED,
    ErrorCategory.ANTI_BOT: TaskStatus.ACCESS_LIMITED,
    ErrorCategory.SELECTOR_UNCERTAINTY: TaskStatus.EXTRACTION_UNRESOLVED,
    ErrorCategory.PARSE_FAILURE: TaskStatus.EXTRACTION_UNRESOLVED,
    ErrorCategory.INVALID_RESPONSE: TaskStatus.EXTRACTION_UNRESOLVED,
    ErrorCategory.CONFIG_ERROR: TaskStatus.PERMANENT_FAILURE,
    ErrorCategory.SOURCE_UNAVAILABLE: TaskStatus.SOURCE_UNAVAILABLE,
    ErrorCategory.UNKNOWN: TaskStatus.PERMANENT_FAILURE,
}

# When a *retryable* category exhausts its budget, the truthful terminal
# status it produces (default PERMANENT_FAILURE).
EXHAUSTION_TERMINAL_STATUS: dict[ErrorCategory, TaskStatus] = {
    ErrorCategory.HTTP_429: TaskStatus.RATE_LIMITED,
    ErrorCategory.HTTP_5XX: TaskStatus.SOURCE_UNAVAILABLE,
}


@dataclass(frozen=True)
class RetryDecision:
    """Result of evaluating a failure against the retry policy."""

    should_retry: bool
    terminal_status: TaskStatus | None  # set when should_retry is False
    reason: str


def evaluate(
    category: ErrorCategory,
    attempt_number: int,
    retry_budget: int,
) -> RetryDecision:
    """Decide whether a failed attempt should be retried.

    Args:
        category: classified reason for the failure.
        attempt_number: the attempt that just failed (1-indexed).
        retry_budget: maximum number of RETRIES allowed (not counting the
            first attempt), e.g. retry_budget=2 allows up to 3 total
            attempts.

    Returns:
        A RetryDecision. If should_retry is False, terminal_status is
        always populated with a truthful terminal/escalation status.
    """
    if retry_budget < 0:
        raise ValueError("retry_budget must be >= 0")

    if category in NEVER_RETRY_CATEGORIES:
        status = CATEGORY_TERMINAL_STATUS[category]
        return RetryDecision(
            should_retry=False,
            terminal_status=status,
            reason=(
                f"Error category {category.value} must never be retried "
                f"(would risk bypassing a security control); escalating to {status.value}."
            ),
        )

    if category in REPORT_NOT_RETRY_CATEGORIES:
        # Extraction/parse uncertainty is not automatically retried - it is
        # reported truthfully so an alternate strategy can be chosen later
        # (see docs/STATE_MODEL.md). Callers MAY requeue with a different
        # strategy, but that is a deliberate decision, not an automatic
        # retry of the same approach.
        return RetryDecision(
            should_retry=False,
            terminal_status=TaskStatus.EXTRACTION_UNRESOLVED,
            reason="Selector/parse uncertainty is reported truthfully, not retried blindly.",
        )

    if category in RETRYABLE_CATEGORIES:
        if attempt_number > retry_budget:
            terminal = EXHAUSTION_TERMINAL_STATUS.get(category, TaskStatus.PERMANENT_FAILURE)
            return RetryDecision(
                should_retry=False,
                terminal_status=terminal,
                reason=(
                    f"Retry budget exhausted after {attempt_number} attempts "
                    f"(budget={retry_budget}); terminal {terminal.value}."
                ),
            )
        return RetryDecision(
            should_retry=True,
            terminal_status=None,
            reason=f"Transient failure ({category.value}), retrying (attempt {attempt_number} of {retry_budget + 1}).",
        )

    # Non-retryable, explicitly-mapped terminal categories (CONFIG_ERROR,
    # SOURCE_UNAVAILABLE, UNKNOWN, ...).
    if category in CATEGORY_TERMINAL_STATUS:
        status = CATEGORY_TERMINAL_STATUS[category]
        return RetryDecision(
            should_retry=False,
            terminal_status=status,
            reason=f"Error category {category.value} is terminal ({status.value}); not retried.",
        )

    # Unknown/unclassified error - fail safe: do not retry indefinitely.
    return RetryDecision(
        should_retry=False,
        terminal_status=TaskStatus.PERMANENT_FAILURE,
        reason=f"Unclassified error category {category.value}; treated as permanent failure.",
    )
