"""Atlas source adapter base (SCAFFOLD — abstract contract only).

A "source" is anywhere Atlas can discover job postings from: an ATS
platform (Workday, Greenhouse, Lever, ...) or a portal (LinkedIn, Naukri,
Indeed, ...). Phase 0.5 only formalizes the abstract capability contract;
NO concrete ATS/portal adapters are implemented — see
docs/AGENT_SKILL_MIGRATION.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceHealth:
    """Result of a lightweight, read-only reachability check for a source
    (e.g. "is workday.com reachable and not showing an access-limitation
    signal right now"). Never performs a search or extraction."""

    healthy: bool
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseSource(ABC):
    """Abstract base for future ATS/portal source adapters.

    Every concrete adapter (Workday, Greenhouse, Lever, LinkedIn, Naukri,
    etc.) will implement these four capabilities. None are implemented
    here or anywhere yet — this is the contract only, so future adapters
    are interchangeable from the worker/orchestration layer's point of
    view.
    """

    source_type: str = "base"
    name: str = "base"

    @abstractmethod
    def discover(self, company: str) -> list[dict[str, Any]]:
        """Discover what job-search entry points exist for `company` on
        this source (e.g. the company's Workday tenant URL). Returns a
        list of generic {"url": ..., "label": ...} dicts. NOT implemented
        for any real source in this build.
        """
        raise NotImplementedError

    @abstractmethod
    def search(self, company: str, keywords: str) -> list[dict[str, Any]]:
        """Search this source for `keywords` at `company`. Returns a list
        of generic {"title": ..., "url": ..., ...} job dicts. NOT
        implemented for any real source in this build.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_detail(self, job_url: str) -> dict[str, Any]:
        """Fetch full detail for one job posting URL. Returns a generic
        dict. NOT implemented for any real source in this build.
        """
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> "SourceHealth":
        """Lightweight, read-only reachability check for this source (no
        search, no extraction). NOT implemented for any real source in
        this build.
        """
        raise NotImplementedError
