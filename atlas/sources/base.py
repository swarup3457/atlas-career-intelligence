"""Atlas source adapter base (Phase 0.5 legacy scaffold).

A "source" is anywhere Atlas can discover job postings from: an ATS
platform (Workday, Greenhouse, Lever, ...) or a portal (LinkedIn, Naukri,
Indeed, ...).

NOTE: This Phase 0.5 ``BaseSource`` is the original minimal, untyped
scaffold. The production contract is the *typed* Phase 1A
:class:`atlas.sources.adapter.SourceAdapter`, which uses typed
request/result objects and an explicit capability model. ``BaseSource``
is retained for backward compatibility; new adapters implement
``SourceAdapter``. ``SourceHealth`` is now the richer typed model from
:mod:`atlas.sources.health` (still constructible as
``SourceHealth(healthy=True)`` for compatibility).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from atlas.sources.health import SourceHealth, SourceHealthState  # noqa: F401  (re-export)


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
