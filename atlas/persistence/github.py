"""Atlas GitHub persistence — SCAFFOLD ONLY, no production writes.

GitHub's role in the target architecture (see docs/ARCHITECTURE.md) is:
    - durable history
    - backup
    - shared state / export
    - configuration / versioning

GitHub is explicitly NOT the high-frequency checkpoint database — that is
SQLite (atlas/persistence/sqlite.py + LangGraph's own SQLite
checkpointer). GitHub should receive periodic, batched, meaningful
snapshots/exports, never a commit per browser click or per micro-checkpoint.

This module defines the interface only. Every write method defaults to a
DRY-RUN mode that logs the intended action and returns without touching
any real GitHub repository, so importing/using this module today cannot
accidentally perform a destructive or production write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("atlas.persistence.github")


@dataclass
class GitHubExportResult:
    dry_run: bool
    would_write_path: Optional[str] = None
    would_commit_message: Optional[str] = None
    detail: str = ""


class GitHubPersistence:
    """Scaffolded interface for future durable GitHub export/backup.

    Concrete implementation (using an authenticated Git client or the
    GitHub API) is intentionally NOT built yet. Do not wire this class to
    perform real commits/pushes until the Workspace Atlas Agent
    specification defines what should actually be exported and how often.
    """

    def __init__(self, repository: Optional[str] = None, dry_run: bool = True):
        self.repository = repository
        self.dry_run = dry_run
        if not dry_run:
            raise NotImplementedError(
                "Real (non-dry-run) GitHub persistence is not implemented in "
                "the foundation build. This is intentional - see "
                "docs/STATE_MODEL.md and docs/ARCHITECTURE.md. Keep dry_run=True."
            )

    def export_run_snapshot(self, run_id: str, payload: dict[str, Any]) -> GitHubExportResult:
        """Would export a batched, human-meaningful snapshot of a run.

        In dry-run mode (the only supported mode today) this only logs
        the intent and returns a descriptive result - no filesystem or
        network write happens.
        """
        message = f"atlas: snapshot for run {run_id}"
        logger.info(
            "GitHubPersistence.export_run_snapshot (DRY RUN): would commit "
            "'%s' with %d top-level keys.",
            message,
            len(payload),
        )
        return GitHubExportResult(
            dry_run=True,
            would_write_path=f"exports/{run_id}.json",
            would_commit_message=message,
            detail="Dry-run only; no GitHub write performed (foundation build).",
        )

    def export_config_snapshot(self, payload: dict[str, Any]) -> GitHubExportResult:
        logger.info(
            "GitHubPersistence.export_config_snapshot (DRY RUN): would commit "
            "config snapshot with %d top-level keys.",
            len(payload),
        )
        return GitHubExportResult(
            dry_run=True,
            would_write_path="exports/config_snapshot.json",
            would_commit_message="atlas: config snapshot",
            detail="Dry-run only; no GitHub write performed (foundation build).",
        )
