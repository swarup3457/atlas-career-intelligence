"""Pytest coverage for atlas.utils.logging structured logging (Phase 0.5
spec section 2). Uses tmp_path only."""

from __future__ import annotations

import json
import logging

import pytest

from atlas.utils.logging import configure_logging, log_task_event

pytestmark = pytest.mark.unit


def test_configure_logging_writes_json_lines(tmp_path):
    logs_dir = tmp_path / "logs"
    logger = configure_logging(logs_dir, level=logging.INFO)
    log_task_event(
        logger,
        run_id="run-1",
        task_id="task-1",
        status="SUCCESS",
        company="Acme",
        attempt_number=1,
    )
    for handler in logger.handlers:
        handler.flush()

    log_file = logs_dir / "atlas.log"
    assert log_file.exists()
    lines = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 1
    payload = json.loads(lines[-1])
    assert payload["message"] == "task_event"
    assert payload["level"] == "INFO"


def test_sensitive_fields_are_redacted(tmp_path):
    logs_dir = tmp_path / "logs"
    logger = configure_logging(logs_dir, level=logging.INFO)
    logger.info(
        "credentials should never leak",
        extra={"atlas_fields": {"password": "hunter2", "auth_token": "abc123", "safe_field": "ok"}},
    )
    for handler in logger.handlers:
        handler.flush()

    content = (logs_dir / "atlas.log").read_text(encoding="utf-8")
    assert "hunter2" not in content
    assert "abc123" not in content
    assert "***REDACTED***" in content
    assert "ok" in content
