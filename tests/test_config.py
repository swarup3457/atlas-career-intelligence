"""Pytest coverage for atlas.config (Phase 0.5 spec section 2 + 10)."""

from __future__ import annotations

import pytest

from atlas.config import ConfigValidationError, Settings, load_settings

pytestmark = pytest.mark.unit


def test_load_settings_defaults_are_valid(project_root):
    settings = load_settings()
    assert settings.project_root == project_root
    assert settings.controller == "none"
    assert settings.retry_budget >= 0


def test_load_settings_overrides_apply():
    settings = load_settings(retry_budget=7, batch_size=3)
    assert settings.retry_budget == 7
    assert settings.batch_size == 3


@pytest.mark.parametrize(
    "override",
    [
        {"browser_channel": "not-a-real-channel"},
        {"controller": "not-a-real-controller"},
        {"retry_budget": -1},
        {"batch_size": 0},
        {"navigation_timeout_ms": 0},
    ],
)
def test_load_settings_rejects_invalid_values(override):
    with pytest.raises(ConfigValidationError):
        load_settings(**override)


def test_validate_reports_every_problem_not_just_first():
    settings = Settings(retry_budget=-1, batch_size=0, controller="bogus")
    with pytest.raises(ConfigValidationError) as excinfo:
        settings.validate()
    message = str(excinfo.value)
    assert "retry_budget" in message
    assert "batch_size" in message
    assert "controller" in message


def test_validate_rejects_relative_paths():
    from pathlib import Path

    settings = Settings(state_db=Path("relative/path.sqlite"))
    with pytest.raises(ConfigValidationError, match="state_db"):
        settings.validate()
