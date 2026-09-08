"""Portal discovery adapter registry (Phase 1D §7/§8).

Registers the READ-ONLY LinkedIn and Naukri discovery adapters under their
distinct :class:`SourceFamily` keys (``linkedin`` / ``naukri``) so they coexist
in ONE :class:`SourceRegistry` with the ATS and generic-career families — never
a second orchestration path. Importing this module performs NO network/browser
I/O and does not touch the process-wide default registry.
"""

from __future__ import annotations

from typing import Any, Optional

from atlas.sources.models import (
    Capability,
    SourceFamily,
    SourceInstance,
    SourceType,
)
from atlas.sources.portals.linkedin import LinkedInGuestAdapter
from atlas.sources.portals.naukri import NaukriPublicAdapter
from atlas.sources.registry import SourceRegistry

PORTAL_ADAPTER_CLASSES = (LinkedInGuestAdapter, NaukriPublicAdapter)

PORTAL_FAMILIES = frozenset(cls.source_family for cls in PORTAL_ADAPTER_CLASSES)


def register_portal_adapters(registry: SourceRegistry) -> SourceRegistry:
    """Register the LinkedIn + Naukri read-only discovery adapters (idempotent)."""
    for cls in PORTAL_ADAPTER_CLASSES:
        if not registry.is_registered_family(cls.adapter_key()):
            registry.register(cls)
    return registry


def build_portals_registry() -> SourceRegistry:
    """A fresh registry with only the portal discovery adapters."""
    registry = SourceRegistry()
    register_portal_adapters(registry)
    return registry


def adapter_class_for_family(family: SourceFamily):
    for cls in PORTAL_ADAPTER_CLASSES:
        if cls.source_family == family:
            return cls
    raise KeyError(f"no portal adapter for family {family!r}")


def make_portal_instance(
    family: SourceFamily,
    instance_id: str,
    *,
    display_name: str = "",
    base_url: Optional[str] = None,
    auth_ref: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> SourceInstance:
    """Construct a portal :class:`SourceInstance`. The instance carries the
    explicit ``source_family`` so the adapter_key resolves to linkedin/naukri
    (never the generic PORTAL bucket). ``auth_ref`` is a NON-secret reference to
    a dedicated profile/env — never a credential value."""
    if family not in PORTAL_FAMILIES:
        raise KeyError(f"{family!r} is not a portal family")
    return SourceInstance(
        instance_id=instance_id,
        source_type=SourceType.PORTAL_LARGE,
        source_family=family,
        display_name=display_name or family.value,
        base_url=base_url,
        enabled=True,
        auth_ref=auth_ref,
        metadata=dict(metadata or {}),
    )


def describe_portal_adapters() -> list[dict]:
    out = []
    for cls in sorted(PORTAL_ADAPTER_CLASSES, key=lambda c: c.source_family.value):
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
                "read_only": True,
            }
        )
    return out


__all__ = [
    "LinkedInGuestAdapter",
    "NaukriPublicAdapter",
    "PORTAL_ADAPTER_CLASSES",
    "PORTAL_FAMILIES",
    "register_portal_adapters",
    "build_portals_registry",
    "adapter_class_for_family",
    "make_portal_instance",
    "describe_portal_adapters",
]
