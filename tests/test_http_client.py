"""Phase 1C-A — read-only HTTP transport tests (build spec 9, matrix B).

Uses an overridable ``_open_raw`` seam so the redirect loop, size cap, gzip
decode, Retry-After parsing, content-type/JSON validation, and the
no-hidden-retry guarantee are all exercised with NO socket. The real urllib/TLS
path is additionally exercised by the opt-in real_web canary.
"""

from __future__ import annotations

import datetime
import gzip
import json

import pytest

from atlas.models import ErrorCategory
from atlas.sources.http_client import (
    HttpError,
    HttpRequest,
    HttpResponse,
    ReadOnlyHttpClient,
    parse_retry_after,
)
from atlas.sources.http_client import _Headers, _RawResponse  # noqa: PLC2701

pytestmark = pytest.mark.unit


class ScriptedClient(ReadOnlyHttpClient):
    """A client whose low-level exchange is scripted (no network)."""

    def __init__(self, script, **kw):
        super().__init__(**kw)
        self._script = list(script)
        self.raw_calls = []

    def _open_raw(self, method, url, headers, body, timeout):
        self.raw_calls.append((method, url))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        status, resp_headers, resp_body, final_url = item
        return _RawResponse(status, _Headers((resp_headers or {}).items()), resp_body, final_url or url)


def _raw(status, *, headers=None, body=b"", url="https://x.local/"):
    return (status, headers or {}, body, url)


def test_timeout_maps_to_timeout_category():
    client = ScriptedClient([HttpError(ErrorCategory.TIMEOUT, "timed out")])
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://x.local/"))
    assert exc.value.category == ErrorCategory.TIMEOUT
    assert len(client.raw_calls) == 1  # exactly one attempt — no hidden retry


def test_404_returned_as_response_not_error():
    client = ScriptedClient([_raw(404, body=b"nope")])
    resp = client.fetch(HttpRequest("https://x.local/"))
    assert resp.status == 404 and not resp.ok


def test_429_numeric_retry_after():
    client = ScriptedClient([_raw(429, headers={"Retry-After": "120"})])
    resp = client.fetch(HttpRequest("https://x.local/"))
    assert resp.status == 429
    assert resp.retry_after_seconds() == 120.0


def test_429_http_date_retry_after():
    now = datetime.datetime(2099, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    future = "Thu, 01 Jan 2099 00:02:00 GMT"
    client = ScriptedClient([_raw(503, headers={"Retry-After": future})])
    resp = client.fetch(HttpRequest("https://x.local/"))
    assert resp.retry_after_seconds(now=now) == 120.0


def test_parse_retry_after_past_date_clamped_to_zero():
    now = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)
    assert parse_retry_after("Thu, 01 Jan 1990 00:00:00 GMT", now=now) == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_returned_as_response(status):
    client = ScriptedClient([_raw(status)])
    assert client.fetch(HttpRequest("https://x.local/")).status == status


def test_redirect_followed_within_limit():
    client = ScriptedClient(
        [_raw(302, headers={"Location": "https://x.local/final"}), _raw(200, body=b"{}", url="https://x.local/final")],
        max_redirects=3,
    )
    resp = client.fetch(HttpRequest("https://x.local/"))
    assert resp.status == 200 and resp.redirects == 1


def test_redirect_loop_detected():
    client = ScriptedClient(
        [_raw(302, headers={"Location": "https://x.local/"}, url="https://x.local/")],
        max_redirects=5,
    )
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://x.local/"))
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_redirect_limit_exceeded():
    script = [_raw(302, headers={"Location": f"https://x.local/{i+1}"}, url=f"https://x.local/{i}") for i in range(6)]
    client = ScriptedClient(script, max_redirects=2)
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://x.local/0"))
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_oversized_response_rejected():
    big = b"x" * 2048
    client = ScriptedClient([_raw(200, body=big)], max_response_bytes=1024)
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://x.local/"))
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_gzip_body_decoded():
    payload = {"jobs": [1, 2, 3]}
    body = gzip.compress(json.dumps(payload).encode())
    client = ScriptedClient([_raw(200, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"}, body=body)])
    resp = client.fetch(HttpRequest("https://x.local/"))
    assert resp.json() == payload


def test_malformed_json_raises_invalid_response():
    resp = HttpResponse.build(200, body=b"{not json", headers={"Content-Type": "application/json"})
    with pytest.raises(HttpError) as exc:
        resp.json()
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_odd_content_type_json_still_parses():
    resp = HttpResponse.build(200, body=b'{"ok": true}', headers={"Content-Type": "text/plain"})
    assert resp.json() == {"ok": True}


def test_html_masquerade_rejected_as_json():
    resp = HttpResponse.build(200, body=b"<!DOCTYPE html><html><body>Just a moment...</body></html>",
                              headers={"Content-Type": "text/html"})
    assert resp.looks_like_html()
    with pytest.raises(HttpError):
        resp.json()


def test_case_insensitive_headers():
    resp = HttpResponse.build(200, headers={"content-TYPE": "application/json", "RETRY-after": "5"})
    assert resp.content_type == "application/json"
    assert resp.retry_after_seconds() == 5.0


def test_tls_verification_on_and_no_proxy_by_default():
    import ssl
    import urllib.request

    client = ReadOnlyHttpClient()
    assert client.verify_tls is True
    assert client._ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert client._ssl_context.check_hostname is True
    # Passing ProxyHandler({}) disables proxying entirely: urllib drops an
    # empty-proxy handler, so NO proxy handler is active and environment
    # proxies are never consulted (no proxy by default).
    active_proxies = [h for h in client._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert active_proxies == [], "no active proxy handler => env proxies ignored"


def test_request_budget_enforced():
    client = ScriptedClient([_raw(200, body=b"{}"), _raw(200, body=b"{}")], request_budget=1)
    client.fetch(HttpRequest("https://x.local/"))
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://x.local/"))
    assert exc.value.category == ErrorCategory.SOURCE_UNAVAILABLE


def test_no_hidden_retry_on_500():
    client = ScriptedClient([_raw(500), _raw(200, body=b"{}")])
    resp = client.fetch(HttpRequest("https://x.local/"))
    assert resp.status == 500  # returns the first response; never silently retries
    assert len(client.raw_calls) == 1
