"""Atlas verification worker (SCAFFOLD).

Will verify extracted job/company data against the future Workspace
Atlas Agent's verification rules. Placeholder only for the foundation
build.
"""

from __future__ import annotations

from atlas.workers.base import BaseWorker, WorkerOutcome
from atlas.models import TaskStatus


class VerificationWorker(BaseWorker):
    """Placeholder verification worker. Not yet wired to real rules."""

    name = "verification"

    def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
        return WorkerOutcome(
            status=TaskStatus.SKIPPED,
            payload={"reason": "VerificationWorker is a foundation-build placeholder."},
        )
