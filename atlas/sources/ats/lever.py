"""Lever official Postings API adapter (Phase 1C-A).

Read-only, GET-only against the public Postings API
(``https://api.lever.co/v0/postings/{site}`` with an EU residency variant). It
NEVER calls the application POST endpoint. See docs/sources/LEVER.md.

Contract facts (from github.com/lever/postings-api):

    * List: ``GET /v0/postings/{site}?mode=json&skip=X&limit=Y`` returns a
      TOP-LEVEL JSON ARRAY; ``mode=json`` forces JSON regardless of Accept. The
      documented ``skip``/``limit`` pagination is honored (this adapter declares
      PAGINATION).
    * ``workplaceType`` is a TOP-LEVEL field (not under ``categories``).
    * The README documents NO date field; live payloads commonly include
      ``createdAt`` as epoch-milliseconds. When present it is treated as the
      employer posted time (provenance ``EMPLOYER_POSTED_AT``); when absent the
      date provenance is ``UNKNOWN`` — never fabricated.
    * ``id`` is a UUID; ``hostedUrl``/``applyUrl`` embed it.
"""

from __future__ import annotations

import datetime
from typing import Any, Optional
from urllib.parse import quote

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, new_result_base
from atlas.sources.ats.base import (
    DateProvenance,
    HttpAtsAdapter,
    extract_lever_site,
    sanitize_description,
)
from atlas.sources.health import SourceHealth
from atlas.sources.models import (
    ActiveState,
    Capability,
    DetailRequest,
    DiscoveryResult,
    SearchRequest,
    SearchResult,
    SourceFamily,
    SourceInstance,
    SourceType,
    VerificationLevel,
    WorkMode,
    ZeroResultKind,
)
from atlas.sources.parsing import parse_isolated

_WORKPLACE = {
    "remote": WorkMode.REMOTE,
    "hybrid": WorkMode.HYBRID,
    "on-site": WorkMode.ONSITE,
    "onsite": WorkMode.ONSITE,
}


