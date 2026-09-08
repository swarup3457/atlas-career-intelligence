"""Atlas job-portal source adapters (Phase 1D).

READ-ONLY discovery adapters for LinkedIn and Naukri. They implement the
standard :class:`atlas.sources.adapter.SourceAdapter` contract and return
normalized results that the market layer turns into append-only PORTAL LEADS —
never official-verified jobs. No adapter here signs in, applies, creates an
account, exports a session, bypasses a challenge, or uses stealth/proxies; a
logged-out / challenge / access-limited response is CLASSIFIED, never bypassed.

See docs/PORTAL_DISCOVERY.md and docs/AGENT_SKILL_MIGRATION.md.
"""

from __future__ import annotations

from atlas.sources.portals.models import (
    OfficialJobObservation,
    PortalJobLead,
    PortalLeadVerification,
)
from atlas.sources.portals.registry import (
    PORTAL_ADAPTER_CLASSES,
    PORTAL_FAMILIES,
    build_portals_registry,
    describe_portal_adapters,
    make_portal_instance,
    register_portal_adapters,
)

__all__ = [
    "PortalJobLead",
    "OfficialJobObservation",
    "PortalLeadVerification",
    "PORTAL_ADAPTER_CLASSES",
    "PORTAL_FAMILIES",
    "build_portals_registry",
    "describe_portal_adapters",
    "make_portal_instance",
    "register_portal_adapters",
]

