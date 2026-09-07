"""Atlas BrowserManager — the single, reusable owner of Chrome lifecycle.

Workers must never launch their own Chrome instances directly. All
browser access flows through this class so that:
    - only one Chrome process ever attaches to the dedicated Atlas
      profile at a time (profile lock prevention)
    - background/headless is the default for ordinary automated runs
    - visible escalation (see atlas/browser/intervention.py) is the only
      sanctioned way a visible window appears
    - navigation retries and access-limitation detection are consistent
      everywhere

See docs/BROWSER_POLICY.md for the full policy and the results of the
headless-vs-authenticated-profile diagnostic that informed these defaults.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from atlas.utils.pidlock import LockHeldError, PidLock

logger = logging.getLogger("atlas.browser.manager")

# Signals that indicate the site is deliberately blocking automated
# access (CAPTCHA / anti-bot / corporate policy block). Detecting these
# must lead to ACCESS_LIMITED classification, never a bypass attempt.
ACCESS_LIMITATION_SIGNALS = [
    "captcha",
    "are you a human",
    "verify you are a human",
    "verify you're human",
    "unusual traffic",
    "access denied",
    "access to this page has been denied",
    "just a moment",
    "checking your browser",
    "perimeterx",
    "please enable javascript and cookies",
    "bot detection",
    "zscaler",
    "blocked by policy",
    "forbidden",
]

# A lock file inside the profile directory itself prevents two Atlas
# BrowserManager instances (in this or another process) from attaching
# Chrome to the same user-data-dir concurrently, which Chrome does not
# support safely. Hardened in Phase 0.5 to be PID-aware (see
# atlas.utils.pidlock.PidLock) rather than purely time-based.
_LOCK_FILENAME = ".atlas-browser-manager.lock"


class ProfileLockedError(RuntimeError):
    """Raised when another process already holds the Atlas profile lock."""


@dataclass
class NavigationRecord:
    """Safe, non-sensitive browser observability record for one navigation.

    Never includes cookies/tokens/passwords/authorization headers/form
    data - only page-level metadata.
    """

    domain: str
    url: str
    final_url: Optional[str]
    duration_ms: Optional[float]
    http_status: Optional[int]
    title: Optional[str]
    classification: str  # e.g. "OK", "ACCESS_LIMITED", "ERROR"
    error: Optional[str] = None


class BrowserManager:
    """Owns one Playwright + Chrome persistent-context lifecycle.

    Usage:
        manager = BrowserManager(profile_dir, channel="chrome")
        page = manager.launch(headless=True)
        ... use page ...
        manager.close()

    Or as a context manager:
        with BrowserManager(profile_dir) as manager:
            page = manager.launch(headless=True)
            ...
    """

    def __init__(
        self,
        profile_dir: Path,
        channel: str = "chrome",
        navigation_timeout_ms: int = 30000,
    ):
        self.profile_dir = Path(profile_dir)
        self.channel = channel
        self.navigation_timeout_ms = navigation_timeout_ms

        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._headless: Optional[bool] = None
        self._lock = PidLock(self.profile_dir / _LOCK_FILENAME)
        self.navigation_log: list[NavigationRecord] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _acquire_lock(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(metadata={"channel": self.channel})
        except LockHeldError as exc:
            raise ProfileLockedError(
                f"Atlas browser profile at {self.profile_dir} is in use by another "
                f"live process (PID {exc.info.pid}, acquired_at={exc.info.acquired_at}). "
                "Refusing to launch a second Chrome instance against the same "
                "user-data-dir."
            ) from exc

    def _release_lock(self) -> None:
        with contextlib.suppress(Exception):
            self._lock.release()

    def launch(self, headless: bool = True) -> Page:
        """Launch (or return the existing) persistent Chrome context and a
        ready page. Raises ProfileLockedError if another process already
        holds the profile lock."""
        if self._context is not None:
            if headless != self._headless:
                raise RuntimeError(
                    "BrowserManager already launched with a different headless "
                    "mode. Call close() before relaunching with a different mode "
                    "(see docs/BROWSER_POLICY.md for the visible-escalation flow)."
                )
            return self._context.pages[0] if self._context.pages else self._context.new_page()

        self._acquire_lock()
        try:
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel=self.channel,
                headless=headless,
            )
            self._headless = headless
            page = self._context.pages[0] if self._context.pages else self._context.new_page()
            page.set_default_navigation_timeout(self.navigation_timeout_ms)
            page.set_default_timeout(self.navigation_timeout_ms)
            return page
        except Exception:
            self._release_lock()
            raise

    def close(self) -> None:
        if self._context is not None:
            with contextlib.suppress(Exception):
                self._context.close()
            self._context = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                self._playwright.stop()
            self._playwright = None
        self._headless = None
        self._release_lock()

    def __enter__(self) -> "BrowserManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def is_running(self) -> bool:
        return self._context is not None

    @property
    def is_headless(self) -> Optional[bool]:
        return self._headless

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------
    def navigate(self, page: Page, url: str, timeout_ms: Optional[int] = None):
        """Navigate with the manager's default timeout; raises on failure
        (caller/worker is responsible for retry-policy classification).

        Records a safe NavigationRecord (domain, duration, HTTP status,
        final URL, title, terminal classification) in
        ``self.navigation_log`` for observability. NEVER records cookies,
        tokens, passwords, authorization headers, or form data.
        """
        domain = urlparse(url).netloc
        started = time.monotonic()
        try:
            response = page.goto(url, wait_until="load", timeout=timeout_ms or self.navigation_timeout_ms)
            duration_ms = (time.monotonic() - started) * 1000
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                title = None
            limitation = self.detect_access_limitation(page)
            classification = "ACCESS_LIMITED" if limitation else "OK"
            self.navigation_log.append(
                NavigationRecord(
                    domain=domain,
                    url=url,
                    final_url=page.url,
                    duration_ms=duration_ms,
                    http_status=response.status if response is not None else None,
                    title=title,
                    classification=classification,
                )
            )
            return response
        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.monotonic() - started) * 1000
            self.navigation_log.append(
                NavigationRecord(
                    domain=domain,
                    url=url,
                    final_url=None,
                    duration_ms=duration_ms,
                    http_status=None,
                    title=None,
                    classification="ERROR",
                    error=str(exc),
                )
            )
            raise

    def detect_access_limitation(self, page: Page) -> Optional[str]:
        """Return a human-readable reason string if the current page shows
        signs of a deliberate access-limitation control, else None."""
        try:
            title = (page.title() or "").lower()
        except Exception:  # noqa: BLE001
            title = ""
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:  # noqa: BLE001
            body = ""
        haystack = f"{title}\n{body}"
        for signal in ACCESS_LIMITATION_SIGNALS:
            if signal in haystack:
                return f"Detected access-limitation signal: '{signal}'"
        return None

    def check_session_state(self, page: Page, url: str) -> dict:
        """Safe, read-only session-state probe: navigate to url and report
        title/final-url/status without touching cookies/credentials."""
        response = self.navigate(page, url)
        return {
            "final_url": page.url,
            "title": page.title(),
            "status": response.status if response is not None else None,
        }
