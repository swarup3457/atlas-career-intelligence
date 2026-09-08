"""Phase 1C-A CORRECTIVE gate — hostile ATS host/token validation and HTTP
transport hardening (build spec 16/17).

Failing-first adversarial regressions: before this gate the extract_* parsers
used SUBSTRING host tests (accepting look-alikes such as evilgreenhouse.io), the
client decompressed the whole body at once (a decompression bomb), never
rejected an HTTPS→HTTP downgrade, carried a POST body / Origin across a
cross-origin redirect, allowed arbitrary methods, and silently ignored
verify_tls=False.
"""

from __future__ import annotations

import zlib

import pytest

from atlas.models import ErrorCategory
from atlas.sources.ats.base import (
    extract_ashby_board_name,
    extract_greenhouse_board_token,
    extract_lever_site,
    host_trusted_for,
    parse_workday_url,
    validate_path_token,
)
from atlas.sources.http_client import (
    HttpError,
    HttpRequest,
    ReadOnlyHttpClient,
    _Headers,
    _RawResponse,
    is_workday_cxs_search,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# §16 — hostile ATS host / token validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fn,url",
    [
        (extract_greenhouse_board_token, "https://evilgreenhouse.io/acme"),
        (extract_greenhouse_board_token, "https://greenhouse.io.evil.example/acme"),
        (extract_greenhouse_board_token, "https://boards.greenhouse.io.evil.example/acme"),
        (extract_lever_site, "https://lever.co.evil.example/acme"),
        (extract_lever_site, "https://evillever.co/acme"),
        (extract_ashby_board_name, "https://notashbyhq.com/acme"),
        (extract_ashby_board_name, "https://ashbyhq.com.evil.example/acme"),
        (parse_workday_url, "https://acme.wd1.myworkdayjobs.com.evil.example/en-US/External"),
        (parse_workday_url, "https://acme.wd1.notmyworkdayjobs.com/en-US/External"),
    ],
)
def test_lookalike_ats_hosts_are_rejected(fn, url):
    with pytest.raises(ValueError):
        fn(url)


@pytest.mark.parametrize(
    "url,token",
    [
        ("https://boards.greenhouse.io/acme", "acme"),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "acme"),
    ],
)
def test_legit_greenhouse_hosts_accepted(url, token):
    assert extract_greenhouse_board_token(url) == token


def test_lever_eu_and_us_hosts_accepted():
    assert extract_lever_site("https://jobs.lever.co/initech")[1].endswith("api.lever.co/v0/postings")
    assert extract_lever_site("https://jobs.eu.lever.co/initech")[1].endswith("api.eu.lever.co/v0/postings")


def test_host_trusted_for_exact_suffix():
    assert host_trusted_for("boards.greenhouse.io", "greenhouse.io")
    assert host_trusted_for("greenhouse.io", "greenhouse.io")
    assert not host_trusted_for("evilgreenhouse.io", "greenhouse.io")
    assert not host_trusted_for("greenhouse.io.evil.example", "greenhouse.io")


@pytest.mark.parametrize("bad", ["../etc", "a/b", "a\\b", "a%2e%2e", "a\x00b", "", "..", "a b"])
def test_hostile_path_tokens_rejected(bad):
    with pytest.raises(ValueError):
        validate_path_token(bad, kind="test")


def test_valid_path_tokens_accepted():
    for good in ["acme", "acme-india", "Acme_2024", "a.b~c"]:
        assert validate_path_token(good, kind="test") == good


def test_workday_external_path_traversal_rejected():
    wd = parse_workday_url("https://acme.wd5.myworkdayjobs.com/en-US/External")
    for bad in ["/../../secret", "//evil.example/x", "/a/..%2f", "/job\\x", "/http:evil"]:
        with pytest.raises(ValueError):
            wd.cxs_detail_url(bad)
    # A legitimate multi-segment external path is allowed.
    assert wd.cxs_detail_url("/job/BLR/Java_R1").endswith("/wday/cxs/acme/External/job/BLR/Java_R1")


