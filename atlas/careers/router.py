"""CareerSourceRouter — choose the execution route for a trusted entry point.

Given a TRUSTED official career entry point (already gated by
:class:`atlas.careers.trust.OfficialUrlTrustPolicy`) plus bounded page evidence,
the router decides:

    1. KNOWN ATS (Greenhouse/Lever/Ashby/Workday, …) -> reuse the EXISTING
       structured adapter (never scrape the ATS page generically);
    2. GENERIC_HTTP -> the page exposes structured data / server-rendered jobs
       the HTTP adapter can extract;
    3. GENERIC_BROWSER -> a JS/SPA shell or client-side search requires rendering;
    4. a truthful terminal classification (ACCESS_LIMITED / AUTH_REQUIRED /
       EXTRACTION_UNRESOLVED / UNSUPPORTED_SITE) — an unresolved route is NEVER
       reported as "no jobs".

The router builds (but does not run) the appropriate :class:`SourceInstance` and
records an append-only route classification. Fingerprinting uses the existing
:func:`atlas.sources.fingerprint.fingerprint_ats` (host/path/marker rules) — a
KNOWN ATS is routed to its adapter even when it is embedded on the company page.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from atlas.careers.extract import extract_scripts, looks_like_spa_shell
from atlas.careers.extract import (
    extract_embedded_jobs,
    extract_job_links,
    extract_jsonld_jobs,
)
from atlas.careers.profile import RouteKind
from atlas.careers.trust import OfficialUrlTrustPolicy, TrustKind
from atlas.company.tenant import extract_site, extract_tenant
from atlas.sources.fingerprint import fingerprint_ats
from atlas.sources.models import (
    Capability,
    SourceFamily,
    SourceInstance,
    SourceType,
    family_for_source_type,
)

_SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_MARKER_SNIPPET_BYTES = 40000


@dataclass(frozen=True)
class RouteDecision:
    route_kind: RouteKind
    entry_url: str
    source_instance: Optional[SourceInstance] = None
    fingerprint_family: Optional[SourceFamily] = None
    confidence: float = 0.0
    reason: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def routable(self) -> bool:
        return self.source_instance is not None and self.route_kind in (
            RouteKind.ATS, RouteKind.GENERIC_HTTP, RouteKind.GENERIC_BROWSER
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_kind": self.route_kind.value,
            "entry_url": self.entry_url,
            "source_instance": self.source_instance.to_dict() if self.source_instance else None,
            "fingerprint_family": self.fingerprint_family.value if self.fingerprint_family else None,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


def _markers_from_html(html: str) -> tuple[str, ...]:
    """Bounded ATS marker signals from a page: script ``src`` targets plus a
    bounded head snippet (so an embedded ATS widget is still fingerprinted)."""
    if not html:
        return ()
    srcs = _SCRIPT_SRC_RE.findall(html[: _MARKER_SNIPPET_BYTES * 2])
    snippet = html[:_MARKER_SNIPPET_BYTES]
    return tuple(srcs) + (snippet,)


def _instance_id(company_id: Optional[str], family: SourceFamily, url: str) -> str:
    base = company_id or "co"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    host = (urlsplit(url).netloc or "").lower().split(":")[0]
    return f"{base}::{family.value}::{host}::{digest}"


class CareerSourceRouter:
    """Classify a trusted entry point into an executable route."""

    def __init__(self, trust_policy: Optional[OfficialUrlTrustPolicy] = None):
        self.trust_policy = trust_policy

    # -- ATS instance construction -----------------------------------------
    def _build_ats_instance(
        self, company_id: Optional[str], source_type: SourceType, url: str
    ) -> SourceInstance:
        family = family_for_source_type(source_type)
        tenant = extract_tenant(source_type, url)
        site = extract_site(source_type, url)
        board_token = None
        try:
            if source_type == SourceType.ATS_GREENHOUSE:
                from atlas.sources.ats.base import extract_greenhouse_board_token

                board_token = extract_greenhouse_board_token(url)
            elif source_type == SourceType.ATS_LEVER:
                from atlas.sources.ats.base import extract_lever_site

                board_token, _ = extract_lever_site(url)
            elif source_type == SourceType.ATS_ASHBY:
                from atlas.sources.ats.base import extract_ashby_board_name

                board_token = extract_ashby_board_name(url)
        except ValueError:
            board_token = None
        metadata: dict[str, Any] = {"entry_url": url}
        if board_token:
            metadata["board_token"] = board_token
        return SourceInstance(
            instance_id=_instance_id(company_id, family, url),
            source_type=source_type,
            source_family=family,
            display_name=f"{source_type.value} ({tenant or board_token or 'careers'})",
            base_url=url,
            tenant=tenant or board_token,
            site=site,
            company_id=company_id,
            enabled=True,
            metadata=metadata,
        )

    def _build_generic_instance(
        self, company_id: Optional[str], url: str, *, browser: bool, recipe: Optional[dict] = None
    ) -> SourceInstance:
        family = SourceFamily.COMPANY_CAREER_BROWSER if browser else SourceFamily.COMPANY_CAREER
        caps = (
            frozenset({Capability.SEARCH, Capability.BROWSER_REQUIRED, Capability.KEYWORD_FILTER})
            if browser
            else frozenset({Capability.SEARCH, Capability.DETAIL, Capability.PAGINATION})
        )
        metadata: dict[str, Any] = {"entry_url": url}
        if recipe:
            metadata["recipe"] = recipe
        return SourceInstance(
            instance_id=_instance_id(company_id, family, url),
            source_type=SourceType.COMPANY_CAREER,
            source_family=family,
            display_name=f"Official careers ({urlsplit(url).netloc})",
            base_url=url,
            company_id=company_id,
            enabled=True,
            capability_overrides=caps,
            metadata=metadata,
        )

    # -- routing ------------------------------------------------------------
    def route(
        self,
        entry_url: str,
        *,
        company_id: Optional[str] = None,
        html: Optional[str] = None,
        status: int = 200,
        final_url: Optional[str] = None,
        challenge: bool = False,
        login_wall: bool = False,
    ) -> RouteDecision:
        """Classify one trusted entry point. ``html`` is the bounded body already
        fetched by a read-only client (the router itself performs no I/O). When
        no evidence is available (``html`` is None) the router still fingerprints
        the URL, so a KNOWN ATS host routes immediately."""
        target_url = final_url or entry_url

        # 0. §5.2 KNOWN-ATS FAST ROUTE (URL fingerprint, marker-independent).
        #    A known ATS host/path is routed to its structured adapter BEFORE any
        #    landing-page access condition is considered — a blocked, aged, or
        #    JS-only HTML landing page NEVER disables the usable structured API
        #    path. If the structured API itself is blocked, the ADAPTER classifies
        #    that real API failure at execution time (not here).
        url_fp = fingerprint_ats(target_url)
        if url_fp.matched and url_fp.source_type is not None and url_fp.source_type in _ATS_ROUTABLE:
            instance = self._build_ats_instance(company_id, url_fp.source_type, target_url)
            return RouteDecision(
                RouteKind.ATS, entry_url, source_instance=instance,
                fingerprint_family=instance.adapter_key, confidence=url_fp.confidence,
                reason=f"known ATS host: {url_fp.matched_on}",
                evidence={
                    "fingerprint": url_fp.to_dict(), "final_url": target_url,
                    "landing_status": status,
                    "landing_blocked": bool(challenge or login_wall or status in (401, 403, 429)),
                },
            )

        # 1. Access / auth conditions dominate for GENERIC routes — classified,
        #    never bypassed.
        if login_wall or status == 401:
            return RouteDecision(RouteKind.AUTH_REQUIRED, entry_url, reason="login wall (no bypass)",
                                 evidence={"status": status})
        if challenge or status in (403, 429):
            return RouteDecision(RouteKind.ACCESS_LIMITED, entry_url,
                                 reason=f"anti-bot/access-limited (status={status})",
                                 evidence={"status": status})

        # 2. Known ATS fingerprint from EMBEDDED markers (a widget on the page).
        markers = _markers_from_html(html or "")
        fp = fingerprint_ats(target_url, markers=markers)
        if fp.matched and fp.source_type is not None and fp.source_type in _ATS_ROUTABLE:
            instance = self._build_ats_instance(company_id, fp.source_type, target_url)
            return RouteDecision(
                RouteKind.ATS, entry_url, source_instance=instance,
                fingerprint_family=instance.adapter_key, confidence=fp.confidence,
                reason=f"known ATS: {fp.matched_on}",
                evidence={"fingerprint": fp.to_dict(), "final_url": target_url},
            )

        # 3. Structured / server-rendered content -> GENERIC_HTTP.
        if html:
            jsonld = extract_jsonld_jobs(html, base_url=target_url)
            embedded = extract_embedded_jobs(html, base_url=target_url) if not jsonld else []
            anchors = extract_job_links(html, base_url=target_url) if not (jsonld or embedded) else []
            method = "jsonld" if jsonld else ("embedded_json" if embedded else ("anchor" if anchors else "none"))
            count = len(jsonld) + len(embedded) + len(anchors)
            if count > 0:
                recipe = {"extraction_method": method}
                instance = self._build_generic_instance(company_id, target_url, browser=False, recipe=recipe)
                return RouteDecision(
                    RouteKind.GENERIC_HTTP, entry_url, source_instance=instance,
                    confidence=0.85 if method in ("jsonld", "embedded_json") else 0.65,
                    reason=f"structured HTTP extraction ({method}, {count} job(s))",
                    evidence={"method": method, "count": count, "final_url": target_url},
                )
            # 4. A JS/SPA shell with no server-rendered jobs -> GENERIC_BROWSER.
            if looks_like_spa_shell(html, extracted_jobs=0):
                instance = self._build_generic_instance(company_id, target_url, browser=True)
                return RouteDecision(
                    RouteKind.GENERIC_BROWSER, entry_url, source_instance=instance,
                    confidence=0.6, reason="JS/SPA shell requires rendering",
                    evidence={"spa_shell": True, "final_url": target_url},
                )
            # Reachable, real content, but no jobs and not a shell: reachable but
            # unresolved (never 'no jobs'); the browser route may still help.
            instance = self._build_generic_instance(company_id, target_url, browser=True)
            return RouteDecision(
                RouteKind.GENERIC_BROWSER, entry_url, source_instance=instance,
                confidence=0.4, reason="no server-rendered jobs; attempt rendered route",
                evidence={"final_url": target_url, "note": "no structured jobs in HTTP body"},
            )

        # 5. No evidence at all and no ATS host match: default to the browser
        #    route (rendering may reveal client-side jobs) with low confidence.
        instance = self._build_generic_instance(company_id, target_url, browser=True)
        return RouteDecision(
            RouteKind.GENERIC_BROWSER, entry_url, source_instance=instance,
            confidence=0.3, reason="no page evidence; attempt rendered route",
            evidence={"final_url": target_url},
        )

    # -- persistence --------------------------------------------------------
    def persist(self, store, decision: RouteDecision, *, company_id: Optional[str] = None) -> None:
        """Persist the SourceInstance (if routable) and an append-only route
        classification for one entry point."""
        cid = company_id or (decision.source_instance.company_id if decision.source_instance else None)
        if decision.source_instance is not None:
            from atlas.sources.instance_store import save_instance

            save_instance(store, decision.source_instance)
        classification_id = "route::" + hashlib.sha1(
            f"{cid}::{decision.entry_url}::{decision.route_kind.value}".encode("utf-8")
        ).hexdigest()[:16]
        store.record_route_classification(
            classification_id,
            decision.entry_url,
            decision.route_kind.value,
            company_id=cid,
            source_instance_id=decision.source_instance.instance_id if decision.source_instance else None,
            fingerprint_family=decision.fingerprint_family.value if decision.fingerprint_family else None,
            confidence=decision.confidence,
            evidence=decision.evidence,
        )


# ATS families the router will route to an existing structured adapter. Only the
# four adapters that actually exist are routed; other fingerprinted families are
# treated as generic (until an adapter is justified by measured usage).
_ATS_ROUTABLE: frozenset[SourceType] = frozenset(
    {
        SourceType.ATS_GREENHOUSE,
        SourceType.ATS_LEVER,
        SourceType.ATS_ASHBY,
        SourceType.ATS_WORKDAY,
    }
)


__all__ = ["RouteDecision", "CareerSourceRouter"]
