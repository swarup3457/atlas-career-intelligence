"""Phase 1A: extended failure taxonomy + retry mapping tests."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory, TaskStatus, is_terminal
from atlas.orchestration.retry import evaluate

pytestmark = pytest.mark.unit


def test_http_429_retryable_then_rate_limited():
    assert evaluate(ErrorCategory.HTTP_429, attempt_number=1, retry_budget=2).should_retry is True
    decision = evaluate(ErrorCategory.HTTP_429, attempt_number=3, retry_budget=2)
    assert decision.should_retry is False
    assert decision.terminal_status == TaskStatus.RATE_LIMITED


def test_http_5xx_retryable_then_source_unavailable():
    assert evaluate(ErrorCategory.HTTP_5XX, attempt_number=1, retry_budget=2).should_retry is True
    decision = evaluate(ErrorCategory.HTTP_5XX, attempt_number=3, retry_budget=2)
    assert decision.terminal_status == TaskStatus.SOURCE_UNAVAILABLE


@pytest.mark.parametrize("category", [ErrorCategory.PARSE_FAILURE, ErrorCategory.INVALID_RESPONSE])
def test_parse_failures_are_extraction_unresolved_not_retried(category):
    decision = evaluate(category, attempt_number=1, retry_budget=3)
    assert decision.should_retry is False
    assert decision.terminal_status == TaskStatus.EXTRACTION_UNRESOLVED


def test_config_error_is_permanent_and_not_retried():
    decision = evaluate(ErrorCategory.CONFIG_ERROR, attempt_number=1, retry_budget=3)
    assert decision.should_retry is False
    assert decision.terminal_status == TaskStatus.PERMANENT_FAILURE


def test_source_unavailable_category_is_terminal():
    decision = evaluate(ErrorCategory.SOURCE_UNAVAILABLE, attempt_number=1, retry_budget=3)
    assert decision.should_retry is False
    assert decision.terminal_status == TaskStatus.SOURCE_UNAVAILABLE


@pytest.mark.parametrize(
    "category,expected",
    [
        (ErrorCategory.CAPTCHA, TaskStatus.WAITING_FOR_HUMAN),
        (ErrorCategory.MFA, TaskStatus.WAITING_FOR_HUMAN),
        (ErrorCategory.ANTI_BOT, TaskStatus.ACCESS_LIMITED),
        (ErrorCategory.LOGIN_WALL, TaskStatus.LOGIN_REQUIRED),
    ],
)
def test_security_controls_preserved_non_bypass(category, expected):
    decision = evaluate(category, attempt_number=1, retry_budget=5)
    assert decision.should_retry is False
    assert decision.terminal_status == expected


def test_new_terminal_statuses_are_terminal():
    assert is_terminal(TaskStatus.RATE_LIMITED)
    assert is_terminal(TaskStatus.SOURCE_UNAVAILABLE)
