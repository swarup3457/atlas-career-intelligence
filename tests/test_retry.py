"""Pytest coverage for atlas.orchestration.retry (Phase 0.5 spec section 2)."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory, TaskStatus
from atlas.orchestration.retry import evaluate

pytestmark = pytest.mark.unit


def test_transient_failure_within_budget_retries():
    decision = evaluate(ErrorCategory.TRANSIENT_NAVIGATION, attempt_number=1, retry_budget=2)
    assert decision.should_retry is True
    assert decision.terminal_status is None


def test_transient_failure_exceeding_budget_is_permanent_failure():
    decision = evaluate(ErrorCategory.TRANSIENT_NAVIGATION, attempt_number=3, retry_budget=2)
    assert decision.should_retry is False
    assert decision.terminal_status == TaskStatus.PERMANENT_FAILURE


@pytest.mark.parametrize(
    "category,expected_status",
    [
        (ErrorCategory.CAPTCHA, TaskStatus.WAITING_FOR_HUMAN),
        (ErrorCategory.MFA, TaskStatus.WAITING_FOR_HUMAN),
        (ErrorCategory.ANTI_BOT, TaskStatus.ACCESS_LIMITED),
        (ErrorCategory.LOGIN_WALL, TaskStatus.LOGIN_REQUIRED),
    ],
)
def test_never_retry_categories_never_retried(category, expected_status):
    decision = evaluate(category, attempt_number=1, retry_budget=5)
    assert decision.should_retry is False
    assert decision.terminal_status == expected_status


def test_selector_uncertainty_is_extraction_unresolved_not_no_results():
    decision = evaluate(ErrorCategory.SELECTOR_UNCERTAINTY, attempt_number=1, retry_budget=2)
    assert decision.terminal_status == TaskStatus.EXTRACTION_UNRESOLVED
    assert decision.terminal_status != TaskStatus.NO_RELEVANT_RESULTS


def test_negative_retry_budget_rejected():
    with pytest.raises(ValueError):
        evaluate(ErrorCategory.TIMEOUT, attempt_number=1, retry_budget=-1)


def test_unknown_category_is_permanent_failure_not_infinite_retry():
    decision = evaluate(ErrorCategory.UNKNOWN, attempt_number=1, retry_budget=5)
    assert decision.should_retry is False
    assert decision.terminal_status == TaskStatus.PERMANENT_FAILURE
