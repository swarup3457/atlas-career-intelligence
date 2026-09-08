"""LinkedIn READ-ONLY job-discovery adapter (Phase 1D §7).

A bounded, read-only discovery adapter that uses LinkedIn's PUBLIC, anonymous
guest job-search endpoint (the same "see more jobs" HTML fragment a logged-out
browser receives) via the shared read-only HTTP client. It implements the
standard :class:`atlas.sources.adapter.SourceAdapter` contract and returns
normalized :class:`DiscoveryResult`\\s that the market layer turns into
append-only PORTAL LEADS — never official-verified jobs.

Strict safety posture (build spec 1/7):

    * READ-ONLY jobs discovery only — never Apply, message, connect, or edit a
      profile; never sign in, create an account, export a cookie/session, solve
      a CAPTCHA, or bypass a challenge/anti-bot control;
    * prefers the ANONYMOUS public guest route (no credentials). An authenticated
      LinkedIn browser profile is used ONLY when an ``auth_ref`` is explicitly
      configured, headless/background, with authenticated-profile concurrency
      exactly 1 (never two owners of one profile);
    * a logged-out/challenge/access-limited response is CLASSIFIED
      (LOGIN_REQUIRED / ACCESS_LIMITED / RATE_LIMITED), never bypassed;
    * bounded pages/cards/time; the recency (time-posted) filter is honored and
      NEVER silently omitted; each card is parsed in isolation so one malformed
      card cannot kill the page; a ghost/partial card lacking a stable job id +
      URL never becomes a canonical lead.

The guest endpoint (public):
    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
        ?keywords=<kw>&location=<loc>&f_TPR=r<seconds>&f_WT=<1|2|3>&start=<offset>
It returns a flat ``<li>`` list of ``base-card`` job cards.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlencode

from atlas.careers.extract import host_of, normalize_url
from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, new_result_base
from atlas.sources.ats.base import HttpAtsAdapter, detect_challenge, work_mode_from_text
from atlas.sources.health import SourceHealth, SourceHealthState
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
    clean_text,
)

_GUEST_BASE = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_JOB_VIEW = "https://www.linkedin.com/jobs/view/"
_PAGE_SIZE = 10  # LinkedIn guest "seeMoreJobPostings" returns 10 cards/page.

# Polite, non-deceptive read-only headers for the public guest endpoint.
_GUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; atlas-career-intelligence/1.0; read-only job discovery)",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

# LinkedIn may serve HTTP 200 with an auth-wall / "join now to see" gate instead
# of a results list. That is a login wall — CLASSIFIED, never bypassed.
_AUTHWALL_MARKERS = (
    "authwall",
    "join now to see",
    "sign in to see",
    "sign in to view",
    "join linkedin to",
    "/checkpoint/",
)

# Work-type filter values for the guest endpoint.
_WT = {WorkMode.ONSITE: "1", WorkMode.REMOTE: "2", WorkMode.HYBRID: "3"}

# Per-card field extraction (applied to ONE card block in isolation).
_CARD_SPLIT_RE = re.compile(r"<li\b", re.IGNORECASE)
_URN_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"', re.IGNORECASE)
_DATAID_RE = re.compile(r'data-id="(\d+)"', re.IGNORECASE)
_LINK_RE = re.compile(r'<a[^>]*base-card__full-link[^>]*href="([^"]+)"', re.IGNORECASE)
_ANY_JOBVIEW_RE = re.compile(r'href="(https://[^"]*?/jobs/view/[^"]+)"', re.IGNORECASE)
_TITLE_RE = re.compile(r'<h3[^>]*base-search-card__title[^>]*>(.*?)</h3>', re.IGNORECASE | re.DOTALL)
_COMPANY_RE = re.compile(r'<h4[^>]*base-search-card__subtitle[^>]*>(.*?)</h4>', re.IGNORECASE | re.DOTALL)
_LOCATION_RE = re.compile(r'<span[^>]*job-search-card__location[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL)
_TIME_DT_RE = re.compile(r'<time[^>]*datetime="([^"]+)"', re.IGNORECASE)
_TIME_TXT_RE = re.compile(r'<time[^>]*>(.*?)</time>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_JOBID_FROM_URL_RE = re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d+)(?:[/?#]|$)")


def _strip_tags(fragment: Optional[str]) -> Optional[str]:
    if not fragment:
        return None
    return clean_text(_TAG_RE.sub(" ", fragment))


class LinkedInGuestAdapter(HttpAtsAdapter):
    """Read-only LinkedIn public guest job-discovery adapter."""

    source_type = SourceType.PORTAL_LARGE
    source_family = SourceFamily.LINKEDIN
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.PAGINATION,
            Capability.RECENCY_FILTER,
            Capability.LOCATION_FILTER,
            Capability.KEYWORD_FILTER,
        }
    )
    adapter_version = "linkedin-guest-1.0.0"
    parser_version = "linkedin-parse-1.0.0"
    concurrency_class = ConcurrencyClass.BROWSER_ANONYMOUS

    def __init__(self, instance: SourceInstance, *, http_client=None):
        super().__init__(instance, http_client=http_client)
        md = instance.metadata or {}
        # A configured base overrides the public endpoint (used by offline
        # fixtures pointing at 127.0.0.1). Default is the real public guest API.
        self.guest_base = md.get("guest_base") or _GUEST_BASE
        self.max_pages = min(int(md.get("max_pages", MAX_PORTAL_PAGES)), MAX_PORTAL_PAGES)
        self.max_cards = min(int(md.get("max_results", MAX_PORTAL_CARDS_PER_PAGE)), MAX_PORTAL_CARDS_PER_PAGE)
        # An authenticated route is opt-in ONLY and pins concurrency to 1.
        self.auth_ref = instance.auth_ref or md.get("auth_ref")
        if self.auth_ref:
            self.concurrency_class = ConcurrencyClass.BROWSER_AUTHENTICATED

    # -- request building ---------------------------------------------------
    def _search_url(self, request: SearchRequest, *, start: int) -> str:
        params: list[tuple[str, str]] = [("keywords", request.query or "")]
        if request.location:
            params.append(("location", request.location))
        # Recency (time-posted range) — honored, NEVER silently omitted. Guard
        # against a degenerate value that would silently drop the filter (a value
        # <=0 or absurdly large is treated as "no filter", matching the public
        # endpoint's own behavior).
        if request.recency_days is not None and 1 <= int(request.recency_days) < 9999:
            params.append(("f_TPR", f"r{int(request.recency_days) * 86400}"))
        wt = _WT.get(request.work_mode) if request.work_mode else None
        if wt:
            params.append(("f_WT", wt))
        params.append(("start", str(start)))
        return f"{self.guest_base}?{urlencode(params)}"

    # -- parsing ------------------------------------------------------------
    def _cards(self, html: str) -> list[str]:
        # Split the flat <li> list into per-card blocks; the leading fragment
        # before the first <li> is dropped.
        parts = _CARD_SPLIT_RE.split(html)
        return [p for p in parts[1:] if "base-card" in p or "/jobs/view/" in p]

    def _job_id(self, card: str, url: Optional[str]) -> Optional[str]:
        m = _URN_RE.search(card) or _DATAID_RE.search(card)
        if m:
            return m.group(1)
        if url:
            m2 = _JOBID_FROM_URL_RE.search(url)
            if m2:
                return m2.group(1)
        return None

    def _parse_card(self, card: str) -> Optional[DiscoveryResult]:
        link_m = _LINK_RE.search(card) or _ANY_JOBVIEW_RE.search(card)
        raw_url = link_m.group(1) if link_m else None
        url = normalize_url("https://www.linkedin.com", raw_url) if raw_url else None
        job_id = self._job_id(card, url)
        title = _strip_tags(_TITLE_RE.search(card).group(1) if _TITLE_RE.search(card) else None)
        # Minimum identity: a stable job id (or job-view URL) AND a title.
        if not (job_id or (url and "/jobs/view/" in url)) or not title:
            raise ValueError("ghost/partial card: missing stable job id/url or title")
        canonical = f"{_JOB_VIEW}{job_id}" if job_id else url
        company = _strip_tags(_COMPANY_RE.search(card).group(1) if _COMPANY_RE.search(card) else None)
        location = _strip_tags(_LOCATION_RE.search(card).group(1) if _LOCATION_RE.search(card) else None)
        dt_m = _TIME_DT_RE.search(card)
        txt_m = _TIME_TXT_RE.search(card)
        posted = dt_m.group(1) if dt_m else _strip_tags(txt_m.group(1) if txt_m else None)
        provenance = {
            "source_family": "linkedin",
            "portal": "linkedin",
            "date_provenance": PORTAL_LISTED_DATE,
            "posted_text": _strip_tags(txt_m.group(1) if txt_m else None),
            "guest_route": True,
        }
        return DiscoveryResult(
            **new_result_base(
                self,
                source_job_id=job_id,
                source_url=url,
                canonical_url=canonical,
                company=company,
                title=title,
                location=location,
                work_mode=work_mode_from_text(title, location),
                posted_at=posted,
                is_active=ActiveState.ACTIVE,
                verification_level=VerificationLevel.PORTAL_LIVE,
                confidence=0.55,
                provenance=provenance,
            )
        )

    # -- operations ---------------------------------------------------------
    def health_check(self) -> SourceHealth:
        try:
            resp = self._fetch(self._search_url(SearchRequest(query="software engineer", limit=1), start=0),
                               headers=_GUEST_HEADERS, accept="text/html,application/xhtml+xml,*/*;q=0.8")
        except AdapterError as exc:
            state = {
                ErrorCategory.HTTP_429: SourceHealthState.RATE_LIMITED,
                ErrorCategory.ANTI_BOT: SourceHealthState.ACCESS_LIMITED,
                ErrorCategory.LOGIN_WALL: SourceHealthState.AUTH_REQUIRED,
                ErrorCategory.HTTP_5XX: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.SOURCE_UNAVAILABLE: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.TIMEOUT: SourceHealthState.SOURCE_UNAVAILABLE,
            }.get(exc.category, SourceHealthState.UNKNOWN)
            return SourceHealth(state, f"linkedin guest probe: {exc.message}")
        challenge = detect_challenge(resp)
        if challenge == ErrorCategory.ANTI_BOT:
            return SourceHealth(SourceHealthState.ACCESS_LIMITED, "linkedin anti-bot/challenge (no bypass)")
        if challenge == ErrorCategory.LOGIN_WALL or self._is_authwall(resp):
            return SourceHealth(SourceHealthState.AUTH_REQUIRED, "linkedin login/auth wall (no bypass)")
        if resp.status != 200:
            return SourceHealth(SourceHealthState.SOURCE_UNAVAILABLE, f"linkedin HTTP {resp.status}")
        cards = self._cards(resp.text())
        if cards:
            return SourceHealth(SourceHealthState.HEALTHY, f"linkedin guest returned {len(cards)} card(s)")
        return SourceHealth(SourceHealthState.SELECTOR_DRIFT_SUSPECTED, "linkedin guest page had no job cards")

    @staticmethod
    def _is_authwall(resp) -> bool:
        try:
            body = resp.body[:8192].decode("utf-8", errors="replace").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(m in body for m in _AUTHWALL_MARKERS)

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        page = max(1, request.page)
        if page > self.max_pages:
            return SearchResult(results=(), page=page, has_more=False,
                                zero_result_kind=ZeroResultKind.NOT_APPLICABLE)
        start = (page - 1) * _PAGE_SIZE
        url = self._search_url(request, start=start)
        resp = self._fetch(url, headers=_GUEST_HEADERS, accept="text/html,application/xhtml+xml,*/*;q=0.8")
        # Classify a challenge/login/rate-limit BEFORE parsing (never bypass).
        self._raise_for_status(resp, context=f"linkedin[{self.instance_id}].search",
                               not_found=ErrorCategory.SOURCE_UNAVAILABLE)
        if self._is_authwall(resp):
            raise AdapterError(ErrorCategory.LOGIN_WALL,
                               f"linkedin[{self.instance_id}]: auth-wall served in place of results (no bypass)")
        html = resp.text()
        cards = self._cards(html)[: self.max_cards]
        parsed = parse_isolated(cards, self._parse_card)
        results = tuple(parsed.results)
        has_more = bool(len(cards) >= _PAGE_SIZE and page < self.max_pages)
        next_cursor = str(start + _PAGE_SIZE) if has_more else None
        if results:
            return SearchResult(
                results=results, page=page, has_more=has_more, next_cursor=next_cursor,
                total_reported=None, zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
                parse_findings=tuple(parsed.finding_reasons()),
            )
        # Zero cards: a reachable guest page that returns no cards is a trusted
        # zero ONLY if the fragment looks like a real (empty) result list; a
        # blank/odd body is unresolved, never a confident "no jobs".
        if "base-card" in html or "jobs-search__results-list" in html or html.strip() == "":
            return SearchResult(results=(), page=page, zero_result_kind=ZeroResultKind.TRUSTED_ZERO)
        return SearchResult(
            results=(), page=page, zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
            parse_findings=("linkedin: response had no recognizable job cards",),
        )


__all__ = ["LinkedInGuestAdapter"]
