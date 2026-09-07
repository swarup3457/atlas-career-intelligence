"""Atlas company-level worker (SCAFFOLD).

Will orchestrate multi-step company processing (career page + ATS
lookups + verification) once the Workspace Atlas Agent specification
defines the real business steps. Today it is a thin placeholder proving
the BaseWorker contract composes.
"""

from __future__ import annotations

from atlas.workers.base import BaseWorker, WorkerOutcome
from atlas.models import TaskStatus


class CompanyWorker(BaseWorker):
    """Placeholder company-level worker.

    NOT yet wired to real multi-step business logic (career page + ATS +
    verification pipeline). See docs/AGENT_SKILL_MIGRATION.md for the
    planned import path.
    """

    name = "company"

    def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
        return WorkerOutcome(
            status=TaskStatus.SKIPPED,
            payload={"reason": "CompanyWorker is a foundation-build placeholder."},
        )
