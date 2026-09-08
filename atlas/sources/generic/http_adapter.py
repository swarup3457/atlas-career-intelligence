"""Generic official-career HTTP adapter (Phase 1C-B, build spec 8).

ONE adapter that reads the OFFICIAL public career page of an arbitrary company
over bounded, read-only HTTP — no per-company Python. It reuses the shared
:class:`atlas.sources.http_client.ReadOnlyHttpClient` (TLS on, GET-only, redirect
safety, size/decompression bounds) and the dependency-free extraction primitives
in :mod:`atlas.careers.extract`.

Extraction order (first structured signal wins; everything is isolated):

    1. schema.org JSON-LD ``JobPosting`` (the Google-recommended contract);
    2. bounded embedded application-state JSON (Next.js/__INITIAL_STATE__/…);
    3. server-rendered job-detail anchors (list → detail).

Ordinary pagination is honored via ``<link rel="next">`` / a ``rel=next`` anchor.

A reachable page that yields zero jobs is TRUSTED_ZERO only when the page has
real content; a JS/SPA shell with no server-rendered jobs is
EXTRACTION_UNRESOLVED (with a ``spa_shell`` provenance flag) so the router can
fall back to the browser route — it is NEVER reported as "no jobs". A challenge/
login/anti-bot page is classified (never bypassed).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from atlas.careers import extract as X
from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError
from atlas.sources.ats.base import HttpAtsAdapter, detect_challenge, work_mode_from_text
from atlas.sources.http_client import HttpError, ReadOnlyHttpClient
from atlas.sources.models import (
    ActiveState,
    Capability,
    ConcurrencyClass,
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
from atlas.sources.adapter import new_result_base
from atlas.sources.parsing import parse_isolated

_HTML_ACCEPT = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"


def _with_page_param(url: str, page: int, param: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[param] = str(page)
    return urlunsplit(parts._replace(query=urlencode(query)))


class GenericCareerHttpAdapter(HttpAtsAdapter):
    """Read-only generic official career-site adapter (HTTP route)."""

    source_type = SourceType.COMPANY_CAREER
    source_family = SourceFamily.COMPANY_CAREER
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.DETAIL,
            Capability.DESCRIPTION,
            Capability.PAGINATION,
            Capability.POSTED_DATE,
            Capability.ACTIVE_STATUS,
        }
    )
    adapter_version = "career-http-1.0.0"
    parser_version = X.PARSER_VERSION
    concurrency_class = ConcurrencyClass.HTTP

    def __init__(self, instance: SourceInstance, *, http_client: Optional[ReadOnlyHttpClient] = None):
        super().__init__(instance, http_client=http_client)
        md = instance.metadata or {}
        self.entry_url = md.get("entry_url") or instance.base_url
        if not self.entry_url:
            raise AdapterError(
                ErrorCategory.CONFIG_ERROR,
                f"GenericCareerHttpAdapter instance {instance.instance_id!r} has no entry_url",
            )
        self.same_host_only = bool(md.get("same_host_only", True))
        # Optional explicit query-param pagination (e.g. "?page=N"); default is
        # rel=next link pagination only (never a fabricated ?page URL).
        self.page_param: Optional[str] = md.get("page_param")
        self.max_pages = int(md.get("max_pages", 3))
        self.max_results = int(md.get("max_results", X.MAX_JOBS_PER_PAGE))

    # -- HTTP helpers -------------------------------------------------------
    def _get_html(self, url: str, *, context: str, not_found: ErrorCategory) -> tuple[str, str]:
        resp = self._fetch(url, accept=_HTML_ACCEPT)
        self._raise_for_status(resp, context=context, not_found=not_found)
        return resp.text(), resp.url

    # -- normalization ------------------------------------------------------
    def _to_result(self, job: X.ExtractedJob, *, detail: bool) -> DiscoveryResult:
        provenance = {
            "source_family": "company_career",
            "extraction_method": job.extraction_method,
            "date_provenance": job.date_provenance,
            "entry_url": self.entry_url,
        }
        is_active = ActiveState.ACTIVE
        if job.is_active is False:
            is_active = ActiveState.INACTIVE
        return DiscoveryResult(
            **new_result_base(
                self,
                source_job_id=job.source_job_id,
                source_url=job.url,
                canonical_url=job.url,
                company=job.company or self.instance.display_name or None,
                title=job.title,
                location=job.location,
                work_mode=(WorkMode.REMOTE if job.remote else work_mode_from_text(job.location)),
                posted_at=job.posted_at,
                deadline=job.deadline,
                employment_type=job.employment_type,
                salary_text=job.salary_text,
                description=job.description if detail else None,
                is_active=is_active,
                verification_level=(
                    VerificationLevel.OFFICIAL_DETAIL_LIVE if (detail and job.description)
                    else VerificationLevel.OFFICIAL_SEARCH_LIVE
                ),
                confidence=0.8 if job.extraction_method in ("jsonld", "embedded_json") else 0.6,
                provenance=provenance,
            )
        )

    def _extract_all(self, html: str, base_url: str) -> tuple[list[X.ExtractedJob], str]:
        """Run the extraction ladder; return (jobs, method_used)."""
        jobs = X.extract_jsonld_jobs(html, base_url=base_url)
        if jobs:
            return jobs, "jsonld"
        jobs = X.extract_embedded_jobs(html, base_url=base_url)
        if jobs:
            return jobs, "embedded_json"
        jobs = X.extract_job_links(html, base_url=base_url, same_host_only=self.same_host_only)
        if jobs:
            return jobs, "anchor"
        return [], "none"

    # -- operations ---------------------------------------------------------
    def health_check(self):
        from atlas.sources.health import HealthEvidence, SourceHealth, SourceHealthState, classify_health

        try:
            resp = self._fetch(self.entry_url, accept=_HTML_ACCEPT)
        except AdapterError as exc:
            state = {
                ErrorCategory.HTTP_429: SourceHealthState.RATE_LIMITED,
                ErrorCategory.HTTP_5XX: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.SOURCE_UNAVAILABLE: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.TIMEOUT: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.ANTI_BOT: SourceHealthState.ACCESS_LIMITED,
                ErrorCategory.LOGIN_WALL: SourceHealthState.AUTH_REQUIRED,
            }.get(exc.category, SourceHealthState.UNKNOWN)
            return SourceHealth(state, f"career http probe failed: {exc.message}")
        challenge = detect_challenge(resp)
        # A challenge/login page (even served as HTTP 200) is access-limited /
        # auth-required — classified, never treated as a healthy board.
        if challenge == ErrorCategory.ANTI_BOT:
            return SourceHealth(SourceHealthState.ACCESS_LIMITED, "challenge/anti-bot page detected (no bypass)")
        if challenge == ErrorCategory.LOGIN_WALL:
            return SourceHealth(SourceHealthState.AUTH_REQUIRED, "login wall detected (no bypass)")
        evidence = HealthEvidence(
            http_status=resp.status,
            challenge_detected=False,
            login_redirect=False,
        )
        if resp.status != 200:
            return classify_health(evidence)
        html = resp.text()
        jobs, _ = self._extract_all(html, resp.url)
        evidence = HealthEvidence(
            http_status=200,
            expected_structure_present=bool(jobs) or not X.looks_like_spa_shell(html),
            result_count=len(jobs),
        )
        return classify_health(evidence)

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        # Determine the page URL: an explicit cursor (a rel=next URL) wins;
        # otherwise page 1 = entry_url, page N via an explicit page_param.
        if request.cursor:
            page_url = request.cursor
        elif request.page > 1 and self.page_param:
            page_url = _with_page_param(self.entry_url, request.page, self.page_param)
        else:
            page_url = self.entry_url

        html, final_url = self._get_html(
            page_url, context=f"career-http[{self.instance.instance_id}].search",
            not_found=ErrorCategory.SOURCE_UNAVAILABLE,
        )
        jobs, method = self._extract_all(html, final_url)

        parsed = parse_isolated(jobs, lambda j: self._to_result(j, detail=False))
        results = tuple(parsed.results)[: self.max_results]

        # Pagination: prefer an explicit rel=next link; else, if a page_param is
        # configured and this page returned a full-looking set, advance by page.
        next_url = X.find_next_page(html, base_url=final_url)
        has_more = False
        next_cursor: Optional[str] = None
        if next_url and request.page < self.max_pages:
            has_more = True
            next_cursor = next_url
        elif self.page_param and results and len(results) >= max(1, request.limit) and request.page < self.max_pages:
            has_more = True
            next_cursor = _with_page_param(self.entry_url, request.page + 1, self.page_param)

        if results:
            return SearchResult(
                results=results, page=request.page, has_more=has_more, next_cursor=next_cursor,
                total_reported=None, zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
                parse_findings=tuple(parsed.finding_reasons()),
            )

        # Zero results: distinguish a real empty board from a JS shell / drift.
        if X.looks_like_spa_shell(html, extracted_jobs=0):
            return SearchResult(
                results=(), page=request.page, zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                parse_findings=("career-http: JS/SPA shell with no server-rendered jobs (spa_shell)",),
            )
        if parsed.findings:
            return SearchResult(
                results=(), page=request.page, zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                parse_findings=tuple(parsed.finding_reasons()),
            )
        # A page with genuine content and no matching JobPosting/job link.
        return SearchResult(
            results=(), page=request.page, zero_result_kind=ZeroResultKind.TRUSTED_ZERO,
            parse_findings=(),
        )

    def fetch_detail(self, request: DetailRequest) -> DiscoveryResult:
        self._require(Capability.DETAIL)
        url = request.url
        if not url and request.source_job_id:
            url = request.source_job_id if request.source_job_id.lower().startswith("http") else None
        if not url:
            raise AdapterError(ErrorCategory.CONFIG_ERROR, "career-http.fetch_detail requires a job url")
        html, final_url = self._get_html(
            url, context=f"career-http[{self.instance.instance_id}].detail",
            not_found=ErrorCategory.INVALID_RESPONSE,
        )
        jobs = X.extract_jsonld_jobs(html, base_url=final_url)
        if not jobs:
            jobs = X.extract_embedded_jobs(html, base_url=final_url)
        if not jobs:
            raise AdapterError(
                ErrorCategory.INVALID_RESPONSE,
                f"career-http detail page {url} had no parseable JobPosting",
            )
        # Prefer the JobPosting whose URL best matches the requested URL.
        job = jobs[0]
        for cand in jobs:
            if cand.url and X.same_site(cand.url, final_url):
                job = cand
                break
        return self._to_result(job, detail=True)


__all__ = ["GenericCareerHttpAdapter"]
