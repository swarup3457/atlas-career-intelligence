"""Generic official-career BROWSER adapter (Phase 1C-B, build spec 9).

ONE adapter that drives the shared Atlas :class:`atlas.browser.manager.BrowserManager`
(installed Chrome, headless by default, a DEDICATED gitignored public-careers
profile) to render an official JavaScript/SPA career page, then extracts jobs
from the RENDERED DOM with the SAME dependency-free primitives the HTTP adapter
uses. There is no per-company Python — the interaction plan (search box, load
more, scroll, job-card selector) is DATA carried on the instance/recipe.

Strict safety posture (build spec 9/17):

    * headless/background by default — a visible window only via explicit human
      escalation, never automatically;
    * interacts ONLY with public job-search UI (search input, submit, load-more,
      bounded scroll, public job cards);
    * NEVER signs in, creates an account, solves a CAPTCHA, bypasses a challenge,
      submits an application, or clicks a final Apply;
    * a detected challenge/login/anti-bot state is CLASSIFIED (ACCESS_LIMITED /
      AUTH_REQUIRED), never bypassed;
    * bounded pagination/load-more/scroll cycles; bounded cards; no stealth.

One :meth:`search` call == one bounded rendered session. The adapter declares no
PAGINATION capability (it collects a whole bounded page in one session), so it
runs through the coverage executor's single-acquisition path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from atlas.careers import extract as X
from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, SourceAdapter, new_result_base
from atlas.sources.ats.base import work_mode_from_text
from atlas.sources.health import SourceHealth, SourceHealthState
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
from atlas.sources.parsing import parse_isolated

# Safe, commonly-correct cookie-consent buttons (dismissing a consent banner is
# not a login/challenge bypass).
_CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept All')",
    "button:has-text('Accept all')",
    "button:has-text('I Accept')",
    "button:has-text('Agree')",
)

_SEARCH_INPUT_SELECTORS = (
    "input[placeholder*='job' i]",
    "input[placeholder*='keyword' i]",
    "input[aria-label*='search' i]",
    "input[name*='query' i]",
    "input[name='search']",
    "input[type='search']",
)

_LOAD_MORE_SELECTORS = (
    "button:has-text('Load more')",
    "button:has-text('Show more')",
    "button:has-text('View more')",
    "a:has-text('Load more')",
    "[data-automation*='load-more' i]",
)


class GenericCareerBrowserAdapter(SourceAdapter):
    """Read-only generic official career-site adapter (browser/SPA route)."""

    source_type = SourceType.COMPANY_CAREER
    source_family = SourceFamily.COMPANY_CAREER_BROWSER
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.DESCRIPTION,
            Capability.BROWSER_REQUIRED,
            Capability.KEYWORD_FILTER,
            Capability.LOCATION_FILTER,
        }
    )
    adapter_version = "career-browser-1.0.0"
    parser_version = X.PARSER_VERSION
    concurrency_class = ConcurrencyClass.BROWSER_ANONYMOUS

    def __init__(self, instance: SourceInstance, *, browser_manager=None, headless: bool = True):
        super().__init__(instance)
        md = instance.metadata or {}
        self.entry_url = md.get("entry_url") or instance.base_url
        if not self.entry_url:
            raise AdapterError(
                ErrorCategory.CONFIG_ERROR,
                f"GenericCareerBrowserAdapter instance {instance.instance_id!r} has no entry_url",
            )
        self._injected_manager = browser_manager
        self.headless = bool(md.get("headless", headless))
        self.profile_dir = Path(md.get("profile_dir") or ".browser-profile-careers")
        self.same_host_only = bool(md.get("same_host_only", True))
        self.max_cycles = int(md.get("max_load_cycles", 2))
        self.wait_ms = int(md.get("render_wait_ms", 2500))
        self.max_results = int(md.get("max_results", X.MAX_JOBS_PER_PAGE))
        # DATA-driven interaction plan (from the recipe); never per-company code.
        recipe = md.get("recipe") or {}
        self.search_selector = recipe.get("search_selector")
        self.submit_selector = recipe.get("submit_selector")
        self.load_more_selector = recipe.get("load_more_selector")
        self.job_card_selector = recipe.get("job_card_selector")

    # -- browser lifecycle --------------------------------------------------
    def _manager(self):
        if self._injected_manager is not None:
            return self._injected_manager
        from atlas.browser.manager import BrowserManager

        return BrowserManager(self.profile_dir, channel="chrome")

    # -- normalization ------------------------------------------------------
    def _to_result(self, job: X.ExtractedJob) -> DiscoveryResult:
        provenance = {
            "source_family": "company_career_browser",
            "extraction_method": job.extraction_method,
            "date_provenance": job.date_provenance,
            "entry_url": self.entry_url,
            "rendered": True,
        }
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
                description=None,
                is_active=ActiveState.ACTIVE,
                verification_level=VerificationLevel.OFFICIAL_SEARCH_LIVE,
                confidence=0.7,
                provenance=provenance,
            )
        )

    # -- interaction helpers (all best-effort, never fatal) -----------------
    def _dismiss_consent(self, page) -> None:
        for sel in _CONSENT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=800):
                    btn.click(timeout=1200)
                    page.wait_for_timeout(300)
                    return
            except Exception:  # noqa: BLE001
                continue

    def _run_search(self, page, query: Optional[str]) -> bool:
        if not query:
            return False
        selectors = ([self.search_selector] if self.search_selector else []) + list(_SEARCH_INPUT_SELECTORS)
        for sel in selectors:
            if not sel:
                continue
            try:
                inp = page.locator(sel).first
                if inp.count() == 0 or not inp.is_visible(timeout=800):
                    continue
                inp.click(timeout=1500)
                inp.fill(query, timeout=1500)
                if self.submit_selector:
                    try:
                        page.locator(self.submit_selector).first.click(timeout=1500)
                    except Exception:  # noqa: BLE001
                        inp.press("Enter", timeout=1500)
                else:
                    inp.press("Enter", timeout=1500)
                page.wait_for_timeout(self.wait_ms)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _load_more_and_scroll(self, page) -> int:
        cycles = 0
        selectors = ([self.load_more_selector] if self.load_more_selector else []) + list(_LOAD_MORE_SELECTORS)
        for _ in range(self.max_cycles):
            clicked = False
            for sel in selectors:
                if not sel:
                    continue
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible(timeout=600):
                        btn.click(timeout=1500)
                        page.wait_for_timeout(self.wait_ms)
                        clicked = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked:
                # Bounded infinite-scroll fallback.
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(min(self.wait_ms, 1500))
                except Exception:  # noqa: BLE001
                    break
            cycles += 1
        return cycles

    def _cards_via_selector(self, page, base_url: str) -> list[X.ExtractedJob]:
        if not self.job_card_selector:
            return []
        try:
            raw = page.eval_on_selector_all(
                self.job_card_selector,
                """els => els.map(e => {
                    const a = e.matches('a[href]') ? e : e.querySelector('a[href]');
                    const title = a ? (a.innerText || a.textContent || '').trim()
                                    : (e.innerText || '').trim().split('\\n')[0];
                    return {t: title, h: a ? a.href : ''};
                })""",
            )
        except Exception:  # noqa: BLE001
            return []
        out: list[X.ExtractedJob] = []
        seen: set[str] = set()
        for item in raw or []:
            href = (item.get("h") or "").strip()
            title = (item.get("t") or "").strip()
            url = X.normalize_url(base_url, href)
            if not url or not title or len(title) < 3:
                continue
            if self.same_host_only and not X.same_site(base_url, url):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(X.ExtractedJob(title=title[:200], url=url, extraction_method="browser_card"))
            if len(out) >= X.MAX_JOBS_PER_PAGE:
                break
        return out

    def _extract_rendered(self, page, base_url: str) -> tuple[list[X.ExtractedJob], str]:
        try:
            html = page.content()
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(ErrorCategory.SELECTOR_UNCERTAINTY, f"could not read rendered DOM: {exc}") from exc
        jobs = X.extract_jsonld_jobs(html, base_url=base_url)
        if jobs:
            return jobs, "jsonld"
        cards = self._cards_via_selector(page, base_url)
        if cards:
            return cards, "browser_card"
        jobs = X.extract_embedded_jobs(html, base_url=base_url)
        if jobs:
            return jobs, "embedded_json"
        jobs = X.extract_job_links(html, base_url=base_url, same_host_only=self.same_host_only)
        if jobs:
            return jobs, "anchor"
        return [], "none"

    # -- operations ---------------------------------------------------------
    def _session(self, query: Optional[str]) -> tuple[list[X.ExtractedJob], str, dict]:
        """Run one bounded rendered session. Returns (jobs, method, diagnostics).
        Raises AdapterError on navigation failure or a detected challenge/login."""
        manager = self._manager()
        owns = self._injected_manager is None
        diagnostics: dict[str, Any] = {}
        try:
            page = manager.launch(headless=self.headless)
            try:
                response = manager.navigate(page, self.entry_url)
            except Exception as exc:  # noqa: BLE001
                raise AdapterError(ErrorCategory.TRANSIENT_NAVIGATION, f"navigation error: {exc}") from exc
            status = response.status if response is not None else None
            limitation = manager.detect_access_limitation(page)
            if limitation or (status is not None and status in (401, 403, 429, 503)):
                cat = ErrorCategory.ANTI_BOT
                if status == 401:
                    cat = ErrorCategory.LOGIN_WALL
                elif status == 429:
                    cat = ErrorCategory.HTTP_429
                raise AdapterError(cat, limitation or f"HTTP {status} from career site (no bypass)")
            self._dismiss_consent(page)
            diagnostics["searched"] = self._run_search(page, query)
            diagnostics["load_cycles"] = self._load_more_and_scroll(page)
            # Re-check for a challenge that appeared after interaction.
            post_limit = manager.detect_access_limitation(page)
            if post_limit:
                raise AdapterError(ErrorCategory.ANTI_BOT, post_limit)
            base_url = getattr(page, "url", self.entry_url) or self.entry_url
            jobs, method = self._extract_rendered(page, base_url)
            diagnostics["final_url"] = base_url
            diagnostics["method"] = method
            return jobs, method, diagnostics
        finally:
            if owns:
                manager.close()

    def health_check(self) -> SourceHealth:
        try:
            jobs, _method, _diag = self._session(None)
        except AdapterError as exc:
            state = {
                ErrorCategory.ANTI_BOT: SourceHealthState.ACCESS_LIMITED,
                ErrorCategory.LOGIN_WALL: SourceHealthState.AUTH_REQUIRED,
                ErrorCategory.HTTP_429: SourceHealthState.RATE_LIMITED,
                ErrorCategory.TRANSIENT_NAVIGATION: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.SELECTOR_UNCERTAINTY: SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
            }.get(exc.category, SourceHealthState.UNKNOWN)
            return SourceHealth(state, f"career browser probe: {exc.message}")
        if jobs:
            return SourceHealth(SourceHealthState.HEALTHY, f"rendered {len(jobs)} job card(s)")
        return SourceHealth(SourceHealthState.SELECTOR_DRIFT_SUSPECTED, "rendered page but no job cards extracted")

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        jobs, method, diag = self._session(request.query)
        parsed = parse_isolated(jobs, self._to_result)
        results = tuple(parsed.results)[: self.max_results]
        if results:
            return SearchResult(
                results=results, page=1, has_more=False,
                zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
                parse_findings=tuple(parsed.finding_reasons()),
            )
        # Rendered but nothing extractable: a browser route that yields nothing is
        # NEVER a trusted zero — it is unresolved so the truth ("we could not
        # extract") is preserved, never "no jobs".
        return SearchResult(
            results=(), page=1, zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED,
            parse_findings=(f"career-browser: rendered ({method}) but no job cards extracted",),
        )

    def fetch_detail(self, request: DetailRequest) -> DiscoveryResult:
        # The browser route surfaces search-list evidence; detail hydration for
        # the browser family is intentionally not supported (no DETAIL capability).
        raise AdapterError(ErrorCategory.CONFIG_ERROR, "career-browser has no detail capability")


__all__ = ["GenericCareerBrowserAdapter"]
