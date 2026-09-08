"""Atlas source→governor worker bridge (Phase 1A).

Wraps a :class:`SourceAdapter` in the existing :class:`BaseWorker` contract
so the proven LangGraph governor drives source discovery WITHOUT any changes
to the governor itself. One ``attempt`` == one search attempt:

    * a classified adapter failure becomes a :class:`WorkerError` — the
      centralized retry policy decides retry/escalate (the worker never
      owns retries);
    * results → SUCCESS;
    * a trusted zero → NO_RELEVANT_RESULTS;
    * an untrusted zero → AT MOST ONE bounded sentinel probe, then a
      truthful terminal status (never an infinite loop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from atlas.models import ErrorCategory, TaskStatus
from atlas.sources.adapter import AdapterError, CapabilityNotSupported, SourceAdapter
from atlas.sources.health import SourceHealthState
from atlas.sources.models import SearchRequest, SearchResult, ZeroResultKind
from atlas.sources.query_signature import QuerySignature
from atlas.sources.zero_result import assess_search, run_sentinel_probe, should_run_sentinel
from atlas.workers.base import BaseWorker, WorkerError, WorkerOutcome

if False:  # TYPE_CHECKING-style hint without a runtime import cost
    from atlas.sources.executor import RateLimitedExecutor


@dataclass(frozen=True)
class SourceTask:
    """One planned search task, keyed by the governor's opaque item id."""

    item_id: str
    instance_id: str
    request: SearchRequest
    lane: Optional[str] = None
    company: Optional[str] = None
    sentinel_request: Optional[SearchRequest] = None
    query_signature: Optional[QuerySignature] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def signature_fingerprint(self) -> Optional[str]:
        return self.query_signature.fingerprint() if self.query_signature is not None else None


# Sentinel health state → terminal task status (all terminal; the sentinel
# never triggers another retry loop).
_SENTINEL_STATE_STATUS: dict[SourceHealthState, TaskStatus] = {
    SourceHealthState.HEALTHY: TaskStatus.NO_RELEVANT_RESULTS,
    SourceHealthState.SELECTOR_DRIFT_SUSPECTED: TaskStatus.EXTRACTION_UNRESOLVED,
    SourceHealthState.DEGRADED: TaskStatus.EXTRACTION_UNRESOLVED,
    SourceHealthState.UNKNOWN: TaskStatus.EXTRACTION_UNRESOLVED,
    SourceHealthState.RATE_LIMITED: TaskStatus.RATE_LIMITED,
    SourceHealthState.SOURCE_UNAVAILABLE: TaskStatus.SOURCE_UNAVAILABLE,
    SourceHealthState.ACCESS_LIMITED: TaskStatus.ACCESS_LIMITED,
    SourceHealthState.AUTH_REQUIRED: TaskStatus.ACCESS_LIMITED,
}


class SourceSearchWorker(BaseWorker):
    """A BaseWorker that executes source search tasks via adapters."""

    name = "source_search"

    def __init__(
        self,
        adapters: Mapping[str, SourceAdapter],
        plan: Mapping[str, SourceTask],
        *,
        history_provider: Optional[Callable[[str], Sequence[int]]] = None,
        executor: Optional["RateLimitedExecutor"] = None,
    ):
        self.adapters = dict(adapters)
        self.plan = dict(plan)
        self.history_provider = history_provider
        self.executor = executor
        # Diagnostic record of every sentinel probe run (for tests/telemetry).
        self.sentinel_log: list[dict] = []

    def _history(self, instance_id: str) -> Sequence[int]:
        if self.history_provider is None:
            return ()
        return self.history_provider(instance_id) or ()

    def _search(self, adapter: SourceAdapter, request: SearchRequest) -> SearchResult:
        """One adapter search, routed through the shared rate-limited executor
        when configured (build spec 10 / P0-13) so pacing/concurrency/Retry-
        After are applied centrally; otherwise a direct single call."""
        if self.executor is not None:
            return self.executor.run_search(adapter, request)
        return adapter.search(request)

    def attempt(self, item: str, attempt_number: int) -> WorkerOutcome:
        task = self.plan.get(item)
        if task is None:
            raise WorkerError(ErrorCategory.CONFIG_ERROR, f"No source task planned for item {item!r}.")
        adapter = self.adapters.get(task.instance_id)
        if adapter is None:
            raise WorkerError(ErrorCategory.CONFIG_ERROR, f"No adapter for instance {task.instance_id!r}.")

        try:
            result: SearchResult = self._search(adapter, task.request)
        except CapabilityNotSupported as exc:
            raise WorkerError(ErrorCategory.CONFIG_ERROR, str(exc)) from exc
        except AdapterError as exc:
            # Defer to the centralized retry policy via the governor.
            raise WorkerError(exc.category, exc.message) from exc

        base_payload = {
            "instance_id": task.instance_id,
            "lane": task.lane,
            "company": task.company,
            "result_count": result.count,
            "parse_findings": list(result.parse_findings),
        }

        if result.count > 0:
            base_payload["results"] = [r.to_dict() for r in result.results]
            base_payload["zero_result_kind"] = ZeroResultKind.NOT_APPLICABLE.value
            return WorkerOutcome(status=TaskStatus.SUCCESS, payload=base_payload)

        history = self._history(task.signature_fingerprint or task.instance_id)
        kind = assess_search(result, historical_yields=history)
        base_payload["zero_result_kind"] = kind.value

        if kind == ZeroResultKind.TRUSTED_ZERO:
            return WorkerOutcome(status=TaskStatus.NO_RELEVANT_RESULTS, payload=base_payload)

        if kind == ZeroResultKind.EXTRACTION_UNRESOLVED:
            return WorkerOutcome(status=TaskStatus.EXTRACTION_UNRESOLVED, payload=base_payload)

        # UNTRUSTED_ZERO → at most one bounded sentinel probe.
        if should_run_sentinel(kind):
            if task.sentinel_request is None:
                base_payload["sentinel"] = {"ran": False, "reason": "no sentinel request configured"}
                return WorkerOutcome(status=TaskStatus.EXTRACTION_UNRESOLVED, payload=base_payload)
            outcome = run_sentinel_probe(adapter, task.sentinel_request, historical_yields=history)
            record = {"item": item, "instance_id": task.instance_id, **outcome.to_dict()}
            self.sentinel_log.append(record)
            base_payload["sentinel"] = outcome.to_dict()
            status = _SENTINEL_STATE_STATUS.get(outcome.health.state, TaskStatus.EXTRACTION_UNRESOLVED)
            return WorkerOutcome(status=status, payload=base_payload)

        return WorkerOutcome(status=TaskStatus.EXTRACTION_UNRESOLVED, payload=base_payload)


__all__ = ["SourceTask", "SourceSearchWorker"]
