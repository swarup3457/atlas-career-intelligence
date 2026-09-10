from __future__ import annotations

import dataclasses
import enum
from typing import Any


class Backend(str, enum.Enum):
    VSCODE_SUBAGENT = "VSCODE_SUBAGENT"
    VSCODE_PERSISTENT_SESSION = "VSCODE_PERSISTENT_SESSION"
    COPILOT_CLI_MCP = "COPILOT_CLI_MCP"
    STRUCTURED_ATS = "STRUCTURED_ATS"


class Action(str, enum.Enum):
    COMPLETE = "COMPLETE"
    FOLLOW_UP_REQUIRED = "FOLLOW_UP_REQUIRED"
    RETRY_INTERNAL = "RETRY_INTERNAL"
    NEEDS_REPAIR = "NEEDS_REPAIR"
    REJECTED_INVALID_RESULT = "REJECTED_INVALID_RESULT"


@dataclasses.dataclass(frozen=True)
class HuntRun:
    run_id: str
    status: str
    company_count: int


@dataclasses.dataclass(frozen=True)
class HuntTask:
    run_id: str
    task_id: str
    company_id: str
    company_name: str
    official_domain: str
    careers_url: str | None
    lanes: tuple[str, ...]
    status: str = "PENDING"
    attempt_number: int = 0


@dataclasses.dataclass(frozen=True)
class WorkerAttempt:
    attempt_id: str
    task_id: str
    attempt_number: int
    backend: Backend
    parent_attempt_id: str | None = None


def result_identity(result: dict[str, Any]) -> tuple[str, str, str]:
    return (str(result.get("run_id", "")), str(result.get("task_id", "")), str(result.get("attempt_id", "")))
