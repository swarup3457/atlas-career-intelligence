"""Atlas Phase 0.95 — disaster recovery, backup & reproducibility.

This package provides Atlas's *local* backup / restore / verify /
retention / safe-reset / support-bundle machinery. Everything here is:

* **Deterministic and testable** — timestamps flow through an injectable
  :class:`~atlas.backup.clock.Clock` so backup identity is reproducible
  in tests without monkeypatching ``datetime`` globally.
* **Consistent** — SQLite databases are captured with the online
  ``sqlite3.Connection.backup()`` API (WAL-safe transactional snapshot),
  never a raw file copy that could catch a torn write.
* **Atomic** — a backup is assembled in a private staging directory,
  every checksum is re-verified from disk, and only then is the staging
  directory atomically published to its final name. A process killed
  mid-backup can never leave a directory that :func:`verify_backup`
  considers VALID, and never touches a previously-good backup.
* **Safe** — machine-local secrets (the authenticated browser profile,
  ``.venv``, caches) are NEVER backed up, and the developer-only
  :func:`safe_reset` helper hard-refuses to delete protected paths.

It deliberately does NOT implement any job-search business logic, ATS
rules, scheduling, or LLM behaviour — it is pure platform plumbing that
mirrors the code-quality style of :mod:`atlas.data_integrity`.
"""

from __future__ import annotations

from atlas.backup.backup import BackupError, create_backup
from atlas.backup.clock import Clock, FixedClock, StepClock, SystemClock
from atlas.backup.manifest import (
    BackupManifest,
    ExcludedComponent,
    IncludedComponent,
    MANIFEST_FILENAME,
    generate_backup_id,
)
from atlas.backup.reset import ResetResult, ResetSafetyError, safe_reset
from atlas.backup.restore import RestoreResult, restore_backup
from atlas.backup.retention import apply_retention
from atlas.backup.support_bundle import create_support_bundle
from atlas.backup.verify import VerifyProblem, VerifyResult, verify_backup

__all__ = [
    "BackupError",
    "create_backup",
    "Clock",
    "SystemClock",
    "FixedClock",
    "StepClock",
    "BackupManifest",
    "IncludedComponent",
    "ExcludedComponent",
    "MANIFEST_FILENAME",
    "generate_backup_id",
    "verify_backup",
    "VerifyResult",
    "VerifyProblem",
    "restore_backup",
    "RestoreResult",
    "apply_retention",
    "safe_reset",
    "ResetResult",
    "ResetSafetyError",
    "create_support_bundle",
]
