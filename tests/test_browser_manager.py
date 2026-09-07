"""Pytest coverage for atlas.browser.manager (Phase 0.5 spec section 2).

Marked real_web + browser because it launches an actual Chrome browser
and navigates to https://example.com - excluded from the default
`python -m pytest` run (see pyproject.toml addopts), matching the
requirement that the normal unit-test suite never needs internet.

Run explicitly with:
    python -m pytest -m real_web tests/test_browser_manager.py
"""

from __future__ import annotations

import pytest

from atlas.browser.manager import BrowserManager

pytestmark = [pytest.mark.real_web, pytest.mark.browser, pytest.mark.slow]


def test_browser_manager_headless_navigate(tmp_path):
    profile_dir = tmp_path / "throwaway-profile"
    with BrowserManager(profile_dir, channel="chrome") as manager:
        page = manager.launch(headless=True)
        manager.navigate(page, "https://example.com")
        title = page.title()
        assert "Example" in title
        assert manager.is_running
        assert manager.is_headless is True


def test_browser_manager_records_navigation_observability(tmp_path):
    profile_dir = tmp_path / "throwaway-profile-2"
    with BrowserManager(profile_dir, channel="chrome") as manager:
        page = manager.launch(headless=True)
        manager.navigate(page, "https://example.com")
        assert len(manager.navigation_log) == 1
        record = manager.navigation_log[0]
        assert record.domain == "example.com"
        assert record.classification == "OK"
        assert record.duration_ms >= 0
        assert record.title and "Example" in record.title
