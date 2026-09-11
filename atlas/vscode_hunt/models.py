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


class TaskKind(str, enum.Enum):
    """PRODUCTION R1 §7 job-lead-first task kinds (supersede universal company lanes)."""
    DISCOVERY_QUERY_BATCH = "DISCOVERY_QUERY_BATCH"
    VERIFY_JOB_LEAD_BATCH = "VERIFY_JOB_LEAD_BATCH"
    EXPAND_VERIFIED_COMPANY = "EXPAND_VERIFIED_COMPANY"
    REVERIFY_HISTORICAL_JOB = "REVERIFY_HISTORICAL_JOB"


class LeadClassification(str, enum.Enum):
    """The terminal outcome every assigned lead must reach (§7)."""
    VERIFIED_ACCEPTED = "VERIFIED_ACCEPTED"
    VERIFIED_STRETCH = "VERIFIED_STRETCH"
    VERIFIED_REJECTED = "VERIFIED_REJECTED"
    PORTAL_ONLY_UNVERIFIED = "PORTAL_ONLY_UNVERIFIED"
    CLOSED = "CLOSED"
    FOREIGN = "FOREIGN"
    DUPLICATE = "DUPLICATE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


TERMINAL_LEAD_CLASSIFICATIONS = frozenset(item.value for item in LeadClassification)


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
    query_families: tuple[str, ...] = ()
    india_policy: str = "INDIA_ONLY_EXPLICIT_LOCATION"
    experience_policy: str = "HARD_REJECT_MANDATORY_4_PLUS"
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


RESULT_SCHEMA_VERSION = 2
TASK_SCHEMA_VERSION = 2

# Canonical five-lane search contract (supersedes the obsolete three-lane one).
TASK_CONTRACT_VERSION = 3

# Runtime handshake contract (PRODUCTION R1 §5): bumped when the runtime tool
# response contract changes, so a session preflight can detect a stale server.
RUNTIME_CONTRACT_VERSION = 1

# Verification batch sizing (§7): a batch carries at most 6-8 promising leads.
VERIFICATION_BATCH_MAX_LEADS = 8

# Attempt ceiling (§13): 1 primary + 1 correction + at most 1 infrastructure retry.
NORMAL_PRIMARY_ATTEMPTS = 1
NORMAL_CORRECTION_ATTEMPTS = 1
MAX_INFRASTRUCTURE_RETRIES = 1
ABSOLUTE_MAX_ATTEMPTS = 3


def batch_is_complete(classifications: Any) -> bool:
    """A VERIFY_JOB_LEAD_BATCH is complete only when every assigned lead has a
    durable terminal classification (§7)."""
    items = [str(item) for item in (classifications or [])]
    return bool(items) and all(item in TERMINAL_LEAD_CLASSIFICATIONS for item in items)

CANONICAL_LANES: tuple[str, ...] = (
    "JAVA_BACKEND",
    "JAVA_FULLSTACK",
    "REACT_FRONTEND",
    "DOTNET",
    "ENTERPRISE_HR_PAYROLL_INTEGRATION",
)

# Obsolete lane names must never remain active canonical obligations.
LEGACY_LANE_ALIASES: dict[str, str] = {
    "DOTNET_BACKEND": "DOTNET",
    "REACT_ENTERPRISE": "REACT_FRONTEND",
}


def map_lane(lane: str) -> str:
    """Translate a single legacy lane alias to its canonical name."""
    return LEGACY_LANE_ALIASES.get(lane, lane)


def normalize_lanes(lanes: Any) -> tuple[str, ...]:
    """Map any legacy aliases to canonical names, de-duplicating in canonical order.

    An empty/absent input yields the full canonical five-lane set.
    """
    if not lanes:
        return CANONICAL_LANES
    mapped = {map_lane(str(lane)) for lane in lanes}
    ordered = [lane for lane in CANONICAL_LANES if lane in mapped]
    extra = [lane for lane in dict.fromkeys(map_lane(str(lane)) for lane in lanes) if lane not in CANONICAL_LANES]
    return tuple(ordered + extra)


def canonical_contract_lanes(_lanes: Any = None) -> tuple[str, ...]:
    """The full canonical obligation set every company task must carry."""
    return CANONICAL_LANES


def contract_hash(lanes: Any) -> str:
    """Stable content hash of a lane set, order-independent."""
    import hashlib
    import json as _json

    payload = _json.dumps(sorted(str(lane) for lane in (lanes or ())), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
