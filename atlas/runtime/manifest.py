"""Atlas run manifest + config fingerprint (Phase 0.75 spec sections 17/18).

The run manifest is a generic, durable summary of one run - no
job-specific fields. The config fingerprint is a deterministic,
non-secret hash of the runtime configuration used for that run, so two
Atlas runs can later be compared to help explain why they behaved
differently.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import atlas
from atlas.config import Settings

# Config fields that are safe to include in the fingerprint. Deliberately
# an explicit allow-list (rather than "everything except a deny-list") so
# adding a future Settings field that happens to be sensitive can never
# leak into a fingerprint by accident.
_FINGERPRINT_FIELDS = (
    "browser_channel",
    "default_headless",
    "navigation_timeout_ms",
    "retry_budget",
    "batch_size",
    "controller",
)

_SENSITIVE_NAME_FRAGMENTS = ("token", "secret", "password", "credential", "cookie", "authorization", "auth_")


def compute_config_fingerprint(settings: Settings) -> str:
    """Deterministic sha256 hex digest of the non-secret, safe subset of
    `settings`. Never includes credentials/cookies/tokens/authorization
    data - only the explicit allow-list above, and any field name that
    looks even remotely sensitive is defensively rejected at build time.
    """
    payload: dict[str, Any] = {}
    for name in _FINGERPRINT_FIELDS:
        lowered = name.lower()
        if any(fragment in lowered for fragment in _SENSITIVE_NAME_FRAGMENTS):
            raise ValueError(f"Refusing to fingerprint field that looks sensitive: {name}")
        payload[name] = getattr(settings, name)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def software_versions() -> dict[str, str]:
    versions = {"atlas": atlas.__version__, "schema_version": "1"}
    try:
        import sys

        versions["python"] = sys.version.split()[0]
    except Exception:  # noqa: BLE001
        pass
    try:
        import langgraph

        versions["langgraph"] = getattr(langgraph, "__version__", "unknown")
    except ImportError:
        pass
    try:
        import playwright

        versions["playwright"] = getattr(playwright, "__version__", "unknown")
    except ImportError:
        pass
    return versions


@dataclass
class RunManifest:
    run_id: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    status: str = "INITIALIZING"
    config_fingerprint: str = ""
    controller: str = "none"
    planned_tasks: int = 0
    completed_tasks: int = 0
    remaining_tasks: int = 0
    retry_counts: dict[str, int] = field(default_factory=dict)
    interventions: int = 0
    runtime_seconds: float = 0.0
    software_version: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: Path) -> "RunManifest":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)
