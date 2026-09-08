"""Optional sanitized remote run-event audit (Phase 1B, build spec section 23).

The legacy GitHub event store must NOT be the local operational database.
Phase 1B exposes an OPTIONAL, sanitized, immutable run-event export that is
never required for a run to succeed: a failed remote audit produces a
``REMOTE_AUDIT_DEGRADED`` warning while local state and the report stay
valid. GitHub availability is never a prerequisite for local search.

No candidate-private data is written remotely; :func:`sanitize_event` drops
any PII-ish keys defensively.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

REMOTE_AUDIT_OK = "REMOTE_AUDIT_OK"
REMOTE_AUDIT_DEGRADED = "REMOTE_AUDIT_DEGRADED"
REMOTE_AUDIT_DISABLED = "REMOTE_AUDIT_DISABLED"

# EXPLICIT ALLOWLIST (build spec 22 / P1-9). A denylist can silently leak any
# newly-added sensitive field; instead the remote sink receives ONLY these
# known-safe, non-sensitive fields and everything else — including unexpected
# nested structures, raw URLs, notes, contacts, resume/profile text, cookies,
# and auth refs — is DROPPED. Lists and mappings are validated recursively.
_ALLOWED_SCALAR_FIELDS: frozenset[str] = frozenset(
    {
        "schema", "schema_version", "version", "type", "event", "kind",
        "run_id", "timestamp", "started_at", "ended_at", "completed_at",
        "policy_fingerprint", "plan_fingerprint", "sealed_fingerprint",
        "terminal_status", "status", "phase",
    }
)
# Aggregate count maps: string -> number ONLY (no nested objects/strings).
_ALLOWED_COUNT_MAPS: frozenset[str] = frozenset(
    {"counters", "coverage_counts", "source_counts", "lane_counts"}
)
# Short non-sensitive error-category CODE lists.
_ALLOWED_CODE_LISTS: frozenset[str] = frozenset(
    {"error_category", "error_categories", "error_codes"}
)

_MAX_STR = 128
_MAX_LIST = 128


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return str(value)[:_MAX_STR]


def _safe_count_map(value: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(value, Mapping):
        return out
    for key, val in value.items():
        if isinstance(key, str) and isinstance(val, (int, float)) and not isinstance(val, bool):
            out[str(key)[:_MAX_STR]] = val
    return out


def _safe_code_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in list(value)[:_MAX_LIST]:
        if isinstance(item, str):
            out.append(item[:_MAX_STR])
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            out.append(str(item))
    return out


def sanitize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """ALLOWLIST an event: keep only explicitly-permitted, non-sensitive fields
    and drop everything else (including unexpected nested structures). Count
    maps keep string->number entries only; code lists keep short strings only.
    This is intentionally stricter than a denylist (build spec 22)."""
    out: dict[str, Any] = {}
    if not isinstance(event, Mapping):
        return out
    for key, value in event.items():
        k = str(key)
        kl = k.lower()
        if kl in _ALLOWED_SCALAR_FIELDS:
            out[k] = _safe_scalar(value)
        elif kl in _ALLOWED_COUNT_MAPS:
            out[k] = _safe_count_map(value)
        elif kl in _ALLOWED_CODE_LISTS:
            out[k] = _safe_code_list(value)
        # else: DROP — not on the allowlist.
    return out


def build_run_audit_event(
    *,
    run_id: str,
    terminal_status: str,
    policy_fingerprint: str = "",
    plan_fingerprint: str = "",
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    counters: Optional[Mapping[str, Any]] = None,
    coverage_counts: Optional[Mapping[str, Any]] = None,
    source_counts: Optional[Mapping[str, Any]] = None,
    error_categories: Optional[Sequence[str]] = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    """Build a typed, allowlisted remote run-event. The result is already
    sanitized and safe to export."""
    return sanitize_event(
        {
            "schema_version": schema_version,
            "type": "run_event",
            "run_id": run_id,
            "terminal_status": terminal_status,
            "policy_fingerprint": policy_fingerprint,
            "plan_fingerprint": plan_fingerprint,
            "started_at": started_at,
            "ended_at": ended_at,
            "counters": dict(counters or {}),
            "coverage_counts": dict(coverage_counts or {}),
            "source_counts": dict(source_counts or {}),
            "error_categories": list(error_categories or []),
        }
    )


@runtime_checkable
class RemoteAuditExporter(Protocol):
    name: str

    def export(self, run_id: str, events: Sequence[Mapping[str, Any]]) -> None:
        ...


class NullRemoteAudit:
    """No-op exporter used when remote audit is disabled (the default)."""

    name = "none"

    def export(self, run_id: str, events: Sequence[Mapping[str, Any]]) -> None:  # noqa: ARG002
        return None


class LocalFileRemoteAudit:
    """Writes sanitized events to a LOCAL file (a stand-in for a remote sink;
    still never required for the run to succeed)."""

    name = "local_file"

    def __init__(self, path):
        from pathlib import Path

        self.path = Path(path)

    def export(self, run_id: str, events: Sequence[Mapping[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"run_id": run_id, "events": [sanitize_event(e) for e in events]}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class FailingRemoteAudit:
    """Test/demonstration exporter that always fails, proving the run still
    completes with a REMOTE_AUDIT_DEGRADED warning."""

    name = "failing"

    def export(self, run_id: str, events: Sequence[Mapping[str, Any]]) -> None:  # noqa: ARG002
        raise ConnectionError("simulated remote audit outage")


@dataclass
class RemoteAuditResult:
    status: str
    exporter: str
    exported_events: int = 0
    warning: str = ""
    events_sanitized: bool = True


def export_run_audit(
    exporter: Optional[RemoteAuditExporter],
    run_id: str,
    events: Sequence[Mapping[str, Any]],
    *,
    enabled: bool = True,
) -> RemoteAuditResult:
    """Attempt a sanitized remote audit export. NEVER raises: a failure is
    captured as ``REMOTE_AUDIT_DEGRADED`` so local state/report remain valid."""
    if not enabled or exporter is None or getattr(exporter, "name", "none") == "none":
        return RemoteAuditResult(status=REMOTE_AUDIT_DISABLED, exporter="none")
    sanitized = [sanitize_event(e) for e in events]
    try:
        exporter.export(run_id, sanitized)
        return RemoteAuditResult(
            status=REMOTE_AUDIT_OK, exporter=exporter.name, exported_events=len(sanitized)
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never propagate
        return RemoteAuditResult(
            status=REMOTE_AUDIT_DEGRADED,
            exporter=getattr(exporter, "name", "unknown"),
            warning=f"{type(exc).__name__}: {exc}",
        )


__all__ = [
    "REMOTE_AUDIT_OK",
    "REMOTE_AUDIT_DEGRADED",
    "REMOTE_AUDIT_DISABLED",
    "sanitize_event",
    "build_run_audit_event",
    "RemoteAuditExporter",
    "NullRemoteAudit",
    "LocalFileRemoteAudit",
    "FailingRemoteAudit",
    "RemoteAuditResult",
    "export_run_audit",
]
