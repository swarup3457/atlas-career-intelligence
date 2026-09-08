"""Atlas official career-site coverage (Phase 1C-B).

This package adds the production architecture that lets Atlas search the
OFFICIAL career site of an arbitrary company — even when that company does not
use one of the four structured ATS adapters (Greenhouse / Lever / Ashby /
Workday). It is deliberately GENERIC: there is no per-company Python adapter.

Logical components (see docs/OFFICIAL_CAREER_DISCOVERY.md):

    * :mod:`atlas.careers.extract`    — dependency-free extraction primitives
      (JSON-LD JobPosting, embedded application-state JSON with strict bounds,
      job-link classification, URL normalization, sitemap parsing, ordinary
      rel=next pagination). Read-only, script-free, link-in-description-safe.
    * :mod:`atlas.careers.trust`      — :class:`OfficialUrlTrustPolicy` (exact
      official domain + approved subdomains + known ATS host allowlist; fail
      closed on ambiguity; never trust a URL found in posting text).
    * :mod:`atlas.careers.profile`    — :class:`CareerSiteProfile` /
      :class:`ExtractionRecipe`: persisted DATA (route kind, selectors, link
      patterns, evidence, confidence, recipe/parser version, health) — never
      per-company code. Revalidated before reuse.
    * :mod:`atlas.careers.router`     — :class:`CareerSourceRouter`: routes a
      trusted entry point to an existing ATS adapter, the generic HTTP adapter,
      or the generic browser adapter — or classifies it truthfully.
    * :mod:`atlas.careers.discovery`  — :class:`CareerSourceDiscoveryService`:
      company + verified official domain -> trusted career entry points, using
      bounded deterministic evidence (nav links, approved subdomains, robots /
      sitemap references, canonical links, redirects to known ATS hosts).
    * :mod:`atlas.careers.planner`    — :class:`OfficialCareerCoveragePlanner`:
      sealed coverage tasks from due company + source instance + lane +
      geography group + DELTA/DEEP mode.
    * :mod:`atlas.careers.pilot`      — the controlled live-pilot config sealer
      and runner.

The generic adapters themselves live under :mod:`atlas.sources.generic` because
they implement the standard :class:`atlas.sources.adapter.SourceAdapter` contract
and flow through the EXISTING production pipeline (one LangGraph governor, the
shared fenced leases, the shared rate-limited executor, append-only observations,
canonicalization, durable verification decisions, the atomic Excel report).
"""

from __future__ import annotations

__all__: list[str] = []