# ---------------------------------------------------------------------------
# §17 — HTTP transport hardening
# ---------------------------------------------------------------------------
class _OneShot(ReadOnlyHttpClient):
    """Client whose single low-level exchange is a fixed raw response."""

    def __init__(self, raw, **kw):
        super().__init__(**kw)
        self._raw = raw
        self.calls = []

    def _open_raw(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        return self._raw


class _Scripted(ReadOnlyHttpClient):
    """Client whose low-level exchanges are scripted so redirect behavior (and
    the exact headers/body sent at each hop) can be asserted."""

    def __init__(self, script, **kw):
        super().__init__(**kw)
        self._script = list(script)
        self.calls = []

    def _open_raw(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        return self._script.pop(0)


def _raw(status, *, headers=None, body=b"", url="https://x.local/"):
    return _RawResponse(status, _Headers((headers or {}).items()), body, url)


def test_verify_tls_false_is_rejected():
    with pytest.raises(ValueError):
        ReadOnlyHttpClient(verify_tls=False)


@pytest.mark.parametrize("encoding,wbits", [("gzip", 16 + zlib.MAX_WBITS), ("deflate", zlib.MAX_WBITS)])
def test_decompression_bomb_is_rejected(encoding, wbits):
    co = zlib.compressobj(9, zlib.DEFLATED, wbits)
    bomb = co.compress(b"A" * 8_000_000) + co.flush()  # ~8MB from a tiny payload
    client = _OneShot(_raw(200, headers={"Content-Encoding": encoding}, body=bomb), max_response_bytes=4096)
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://x.local/"))
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_small_gzip_still_decodes():
    import gzip
    body = gzip.compress(b'{"ok": true}')
    client = _OneShot(_raw(200, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"}, body=body))
    resp = client.fetch(HttpRequest("https://x.local/"))
    assert resp.json() == {"ok": True}


def test_unsupported_content_encoding_rejected():
    client = _OneShot(_raw(200, headers={"Content-Encoding": "br"}, body=b"\x00\x01"))
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://x.local/"))
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_https_to_http_downgrade_redirect_rejected():
    client = _Scripted([_raw(302, headers={"Location": "http://x.local/insecure"})])
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://x.local/"))
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_redirect_to_non_approved_host_rejected():
    client = _Scripted([_raw(302, headers={"Location": "https://evil.example/x"})])
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://acme.myworkdayjobs.com/"))
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_cross_origin_redirect_drops_body_and_strips_origin_headers():
    # Explicitly allow the cross-origin host, then assert the body is dropped and
    # Origin/Referer/Authorization are stripped on the second hop.
    client = _Scripted(
        [
            _raw(307, headers={"Location": "https://other.example/next"}, url="https://acme.myworkdayjobs.com/wday/cxs/acme/x/jobs"),
            _raw(200, body=b"{}", url="https://other.example/next"),
        ],
        redirect_host_allowlist=frozenset({"other.example"}),
    )
    resp = client.fetch(
        HttpRequest(
            "https://acme.myworkdayjobs.com/wday/cxs/acme/x/jobs",
            method="POST",
            body=b'{"appliedFacets":{}}',
            headers={"Origin": "https://acme.myworkdayjobs.com", "Referer": "https://acme.myworkdayjobs.com/x",
                     "Authorization": "Bearer secret"},
        )
    )
    assert resp.status == 200
    second = client.calls[1]
    assert second["method"] == "GET"  # cross-origin downgraded to GET
    assert second["body"] is None  # POST body never carried to another host
    lowered = {k.lower() for k in second["headers"]}
    assert "origin" not in lowered and "referer" not in lowered and "authorization" not in lowered


def test_same_origin_redirect_keeps_host_and_follows():
    client = _Scripted(
        [_raw(302, headers={"Location": "https://x.local/final"}), _raw(200, body=b"{}", url="https://x.local/final")]
    )
    resp = client.fetch(HttpRequest("https://x.local/"))
    assert resp.status == 200 and resp.redirects == 1


def test_only_get_and_workday_cxs_post_allowed():
    client = ReadOnlyHttpClient()
    with pytest.raises(HttpError) as exc:
        client.fetch(HttpRequest("https://x.local/", method="PUT"))
    assert exc.value.category == ErrorCategory.CONFIG_ERROR
    with pytest.raises(HttpError):
        client.fetch(HttpRequest("https://x.local/", method="POST", body=b"{}"))  # non-Workday POST
    assert is_workday_cxs_search("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs")
    assert not is_workday_cxs_search("https://acme.wd5.myworkdayjobs.com/en-US/External")


def test_workday_cxs_post_is_allowed_through_fetch():
    client = _OneShot(_raw(200, body=b'{"total":0,"jobPostings":[]}', headers={"Content-Type": "application/json"},
                           url="https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"))
    resp = client.fetch(HttpRequest("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs",
                                    method="POST", body=b"{}"))
    assert resp.ok and client.calls[0]["method"] == "POST"
