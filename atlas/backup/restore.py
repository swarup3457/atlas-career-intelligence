"""Restore a verified backup into a caller-provided target (Phase 0.95).

:func:`restore_backup` is deliberately conservative. It will only restore a
backup that:

1. **verifies** — every checksum matches and no SQLite DB is corrupt
   (:func:`atlas.backup.verify.verify_backup` must pass first);
2. is **schema-compatible** — its ``state_schema_version`` is not *newer*
   than this codebase's :data:`atlas.persistence.sqlite.SCHEMA_VERSION`
   (equal or older is fine — forward migrations can run; a newer state DB
   from a future Atlas is refused). Patch-level ``atlas_version``
   differences are explicitly NOT treated as incompatible; and
3. targets a **safe directory** — never the real Atlas project root, the
   live ``state/`` directory, ``fixtures/real``, or a browser profile.
   Callers must pass a disposable directory (e.g. pytest ``tmp_path``).

Only after the restored files are re-validated in place (SQLite integrity
+ schema query, checkpoint DB opens, config export parses) is a
``_RESTORE_OK`` marker written and the restore reported as usable.
"""

from __future__ import annotations

import datetime
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import atlas
from atlas.backup.manifest import BackupManifest, MANIFEST_FILENAME
from atlas.backup.sqlite_backup import integrity_check
from atlas.backup.verify import VerifyResult, verify_backup
from atlas.orchestration.checkpoints import open_checkpointer
from atlas.persistence.sqlite import SCHEMA_VERSION, StateStore

# Status constants (stable strings for callers/tests).
RESTORED = "RESTORED"
REFUSED_INVALID_BACKUP = "REFUSED_INVALID_BACKUP"
REFUSED_INCOMPATIBLE_SCHEMA = "REFUSED_INCOMPATIBLE_SCHEMA"
FAILED_VALIDATION = "FAILED_VALIDATION"

RESTORE_MARKER_NAME = "_RESTORE_OK"


class RestoreSafetyError(RuntimeError):
    """Raised when a restore target would overwrite real/protected data."""


@dataclass
class RestoreResult:
    status: str
    target_dir: str
    backup_id: Optional[str] = None
    restored_files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    state_schema_version: Optional[int] = None
    checkpoint_ok: bool = False
    config_ok: bool = False
    marker_path: Optional[str] = None
    verify: Optional[VerifyResult] = None

    @property
    def ok(self) -> bool:
        return self.status == RESTORED

    def render(self) -> str:
        lines = [f"Restore: {self.status} -> {self.target_dir}"]
        if self.backup_id:
            lines.append(f"  backup_id: {self.backup_id}")
        lines.append(f"  files restored: {len(self.restored_files)}")
        lines.append(f"  state schema: {self.state_schema_version}")
        lines.append(f"  checkpoint openable: {self.checkpoint_ok}")
        lines.append(f"  config parses: {self.config_ok}")
        if self.problems:
            lines.append("  problems:")
            for p in self.problems:
                lines.append(f"    - {p}")
        return "\n".join(lines)


def _real_project_root() -> Path:
    return Path(atlas.__file__).resolve().parents[1]


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_safe_restore_target(target_dir: Path) -> None:
    """Refuse a target that resolves onto real/protected Atlas data."""
    target = Path(target_dir).resolve()
    root = _real_project_root()
    protected = [
        root,
        root / "state",
        root / "fixtures" / "real",
        root / ".browser-profile-chrome",
        root / ".browser-profile",
        root / ".venv",
    ]
    for p in protected:
        if target == p or _is_relative_to(target, p):
            raise RestoreSafetyError(
                f"Refusing to restore into protected path {target} (matches {p}). "
                "Restore into a disposable directory (e.g. a tmp_path) instead."
            )


def restore_backup(backup_path: Path, target_dir: Path) -> RestoreResult:
    """Restore the backup at ``backup_path`` into ``target_dir``.

    ``target_dir`` MUST be a caller-provided disposable directory.
    """
    backup_path = Path(backup_path)
    target_dir = Path(target_dir)

    # Safety guard first — never let an invalid backup or a wrong target
    # even begin touching the filesystem.
    _assert_safe_restore_target(target_dir)

    result = RestoreResult(status=FAILED_VALIDATION, target_dir=str(target_dir))

    # 1) verify the backup end-to-end.
    verification = verify_backup(backup_path)
    result.verify = verification
    result.backup_id = verification.backup_id
    if not verification.ok:
        result.status = REFUSED_INVALID_BACKUP
        result.problems = [str(p) for p in verification.problems]
        return result

    manifest = BackupManifest.from_json((backup_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    result.backup_id = manifest.backup_id

    # 2) schema compatibility — refuse a state DB newer than we understand.
    if manifest.state_schema_version > SCHEMA_VERSION:
        result.status = REFUSED_INCOMPATIBLE_SCHEMA
        result.problems.append(
            f"backup state_schema_version={manifest.state_schema_version} is newer than this "
            f"codebase's SCHEMA_VERSION={SCHEMA_VERSION}; cannot safely restore."
        )
        return result

    # 3) copy files into the target (auto-creating parents).
    target_dir.mkdir(parents=True, exist_ok=True)
    for component in manifest.included_components:
        src = backup_path / component.relative_path
        dest = target_dir / component.relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        result.restored_files.append(component.relative_path)

    # 4) validate restored artifacts in place.
    for component in manifest.included_components:
        dest = target_dir / component.relative_path
        if component.kind == "sqlite":
            problems = integrity_check(dest)
            if problems:
                result.problems.append(f"{component.relative_path}: {'; '.join(problems[:3])}")
                continue
            if "checkpoint" in Path(component.relative_path).name.lower():
                try:
                    with open_checkpointer(dest):
                        pass
                    result.checkpoint_ok = True
                except Exception as exc:  # noqa: BLE001
                    result.problems.append(f"{component.relative_path}: checkpoint DB failed to open: {exc}")
            else:
                try:
                    with StateStore(dest) as store:
                        result.state_schema_version = store.schema_version()
                except Exception as exc:  # noqa: BLE001
                    result.problems.append(f"{component.relative_path}: state DB failed to open: {exc}")
        elif component.kind == "config":
            try:
                json.loads(dest.read_text(encoding="utf-8"))
                result.config_ok = True
            except Exception as exc:  # noqa: BLE001
                result.problems.append(f"{component.relative_path}: config export failed to parse: {exc}")

    # 5) publish restore status.
    if result.problems:
        result.status = FAILED_VALIDATION
        return result

    marker = target_dir / RESTORE_MARKER_NAME
    marker.write_text(
        json.dumps(
            {
                "backup_id": manifest.backup_id,
                "restored_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "state_schema_version": result.state_schema_version,
                "atlas_version": manifest.atlas_version,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    result.marker_path = str(marker)
    result.status = RESTORED
    return result


__all__ = [
    "RestoreResult",
    "RestoreSafetyError",
    "restore_backup",
    "RESTORED",
    "REFUSED_INVALID_BACKUP",
    "REFUSED_INCOMPATIBLE_SCHEMA",
    "FAILED_VALIDATION",
    "RESTORE_MARKER_NAME",
]
