"""Agentic V4 — explicit status model + challenge/auth classification (prompt s.10, s.3.4, s.3.5)."""

from __future__ import annotations

from atlas.pilot.status_v4 import (
    AccessSignals,
    CompanySearchStatus,
    EXTERNAL_BLOCKER,
    GENUINELY_SEARCHED,
    INTERNAL_RETRYABLE,
    SEARCH_SUCCESS,
    TERMINAL,
    classify_blocker,
    is_genuinely_searched,
    is_internal_retryable,
    is_terminal,
    looks_like_login_wall,
    map_legacy_status,
)


# --- s.3.4 challenge / auth classification --------------------------------
def test_visible_signin_link_is_not_auth_wall():
    sig = AccessSignals(signin_link_visible=True, content_visible=True)
    assert classify_blocker(sig) is None


def test_http_403_is_access_limited_not_auth():
    sig = AccessSignals(http_status=403)
    assert classify_blocker(sig) == CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value


def test_http_403_with_confirmed_login_wall_is_auth():
    sig = AccessSignals(http_status=403, login_wall_confirmed=True)
    assert classify_blocker(sig) == CompanySearchStatus.AUTH_REQUIRED_CONFIRMED.value


def test_tool_exception_is_retryable_browser_error():
    sig = AccessSignals(tool_exception="PlaywrightTimeout")
    status = classify_blocker(sig)
    assert status == CompanySearchStatus.BROWSER_TOOL_ERROR.value
    assert is_internal_retryable(status)
    assert not is_terminal(status)


def test_async_runtime_error_is_retryable():
    sig = AccessSignals(async_runtime_error=True)
    status = classify_blocker(sig)
    assert status == CompanySearchStatus.ASYNC_RUNTIME_ERROR.value
    assert is_internal_retryable(status)


def test_login_wall_requires_password_and_no_content():
    assert looks_like_login_wall(password_field_present=True, job_content_visible=False) is True
    # a header "Sign in" link on a populated results page is NOT a wall
    assert looks_like_login_wall(
        password_field_present=False, job_content_visible=True, signin_link_only=True
    ) is False
    assert looks_like_login_wall(password_field_present=True, job_content_visible=True) is False


# --- s.10 classification invariants ---------------------------------------
def test_only_search_success_is_genuinely_searched():
    for s in SEARCH_SUCCESS:
        assert is_genuinely_searched(s)
    for s in EXTERNAL_BLOCKER | INTERNAL_RETRYABLE:
        assert not is_genuinely_searched(s)


def test_internal_failures_are_never_terminal():
    for s in INTERNAL_RETRYABLE:
        assert not is_terminal(s), s
    for s in TERMINAL:
        assert is_terminal(s)


def test_external_blocker_terminal_but_not_searched():
    for s in EXTERNAL_BLOCKER:
        assert is_terminal(s)
        assert not is_genuinely_searched(s)


# --- s.3.5 a browser error can never count as searched --------------------
def test_browser_error_not_searched():
    status = classify_blocker(AccessSignals(tool_exception="X"))
    assert not is_genuinely_searched(status)
    assert not is_terminal(status)


# --- legacy interop --------------------------------------------------------
def test_legacy_unsupported_site_becomes_retryable():
    assert map_legacy_status("UNSUPPORTED_SITE") == CompanySearchStatus.BROWSER_TOOL_ERROR.value
    assert is_internal_retryable(map_legacy_status("UNSUPPORTED_SITE"))


def test_legacy_complete_maps_by_matches():
    assert map_legacy_status("COMPLETE", has_matches=True) == \
        CompanySearchStatus.SEARCHED_COMPLETE_WITH_MATCHES.value
    assert map_legacy_status("COMPLETE", has_matches=False) == \
        CompanySearchStatus.SEARCHED_COMPLETE_NO_MATCHES.value
