"""Atlas dynamic source registry (Phase 1A).

A lightweight, explicit, in-repo registry — deliberately NOT a plugin
framework and NOT based on Python entry points. Adapter classes register
themselves with the :func:`register_source` decorator (into the process
default registry), or into an isolated :class:`SourceRegistry` for tests.

Invariants:
    * one adapter class per :class:`SourceType` family (a second class for
      the same family is a duplicate-registration error);
    * ``create(instance)`` instantiates the right adapter for a configured
      :class:`SourceInstance`, filtering disabled instances;
    * capability queries operate on declared capabilities, so the governor
      never branches on source *names*;
    * ordering is deterministic (sorted by source-type value).

In Phase 1A the process default registry is intentionally EMPTY — no real
adapters exist yet (see section 37 of the build spec). The Fake/Fixture
test doubles are registered into ephemeral registries by tests.
"""

from __future__ import annotations

from typing import Iterable, Optional

from atlas.sources.adapter import SourceAdapter
from atlas.sources.models import Capability, SourceInstance, SourceType


class DuplicateRegistrationError(ValueError):
    """Raised when a second adapter class is registered for a source type."""


class UnknownSourceTypeError(KeyError):
    """Raised when no adapter is registered for a requested source type."""


class SourceRegistry:
    """A deterministic mapping of :class:`SourceType` → adapter class."""

    def __init__(self) -> None:
        self._by_type: dict[SourceType, type[SourceAdapter]] = {}

    def register(self, cls: type[SourceAdapter]) -> type[SourceAdapter]:
        if not (isinstance(cls, type) and issubclass(cls, SourceAdapter)):
            raise TypeError("register() requires a SourceAdapter subclass")
        source_type = cls.source_type
        if not isinstance(source_type, SourceType):
            raise TypeError(f"{cls.__name__}.source_type must be a SourceType")
        if not cls.CAPABILITIES:
            raise ValueError(f"{cls.__name__} must declare at least one Capability")
        if not cls.adapter_version or not cls.parser_version:
            raise ValueError(f"{cls.__name__} must set non-empty adapter_version/parser_version")
        existing = self._by_type.get(source_type)
        if existing is not None and existing is not cls:
            raise DuplicateRegistrationError(
                f"Source type {source_type.value} already registered to "
                f"{existing.__name__}; refusing to register {cls.__name__}."
            )
        self._by_type[source_type] = cls
        return cls

    def is_registered(self, source_type: SourceType) -> bool:
        return source_type in self._by_type

    def adapter_for(self, source_type: SourceType) -> type[SourceAdapter]:
        try:
            return self._by_type[source_type]
        except KeyError as exc:
            raise UnknownSourceTypeError(
                f"No adapter registered for source type {source_type.value}."
            ) from exc

    def registered_types(self) -> list[SourceType]:
        return sorted(self._by_type, key=lambda t: t.value)

    def create(self, instance: SourceInstance) -> SourceAdapter:
        cls = self.adapter_for(instance.source_type)
        return cls(instance)

    def create_all(
        self, instances: Iterable[SourceInstance], *, include_disabled: bool = False
    ) -> list[SourceAdapter]:
        adapters = [
            self.create(inst)
            for inst in instances
            if include_disabled or inst.enabled
        ]
        return sorted(adapters, key=lambda a: (a.source_type.value, a.instance_id))

    @staticmethod
    def with_capability(
        adapters: Iterable[SourceAdapter], capability: Capability
    ) -> list[SourceAdapter]:
        return [a for a in adapters if a.supports(capability)]

    def describe(self) -> list[dict]:
        """Deterministic, non-sensitive metadata for doctor/reporting."""
        out = []
        for source_type in self.registered_types():
            cls = self._by_type[source_type]
            out.append(
                {
                    "source_type": source_type.value,
                    "adapter_class": cls.__name__,
                    "adapter_version": cls.adapter_version,
                    "parser_version": cls.parser_version,
                    "capabilities": sorted(c.value for c in cls.CAPABILITIES),
                }
            )
        return out

    def clear(self) -> None:
        self._by_type.clear()


# Process-wide default registry (empty in Phase 1A).
_DEFAULT_REGISTRY = SourceRegistry()


def default_registry() -> SourceRegistry:
    return _DEFAULT_REGISTRY


def register_source(cls: Optional[type[SourceAdapter]] = None):
    """Class decorator registering an adapter into the default registry.

    Usage::

        @register_source
        class MyAdapter(SourceAdapter): ...
    """

    def _apply(target: type[SourceAdapter]) -> type[SourceAdapter]:
        return _DEFAULT_REGISTRY.register(target)

    if cls is not None:  # used as bare @register_source
        return _apply(cls)
    return _apply  # used as @register_source()


__all__ = [
    "DuplicateRegistrationError",
    "UnknownSourceTypeError",
    "SourceRegistry",
    "default_registry",
    "register_source",
]
