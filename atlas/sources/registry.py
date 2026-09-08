"""Atlas dynamic source registry (Phase 1A, corrected Phase 1B).

A lightweight, explicit, in-repo registry — deliberately NOT a plugin
framework and NOT based on Python entry points. Adapter classes register
themselves with the :func:`register_source` decorator (into the process
default registry), or into an isolated :class:`SourceRegistry` for tests.

Phase 1B correction (build spec 7.1): registry identity is the
:class:`SourceFamily` (``adapter_key``), NOT the broad
:class:`SourceType`. This lets multiple adapters that share a broad
category coexist — e.g. LinkedIn and Naukri are both PORTAL but register
under distinct families ``linkedin`` and ``naukri``. Backward-compatible
``source_type`` lookups still work when a type resolves to exactly one
registered family.

In Phase 1A/1B the process default registry is intentionally EMPTY — no
real adapters exist yet. The Fake/Fixture test doubles are registered into
ephemeral registries by tests.
"""

from __future__ import annotations

from typing import Iterable, Optional

from atlas.sources.adapter import SourceAdapter
from atlas.sources.models import (
    Capability,
    SourceFamily,
    SourceInstance,
    SourceType,
)


class DuplicateRegistrationError(ValueError):
    """Raised when a second adapter class is registered for a family."""


class UnknownSourceTypeError(KeyError):
    """Raised when no adapter is registered for a requested type/family."""


class AmbiguousSourceTypeError(KeyError):
    """Raised when a broad ``SourceType`` maps to more than one registered
    family, so a family (adapter_key) must be used to disambiguate."""


class SourceRegistry:
    """A deterministic mapping of :class:`SourceFamily` → adapter class."""

    def __init__(self) -> None:
        self._by_family: dict[SourceFamily, type[SourceAdapter]] = {}

    def register(self, cls: type[SourceAdapter]) -> type[SourceAdapter]:
        if not (isinstance(cls, type) and issubclass(cls, SourceAdapter)):
            raise TypeError("register() requires a SourceAdapter subclass")
        if not isinstance(cls.source_type, SourceType):
            raise TypeError(f"{cls.__name__}.source_type must be a SourceType")
        family = cls.adapter_key()
        if not isinstance(family, SourceFamily):
            raise TypeError(f"{cls.__name__}.adapter_key() must be a SourceFamily")
        if not cls.CAPABILITIES:
            raise ValueError(f"{cls.__name__} must declare at least one Capability")
        if not cls.adapter_version or not cls.parser_version:
            raise ValueError(f"{cls.__name__} must set non-empty adapter_version/parser_version")
        existing = self._by_family.get(family)
        if existing is not None and existing is not cls:
            raise DuplicateRegistrationError(
                f"Source family {family.value} already registered to "
                f"{existing.__name__}; refusing to register {cls.__name__}."
            )
        self._by_family[family] = cls
        return cls

    # -- family-first API ---------------------------------------------------
    def is_registered_family(self, family: SourceFamily) -> bool:
        return family in self._by_family

    def adapter_for_family(self, family: SourceFamily) -> type[SourceAdapter]:
        try:
            return self._by_family[family]
        except KeyError as exc:
            raise UnknownSourceTypeError(
                f"No adapter registered for source family {family.value}."
            ) from exc

    def registered_families(self) -> list[SourceFamily]:
        return sorted(self._by_family, key=lambda f: f.value)

    # -- backward-compatible source_type API --------------------------------
    def _families_for_type(self, source_type: SourceType) -> list[SourceFamily]:
        return [f for f, cls in self._by_family.items() if cls.source_type == source_type]

    def is_registered(self, source_type: SourceType) -> bool:
        return bool(self._families_for_type(source_type))

    def adapter_for(self, source_type: SourceType) -> type[SourceAdapter]:
        families = self._families_for_type(source_type)
        if not families:
            raise UnknownSourceTypeError(
                f"No adapter registered for source type {source_type.value}."
            )
        if len(families) > 1:
            raise AmbiguousSourceTypeError(
                f"Source type {source_type.value} maps to multiple registered "
                f"families {sorted(f.value for f in families)}; use adapter_for_family()."
            )
        return self._by_family[families[0]]

    def registered_types(self) -> list[SourceType]:
        return sorted({cls.source_type for cls in self._by_family.values()}, key=lambda t: t.value)

    # -- construction -------------------------------------------------------
    def create(self, instance: SourceInstance) -> SourceAdapter:
        cls = self.adapter_for_family(instance.adapter_key)
        return cls(instance)

    def create_all(
        self, instances: Iterable[SourceInstance], *, include_disabled: bool = False
    ) -> list[SourceAdapter]:
        adapters = [
            self.create(inst)
            for inst in instances
            if include_disabled or inst.enabled
        ]
        return sorted(adapters, key=lambda a: (a.adapter_key().value, a.instance_id))

    @staticmethod
    def with_capability(
        adapters: Iterable[SourceAdapter], capability: Capability
    ) -> list[SourceAdapter]:
        return [a for a in adapters if a.supports(capability)]

    def describe(self) -> list[dict]:
        """Deterministic, non-sensitive metadata for doctor/reporting."""
        out = []
        for family in self.registered_families():
            cls = self._by_family[family]
            out.append(
                {
                    "source_family": family.value,
                    "source_type": cls.source_type.value,
                    "category": cls.category().value,
                    "adapter_class": cls.__name__,
                    "adapter_version": cls.adapter_version,
                    "parser_version": cls.parser_version,
                    "capabilities": sorted(c.value for c in cls.CAPABILITIES),
                }
            )
        return out

    def clear(self) -> None:
        self._by_family.clear()


# Process-wide default registry (empty in Phase 1A/1B).
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
    "AmbiguousSourceTypeError",
    "SourceRegistry",
    "default_registry",
    "register_source",
]
