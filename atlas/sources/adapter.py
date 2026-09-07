"""Atlas SourceAdapter V1 — the typed production discovery contract (Phase 1A).

Every real/fake/fixture adapter implements this. It supersedes the untyped
Phase 0.5 ``BaseSource`` (kept for backward compatibility). Key rules:

    * Operations use typed requests/results from :mod:`atlas.sources.models`
      — a raw ``list[dict]`` is never the production contract.
    * An adapter declares its :class:`Capability` set. Unsupported
      functionality raises :class:`CapabilityNotSupported` — an adapter
      MUST NOT silently return ``[]`` for something it cannot do.
    * An adapter performs exactly ONE attempt per call. It NEVER owns
      orchestration retries, sleeps, or budgets — the LangGraph governor
      and :mod:`atlas.orchestration.retry` remain the retry authority.
      Adapters raise :class:`AdapterError` with an
      :class:`atlas.models.ErrorCategory`; the worker bridge maps that to
      the centralized policy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from atlas.models import ErrorCategory
from atlas.sources.health import SourceHealth
from atlas.sources.models import (
    Capability,
    ConcurrencyClass,
    DetailRequest,
    DiscoverRequest,
    DiscoverResult,
    DiscoveryResult,
    SearchRequest,
    SearchResult,
    SourceInstance,
    SourceType,
)


class CapabilityNotSupported(Exception):
    """Raised when an operation is requested that the adapter does not
    declare support for. This is an explicit, typed condition — never a
    silent empty result."""

    def __init__(self, adapter: str, capability: Capability):
        super().__init__(f"Adapter {adapter!r} does not support {capability.value}.")
        self.adapter = adapter
        self.capability = capability


class AdapterError(Exception):
    """A classified single-attempt failure raised by an adapter. Carries an
    :class:`ErrorCategory` so the worker bridge can defer to the central
    retry policy. Adapters never decide retry themselves."""

    def __init__(self, category: ErrorCategory, message: str):
        super().__init__(message)
        self.category = category
        self.message = message


class SourceAdapter(ABC):
    """Abstract base for all Phase 1A production source adapters.

    Subclasses set the class attributes ``source_type``, ``CAPABILITIES``,
    ``adapter_version`` and ``parser_version``, and are constructed with a
    configured :class:`SourceInstance`.
    """

    source_type: SourceType = SourceType.FAKE
    CAPABILITIES: frozenset[Capability] = frozenset()
    adapter_version: str = "0.0.0"
    parser_version: str = "0.0.0"
    concurrency_class: ConcurrencyClass = ConcurrencyClass.HTTP

    def __init__(self, instance: SourceInstance):
        if not isinstance(instance, SourceInstance):
            raise TypeError("SourceAdapter requires a SourceInstance")
        if instance.source_type != self.source_type:
            raise ValueError(
                f"Instance source_type {instance.source_type.value} does not match "
                f"adapter source_type {self.source_type.value}."
            )
        if not self.adapter_version or not self.parser_version:
            raise ValueError("adapter_version and parser_version must be non-empty.")
        self.instance = instance

    # -- Identity / metadata ------------------------------------------------
    @property
    def instance_id(self) -> str:
        return self.instance.instance_id

    def capabilities(self) -> frozenset[Capability]:
        """Effective capabilities = class-declared set, extended by any
        per-instance overrides in configuration."""
        return frozenset(self.CAPABILITIES) | frozenset(self.instance.capability_overrides)

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities()

    def _require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise CapabilityNotSupported(self.instance_id, capability)

    def describe(self) -> dict:
        """Deterministic, non-sensitive descriptor for doctor/registry."""
        return {
            "instance_id": self.instance_id,
            "source_type": self.source_type.value,
            "adapter_class": type(self).__name__,
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "concurrency_class": self.concurrency_class.value,
            "capabilities": sorted(c.value for c in self.capabilities()),
            "enabled": self.instance.enabled,
        }

    # -- Operations (override only those the adapter supports) --------------
    @abstractmethod
    def health_check(self) -> SourceHealth:
        """Lightweight, read-only reachability/health probe. No search, no
        extraction, no bypass of any control."""
        raise NotImplementedError

    def discover(self, request: DiscoverRequest) -> DiscoverResult:  # noqa: ARG002
        self._require(Capability.SEARCH)  # discovery presupposes a searchable source
        raise CapabilityNotSupported(self.instance_id, Capability.SEARCH)

    def search(self, request: SearchRequest) -> SearchResult:  # noqa: ARG002
        raise CapabilityNotSupported(self.instance_id, Capability.SEARCH)

    def fetch_detail(self, request: DetailRequest) -> DiscoveryResult:  # noqa: ARG002
        raise CapabilityNotSupported(self.instance_id, Capability.DETAIL)


def new_result_base(adapter: SourceAdapter, **kwargs) -> dict:
    """Helper: seed the common provenance fields of a DiscoveryResult from
    an adapter so every result carries adapter/parser versions and its
    source identity without each adapter repeating the boilerplate."""
    base = {
        "source_type": adapter.source_type,
        "source_instance": adapter.instance_id,
        "adapter_version": adapter.adapter_version,
        "parser_version": adapter.parser_version,
    }
    base.update(kwargs)
    return base


__all__ = [
    "CapabilityNotSupported",
    "AdapterError",
    "SourceAdapter",
    "new_result_base",
]
