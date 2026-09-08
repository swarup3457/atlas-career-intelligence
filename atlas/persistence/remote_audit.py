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

# Keys that must never leave the local machine in a remote audit event.
_PII_KEYS: frozenset[str] = frozenset(
    {
        "email", "phone", "resume", "profile", "candidate", "name", "address",
        "contact", "personal", "pii", "cookie", "token", "secret", "authorization",
    }
)


def sanitize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``event`` with any PII-ish keys removed (recursively)."""
    out: dict[str, Any] = {}
    for key, value in event.items():
        if str(key).lower() in _PII_KEYS:
            continue
        if isinstance(value, Mapping):
            out[key] = sanitize_event(value)
        else:
            out[key] = value
    return out


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
    "RemoteAuditExporter",
    "NullRemoteAudit",
    "LocalFileRemoteAudit",
    "FailingRemoteAudit",
    "RemoteAuditResult",
    "export_run_audit",
]
