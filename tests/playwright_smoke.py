"""Atlas Career Intelligence - Playwright Smoke Test.

Purpose:
    Prove that Playwright browser automation works correctly on this
    Windows machine using a dedicated, persistent Atlas browser profile.

This script intentionally does NOT implement any Atlas business logic
(no job search, no orchestration, no GitHub persistence, no Excel, no
agents). It only verifies that:

    1. A visible (headless=False) persistent browser context launches.
    2. Basic navigation / DOM reading works against https://example.com.
    3. The browser can then be pointed at https://www.linkedin.com/ so a
       human can OPTIONALLY sign in manually to establish a reusable
       session in the dedicated Atlas browser profile.

No credentials are ever read, stored, or typed by this script. Any
LinkedIn session cookies that result from a manual sign-in are persisted
only inside the C:\\Atlas\\.browser-profile directory (a Chromium user
data dir), never in source code or in this script.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# Dedicated, persistent Atlas browser profile (Chromium user data dir).
# This directory holds cookies / session state only - never source code.
PROFILE_DIR = Path(r"C:\Atlas\.browser-profile")

EXAMPLE_URL = "https://example.com"
LINKEDIN_URL = "https://www.linkedin.com/"


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def run_smoke_test() -> bool:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    overall_ok = True

    with sync_playwright() as p:
        print(f"Playwright version: {p.chromium.__class__.__module__}")
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            channel=None,  # use the bundled Chromium that ships with Playwright
        )

        try:
            page = context.pages[0] if context.pages else context.new_page()

            # --- Basic browser sanity test against https://example.com ---
            print(f"\n=== Basic sanity test: {EXAMPLE_URL} ===")

            try:
                response = page.goto(EXAMPLE_URL, wait_until="load", timeout=30000)
                if response is not None and response.ok:
                    _pass(f"Navigation succeeded (HTTP {response.status})")
                elif response is not None:
                    _fail(f"Navigation returned HTTP {response.status}")
                    overall_ok = False
                else:
                    _pass("Navigation completed (no Response object, but no exception raised)")
            except Exception as exc:  # noqa: BLE001
                _fail(f"Navigation raised an exception: {exc}")
                overall_ok = False

            try:
                title = page.title()
                if title:
                    _pass(f"Page title read: '{title}'")
                else:
                    _fail("Page title was empty")
                    overall_ok = False
            except Exception as exc:  # noqa: BLE001
                _fail(f"Reading page title raised an exception: {exc}")
                overall_ok = False

            try:
                body_text = page.inner_text("body")
                if body_text and len(body_text.strip()) > 0:
                    snippet = body_text.strip().replace("\n", " ")[:80]
                    _pass(f"Page text read ({len(body_text)} chars): '{snippet}...'")
                else:
                    _fail("Page text was empty")
                    overall_ok = False
            except Exception as exc:  # noqa: BLE001
                _fail(f"Reading page text raised an exception: {exc}")
                overall_ok = False

            try:
                links = page.eval_on_selector_all(
                    "a[href]", "elements => elements.map(e => e.href)"
                )
                _pass(f"Links extracted: {len(links)} found -> {links}")
            except Exception as exc:  # noqa: BLE001
                _fail(f"Extracting links raised an exception: {exc}")
                overall_ok = False
                links = []

            if not overall_ok:
                print("\nBasic sanity test FAILED. Skipping LinkedIn step.")
                return overall_ok

            print("\nBasic sanity test PASSED.")

            # --- Open LinkedIn for OPTIONAL manual sign-in ---
            # This step is best-effort: network-level security controls (e.g. a
            # corporate proxy/firewall) may block LinkedIn entirely. That is a
            # network/environment limitation, not a Playwright or code failure,
            # so it does NOT flip the overall smoke-test result to FAIL. We do
            # not attempt to bypass any such security control.
            print(f"\n=== Opening {LINKEDIN_URL} (manual sign-in optional) ===")
            linkedin_opened = False
            try:
                page.goto(LINKEDIN_URL, wait_until="load", timeout=30000)
                _pass("LinkedIn page opened.")
                linkedin_opened = True
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[INFO] Could not open LinkedIn: {exc}\n"
                    "        This looks like a network-level security control "
                    "(e.g. corporate proxy/firewall) blocking the site, not a "
                    "Playwright or code problem. Not attempting to bypass it."
                )

            if linkedin_opened:
                print(
                    "\nA visible Chromium window is now open at LinkedIn.\n"
                    "This script does NOT automate login and will NOT enter any\n"
                    "credentials or scrape any data.\n\n"
                    "If you want to establish a reusable Atlas browser session,\n"
                    "you may sign in to LinkedIn MANUALLY in the visible window now.\n"
                    "Your session/cookies will be saved only in:\n"
                    f"  {PROFILE_DIR}\n\n"
                    "Press ENTER in this terminal when you are done (or to skip)\n"
                    "and the browser will close."
                )
            else:
                print(
                    "\nLeaving the visible Chromium window open in case you want to\n"
                    "navigate manually. Press ENTER in this terminal to close it."
                )
            try:
                input(">>> Press ENTER to close the browser... ")
            except EOFError:
                # Non-interactive environment; do not block forever.
                print("(No interactive input available - closing automatically.)")

        finally:
            context.close()

    return overall_ok


if __name__ == "__main__":
    result = run_smoke_test()
    print("\n=== SMOKE TEST RESULT ===")
    print("PASS" if result else "FAIL")
    sys.exit(0 if result else 1)
