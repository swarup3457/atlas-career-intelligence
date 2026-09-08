"""Workday public careers (CXS) adapter (Phase 1C-A) — highest risk.

Workday has NO officially documented public job-board API. Each employer hosts
its own careers site on a Workday tenant; the public careers page itself calls
an internal "CXS" JSON endpoint. Atlas treats this as OBSERVED public web
behavior, NOT a stability promise, and uses ONLY the same public interface the
employer's own careers page uses. See docs/sources/WORKDAY.md.

Safety (non-negotiable): this adapter issues the SAME public CXS *search* the
careers page issues (a facet/keyword query carrying NO candidate data) plus a
public detail GET. It NEVER logs in, creates an account, sends candidate data,
or bypasses any Cloudflare/Zscaler/anti-bot/login challenge — a challenge is
DETECTED and classified ACCESS_LIMITED/AUTH_REQUIRED, then it stops.

Contract facts (observed public behavior):

    * URL: ``https://{tenant}.{dc}.myworkdayjobs.com/{locale}/{site}`` — the
      datacenter shard (``wd1``/``wd3``/``wd5``/…) is part of the hostname and
      CANNOT be guessed; it must come from the real careers URL.
    * Search: POST ``.../wday/cxs/{tenant}/{site}/jobs`` with
      ``{"appliedFacets":{}, "limit":N, "offset":M, "searchText":"..."}`` →
      ``{total, jobPostings:[{title, externalPath, locationsText, postedOn,
      bulletFields, jobReqId}]}`` (page size capped low, ~20).
    * Detail: GET ``.../wday/cxs/{tenant}/{site}{externalPath}`` →
      ``{jobPostingInfo:{title, jobDescription, location, startDate, jobReqId,
      externalUrl, postedOn, ...}}``.
    * ``postedOn`` is RELATIVE text ("Posted Today", "Posted 30+ Days Ago"), so
      it is recorded as ``RELATIVE_POSTED_TEXT`` provenance with the raw string
      — an absolute posted date is NEVER fabricated from it.
    * Schema variation/drift degrades to EXTRACTION_UNRESOLVED, never a false
      zero.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, new_result_base
from atlas.sources.ats.base import (
    DateProvenance,
    HttpAtsAdapter,
    WorkdayIdentity,
    parse_workday_url,
    sanitize_description,
    work_mode_from_text,
)
from atlas.sources.health import SourceHealth, SourceHealthState
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
    ZeroResultKind,
)
from atlas.sources.parsing import parse_isolated

# Workday caps the CXS page size low; keep requests bounded and honest.
_MAX_PAGE = 20


class WorkdayAdapter(HttpAtsAdapter):
    source_type = SourceType.ATS_WORKDAY
    source_family = SourceFamily.WORKDAY
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.DETAIL,
            Capability.PAGINATION,
            Capability.DESCRIPTION,
            Capability.ACTIVE_STATUS,
            Capability.API_AVAILABLE,
        }
    )
    adapter_version = "workday-1.0.0"
    parser_version = "workday-parser-1.0.0"

    def __init__(self, instance: SourceInstance, *, http_client=None):
        super().__init__(instance, http_client=http_client)
        self.identity = self._resolve_identity(instance)

    @staticmethod
    def _resolve_identity(instance: SourceInstance) -> WorkdayIdentity:
        md = instance.metadata or {}
        if instance.base_url:
            try:
                return parse_workday_url(str(instance.base_url))
            except ValueError:
                pass
        # Explicit fields (datacenter cannot be guessed — must be supplied).
        tenant = md.get("tenant") or instance.tenant
        dc = md.get("datacenter") or md.get("dc")
        site = md.get("site") or instance.site
        locale = md.get("locale", "en-US")
        if not (tenant and dc and site):
            raise AdapterError(
                ErrorCategory.CONFIG_ERROR,
                f"Workday instance {instance.instance_id!r} needs a careers base_url or tenant+datacenter+site "
                "(the datacenter shard cannot be guessed)",
            )
        host = f"{tenant}.{dc}.myworkdayjobs.com"
        return WorkdayIdentity(tenant=str(tenant), datacenter=str(dc), site=str(site), locale=str(locale), host=host)

    # -- request bodies -----------------------------------------------------
    def _search_body(self, *, offset: int, limit: int, search_text: str) -> bytes:
        # The SAME shape the public careers page posts — empty facets, bounded
        # page, and a plain keyword (never candidate data).
        return json.dumps(
            {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": search_text},
            sort_keys=True,
        ).encode("utf-8")

    # -- parsing ------------------------------------------------------------
    def _parse_posting(self, raw: Any, *, detail: bool = False) -> Optional[DiscoveryResult]:
        if not isinstance(raw, dict):
            raise ValueError("posting is not a JSON object")
        info = raw.get("jobPostingInfo") if detail and isinstance(raw.get("jobPostingInfo"), dict) else raw
        title = info.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("posting missing/invalid 'title'")
        external_path = info.get("externalPath") or raw.get("externalPath") or ""
        req_id = info.get("jobReqId") or raw.get("jobReqId")
        if not req_id and isinstance(raw.get("bulletFields"), list) and raw["bulletFields"]:
            # Tenant schema variation: bulletFields is ["R-123"] or [{label,value}].
            first = raw["bulletFields"][0]
            if isinstance(first, str):
                req_id = first
            elif isinstance(first, dict):
                req_id = first.get("value") or first.get("label")
        location = info.get("locationsText") or info.get("location")

        posted_raw = info.get("postedOn") or info.get("startDate")
        provenance = {
            "source_family": "workday",
            "tenant": self.identity.tenant,
            "datacenter": self.identity.datacenter,
            "site": self.identity.site,
            # postedOn is a RELATIVE string; never fabricate an absolute date.
            "date_provenance": (DateProvenance.RELATIVE_POSTED_TEXT if posted_raw else DateProvenance.UNKNOWN).value,
            "posted_raw": posted_raw,
            "requisition_id": req_id,
        }
        source_id = str(req_id) if req_id else (external_path or title)
        canonical = self.identity.public_job_url(external_path) if external_path else info.get("externalUrl")
        description = None
        if detail:
            description = sanitize_description(info.get("jobDescription"))
        return DiscoveryResult(
            **new_result_base(
                self,
                source_job_id=source_id,
                source_url=canonical,
                canonical_url=canonical,
                company=self.instance.display_name or self.identity.tenant,
                title=title.strip(),
                location=location if isinstance(location, str) else None,
                work_mode=work_mode_from_text(location if isinstance(location, str) else None),
                # posted_at intentionally None: only a relative text is known.
                description=description,
                is_active=ActiveState.ACTIVE,
                verification_level=(
                    VerificationLevel.OFFICIAL_DETAIL_LIVE if detail else VerificationLevel.OFFICIAL_SEARCH_LIVE
                ),
                confidence=0.9,
                provenance=provenance,
            )
        )

    # -- operations ---------------------------------------------------------
    def health_check(self) -> SourceHealth:
        body = self._search_body(offset=0, limit=1, search_text="")
        return self._probe(
            self.identity.cxs_search_url(), method="POST", body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"https://{self.identity.host}",
                "Referer": f"https://{self.identity.host}/{self.identity.locale}/{self.identity.site}",
            },
            expected_keys=("jobPostings", "total"),
        )

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        limit = min(max(1, request.limit), _MAX_PAGE)
        offset = (max(1, request.page) - 1) * limit
        # Conservative canary behavior: list published postings (empty
        # searchText) so the public interface is exercised the same way the
        # careers page does; downstream matching handles lane relevance.
        body = self._search_body(offset=offset, limit=limit, search_text="")
        data = self._get_json(
            self.identity.cxs_search_url(), context=f"workday[{self.identity.tenant}/{self.identity.site}].search",
            not_found=ErrorCategory.SOURCE_UNAVAILABLE, method="POST", body=body,
            headers={
                "Content-Type": "application/json",
                # The same Origin/Referer the employer's own careers page sends
                # for this public search (honest, never spoofed/rotated).
                "Origin": f"https://{self.identity.host}",
                "Referer": f"https://{self.identity.host}/{self.identity.locale}/{self.identity.site}",
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list):
            # Schema drift / masquerade → degraded, NEVER a false zero.
            return SearchResult(results=(), zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                                parse_findings=("workday: response missing 'jobPostings' array (schema drift?)",))
        raw_jobs = data["jobPostings"]
        total = data.get("total")
        parsed = parse_isolated(raw_jobs, lambda r: self._parse_posting(r, detail=False))
        results = tuple(parsed.results[:limit])
        has_more = isinstance(total, int) and (offset + limit) < total and len(results) > 0
        if results:
            return SearchResult(
                results=results, page=request.page, has_more=has_more,
                next_cursor=str(offset + limit) if has_more else None,
                total_reported=total if isinstance(total, int) else None,
                zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
                parse_findings=tuple(parsed.finding_reasons()),
            )
        if parsed.findings and not parsed.results and len(raw_jobs) > 0:
            kind = ZeroResultKind.EXTRACTION_UNRESOLVED
        else:
            kind = ZeroResultKind.TRUSTED_ZERO
        return SearchResult(results=(), page=request.page,
                            total_reported=total if isinstance(total, int) else None,
                            zero_result_kind=kind, parse_findings=tuple(parsed.finding_reasons()))

    def fetch_detail(self, request: DetailRequest) -> DiscoveryResult:
        self._require(Capability.DETAIL)
        external_path = None
        if request.url:
            # Accept a public job URL or a raw externalPath.
            external_path = request.url
            marker = f"/{self.identity.site}"
            if marker in request.url:
                external_path = request.url.split(marker, 1)[1]
        if not external_path and request.source_job_id:
            external_path = request.source_job_id
        if not external_path:
            raise AdapterError(ErrorCategory.CONFIG_ERROR, "workday.fetch_detail requires an externalPath url/id")
        data = self._get_json(
            self.identity.cxs_detail_url(external_path),
            context=f"workday[{self.identity.tenant}/{self.identity.site}].detail",
            not_found=ErrorCategory.INVALID_RESPONSE,
        )
        if not isinstance(data, dict) or "jobPostingInfo" not in data:
            raise AdapterError(ErrorCategory.INVALID_RESPONSE, "workday detail: missing jobPostingInfo (schema drift?)")
        result = self._parse_posting(data, detail=True)
        if result is None:
            raise AdapterError(ErrorCategory.INVALID_RESPONSE, "workday detail could not be parsed")
        return result


__all__ = ["WorkdayAdapter"]
