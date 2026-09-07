"""Backup manifest model (Phase 0.95).

The manifest is the authoritative, self-describing index of a backup: it
records *what* was captured (every included file with its sha256 and
size), *what was deliberately left out and why* (browser profile,
secrets, venv, caches), and the environment metadata needed to decide
whether the backup can be safely restored into a given codebase
(schema/version fingerprints).

Everything is plain JSON so a manifest can be read by a future Atlas
version, by an operator, or by tooling that never imports Atlas.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import platform as _platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import atlas
from atlas.utils.ids import new_ulid

MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1

_SHA_CHUNK = 1024 * 1024  # 1 MiB streaming reads keep large DBs off-heap


def sha256_file(path: Path) -> str:
    """Streaming sha256 hex digest of a file (memory-bounded)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_SHA_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class IncludedComponent:
    """One file captured in the backup.

    ``relative_path`` is POSIX-style and relative to the backup root, so a
    manifest is portable across operating systems.
    """

    relative_path: str
    sha256: str
    size: int
    kind: str = "file"  # file | sqlite | config | run_manifest

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncludedComponent":
        return cls(
            relative_path=str(data["relative_path"]),
            sha256=str(data["sha256"]),
            size=int(data["size"]),
            kind=str(data.get("kind", "file")),
        )


@dataclass
class ExcludedComponent:
    """Something intentionally NOT backed up, with the reason why."""

    name: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExcludedComponent":
        return cls(name=str(data["name"]), reason=str(data["reason"]))


@dataclass
class BackupManifest:
    backup_id: str
    created_at: str  # UTC ISO-8601
    atlas_version: str
    python_version: str
    langgraph_version: str
    playwright_version: str
    state_schema_version: int
    checkpoint_schema_info: str
    platform: str
    config_fingerprint: str
    included_components: list[IncludedComponent] = field(default_factory=list)
    excluded_components: list[ExcludedComponent] = field(default_factory=list)
    manifest_schema_version: int = MANIFEST_SCHEMA_VERSION

    # ------------------------------------------------------------------
    def total_size(self) -> int:
        return sum(c.size for c in self.included_components)

    def find(self, relative_path: str) -> Optional[IncludedComponent]:
        for c in self.included_components:
            if c.relative_path == relative_path:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_schema_version": self.manifest_schema_version,
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "atlas_version": self.atlas_version,
            "python_version": self.python_version,
            "langgraph_version": self.langgraph_version,
            "playwright_version": self.playwright_version,
            "state_schema_version": self.state_schema_version,
            "checkpoint_schema_info": self.checkpoint_schema_info,
            "platform": self.platform,
            "config_fingerprint": self.config_fingerprint,
            "included_components": [c.to_dict() for c in self.included_components],
            "excluded_components": [c.to_dict() for c in self.excluded_components],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackupManifest":
        return cls(
            backup_id=str(data["backup_id"]),
            created_at=str(data["created_at"]),
            atlas_version=str(data["atlas_version"]),
            python_version=str(data["python_version"]),
            langgraph_version=str(data["langgraph_version"]),
            playwright_version=str(data["playwright_version"]),
            state_schema_version=int(data["state_schema_version"]),
            checkpoint_schema_info=str(data["checkpoint_schema_info"]),
            platform=str(data["platform"]),
            config_fingerprint=str(data["config_fingerprint"]),
            included_components=[
                IncludedComponent.from_dict(c) for c in data.get("included_components", [])
            ],
            excluded_components=[
                ExcludedComponent.from_dict(c) for c in data.get("excluded_components", [])
            ],
            manifest_schema_version=int(data.get("manifest_schema_version", MANIFEST_SCHEMA_VERSION)),
        )

    @classmethod
    def from_json(cls, text: str) -> "BackupManifest":
        return cls.from_dict(json.loads(text))


def generate_backup_id(moment: datetime.datetime) -> str:
    """Create a sortable, collision-resistant backup id.

    Shape: ``<compact-utc-stamp>-<ulid>`` (e.g.
    ``20260906T180000Z-01J...``). The leading UTC stamp makes a directory
    listing human-chronological; the trailing ULID adds 80 bits of
    randomness so two backups created in the same second (or with the same
    injected :class:`FixedClock`) still get distinct, sortable ids.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    moment = moment.astimezone(datetime.timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{new_ulid(moment)}"


def gather_software_versions() -> dict[str, str]:
    """Best-effort version fingerprint of the running environment.

    Reuses the same lenient import strategy as
    :func:`atlas.runtime.manifest.software_versions` so a missing optional
    dependency degrades to ``"unknown"`` instead of raising.
    """
    versions = {
        "atlas": atlas.__version__,
        "python": sys.version.split()[0],
        "platform": _platform.platform(),
    }
    try:
        import langgraph

        versions["langgraph"] = getattr(langgraph, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        versions["langgraph"] = "unknown"
    try:
        import playwright

        versions["playwright"] = getattr(playwright, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        versions["playwright"] = "unknown"
    return versions


__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "sha256_file",
    "IncludedComponent",
    "ExcludedComponent",
    "BackupManifest",
    "generate_backup_id",
    "gather_software_versions",
]
