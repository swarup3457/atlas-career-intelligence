"""Backup retention (Phase 0.95).

:func:`apply_retention` keeps the newest ``keep_latest_n`` backups in an
Atlas-managed backups directory and deletes the rest. It is intentionally
paranoid about *where* it deletes:

* it only ever considers immediate subdirectories of ``backups_dir`` that
  look like real backups (they contain a parseable ``manifest.json``);
* before deleting anything it re-checks that the candidate path is a
  *strict subpath* of ``backups_dir`` — a candidate that resolves outside
  (e.g. via a symlink) is refused with :class:`RetentionSafetyError`
  rather than silently deleted.

Ordering is by ``created_at`` (falling back to the sortable ``backup_id``),
so "newest" is unambiguous and deterministic.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from atlas.backup.manifest import BackupManifest, MANIFEST_FILENAME


class RetentionSafetyError(RuntimeError):
    """Raised when a retention candidate would delete outside backups_dir."""


@dataclass
class _Candidate:
    path: Path
    backup_id: str
    created_at: str

    @property
    def sort_key(self) -> tuple[str, str]:
        # created_at is the primary key; backup_id (sortable) breaks ties.
        return (self.created_at, self.backup_id)


def _is_strict_subpath(child: Path, parent: Path) -> bool:
    child = child.resolve()
    parent = parent.resolve()
    if child == parent:
        return False
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _discover(backups_dir: Path) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for entry in sorted(backups_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".staging-"):
            continue  # in-flight/aborted backup, not a published one
        manifest_path = entry / MANIFEST_FILENAME
        if not manifest_path.exists():
            continue
        try:
            manifest = BackupManifest.from_json(manifest_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - unparseable => not a valid backup
            continue
        candidates.append(
            _Candidate(path=entry, backup_id=manifest.backup_id, created_at=manifest.created_at)
        )
    return candidates


def apply_retention(backups_dir: Path, keep_latest_n: int) -> list[str]:
    """Delete all but the newest ``keep_latest_n`` valid backups.

    Returns the list of deleted ``backup_id`` values (newest kept). Raises
    :class:`ValueError` for a negative ``keep_latest_n`` and
    :class:`RetentionSafetyError` if a candidate resolves outside
    ``backups_dir``.
    """
    if keep_latest_n < 0:
        raise ValueError(f"keep_latest_n must be >= 0, got {keep_latest_n}")

    backups_dir = Path(backups_dir)
    if not backups_dir.exists():
        return []

    candidates = _discover(backups_dir)
    # Newest first.
    candidates.sort(key=lambda c: c.sort_key, reverse=True)
    doomed = candidates[keep_latest_n:]

    deleted: list[str] = []
    for candidate in doomed:
        if not _is_strict_subpath(candidate.path, backups_dir):
            raise RetentionSafetyError(
                f"Refusing to delete {candidate.path}: not a strict subpath of {backups_dir}."
            )
        shutil.rmtree(candidate.path)
        deleted.append(candidate.backup_id)
    return deleted


__all__ = ["apply_retention", "RetentionSafetyError"]
