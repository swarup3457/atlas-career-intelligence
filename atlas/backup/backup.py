"""Create consistent, atomic, self-verifying backups (Phase 0.95).

:func:`create_backup` gathers Atlas's durable, machine-portable state into
a single timestamped backup directory:

* ``state/<db>.sqlite``      — consistent online snapshots (WAL-safe)
* ``config/config_export.json`` — sanitized (secret-redacted) config
* ``agents/…`` / ``skills/…`` — the agent & skill definition trees
* ``run_manifests/…``        — run manifest / schema-metadata JSONs

and NEVER captures machine-local secrets: the authenticated browser
profile(s), ``.venv``, caches, or the immutable source fixture.

Write protocol (mirrors :mod:`atlas.data_integrity.report_writer`):

1. **stage** — build everything under a private ``.staging-<id>`` dir.
2. **checksum + manifest** — hash every file, write ``manifest.json``.
3. **verify** — re-run :func:`atlas.backup.verify.verify_backup` against
   the staging dir; a checksum/integrity failure aborts before publish.
4. **atomic publish** — ``os.replace`` the staging dir onto its final
   name. Readers only ever see a fully-verified backup or nothing.

Consequently a process killed at *any* point before step 4 leaves only a
throwaway staging dir — :func:`verify_backup` at the final path finds
nothing valid, and any previously-published backup is untouched.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from atlas.backup.clock import Clock, resolve_clock
from atlas.backup.config_export import build_config_export
from atlas.backup.manifest import (
    BackupManifest,
    ExcludedComponent,
    IncludedComponent,
    MANIFEST_FILENAME,
    gather_software_versions,
    generate_backup_id,
    sha256_file,
)
from atlas.backup.sqlite_backup import backup_sqlite_database
from atlas.backup.timezone_utils import isoformat_utc
from atlas.backup.verify import verify_backup
from atlas.config import Settings
from atlas.persistence.sqlite import SCHEMA_VERSION
from atlas.runtime.manifest import compute_config_fingerprint

# Directory/file names never copied into a backup (regenerable / noise).
_SKIP_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".git", ".ipynb_checkpoints"})
_SKIP_FILE_SUFFIXES = (".pyc", ".pyo", ".lock")
_SKIP_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})


class BackupError(RuntimeError):
    """Raised when a backup cannot be produced *and verified* safely."""


@dataclass
class _Staged:
    included: list[IncludedComponent]
    excluded: list[ExcludedComponent]


def _should_skip(path: Path) -> bool:
    if path.name in _SKIP_FILE_NAMES:
        return True
    if any(path.name.endswith(suffix) for suffix in _SKIP_FILE_SUFFIXES):
        return True
    return False


def _copy_tree(src: Path, staging: Path, rel_root: str, kind: str) -> list[IncludedComponent]:
    """Copy ``src`` into ``staging/rel_root`` skipping caches; hash each file."""
    included: list[IncludedComponent] = []
    if not src.exists():
        return included
    for current in sorted(src.rglob("*")):
        # Skip any file living under a cache directory.
        if any(part in _SKIP_DIR_NAMES for part in current.relative_to(src).parts):
            continue
        if current.is_dir() or not current.is_file():
            continue
        if _should_skip(current):
            continue
        rel = f"{rel_root}/{current.relative_to(src).as_posix()}"
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, dest)
        included.append(
            IncludedComponent(
                relative_path=rel,
                sha256=sha256_file(dest),
                size=dest.stat().st_size,
                kind=kind,
            )
        )
    return included


def _snapshot_db(src_db: Path, staging: Path, rel_path: str) -> Optional[IncludedComponent]:
    dest = staging / rel_path
    backup_sqlite_database(src_db, dest)
    return IncludedComponent(
        relative_path=rel_path,
        sha256=sha256_file(dest),
        size=dest.stat().st_size,
        kind="sqlite",
    )


def _build_staging(settings: Settings, staging: Path) -> _Staged:
    included: list[IncludedComponent] = []
    excluded: list[ExcludedComponent] = []

    # --- durable SQLite databases (consistent snapshots) ---------------
    for db_path in (settings.state_db, settings.checkpoint_db):
        if db_path.exists():
            comp = _snapshot_db(db_path, staging, f"state/{db_path.name}")
            if comp is not None:
                included.append(comp)
        else:
            excluded.append(
                ExcludedComponent(f"state/{db_path.name}", "not present at backup time")
            )

    # --- sanitized config export --------------------------------------
    import json as _json

    config_dir = settings.project_root / "config"
    export = build_config_export(settings, config_dir)
    config_rel = "config/config_export.json"
    config_dest = staging / config_rel
    config_dest.parent.mkdir(parents=True, exist_ok=True)
    config_dest.write_text(_json.dumps(export, indent=2, sort_keys=True), encoding="utf-8")
    included.append(
        IncludedComponent(
            relative_path=config_rel,
            sha256=sha256_file(config_dest),
            size=config_dest.stat().st_size,
            kind="config",
        )
    )

    # --- agents / skills definition trees ------------------------------
    included += _copy_tree(settings.agents_dir, staging, "agents", "file")
    included += _copy_tree(settings.skills_dir, staging, "skills", "file")

    # --- run manifests / schema metadata found in output_dir ----------
    run_manifest_count = 0
    if settings.output_dir.exists():
        for jf in sorted(settings.output_dir.glob("run_manifest_*.json")):
            if not jf.is_file():
                continue
            rel = f"run_manifests/{jf.name}"
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(jf, dest)
            included.append(
                IncludedComponent(
                    relative_path=rel,
                    sha256=sha256_file(dest),
                    size=dest.stat().st_size,
                    kind="run_manifest",
                )
            )
            run_manifest_count += 1

    # --- explicit, documented exclusions (never captured) --------------
    excluded.extend(
        [
            ExcludedComponent(
                "browser_profile",
                "machine-local authenticated Chrome profile (cookies/session state); "
                "requires re-authentication after restore — see docs/DISASTER_RECOVERY.md",
            ),
            ExcludedComponent(
                ".browser-profile",
                "alternate machine-local authenticated Chrome profile; never backed up",
            ),
            ExcludedComponent(
                "secrets",
                "credentials/tokens/cookies are never captured; sensitively-named config "
                "keys are redacted to '***REDACTED***' in config_export.json",
            ),
            ExcludedComponent(
                ".venv",
                "Python virtual environment is reproducible from pyproject.toml + "
                "requirements-lock.txt; not backed up",
            ),
            ExcludedComponent(
                "caches",
                "__pycache__, .pytest_cache and *.pyc are regenerable and excluded",
            ),
            ExcludedComponent(
                "fixtures/real",
                "immutable, source-controlled input fixture — intentionally not duplicated "
                "into backups and never overwritten",
            ),
            ExcludedComponent(
                "logs",
                "raw logs may contain sensitive runtime detail; a sanitized tail is available "
                "via 'atlas support-bundle' instead",
            ),
        ]
    )

    included.sort(key=lambda c: c.relative_path)
    return _Staged(included=included, excluded=excluded)


def _checkpoint_schema_info(versions: dict[str, str]) -> str:
    try:
        import importlib.metadata as _md

        cp_version = _md.version("langgraph-checkpoint-sqlite")
    except Exception:  # noqa: BLE001
        cp_version = "unknown"
    return f"SqliteSaver(langgraph={versions.get('langgraph', 'unknown')}, langgraph-checkpoint-sqlite={cp_version})"


def _publish_atomically(staging_dir: Path, final_dir: Path, attempts: int = 10, delay: float = 0.05) -> None:
    """Rename ``staging_dir`` onto ``final_dir`` atomically.

    On Windows a directory rename can transiently fail with a sharing
    violation / access-denied (WinError 5/32/33) when antivirus or the
    search indexer is momentarily scanning a just-written file inside the
    staging dir. We retry a few times with a short backoff — the same
    "locked destination is transient" posture the Phase 0.9 report writer
    takes. Non-sharing errors (and any non-OSError) propagate immediately.
    """
    last: BaseException | None = None
    for i in range(attempts):
        try:
            os.replace(staging_dir, final_dir)
            return
        except PermissionError as exc:
            last = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33):
                raise
            last = exc
        time.sleep(delay)
    assert last is not None
    raise last


def create_backup(
    settings: Settings,
    output_dir: Path,
    clock: Optional[Clock] = None,
) -> BackupManifest:
    """Create and verify a backup under ``output_dir``.

    Returns the published :class:`BackupManifest`. The backup directory is
    ``output_dir / manifest.backup_id``. Raises :class:`BackupError` (or
    propagates an unexpected error after cleaning up staging) if a fully
    verified backup could not be published.
    """
    clock = resolve_clock(clock)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now = clock.utcnow()
    backup_id = generate_backup_id(now)
    final_dir = output_dir / backup_id
    staging_dir = output_dir / f".staging-{backup_id}"

    if final_dir.exists():
        raise BackupError(f"Backup id collision: {final_dir} already exists.")
    # Clear any leftover staging dir from a previous crashed attempt.
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)

    versions = gather_software_versions()

    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
        staged = _build_staging(settings, staging_dir)

        manifest = BackupManifest(
            backup_id=backup_id,
            created_at=isoformat_utc(now),
            atlas_version=versions.get("atlas", "unknown"),
            python_version=versions.get("python", "unknown"),
            langgraph_version=versions.get("langgraph", "unknown"),
            playwright_version=versions.get("playwright", "unknown"),
            state_schema_version=SCHEMA_VERSION,
            checkpoint_schema_info=_checkpoint_schema_info(versions),
            platform=versions.get("platform", "unknown"),
            config_fingerprint=compute_config_fingerprint(settings),
            included_components=staged.included,
            excluded_components=staged.excluded,
        )
        (staging_dir / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")

        # Re-verify the staging dir from disk before it is allowed to
        # become a real backup. A corrupt/mismatched staging dir aborts.
        verification = verify_backup(staging_dir)
        if not verification.ok:
            raise BackupError(
                "Refusing to publish backup: staging verification failed:\n"
                + verification.render()
            )

        # Atomic publish: staging -> final. Same directory (same volume),
        # so os.replace is atomic; a reader never sees a partial backup.
        # (Retries absorb transient Windows AV/indexer sharing violations.)
        _publish_atomically(staging_dir, final_dir)
    except BaseException:
        # Never touch the final path or any sibling backup; only clean up
        # our own throwaway staging directory.
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return manifest


def backup_dir_for(output_dir: Path, manifest: BackupManifest) -> Path:
    """Convenience: the published directory for ``manifest`` under ``output_dir``."""
    return Path(output_dir) / manifest.backup_id


__all__ = ["BackupError", "create_backup", "backup_dir_for"]
