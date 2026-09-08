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
    SourceCategory,
    SourceFamily,
    SourceInstance,
    SourceType,
    category_for_family,
    family_for_source_type,
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
    retry policy. Adapters never decide retry themselves. An optional
    ``retry_after`` (seconds) conveys an explicit ``Retry-After`` from a 429
    so the shared rate-limited executor can honor it centrally."""

    def __init__(self, category: ErrorCategory, message: str, *, retry_after: Optional[float] = None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.retry_after = retry_after


class SourceAdapter(ABC):
    """Abstract base for all Phase 1A production source adapters.

    Subclasses set the class attributes ``source_type``, ``CAPABILITIES``,
    ``adapter_version`` and ``parser_version``, and are constructed with a
    configured :class:`SourceInstance`.
    """

    source_type: SourceType = SourceType.FAKE
    source_family: Optional[SourceFamily] = None
    CAPABILITIES: frozenset[Capability] = frozenset()
    adapter_version: str = "0.0.0"
    parser_version: str = "0.0.0"
    concurrency_class: ConcurrencyClass = ConcurrencyClass.HTTP

    @classmethod
    def adapter_key(cls) -> SourceFamily:
        """Registry identity for this adapter class. The explicit
        ``source_family`` when set, otherwise the deterministic default for
        the ``source_type``. Two adapters that share a broad category but
        declare different families (e.g. LinkedIn vs Naukri) coexist."""
        return cls.source_family or family_for_source_type(cls.source_type)

    @classmethod
    def category(cls) -> SourceCategory:
        return category_for_family(cls.adapter_key())

    def __init__(self, instance: SourceInstance):
        if not isinstance(instance, SourceInstance):
            raise TypeError("SourceAdapter requires a SourceInstance")
        if instance.source_type != self.source_type:
            raise ValueError(
                f"Instance source_type {instance.source_type.value} does not match "
                f"adapter source_type {self.source_type.value}."
            )
        if instance.adapter_key != self.adapter_key():
            raise ValueError(
                f"Instance adapter_key {instance.adapter_key.value} does not match "
                f"adapter family {self.adapter_key().value}."
            )
        if not self.adapter_version or not self.parser_version:
            raise ValueError("adapter_version and parser_version must be non-empty.")
        self.instance = instance

    # -- Identity / metadata ------------------------------------------------
    @property
    def instance_id(self) -> str:
        return self.instance.instance_id

    def capabilities(self) -> frozenset[Capability]:
        """Effective capabilities = class-declared set, extended by per-instance
        additions, then reduced by per-instance removals. Removals let a tenant
        that does NOT expose a family-default capability drop it, so it can
        never falsely claim a capability it cannot honor (build spec 7.6)."""
        effective = frozenset(self.CAPABILITIES) | frozenset(self.instance.capability_overrides)
        return effective - frozenset(self.instance.capability_removals)

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
            "source_family": self.adapter_key().value,
            "category": self.category().value,
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
        """Discover search entry points (careers pages / ATS boards / tenants)
        for a company. This is a DISTINCT capability from SEARCH: a source may
        be able to enumerate entry points without running queries, or vice
        versa. Requires the explicit :attr:`Capability.DISCOVER`."""
        self._require(Capability.DISCOVER)
        raise CapabilityNotSupported(self.instance_id, Capability.DISCOVER)

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
