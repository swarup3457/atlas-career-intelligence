"""Naukri READ-ONLY job-discovery adapter (Phase 1D §8).

A bounded, read-only discovery adapter for Naukri, optimized for the Indian
market. It uses Naukri's PUBLIC search JSON endpoint (``jobapi/v3/search``) via
the shared read-only HTTP client and returns normalized
:class:`DiscoveryResult`\\s that the market layer turns into append-only PORTAL
LEADS — never official-verified jobs.

Strict safety posture (build spec 1/8):

    * public/read-only search only — never Apply, sign in, create an account,
      solve a CAPTCHA, or bypass a challenge/anti-bot control; no proxies, no
      stealth;
    * query, location, experience and recency inputs are explicit and validated;
      the recency (``jobAge`` in days) filter is honored and NEVER silently
      omitted;
    * Bengaluru/Bangalore and Hyderabad location aliases are normalized;
    * LPA/INR salary text and experience text are preserved verbatim when
      present;
    * bounded pages/cards/time; each ``jobDetails`` element is parsed in
      isolation; a ghost element lacking a stable ``jobId`` + title never
      becomes a canonical lead;
    * a challenge/login/access-limited response is CLASSIFIED (ACCESS_LIMITED /
      AUTH_REQUIRED / RATE_LIMITED), never bypassed. If current Naukri policy
      blocks automation on this machine the adapter classifies ACCESS_LIMITED
      truthfully and never fabricates results.

Naukri rejects the JSON endpoint without its ``appid``/``systemid`` client
headers; these are non-secret public client identifiers, not credentials.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, new_result_base
from atlas.sources.ats.base import HttpAtsAdapter, work_mode_from_text
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.http_client import HttpError
from atlas.sources.models import (
    ActiveState,
    Capability,
    ConcurrencyClass,
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
from atlas.sources.portals.base import (
    MAX_PORTAL_CARDS_PER_PAGE,
    MAX_PORTAL_PAGES,
    PORTAL_LISTED_DATE,
    canonical_city,
    clean_text,
    looks_like_experience,
    looks_like_salary,
)

_SEARCH_BASE = "https://www.naukri.com/jobapi/v3/search"
_JOB_BASE = "https://www.naukri.com/job-listings-"
_PAGE_SIZE = 20

# Non-secret public client identifiers Naukri's web client sends. These are NOT
# credentials — they are the public app/system ids the anonymous JSON endpoint
# requires; without them the endpoint refuses the request.
_NAUKRI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; atlas-career-intelligence/1.0; read-only job discovery)",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "appid": "109",
    "systemid": "109",
}


class NaukriPublicAdapter(HttpAtsAdapter):
    """Read-only Naukri public search adapter (India-optimized)."""

    source_type = SourceType.PORTAL_LARGE
    source_family = SourceFamily.NAUKRI
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.PAGINATION,
            Capability.RECENCY_FILTER,
            Capability.LOCATION_FILTER,
            Capability.KEYWORD_FILTER,
        }
    )
    adapter_version = "naukri-public-1.0.0"
    parser_version = "naukri-parse-1.0.0"
    concurrency_class = ConcurrencyClass.BROWSER_ANONYMOUS

    def __init__(self, instance: SourceInstance, *, http_client=None):
        super().__init__(instance, http_client=http_client)
        md = instance.metadata or {}
        self.search_base = md.get("search_base") or _SEARCH_BASE
        self.max_pages = min(int(md.get("max_pages", MAX_PORTAL_PAGES)), MAX_PORTAL_PAGES)
        self.max_cards = min(int(md.get("max_results", _PAGE_SIZE)), MAX_PORTAL_CARDS_PER_PAGE)
        self.auth_ref = instance.auth_ref or md.get("auth_ref")
        if self.auth_ref:
            self.concurrency_class = ConcurrencyClass.BROWSER_AUTHENTICATED

    # -- request building ---------------------------------------------------
    def _search_url(self, request: SearchRequest, *, page_no: int) -> str:
        params: list[tuple[str, str]] = [
            ("noOfResults", str(_PAGE_SIZE)),
            ("urlType", "search_by_key_loc"),
            ("searchType", "adv"),
            ("keyword", request.query or ""),
            ("k", request.query or ""),
            ("pageNo", str(page_no)),
        ]
        if request.location:
            params.append(("location", request.location))
            params.append(("l", request.location))
        if request.experience_hint:
            params.append(("experience", str(request.experience_hint)))
        # Recency (jobAge in days) — honored, NEVER silently omitted.
        if request.recency_days is not None and int(request.recency_days) >= 1:
            params.append(("jobAge", str(int(request.recency_days))))
        return f"{self.search_base}?{urlencode(params)}"

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _placeholder(job: dict, kind: str) -> Optional[str]:
        for ph in job.get("placeholders") or []:
            if isinstance(ph, dict) and str(ph.get("type", "")).lower() == kind:
                return clean_text(ph.get("label"))
        return None

    def _canonical_url(self, job: dict) -> Optional[str]:
        jd = job.get("jdURL") or job.get("jd_url")
        if jd:
            if jd.lower().startswith("http"):
                return jd
            return "https://www.naukri.com" + (jd if jd.startswith("/") else "/" + jd)
        jid = job.get("jobId") or job.get("jobid")
        return f"{_JOB_BASE}{jid}" if jid else None

    def _parse_job(self, job: dict) -> Optional[DiscoveryResult]:
        if not isinstance(job, dict):
            raise ValueError("non-object job element")
        job_id = str(job.get("jobId") or job.get("jobid") or "").strip() or None
        title = clean_text(job.get("title") or job.get("jobTitle"))
        url = self._canonical_url(job)
        # Minimum identity: a stable job id (or a job URL) AND a title.
        if not (job_id or url) or not title:
            raise ValueError("ghost/partial job: missing stable jobId/url or title")
        company = clean_text(job.get("companyName") or job.get("company"))
        location = self._placeholder(job, "location") or clean_text(job.get("location"))
        experience = self._placeholder(job, "experience")
        salary = self._placeholder(job, "salary")
        posted = clean_text(job.get("footerPlaceholderLabel") or job.get("createdDate") or job.get("postedDate"))
        skills_raw = job.get("tagsAndSkills") or ""
        skills = tuple(s.strip() for s in str(skills_raw).split(",") if s.strip())[:20]
        # Preserve salary/experience text even if it appears in an unexpected slot.
        if experience and not looks_like_experience(experience) and looks_like_salary(experience):
            experience, salary = None, experience
        provenance = {
            "source_family": "naukri",
            "portal": "naukri",
            "date_provenance": PORTAL_LISTED_DATE,
            "posted_text": posted,
            "canonical_city": canonical_city(location),
        }
        return DiscoveryResult(
            **new_result_base(
                self,
                source_job_id=job_id,
                source_url=url,
                canonical_url=url,
                company=company,
                title=title,
                location=location,
                work_mode=work_mode_from_text(title, location, job.get("jobDescription")),
                posted_at=posted,
                experience_text=experience,
                salary_text=salary,
                skills=skills,
                is_active=ActiveState.ACTIVE,
                verification_level=VerificationLevel.PORTAL_LIVE,
                confidence=0.55,
                provenance=provenance,
            )
        )

    # -- operations ---------------------------------------------------------
    def health_check(self) -> SourceHealth:
        try:
            resp = self._fetch(
                self._search_url(SearchRequest(query="software engineer", location="India", limit=1), page_no=1),
                headers=_NAUKRI_HEADERS, accept="application/json",
            )
        except AdapterError as exc:
            state = {
                ErrorCategory.HTTP_429: SourceHealthState.RATE_LIMITED,
                ErrorCategory.ANTI_BOT: SourceHealthState.ACCESS_LIMITED,
                ErrorCategory.LOGIN_WALL: SourceHealthState.AUTH_REQUIRED,
                ErrorCategory.HTTP_5XX: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.SOURCE_UNAVAILABLE: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.TIMEOUT: SourceHealthState.SOURCE_UNAVAILABLE,
            }.get(exc.category, SourceHealthState.UNKNOWN)
            return SourceHealth(state, f"naukri probe: {exc.message}")
        from atlas.sources.ats.base import detect_challenge

        challenge = detect_challenge(resp)
        if challenge == ErrorCategory.ANTI_BOT or resp.status in (403, 406):
            return SourceHealth(SourceHealthState.ACCESS_LIMITED, f"naukri anti-bot/challenge HTTP {resp.status} (no bypass)")
        if challenge == ErrorCategory.LOGIN_WALL:
            return SourceHealth(SourceHealthState.AUTH_REQUIRED, "naukri login wall (no bypass)")
        if resp.status != 200:
            return SourceHealth(SourceHealthState.SOURCE_UNAVAILABLE, f"naukri HTTP {resp.status}")
        try:
            data = resp.json()
        except HttpError:
            return SourceHealth(SourceHealthState.ACCESS_LIMITED, "naukri: 200 but body not JSON (likely block page)")
        jobs = (data or {}).get("jobDetails") if isinstance(data, dict) else None
        if jobs:
            return SourceHealth(SourceHealthState.HEALTHY, f"naukri returned {len(jobs)} job(s)")
        return SourceHealth(SourceHealthState.SELECTOR_DRIFT_SUSPECTED, "naukri response had no jobDetails")

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        page = max(1, request.page)
        if page > self.max_pages:
            return SearchResult(results=(), page=page, has_more=False,
                                zero_result_kind=ZeroResultKind.NOT_APPLICABLE)
        url = self._search_url(request, page_no=page)
        resp = self._fetch(url, headers=_NAUKRI_HEADERS, accept="application/json")
        # Naukri answers anti-automation with 403/406/429 (HTTP 406 "Not
        # Acceptable" is its common block for non-browser clients) — CLASSIFIED
        # as access-limited/rate-limited, never bypassed.
        if resp.status in (403, 406):
            raise AdapterError(ErrorCategory.ANTI_BOT,
                               f"naukri[{self.instance_id}]: HTTP {resp.status} (access limited; no bypass)")
        self._raise_for_status(resp, context=f"naukri[{self.instance_id}].search",
                               not_found=ErrorCategory.SOURCE_UNAVAILABLE)
        try:
            data = resp.json()
        except HttpError:
            raise AdapterError(ErrorCategory.ANTI_BOT,
                               f"naukri[{self.instance_id}]: 200 but body not JSON (likely block page)")
        if not isinstance(data, dict):
            return SearchResult(results=(), page=page, zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                                parse_findings=("naukri: unexpected non-object response",))
        jobs = data.get("jobDetails") or []
        total = data.get("noOfJobs")
        parsed = parse_isolated(list(jobs)[: self.max_cards], self._parse_job)
        results = tuple(parsed.results)
        has_more = bool(len(jobs) >= _PAGE_SIZE and page < self.max_pages)
        next_cursor = str(page + 1) if has_more else None
        if results:
            return SearchResult(
                results=results, page=page, has_more=has_more, next_cursor=next_cursor,
                total_reported=int(total) if isinstance(total, int) else None,
                zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
                parse_findings=tuple(parsed.finding_reasons()),
            )
        if isinstance(jobs, list):
            # A well-formed empty jobDetails list is a trusted zero.
            return SearchResult(results=(), page=page, zero_result_kind=ZeroResultKind.TRUSTED_ZERO,
                                total_reported=int(total) if isinstance(total, int) else None)
        return SearchResult(results=(), page=page, zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
                            parse_findings=tuple(parsed.finding_reasons()) or ("naukri: no jobDetails array",))


__all__ = ["NaukriPublicAdapter"]
