"""Official ATS adapters package (Phase 1C-A).

Registers the four real, read-only official ATS adapters — Greenhouse, Lever,
Ashby, and Workday — into an explicit registry. Importing this package performs
NO network I/O and does NOT register into the process-wide default registry
(that stays empty so Phase 1A/1B behavior is unchanged); adapters are opt-in via
:func:`build_ats_registry`. Instances are disabled unless configured (a canary
config or explicit SourceInstance), so ``atlas doctor`` remains fully offline.
"""

from __future__ import annotations

from atlas.sources.ats.ashby import AshbyAdapter
from atlas.sources.ats.greenhouse import GreenhouseAdapter
from atlas.sources.ats.lever import LeverAdapter
from atlas.sources.ats.workday import WorkdayAdapter
from atlas.sources.models import SourceFamily
from atlas.sources.registry import SourceRegistry

# One adapter class per official ATS family (build spec 10/11/12/13/14).
ATS_ADAPTER_CLASSES = (GreenhouseAdapter, LeverAdapter, AshbyAdapter, WorkdayAdapter)

ATS_FAMILIES = frozenset(cls.source_family for cls in ATS_ADAPTER_CLASSES)


def register_ats_adapters(registry: SourceRegistry) -> SourceRegistry:
    """Register the four official ATS adapters into ``registry`` (idempotent)."""
    for cls in ATS_ADAPTER_CLASSES:
        registry.register(cls)
    return registry


def build_ats_registry() -> SourceRegistry:
    """A fresh registry containing exactly the four official ATS adapters."""
    return register_ats_adapters(SourceRegistry())


def describe_ats_adapters() -> list[dict]:
    """Deterministic, non-sensitive descriptors (from class attributes only — no
    construction, no network) for doctor / ``atlas adapters list``."""
    out = []
    for cls in sorted(ATS_ADAPTER_CLASSES, key=lambda c: c.source_family.value):
        out.append(
            {
                "source_family": cls.source_family.value,
                "source_type": cls.source_type.value,
                "category": cls.category().value,
                "adapter_class": cls.__name__,
                "adapter_version": cls.adapter_version,
                "parser_version": cls.parser_version,
                "capabilities": sorted(c.value for c in cls.CAPABILITIES),
            }
        )
    return out


def adapter_class_for_family(family: SourceFamily):
    for cls in ATS_ADAPTER_CLASSES:
        if cls.source_family == family:
            return cls
    raise KeyError(f"no official ATS adapter for family {family!r}")


__all__ = [
    "AshbyAdapter",
    "GreenhouseAdapter",
    "LeverAdapter",
    "WorkdayAdapter",
    "ATS_ADAPTER_CLASSES",
    "ATS_FAMILIES",
    "register_ats_adapters",
    "build_ats_registry",
    "describe_ats_adapters",
    "adapter_class_for_family",
]
