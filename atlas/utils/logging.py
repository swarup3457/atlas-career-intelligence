"""Atlas structured logging.

Every run/task log record should carry: run_id, task_id, company/source
(where applicable), attempt number, start/end time, status, error
category/detail, and next action. Logs must NEVER contain passwords,
auth tokens, session cookies, or other secret values — this module
provides a small redaction safety net for common secret-looking keys in
addition to the convention of simply never passing secrets in.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path
from typing import Any, Optional

_REDACTED = "***REDACTED***"
_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "auth",
    "credential",
    "api_key",
    "apikey",
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for k, v in value.items():
            if any(frag in k.lower() for frag in _SENSITIVE_KEY_FRAGMENTS):
                redacted[k] = _REDACTED
            else:
                redacted[k] = _redact(v)
        return redacted
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "atlas_fields", None)
        if extra:
            payload.update(_redact(extra))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(logs_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """Configure the root 'atlas' logger with a rotating JSON-lines file
    handler under logs_dir plus a plain console handler."""
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("atlas")
    logger.setLevel(level)
    logger.handlers.clear()

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "atlas.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(StructuredFormatter())
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(console_handler)

    logger.propagate = False
    return logger


def log_task_event(
    logger: logging.Logger,
    *,
    run_id: str,
    task_id: str,
    status: str,
    company: Optional[str] = None,
    source: Optional[str] = None,
    attempt_number: Optional[int] = None,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    error_category: Optional[str] = None,
    error_detail: Optional[str] = None,
    next_action: Optional[str] = None,
    message: str = "task_event",
) -> None:
    """Emit one structured task log record with the standard field set."""
    fields = {
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "company": company,
        "source": source,
        "attempt_number": attempt_number,
        "started_at": started_at,
        "ended_at": ended_at,
        "error_category": error_category,
        "error_detail": error_detail,
        "next_action": next_action,
    }
    logger.info(message, extra={"atlas_fields": fields})
