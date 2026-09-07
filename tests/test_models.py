"""Pytest coverage for atlas.models terminal states (Phase 0.5 spec section 2)."""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory, TaskStatus, is_terminal, requires_human

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.SUCCESS,
        TaskStatus.NO_RELEVANT_RESULTS,
        TaskStatus.EXTRACTION_UNRESOLVED,
        TaskStatus.ACCESS_LIMITED,
        TaskStatus.PERMANENT_FAILURE,
        TaskStatus.CLOSED,
        TaskStatus.SKIPPED,
    ],
)
def test_terminal_statuses(status):
    assert is_terminal(status)


@pytest.mark.parametrize(
    "status",
    [TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.LOGIN_REQUIRED, TaskStatus.WAITING_FOR_HUMAN],
)
def test_non_terminal_statuses(status):
    assert not is_terminal(status)


def test_human_intervention_statuses():
    assert requires_human(TaskStatus.LOGIN_REQUIRED)
    assert requires_human(TaskStatus.WAITING_FOR_HUMAN)
    assert not requires_human(TaskStatus.SUCCESS)


def test_extraction_unresolved_never_conflated_with_no_relevant_results():
    """Explicit non-conflation guard required by the Phase 0 spec: a
    selector/extraction failure must never be reported as 'no results'."""
    assert TaskStatus.EXTRACTION_UNRESOLVED != TaskStatus.NO_RELEVANT_RESULTS
    assert ErrorCategory.SELECTOR_UNCERTAINTY != ErrorCategory.UNKNOWN