class LeverAdapter(HttpAtsAdapter):
    source_type = SourceType.ATS_LEVER
    source_family = SourceFamily.LEVER
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.DETAIL,
            Capability.PAGINATION,
            Capability.DESCRIPTION,
            Capability.POSTED_DATE,
            Capability.ACTIVE_STATUS,
            Capability.API_AVAILABLE,
        }
    )
    adapter_version = "lever-1.0.0"
    parser_version = "lever-parser-1.0.0"

    def __init__(self, instance: SourceInstance, *, http_client=None):
        super().__init__(instance, http_client=http_client)
        self.site, self.api_base = self._resolve_site(instance)
        if not self.site:
            raise AdapterError(ErrorCategory.CONFIG_ERROR, f"Lever instance {instance.instance_id!r} has no site")

    @staticmethod
    def _resolve_site(instance: SourceInstance) -> tuple[Optional[str], str]:
        md = instance.metadata or {}
        default_base = "https://api.eu.lever.co/v0/postings" if md.get("eu") else "https://api.lever.co/v0/postings"
        site = md.get("site") or instance.site or instance.company_id
        if instance.base_url:
            try:
                return extract_lever_site(str(instance.base_url))
            except ValueError:
                pass
        return (str(site) if site else None), default_base

    # -- endpoints ----------------------------------------------------------
    def _list_url(self, skip: int, limit: int) -> str:
        return f"{self.api_base}/{quote(self.site)}?mode=json&skip={skip}&limit={limit}"

    def _detail_url(self, posting_id: str) -> str:
        return f"{self.api_base}/{quote(self.site)}/{quote(posting_id)}?mode=json"

    # -- parsing ------------------------------------------------------------
    def _parse_posting(self, raw: Any, *, detail: bool = False) -> Optional[DiscoveryResult]:
        if not isinstance(raw, dict):
            raise ValueError("posting is not a JSON object")
        posting_id = raw.get("id")
        title = raw.get("text")
        if not posting_id:
            raise ValueError("posting missing 'id'")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("posting missing/invalid 'text' (title)")

        categories = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
        location = categories.get("location")
        department = categories.get("department")
        team = categories.get("team")
        commitment = categories.get("commitment")

        workplace_raw = str(raw.get("workplaceType", "")).lower()
        work_mode = _WORKPLACE.get(workplace_raw, WorkMode.UNKNOWN)

        posted_at = None
        date_prov = DateProvenance.UNKNOWN
        created = raw.get("createdAt")
        if isinstance(created, (int, float)) and created > 0:
            try:
                posted_at = datetime.datetime.fromtimestamp(created / 1000.0, tz=datetime.timezone.utc).isoformat()
                date_prov = DateProvenance.EMPLOYER_POSTED_AT
            except (OverflowError, OSError, ValueError):
                posted_at = None

        description = None
        if detail or raw.get("descriptionPlain") or raw.get("description"):
            description = sanitize_description(raw.get("descriptionPlain") or raw.get("description"))

        provenance = {
            "source_family": "lever",
            "site": self.site,
            "date_provenance": date_prov.value,
            "department": department,
            "team": team,
            "commitment": commitment,
        }
        return DiscoveryResult(
            **new_result_base(
                self,
                source_job_id=str(posting_id),
                source_url=raw.get("hostedUrl"),
                canonical_url=raw.get("hostedUrl"),
                company=self.instance.display_name or self.site,
                title=title.strip(),
                location=location,
                work_mode=work_mode,
                posted_at=posted_at,
                employment_type=commitment,
                description=description,
                is_active=ActiveState.ACTIVE,  # only published postings are exposed
                verification_level=(
                    VerificationLevel.OFFICIAL_DETAIL_LIVE if detail else VerificationLevel.OFFICIAL_SEARCH_LIVE
                ),
                confidence=0.95,
                provenance=provenance,
            )
        )

    # -- operations ---------------------------------------------------------
    def health_check(self) -> SourceHealth:
        return self._probe(self._list_url(0, 1))

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        limit = max(1, request.limit)
        skip = (max(1, request.page) - 1) * limit
        data = self._get_json(
            self._list_url(skip, limit), context=f"lever[{self.site}].search",
            not_found=ErrorCategory.SOURCE_UNAVAILABLE,
        )
        if not isinstance(data, list):
            return SearchResult(results=(), zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                                parse_findings=("lever: expected a top-level JSON array",))
        parsed = parse_isolated(data, lambda r: self._parse_posting(r, detail=False))
        results = tuple(parsed.results[:limit])
        has_more = len(data) >= limit and len(results) > 0
        if results:
            return SearchResult(
                results=results, page=request.page, has_more=has_more,
                next_cursor=str(skip + limit) if has_more else None,
                zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
                parse_findings=tuple(parsed.finding_reasons()),
            )
        if parsed.findings and not parsed.results and len(data) > 0:
            kind = ZeroResultKind.EXTRACTION_UNRESOLVED
        else:
            kind = ZeroResultKind.TRUSTED_ZERO
        return SearchResult(results=(), page=request.page, zero_result_kind=kind,
                            parse_findings=tuple(parsed.finding_reasons()))

    def fetch_detail(self, request: DetailRequest) -> DiscoveryResult:
        self._require(Capability.DETAIL)
        posting_id = request.source_job_id
        if posting_id is None and request.url:
            posting_id = request.url.rstrip("/").rsplit("/", 1)[-1]
        if not posting_id:
            raise AdapterError(ErrorCategory.CONFIG_ERROR, "lever.fetch_detail requires a posting id or url")
        data = self._get_json(
            self._detail_url(posting_id), context=f"lever[{self.site}].detail",
            not_found=ErrorCategory.INVALID_RESPONSE,
        )
        # The detail endpoint returns a single object (some deployments wrap it).
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            raise AdapterError(ErrorCategory.INVALID_RESPONSE, f"lever posting {posting_id} not found")
        result = self._parse_posting(data, detail=True)
        if result is None:
            raise AdapterError(ErrorCategory.INVALID_RESPONSE, f"lever posting {posting_id} could not be parsed")
        return result


__all__ = ["LeverAdapter"]
