"""Ashby official Public Job Postings API adapter (Phase 1C-A).

Read-only, GET-only against the single public endpoint
``GET https://api.ashbyhq.com/posting-api/job-board/{jobBoardName}``. It never
performs any application action. See docs/sources/ASHBY.md.

Contract facts (from developers.ashbyhq.com/docs/public-job-posting-api):

    * The endpoint is list-only and returns ``{apiVersion, jobs:[...]}`` with
      ALL currently published postings in one response (NO pagination).
    * Each posting already includes ``descriptionHtml``/``descriptionPlain``,
      so there is NO separate detail endpoint — this adapter declares no DETAIL
      capability and populates the description directly from the list.
    * ``publishedAt`` (ISO, "last published") is the closest posted date →
      provenance ``EMPLOYER_POSTED_AT`` (with the documented caveat that it may
      move on re-publish); there is NO employer-updated timestamp.
    * ``isListed:false`` postings are direct-link-only and are excluded from the
      public board listing.
    * The docs do NOT guarantee a per-job ``id``; a stable source id is derived
      from ``id`` when present, else the final ``jobUrl`` path segment, else a
      deterministic hash of title + publishedAt.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional
from urllib.parse import quote

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, new_result_base
from atlas.sources.ats.base import (
    DateProvenance,
    HttpAtsAdapter,
    extract_ashby_board_name,
    sanitize_description,
)
from atlas.sources.health import SourceHealth
from atlas.sources.models import (
    ActiveState,
    Capability,
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

_API_BASE = "https://api.ashbyhq.com/posting-api/job-board"
_WORKPLACE = {"remote": WorkMode.REMOTE, "hybrid": WorkMode.HYBRID, "onsite": WorkMode.ONSITE}


class AshbyAdapter(HttpAtsAdapter):
    source_type = SourceType.ATS_ASHBY
    source_family = SourceFamily.ASHBY
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.DESCRIPTION,
            Capability.POSTED_DATE,
            Capability.ACTIVE_STATUS,
            Capability.API_AVAILABLE,
        }
    )
    adapter_version = "ashby-1.0.0"
    parser_version = "ashby-parser-1.0.0"

    def __init__(self, instance: SourceInstance, *, http_client=None):
        super().__init__(instance, http_client=http_client)
        self.board_name = self._resolve_board(instance)
        if not self.board_name:
            raise AdapterError(ErrorCategory.CONFIG_ERROR, f"Ashby instance {instance.instance_id!r} has no board name")
        self.include_compensation = bool((instance.metadata or {}).get("include_compensation", False))

    @staticmethod
    def _resolve_board(instance: SourceInstance) -> Optional[str]:
        md = instance.metadata or {}
        board = md.get("board_name") or instance.company_id or instance.tenant
        if board:
            return str(board)
        if instance.base_url:
            try:
                return extract_ashby_board_name(str(instance.base_url))
            except ValueError:
                return None
        return None

    def _board_url(self) -> str:
        flag = "true" if self.include_compensation else "false"
        return f"{_API_BASE}/{quote(self.board_name, safe='')}?includeCompensation={flag}"

    # -- parsing ------------------------------------------------------------
    def _stable_id(self, raw: dict, title: str) -> str:
        if raw.get("id"):
            return str(raw["id"])
        job_url = raw.get("jobUrl")
        if isinstance(job_url, str) and job_url.strip():
            tail = job_url.rstrip("/").rsplit("/", 1)[-1]
            if tail:
                return tail
        return "ashby-" + hashlib.sha256(f"{title}|{raw.get('publishedAt','')}".encode("utf-8")).hexdigest()[:16]

    def _parse_job(self, raw: Any) -> Optional[DiscoveryResult]:
        if not isinstance(raw, dict):
            raise ValueError("job is not a JSON object")
        # Exclude direct-link-only postings from the public listing.
        if raw.get("isListed") is False:
            return None
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("job missing/invalid 'title'")
        location = raw.get("location")
        if not isinstance(location, str):
            location = None

        workplace_raw = str(raw.get("workplaceType", "")).lower()
        work_mode = _WORKPLACE.get(workplace_raw, WorkMode.UNKNOWN)
        if work_mode == WorkMode.UNKNOWN and raw.get("isRemote") is True:
            work_mode = WorkMode.REMOTE

        published_at = raw.get("publishedAt")
        provenance = {
            "source_family": "ashby",
            "board_name": self.board_name,
            "date_provenance": (DateProvenance.EMPLOYER_POSTED_AT if published_at else DateProvenance.UNKNOWN).value,
            "department": raw.get("department"),
            "team": raw.get("team"),
        }
        return DiscoveryResult(
            **new_result_base(
                self,
                source_job_id=self._stable_id(raw, title.strip()),
                source_url=raw.get("jobUrl"),
                canonical_url=raw.get("jobUrl"),
                company=self.instance.display_name or self.board_name,
                title=title.strip(),
                location=location,
                work_mode=work_mode,
                posted_at=published_at,
                employment_type=raw.get("employmentType"),
                description=sanitize_description(raw.get("descriptionPlain") or raw.get("descriptionHtml")),
                is_active=ActiveState.ACTIVE,
                verification_level=VerificationLevel.OFFICIAL_SEARCH_LIVE,
                confidence=0.95,
                provenance=provenance,
            )
        )

    # -- operations ---------------------------------------------------------
    def health_check(self) -> SourceHealth:
        return self._probe(self._board_url(), expected_keys=("jobs", "apiVersion"))

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        data = self._get_json(
            self._board_url(), context=f"ashby[{self.board_name}].search",
            not_found=ErrorCategory.SOURCE_UNAVAILABLE,
        )
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return SearchResult(results=(), zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                                parse_findings=("ashby: response missing 'jobs' array",))
        raw_jobs = data["jobs"]
        parsed = parse_isolated(raw_jobs, self._parse_job)
        all_results = tuple(parsed.results)  # whole board — NEVER silently sliced
        total = len(all_results)
        # Deterministic LOCAL offset pagination over the whole board (build spec 10).
        if request.cursor is not None:
            try:
                offset = max(0, int(request.cursor))
            except (TypeError, ValueError):
                offset = 0
        else:
            offset = max(0, (max(1, request.page) - 1) * request.limit)
        page_results = all_results[offset:offset + request.limit]
        has_more = (offset + request.limit) < total
        next_cursor = str(offset + request.limit) if has_more else None
        if page_results:
            return SearchResult(
                results=page_results, page=request.page, has_more=has_more, next_cursor=next_cursor,
                total_reported=total, zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
                parse_findings=tuple(parsed.finding_reasons()),
            )
        if parsed.findings and not all_results and len(raw_jobs) > 0:
            kind = ZeroResultKind.EXTRACTION_UNRESOLVED
        else:
            kind = ZeroResultKind.TRUSTED_ZERO
        return SearchResult(results=(), page=request.page, total_reported=total, zero_result_kind=kind,
                            parse_findings=tuple(parsed.finding_reasons()))


__all__ = ["AshbyAdapter"]
