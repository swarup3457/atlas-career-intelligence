"""Deterministic FakeAdapter (Phase 1A test double).

Scripts every source scenario the framework must handle — results, trusted
zero, untrusted zero, 429, 5xx, timeout, parse failure, access-limited,
selector drift, and a transient-then-success sequence — with NO network.
The scenario is read from the instance metadata so ``registry.create(instance)``
constructs a fully-scripted adapter, and can also be passed explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, SourceAdapter, new_result_base
from atlas.sources.health import HealthEvidence, SourceHealth, SourceHealthState, classify_health
from atlas.sources.models import (
    ActiveState,
    Capability,
    DetailRequest,
    DiscoverRequest,
    DiscoverResult,
    DiscoveredEntryPoint,
    DiscoveryResult,
    SearchRequest,
    SearchResult,
    SourceInstance,
    SourceType,
    VerificationLevel,
    WorkMode,
    ZeroResultKind,
)

_ERROR_CATEGORY_BY_NAME = {c.value: c for c in ErrorCategory}


@dataclass
class FakeScenario:
    """Declarative script for a FakeAdapter instance."""

    kind: str = "results"          # results|zero|untrusted_zero|error|parse_failure|selector_drift
    result_count: int = 3
    error_category: Optional[ErrorCategory] = None
    retry_after: Optional[float] = None
    # For untrusted_zero / selector_drift: how the single sentinel probe behaves.
    sentinel: str = "healthy"      # healthy|drift|zero|error
    sentinel_error_category: Optional[ErrorCategory] = None
    # Transient sequence: fail the first N calls with `transient_category`, then succeed.
    fail_first_n: int = 0
    transient_category: ErrorCategory = ErrorCategory.TIMEOUT
    active: ActiveState = ActiveState.ACTIVE
    health_state: SourceHealthState = SourceHealthState.HEALTHY

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> "FakeScenario":
        def cat(name: Optional[str]) -> Optional[ErrorCategory]:
            return _ERROR_CATEGORY_BY_NAME.get(name) if name else None

        return cls(
            kind=str(metadata.get("scenario", "results")),
            result_count=int(metadata.get("result_count", 3)),
            error_category=cat(metadata.get("error_category")),
            retry_after=(float(metadata["retry_after"]) if metadata.get("retry_after") is not None else None),
            sentinel=str(metadata.get("sentinel", "healthy")),
            sentinel_error_category=cat(metadata.get("sentinel_error_category")),
            fail_first_n=int(metadata.get("fail_first_n", 0)),
            transient_category=cat(metadata.get("transient_category")) or ErrorCategory.TIMEOUT,
            active=ActiveState(metadata.get("active", ActiveState.ACTIVE.value)),
            health_state=SourceHealthState(metadata.get("health_state", SourceHealthState.HEALTHY.value)),
        )


class FakeAdapter(SourceAdapter):
    source_type = SourceType.FAKE
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.DETAIL,
            Capability.PAGINATION,
            Capability.RECENCY_FILTER,
            Capability.LOCATION_FILTER,
            Capability.KEYWORD_FILTER,
            Capability.ACTIVE_STATUS,
            Capability.POSTED_DATE,
            Capability.API_AVAILABLE,
        }
    )
    adapter_version = "fake-1.0.0"
    parser_version = "fake-parser-1.0.0"

    def __init__(self, instance: SourceInstance, scenario: Optional[FakeScenario] = None):
        super().__init__(instance)
        self.scenario = scenario or FakeScenario.from_metadata(dict(instance.metadata))
        self._search_calls = 0

    # -- helpers ------------------------------------------------------------
    def _make_result(self, i: int, *, hydrated: bool = False) -> DiscoveryResult:
        company = self.instance.metadata.get("company", f"FakeCo-{self.instance_id}")
        return DiscoveryResult(
            **new_result_base(
                self,
                source_job_id=f"{self.instance_id}-job-{i}",
                source_url=f"https://fake.example/{self.instance_id}/{i}",
                canonical_url=f"https://fake.example/{self.instance_id}/{i}",
                company=company,
                title=f"Fake Engineer {i}",
                location="Remote, India",
                work_mode=WorkMode.REMOTE,
                posted_at="2026-09-01",
                is_active=self.scenario.active,
                verification_level=VerificationLevel.PORTAL_LIVE,
                description=("Full description " + "x" * 20) if hydrated else None,
                confidence=0.9,
            )
        )

    def _is_sentinel(self, request: SearchRequest) -> bool:
        return bool(request.extra_filters.get("sentinel"))

    # -- operations ---------------------------------------------------------
    def health_check(self) -> SourceHealth:
        return SourceHealth(self.scenario.health_state, "FakeAdapter health probe.")

    def discover(self, request: DiscoverRequest) -> DiscoverResult:
        self._require(Capability.SEARCH)
        return DiscoverResult(
            entry_points=(
                DiscoveredEntryPoint(
                    url=f"https://fake.example/{self.instance_id}/careers",
                    label=f"{request.company} careers (fake)",
                    source_type=SourceType.FAKE,
                ),
            )
        )

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        self._search_calls += 1
        is_sentinel = self._is_sentinel(request)

        # Transient-then-success sequence (only for the primary query).
        if not is_sentinel and self._search_calls <= self.scenario.fail_first_n:
            raise AdapterError(self.scenario.transient_category, f"Simulated transient failure #{self._search_calls}.")

        if is_sentinel:
            return self._sentinel_result()

        kind = self.scenario.kind
        if kind == "error":
            category = self.scenario.error_category or ErrorCategory.HTTP_5XX
            raise AdapterError(category, f"Simulated {category.value} failure.", retry_after=self.scenario.retry_after)
        if kind == "parse_failure":
            return SearchResult(
                results=(),
                zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                parse_findings=("ValueError: malformed card 0", "KeyError: 'title'"),
            )
        if kind in ("zero",):
            return SearchResult(results=(), zero_result_kind=ZeroResultKind.TRUSTED_ZERO)
        if kind in ("untrusted_zero", "selector_drift"):
            return SearchResult(results=(), zero_result_kind=ZeroResultKind.UNTRUSTED_ZERO)

        # Default: results (respecting limit/pagination).
        n = min(self.scenario.result_count, request.limit)
        results = tuple(self._make_result(i) for i in range(n))
        has_more = self.scenario.result_count > request.page * request.limit
        return SearchResult(
            results=results,
            page=request.page,
            has_more=has_more,
            total_reported=self.scenario.result_count,
            zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
        )

    def _sentinel_result(self) -> SearchResult:
        mode = self.scenario.sentinel
        if mode == "error":
            category = self.scenario.sentinel_error_category or ErrorCategory.HTTP_429
            raise AdapterError(category, f"Sentinel simulated {category.value}.")
        if mode == "healthy":
            return SearchResult(
                results=(self._make_result(0),),
                total_reported=1,
                zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
            )
        if mode == "drift":
            return SearchResult(
                results=(),
                zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                parse_findings=("sentinel: expected result cards missing",),
            )
        # "zero": sentinel also returns clean zero → trusted zero.
        return SearchResult(results=(), zero_result_kind=ZeroResultKind.TRUSTED_ZERO)

    def fetch_detail(self, request: DetailRequest) -> DiscoveryResult:
        self._require(Capability.DETAIL)
        return self._make_result(0, hydrated=True)


def make_fake_instance(instance_id: str, **metadata: Any) -> SourceInstance:
    """Convenience: a FAKE SourceInstance carrying a scenario in metadata."""
    return SourceInstance(
        instance_id=instance_id,
        source_type=SourceType.FAKE,
        display_name=f"Fake {instance_id}",
        metadata=metadata,
    )


__all__ = ["FakeScenario", "FakeAdapter", "make_fake_instance"]
