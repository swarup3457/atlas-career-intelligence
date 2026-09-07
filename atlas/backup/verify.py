"""Backup verification (Phase 0.95).

:func:`verify_backup` is the single source of truth for "is this backup
trustworthy?". It is used in three places:

* during :func:`atlas.backup.backup.create_backup`, against the *staging*
  directory, so an incompletely-written or checksum-mismatched backup is
  never published;
* by ``atlas backup verify`` for operators; and
* at the top of :func:`atlas.backup.restore.restore_backup`, so a corrupt
  backup can never be restored over anything.

It returns a *specific* list of problems (missing files, hash mismatches,
size mismatches, corrupt SQLite databases, malformed/absent manifest) —
never a bare boolean — so a human or the CLI can see exactly what is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from atlas.backup.manifest import BackupManifest, MANIFEST_FILENAME, sha256_file
from atlas.backup.sqlite_backup import integrity_check

# Problem kinds (stable strings so callers/tests can assert on them).
MANIFEST_MISSING = "MANIFEST_MISSING"
MANIFEST_MALFORMED = "MANIFEST_MALFORMED"
MISSING_FILE = "MISSING_FILE"
HASH_MISMATCH = "HASH_MISMATCH"
SIZE_MISMATCH = "SIZE_MISMATCH"
CORRUPT_DB = "CORRUPT_DB"


@dataclass
class VerifyProblem:
    kind: str
    path: str
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.kind}] {self.path}" + (f" - {self.detail}" if self.detail else "")


@dataclass
class VerifyResult:
    ok: bool
    backup_path: str
    backup_id: Optional[str] = None
    checked_files: int = 0
    problems: list[VerifyProblem] = field(default_factory=list)

    def render(self) -> str:
        header = f"Backup verify: {'PASS' if self.ok else 'FAIL'} ({self.backup_path})"
        if self.backup_id:
            header += f"\n  backup_id: {self.backup_id}"
        header += f"\n  files checked: {self.checked_files}"
        if self.problems:
            header += "\n  problems:"
            for p in self.problems:
                header += f"\n    - {p}"
        return header


def _load_manifest(backup_path: Path) -> tuple[Optional[BackupManifest], Optional[VerifyProblem]]:
    manifest_path = backup_path / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None, VerifyProblem(MANIFEST_MISSING, str(manifest_path), "manifest.json not found")
    try:
        manifest = BackupManifest.from_json(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse/shape error is malformed
        return None, VerifyProblem(MANIFEST_MALFORMED, str(manifest_path), str(exc))
    return manifest, None


def verify_backup(backup_path: Path) -> VerifyResult:
    """Verify every manifest-listed file of the backup at ``backup_path``.

    ``backup_path`` is the backup root directory (the one containing
    ``manifest.json``).
    """
    backup_path = Path(backup_path)
    result = VerifyResult(ok=True, backup_path=str(backup_path))

    if not backup_path.exists() or not backup_path.is_dir():
        result.ok = False
        result.problems.append(
            VerifyProblem(MANIFEST_MISSING, str(backup_path), "backup directory does not exist")
        )
        return result

    manifest, problem = _load_manifest(backup_path)
    if problem is not None:
        result.ok = False
        result.problems.append(problem)
        return result
    assert manifest is not None
    result.backup_id = manifest.backup_id

    for component in manifest.included_components:
        target = backup_path / component.relative_path
        result.checked_files += 1
        if not target.exists():
            result.ok = False
            result.problems.append(
                VerifyProblem(MISSING_FILE, component.relative_path, "listed in manifest but not present")
            )
            continue

        actual_size = target.stat().st_size
        if actual_size != component.size:
            result.ok = False
            result.problems.append(
                VerifyProblem(
                    SIZE_MISMATCH,
                    component.relative_path,
                    f"expected {component.size} bytes, found {actual_size}",
                )
            )

        actual_hash = sha256_file(target)
        if actual_hash != component.sha256:
            result.ok = False
            result.problems.append(
                VerifyProblem(
                    HASH_MISMATCH,
                    component.relative_path,
                    f"expected {component.sha256[:12]}..., found {actual_hash[:12]}...",
                )
            )
            # A hash mismatch on a DB already proves corruption; skip the
            # (now-meaningless) integrity check for that file.
            continue

        if component.kind == "sqlite":
            db_problems = integrity_check(target)
            if db_problems:
                result.ok = False
                result.problems.append(
                    VerifyProblem(
                        CORRUPT_DB,
                        component.relative_path,
                        "; ".join(db_problems[:5]),
                    )
                )

    return result


__all__ = [
    "VerifyProblem",
    "VerifyResult",
    "verify_backup",
    "MANIFEST_MISSING",
    "MANIFEST_MALFORMED",
    "MISSING_FILE",
    "HASH_MISMATCH",
    "SIZE_MISMATCH",
    "CORRUPT_DB",
]
