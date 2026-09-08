"""Greenhouse official Job Board API adapter (Phase 1C-A).

Read-only, GET-only against the public Job Board API
(``https://boards-api.greenhouse.io/v1``). It NEVER touches the application
submission endpoint (a POST that requires Basic Auth). See docs/sources/GREENHOUSE.md.

Contract facts encoded here (from developers.greenhouse.io/job-board.html):

    * List: ``GET /v1/boards/{board_token}/jobs`` returns ALL posts in ONE
      response (``meta.total``) — Greenhouse does NOT paginate jobs, so this
      adapter declares no PAGINATION capability.
    * The list stub exposes only ``updated_at`` (an employer EDIT/re-publish
      time), so search results carry ``updated_at`` with provenance
      ``EMPLOYER_UPDATED_AT`` and NEVER a fabricated posted date.
    * ``first_published`` (the true posted date) exists ONLY on the single-job
      endpoint, so ``fetch_detail`` hydrates ``posted_at`` with provenance
      ``EMPLOYER_POSTED_AT`` plus the full (sanitized) description.
    * A post with a null ``internal_job_id`` is a prospect/general-interest
      post and is filtered out of real job results.
"""

from __future__ import annotations

from typing import Any, Optional

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, SourceAdapter, new_result_base
from atlas.sources.ats.base import (
    DateProvenance,
    HttpAtsAdapter,
    extract_greenhouse_board_token,
    sanitize_description,
    work_mode_from_text,
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
    ZeroResultKind,
)
from atlas.sources.parsing import parse_isolated

_API_BASE = "https://boards-api.greenhouse.io/v1"


class GreenhouseAdapter(HttpAtsAdapter):
    source_type = SourceType.ATS_GREENHOUSE
    source_family = SourceFamily.GREENHOUSE
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.DETAIL,
            Capability.DESCRIPTION,
            Capability.POSTED_DATE,
            Capability.ACTIVE_STATUS,
            Capability.API_AVAILABLE,
        }
    )
    adapter_version = "greenhouse-1.0.0"
    parser_version = "greenhouse-parser-1.0.0"

    def __init__(self, instance: SourceInstance, *, http_client=None):
        super().__init__(instance, http_client=http_client)
        self.board_token = self._resolve_board_token(instance)
        if not self.board_token:
            raise AdapterError(ErrorCategory.CONFIG_ERROR, f"Greenhouse instance {instance.instance_id!r} has no board_token")

    @staticmethod
    def _resolve_board_token(instance: SourceInstance) -> Optional[str]:
        md = instance.metadata or {}
        token = md.get("board_token") or instance.company_id or instance.tenant
        if token:
            return str(token)
        for candidate in (instance.base_url,):
            if candidate:
                try:
                    return extract_greenhouse_board_token(str(candidate))
                except ValueError:
                    continue
        return None

    # -- endpoints ----------------------------------------------------------
    def _jobs_url(self, *, content: bool = False) -> str:
        url = f"{_API_BASE}/boards/{self.board_token}/jobs"
        return url + "?content=true" if content else url

    def _job_url(self, job_id: str) -> str:
        return f"{_API_BASE}/boards/{self.board_token}/jobs/{job_id}"

    # -- parsing ------------------------------------------------------------
    def _parse_job(self, raw: Any, *, detail: bool = False) -> Optional[DiscoveryResult]:
        if not isinstance(raw, dict):
            raise ValueError("job is not a JSON object")
        job_id = raw.get("id")
        if job_id is None:
            raise ValueError("job missing 'id'")
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("job missing/invalid 'title'")
        # Prospect / general-interest posts (null internal_job_id) are not jobs.
        if not detail and "internal_job_id" in raw and raw.get("internal_job_id") is None:
            return None

        location = None
        loc = raw.get("location")
        if isinstance(loc, dict):
            location = loc.get("name")
        elif isinstance(loc, str):
            location = loc

        posted_at = None
        date_prov = DateProvenance.EMPLOYER_UPDATED_AT
        if detail and raw.get("first_published"):
            posted_at = raw.get("first_published")
            date_prov = DateProvenance.EMPLOYER_POSTED_AT
        updated_at = raw.get("updated_at")

        provenance = {
            "source_family": "greenhouse",
            "board_token": self.board_token,
            "date_provenance": date_prov.value,
            "requisition_id": raw.get("requisition_id"),
        }
        return DiscoveryResult(
            **new_result_base(
                self,
                source_job_id=str(job_id),
                source_url=raw.get("absolute_url"),
                canonical_url=raw.get("absolute_url"),
                company=self.instance.display_name or self.board_token,
                title=title.strip(),
                location=location,
                work_mode=work_mode_from_text(location),
                posted_at=posted_at,
                updated_at=updated_at,
                deadline=raw.get("application_deadline"),
                description=sanitize_description(raw.get("content")) if detail else None,
                is_active=ActiveState.ACTIVE,  # the board lists only live posts
                verification_level=(
                    VerificationLevel.OFFICIAL_DETAIL_LIVE if detail else VerificationLevel.OFFICIAL_SEARCH_LIVE
                ),
                confidence=0.95,
                provenance=provenance,
            )
        )

    # -- operations ---------------------------------------------------------
    def health_check(self) -> SourceHealth:
        return self._probe(self._jobs_url(), expected_keys=("jobs", "meta"))

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        data = self._get_json(
            self._jobs_url(), context=f"greenhouse[{self.board_token}].search",
            not_found=ErrorCategory.SOURCE_UNAVAILABLE,
        )
        if not isinstance(data, dict) or "jobs" not in data or not isinstance(data["jobs"], list):
            return SearchResult(results=(), zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                                parse_findings=("greenhouse: response missing 'jobs' array",))
        raw_jobs = data["jobs"]
        parsed = parse_isolated(raw_jobs, lambda r: self._parse_job(r, detail=False))
        results = tuple(parsed.results[: request.limit])
        total = data.get("meta", {}).get("total") if isinstance(data.get("meta"), dict) else len(raw_jobs)

        if results:
            return SearchResult(
                results=results, page=request.page, total_reported=total,
                zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
                parse_findings=tuple(parsed.finding_reasons()),
            )
        # Zero results.
        if parsed.findings and not parsed.results and len(raw_jobs) > 0:
            kind = ZeroResultKind.EXTRACTION_UNRESOLVED
        else:
            kind = ZeroResultKind.TRUSTED_ZERO  # a reachable board with no live posts
        return SearchResult(results=(), page=request.page, total_reported=total,
                            zero_result_kind=kind, parse_findings=tuple(parsed.finding_reasons()))

    def fetch_detail(self, request: DetailRequest) -> DiscoveryResult:
        self._require(Capability.DETAIL)
        job_id = request.source_job_id
        if job_id is None and request.url:
            job_id = request.url.rstrip("/").rsplit("/", 1)[-1]
        if not job_id:
            raise AdapterError(ErrorCategory.CONFIG_ERROR, "greenhouse.fetch_detail requires a job id or url")
        data = self._get_json(
            self._job_url(job_id), context=f"greenhouse[{self.board_token}].detail",
            not_found=ErrorCategory.INVALID_RESPONSE,
        )
        result = self._parse_job(data, detail=True)
        if result is None:
            raise AdapterError(ErrorCategory.INVALID_RESPONSE, f"greenhouse job {job_id} could not be parsed")
        return result


__all__ = ["GreenhouseAdapter"]
