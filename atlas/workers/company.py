"""Atlas company discovery worker (Phase 1A.5).

A real, deterministic BaseWorker that performs ONE company-discovery attempt:
normalize a company observation, resolve/register identity, fingerprint any
career-source candidate, and persist a SourceInstance relationship — via the
:class:`CompanyRegistry` and the domain-safe ``register_employer`` flow.

It does NOT search jobs, rank companies, decide candidate fit, or own
retries (the governor + centralized retry policy do that). Failures are
raised as classified ``WorkerError``; malformed observations resolve to
``EXTRACTION_UNRESOLVED`` (never silently "no results").
"""

from __future__ import annotations

from typing import Optional

from atlas.company.discovery import register_employer
from atlas.company.models import CompanyObservation
from atlas.company.registry import CompanyRegistry
from atlas.models import ErrorCategory, TaskStatus
from atlas.workers.base import BaseWorker, WorkerError, WorkerOutcome


class CompanyDiscoveryWorker(BaseWorker):
    """Processes one company-discovery task per attempt."""

    name = "company_discovery"

    def __init__(
        self,
        registry: CompanyRegistry,
        plan: dict[str, CompanyObservation],
        *,
        simulated_first_attempt_failures: Optional[set[str]] = None,
    ):
        self.registry = registry
        self.plan = dict(plan)
        self.simulated_first_attempt_failures = simulated_first_attempt_failures or set()
        # item id -> EmployerRegistrationResult, for inspection/telemetry.
        self.results: dict[str, object] = {}

    def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
        if item in self.simulated_first_attempt_failures and attempt_number == 1:
            raise WorkerError(
                ErrorCategory.TRANSIENT_NAVIGATION,
                f"Simulated deterministic transient failure for {item} (test-only).",
            )

        obs = self.plan.get(item)
        if obs is None:
            raise WorkerError(ErrorCategory.CONFIG_ERROR, f"No company observation planned for item {item!r}.")

        # A malformed observation (no usable company name) cannot be
        # identified — report truthfully, never as a clean success.
        if not (obs.name and obs.name.strip()):
            return WorkerOutcome(
                status=TaskStatus.EXTRACTION_UNRESOLVED,
                payload={"reason": "malformed company observation: empty name"},
            )

        result = register_employer(self.registry, obs)
        self.results[item] = result
        return WorkerOutcome(
            status=TaskStatus.SUCCESS,
            payload={
                "company_id": result.company.company_id,
                "canonical_name": result.company.canonical_name,
                "source_registered": result.source_registered,
                "rejected_untrusted": result.rejected_untrusted,
                "instance_id": result.source_instance.instance_id if result.source_instance else None,
                "source_type": result.relationship.source_type if result.relationship else None,
            },
        )


# Backward-compatible name (the Phase 0.5 placeholder was ``CompanyWorker``).
CompanyWorker = CompanyDiscoveryWorker


__all__ = ["CompanyDiscoveryWorker", "CompanyWorker"]

