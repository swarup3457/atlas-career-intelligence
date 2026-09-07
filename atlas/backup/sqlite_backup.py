"""Consistent SQLite backups via the online backup API (Phase 0.95).

Atlas SQLite databases run in WAL mode (see
:mod:`atlas.persistence.sqlite`). A naive ``shutil.copy`` of the ``.sqlite``
file can capture a *torn* state: recent committed pages may live only in
the sibling ``-wal`` file, so the copied main file alone is inconsistent
or stale. Copying the ``-wal``/``-shm`` files too is racy — a concurrent
writer can move the WAL underneath you.

The correct primitive is SQLite's own online backup API,
``sqlite3.Connection.backup()``. It walks a read transaction over the live
database and writes a *transactionally consistent* snapshot into a brand
new database file, safely coexisting with WAL and with concurrent writers.
The produced file is a self-contained, already-checkpointed database (no
``-wal`` companion required to be valid).

This module is the single chokepoint for "make a consistent copy of a
SQLite DB" so backup/restore never hand-roll file copies.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


class SqliteBackupError(RuntimeError):
    """Raised when a consistent SQLite snapshot cannot be produced."""


def backup_sqlite_database(source_db: Path, dest_db: Path) -> Path:
    """Write a transactionally consistent snapshot of ``source_db`` to ``dest_db``.

    Uses the online backup API, which is WAL-safe. ``dest_db`` (and its
    parent directory) is created fresh; any pre-existing destination file
    is overwritten with the new snapshot.

    Raises :class:`SqliteBackupError` if the source is missing or the
    snapshot cannot be completed.
    """
    source_db = Path(source_db)
    dest_db = Path(dest_db)
    if not source_db.exists():
        raise SqliteBackupError(f"Source SQLite database does not exist: {source_db}")

    dest_db.parent.mkdir(parents=True, exist_ok=True)
    # Start from a clean destination so a stale file can never survive.
    if dest_db.exists():
        dest_db.unlink()

    src: Optional[sqlite3.Connection] = None
    dst: Optional[sqlite3.Connection] = None
    try:
        # Open the source read-only via URI so a backup never accidentally
        # mutates the live database (e.g. by auto-creating it).
        src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(str(dest_db), timeout=30)
        src.backup(dst)  # atomic, consistent page-by-page snapshot
        dst.commit()
    except sqlite3.Error as exc:
        # Do not leave a half-written snapshot behind.
        try:
            if dst is not None:
                dst.close()
                dst = None
            if dest_db.exists():
                dest_db.unlink()
        except OSError:
            pass
        raise SqliteBackupError(
            f"Failed to back up SQLite database {source_db} -> {dest_db}: {exc}"
        ) from exc
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
    return dest_db


def integrity_check(db_path: Path) -> list[str]:
    """Open ``db_path`` and run ``PRAGMA integrity_check``.

    Returns a list of problem strings — empty means the database is
    structurally sound. A file that is not a valid SQLite database, or is
    unreadable, is reported as a single problem rather than raising, so
    callers (verify/restore) can aggregate diagnostics.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return [f"database file missing: {db_path}"]
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        results = [str(r[0]) for r in rows]
        if results == ["ok"]:
            return []
        return results
    except sqlite3.DatabaseError as exc:
        return [f"not a valid SQLite database: {exc}"]
    except sqlite3.Error as exc:  # pragma: no cover - unexpected driver error
        return [f"integrity check failed: {exc}"]
    finally:
        if conn is not None:
            conn.close()


__all__ = ["SqliteBackupError", "backup_sqlite_database", "integrity_check"]
