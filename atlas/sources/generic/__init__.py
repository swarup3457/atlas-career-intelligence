"""Generic official-career adapters package (Phase 1C-B).

Exposes the two GENERIC career-site adapters — one HTTP, one browser — and a
registry builder that combines them with the four official ATS adapters. There
is one adapter class per :class:`atlas.sources.models.SourceFamily`, so the
generic HTTP adapter (``company_career``) and the generic browser adapter
(``company_career_browser``) coexist with the ATS families in ONE registry that
the production runtime drives — no second orchestration path.

Importing this package performs NO network/browser I/O and does NOT touch the
process-wide default registry.
"""

from __future__ import annotations

from atlas.sources.ats import register_ats_adapters
from atlas.sources.generic.browser_adapter import GenericCareerBrowserAdapter
from atlas.sources.generic.http_adapter import GenericCareerHttpAdapter
from atlas.sources.models import SourceFamily
from atlas.sources.registry import SourceRegistry

GENERIC_CAREER_ADAPTER_CLASSES = (GenericCareerHttpAdapter, GenericCareerBrowserAdapter)

GENERIC_CAREER_FAMILIES = frozenset(cls.source_family for cls in GENERIC_CAREER_ADAPTER_CLASSES)


def register_generic_career_adapters(registry: SourceRegistry) -> SourceRegistry:
    """Register the generic HTTP + browser career adapters (idempotent)."""
    for cls in GENERIC_CAREER_ADAPTER_CLASSES:
        registry.register(cls)
    return registry


def build_careers_registry() -> SourceRegistry:
    """A fresh registry with the four ATS adapters PLUS the two generic career
    adapters — the complete set the official-career-coverage pipeline needs."""
    registry = SourceRegistry()
    register_ats_adapters(registry)
    register_generic_career_adapters(registry)
    return registry


def describe_generic_career_adapters() -> list[dict]:
    out = []
    for cls in sorted(GENERIC_CAREER_ADAPTER_CLASSES, key=lambda c: c.source_family.value):
        out.append(
            {
                "source_family": cls.source_family.value,
                "source_type": cls.source_type.value,
                "category": cls.category().value,
                "adapter_class": cls.__name__,
                "adapter_version": cls.adapter_version,
                "parser_version": cls.parser_version,
                "capabilities": sorted(c.value for c in cls.CAPABILITIES),
                "concurrency_class": cls.concurrency_class.value,
            }
        )
    return out


def adapter_class_for_family(family: SourceFamily):
    for cls in GENERIC_CAREER_ADAPTER_CLASSES:
        if cls.source_family == family:
            return cls
    raise KeyError(f"no generic career adapter for family {family!r}")


__all__ = [
    "GenericCareerHttpAdapter",
    "GenericCareerBrowserAdapter",
    "GENERIC_CAREER_ADAPTER_CLASSES",
    "GENERIC_CAREER_FAMILIES",
    "register_generic_career_adapters",
    "build_careers_registry",
    "describe_generic_career_adapters",
    "adapter_class_for_family",
]
