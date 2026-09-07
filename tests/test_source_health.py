"""Phase 1A: source health model + classification tests."""

from __future__ import annotations

import pytest

from atlas.models import TaskStatus
from atlas.sources.health import (
    HealthEvidence,
    SourceHealth,
    SourceHealthState,
    classify_health,
)

pytestmark = pytest.mark.unit


def test_boolean_backward_compat():
    assert SourceHealth(healthy=True).healthy is True
    assert SourceHealth(healthy=True).state == SourceHealthState.HEALTHY
    assert SourceHealth(healthy=False).healthy is False
    assert SourceHealth(healthy=False).state == SourceHealthState.DEGRADED


def test_classify_challenge_is_access_limited():
    h = classify_health(HealthEvidence(challenge_detected=True))
    assert h.state == SourceHealthState.ACCESS_LIMITED


def test_classify_login_redirect_is_auth_required():
    h = classify_health(HealthEvidence(login_redirect=True))
    assert h.state == SourceHealthState.AUTH_REQUIRED


def test_classify_429_and_5xx():
    assert classify_health(HealthEvidence(http_status=429)).state == SourceHealthState.RATE_LIMITED
    assert classify_health(HealthEvidence(http_status=503)).state == SourceHealthState.SOURCE_UNAVAILABLE


def test_classify_structure_missing_is_drift():
    h = classify_health(HealthEvidence(expected_structure_present=False))
    assert h.state == SourceHealthState.SELECTOR_DRIFT_SUSPECTED


def test_classify_zero_with_history_is_drift():
    h = classify_health(HealthEvidence(result_count=0, historical_yields=(42, 38, 47)))
    assert h.state == SourceHealthState.SELECTOR_DRIFT_SUSPECTED


def test_classify_healthy_when_no_signals():
    h = classify_health(HealthEvidence(http_status=200, result_count=10, expected_structure_present=True))
    assert h.state == SourceHealthState.HEALTHY
    assert h.healthy is True


def test_health_is_separate_from_task_status():
    # Diagnostic health states are their own enum, deliberately not merged
    # into the task lifecycle enum.
    health_values = {s.value for s in SourceHealthState}
    task_values = {s.value for s in TaskStatus}
    assert "SELECTOR_DRIFT_SUSPECTED" in health_values
    assert "SELECTOR_DRIFT_SUSPECTED" not in task_values


def test_health_to_dict_has_state_and_evidence():
    h = classify_health(HealthEvidence(http_status=200, result_count=3, expected_structure_present=True))
    d = h.to_dict()
    assert d["state"] == "HEALTHY"
    assert d["evidence"]["result_count"] == 3
