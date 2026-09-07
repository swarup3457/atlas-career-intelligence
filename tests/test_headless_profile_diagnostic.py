"""Diagnostic (foundation build): does headless=True launch_persistent_context
safely reuse the authenticated Atlas Chrome profile without corrupting or
logging out the session?

Safe, read-only session-state detection ONLY:
    - navigate to LinkedIn's homepage
    - read title/final URL
    - do NOT scrape, do NOT log in, do NOT touch cookies/credentials

Also proves BrowserManager's profile-lock mechanism: a second concurrent
launch attempt against the same profile directory must be refused rather
than silently starting a second Chrome process against the same
user-data-dir.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\Atlas")))

from atlas.browser.manager import BrowserManager, ProfileLockedError  # noqa: E402

PROFILE_DIR = Path(r"C:\Atlas\.browser-profile-chrome")
LINKEDIN_URL = "https://www.linkedin.com/"


def main() -> int:
    print("=== Step 1: headless=True launch against the authenticated Atlas profile ===")
    manager = BrowserManager(PROFILE_DIR, channel="chrome")
    try:
        page = manager.launch(headless=True)
        print(f"[PASS] Persistent context launched headless=True (is_headless={manager.is_headless})")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] Could not launch headless persistent context: {exc}")
        return 1

    try:
        state = manager.check_session_state(page, LINKEDIN_URL)
        print(f"[INFO] Session state probe: {state}")
        limitation = manager.detect_access_limitation(page)
        print(f"[INFO] Access-limitation detected: {limitation}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] Session-state probe raised: {exc}")
        manager.close()
        return 1

    print("\n=== Step 2: concurrent second launch against same profile must be refused ===")
    second_manager = BrowserManager(PROFILE_DIR, channel="chrome")
    try:
        second_manager.launch(headless=True)
        print("[FAIL] Second concurrent launch unexpectedly succeeded (profile lock did not work).")
        second_manager.close()
        manager.close()
        return 1
    except ProfileLockedError as exc:
        print(f"[PASS] Second concurrent launch correctly refused: {exc}")

    print("\n=== Step 3: close and relaunch headless=True again (verify no corruption) ===")
    manager.close()
    try:
        manager2 = BrowserManager(PROFILE_DIR, channel="chrome")
        page2 = manager2.launch(headless=True)
        state2 = manager2.check_session_state(page2, LINKEDIN_URL)
        print(f"[PASS] Relaunch after close succeeded. Session state: {state2}")
        same_title = state2.get("title") == state.get("title")
        print(f"[INFO] Title matches previous headless run: {same_title}")
        manager2.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] Relaunch after close raised: {exc}")
        return 1

    print("\nHEADLESS_PROFILE_DIAGNOSTIC=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
