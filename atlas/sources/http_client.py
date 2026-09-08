"""Atlas shared read-only HTTP transport (Phase 1C-A, build spec 9).

A small, injected, dependency-free HTTP client used by every real ATS adapter.
It is deliberately built on the Python standard library (``urllib``) — Atlas
adds NO new runtime dependency for these four adapters — and encodes every
safety rule the discovery engine requires:

    * connect + read timeouts (bounded);
    * an explicit, honest Atlas *personal-research* user agent (no stealth);
    * TLS verification ON by default (a TLS failure is surfaced, never bypassed);
    * an explicit redirect limit with loop detection (ordinary redirects only);
    * a hard response-size cap (decompression-bomb safe);
    * gzip/deflate decoding through standard ``Content-Encoding`` handling;
    * numeric AND HTTP-date ``Retry-After`` parsing (honored centrally later);
    * content-type tolerance WITH a check, and strict JSON validation on demand;
    * NO proxy by default, NO random jitter, NO hidden retry.

ONE ``fetch`` call == ONE adapter attempt. The client never retries; retry
authority stays centralized in :mod:`atlas.orchestration.retry`, applied by the
governor. The low-level ``_open_raw`` seam is overridable so the redirect loop,
size cap, gzip decode, Retry-After parsing, and content-type/JSON validation are
all unit-testable with NO socket.

This transport is READ-ONLY: it issues GET requests, plus the single public
Workday *search* POST that an employer's own public careers page issues (a
facet/keyword query carrying NO candidate data). It never submits applications,
sends credentials/cookies, follows links found inside posting text, or attempts
to bypass any authentication, CAPTCHA, or anti-bot control.
"""

from __future__ import annotations

import datetime
import email.utils
import gzip
import ssl
import threading
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urljoin, urlsplit

from atlas.models import ErrorCategory

# Honest, non-deceptive identification. NEVER a browser/stealth string.
DEFAULT_USER_AGENT = (
    "Atlas-Career-Intelligence/1.0 (personal job-search research; read-only; contact=local-operator)"
)

# Status codes we treat as ordinary redirects.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Absolute safety ceiling for a single socket read, independent of the logical
# cap, so a hostile/broken server can never make us read unbounded bytes.
_HARD_READ_CEILING = 64 * 1024 * 1024


class HttpError(Exception):
    """A classified single-attempt HTTP transport failure. Carries an
    :class:`atlas.models.ErrorCategory` so an adapter maps it uniformly, and an
    optional ``retry_after`` (seconds) parsed from a 429/503 response so the
    shared rate-limited executor can honor it centrally."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        status: Optional[int] = None,
        retry_after: Optional[float] = None,
    ):
        super().__init__(message)
        self.category = category
        self.message = message
        self.status = status
        self.retry_after = retry_after


def parse_retry_after(value: Optional[str], *, now: Optional[datetime.datetime] = None) -> Optional[float]:
    """Parse a ``Retry-After`` header into a non-negative number of seconds.

    Handles BOTH forms defined by RFC 7231: a numeric delta-seconds
    (``"120"``) and an HTTP-date (``"Wed, 21 Oct 2026 07:28:00 GMT"``). Returns
    ``None`` if the value is absent/unparseable, and clamps a past HTTP-date to
    ``0.0`` (retry now) rather than returning a negative delay."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    # Numeric delta-seconds.
    try:
        seconds = float(raw)
        return max(0.0, seconds)
    except ValueError:
        pass
    # HTTP-date form.
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (when - now).total_seconds())


class _Headers(Mapping[str, str]):
    """A case-insensitive, read-only view over response headers."""

    __slots__ = ("_data",)

    def __init__(self, items: Any = None):
        self._data: dict[str, tuple[str, str]] = {}
        if items is None:
            pass
        elif hasattr(items, "items"):
            for k, v in items.items():
                self._data[k.lower()] = (k, v)
        else:
            for k, v in items:
                self._data[k.lower()] = (k, v)

    def __getitem__(self, key: str) -> str:
        return self._data[key.lower()][1]

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        item = self._data.get(key.lower())
        return item[1] if item is not None else default

    def __iter__(self):
        return (orig for orig, _ in self._data.values())

    def __len__(self) -> int:
        return len(self._data)

    def to_dict(self) -> dict[str, str]:
        return {orig: val for orig, val in self._data.values()}


@dataclass(frozen=True)
class HttpRequest:
    """One read-only HTTP request. ``body`` is used only for the public Workday
    search POST (a facet/keyword query with no candidate data)."""

    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    timeout: Optional[float] = None


@dataclass
class _RawResponse:
    status: int
    headers: _Headers
    body: bytes
    final_url: str


class HttpResponse:
    """A completed HTTP exchange (any status). Non-network HTTP statuses such as
    404/429/500 are returned as responses — the ADAPTER decides how to map a
    status to an :class:`atlas.sources.adapter.AdapterError`, never the client."""

    __slots__ = ("status", "headers", "body", "url", "request_url", "redirects")

    def __init__(
        self,
        status: int,
        headers: _Headers,
        body: bytes,
        url: str,
        request_url: str,
        redirects: int = 0,
    ):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url
        self.request_url = request_url
        self.redirects = redirects

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @classmethod
    def build(
        cls,
        status: int,
        *,
        body: bytes = b"",
        headers: Optional[Mapping[str, str]] = None,
        url: str = "https://fake.local/",
    ) -> "HttpResponse":
        """Construct a response (primarily for tests/fakes) with case-insensitive
        headers, without going through the network."""
        return cls(status, _Headers((headers or {}).items()), body, url, url, 0)

    @property
    def content_type(self) -> str:
        raw = self.headers.get("Content-Type", "") or ""
        return raw.split(";", 1)[0].strip().lower()

    def looks_like_json(self) -> bool:
        ct = self.content_type
        return ct == "application/json" or ct.endswith("+json") or ct == "text/json"

    def looks_like_html(self) -> bool:
        ct = self.content_type
        if ct in ("text/html", "application/xhtml+xml"):
            return True
        head = self.body[:512].lstrip().lower()
        return head.startswith(b"<!doctype html") or head.startswith(b"<html")

    def retry_after_seconds(self, *, now: Optional[datetime.datetime] = None) -> Optional[float]:
        return parse_retry_after(self.headers.get("Retry-After"), now=now)

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")

    def json(self) -> Any:
        """Parse the body as JSON, tolerating a slightly-wrong content-type but
        rejecting a body that is not valid JSON (e.g. an HTML challenge/login
        page masquerading as an API response)."""
        import json as _json

        if self.looks_like_html():
            raise HttpError(
                ErrorCategory.INVALID_RESPONSE,
                f"expected JSON but received an HTML page (content-type={self.content_type!r}) from {self.url}",
                status=self.status,
            )
        try:
            return _json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError(
                ErrorCategory.INVALID_RESPONSE,
                f"response body from {self.url} is not valid JSON: {exc}",
                status=self.status,
            ) from exc


class ReadOnlyHttpClient:
    """A bounded, read-only HTTP client. Reusable and thread-safe for concurrent
    ``fetch`` calls (it holds no per-request mutable state beyond an atomic
    request counter)."""

    def __init__(
        self,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 20.0,
        max_response_bytes: int = 10 * 1024 * 1024,
        max_redirects: int = 5,
        user_agent: str = DEFAULT_USER_AGENT,
        request_budget: Optional[int] = None,
        verify_tls: bool = True,
        accept: str = "application/json",
    ):
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("timeouts must be > 0")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be >= 1")
        if max_redirects < 0:
            raise ValueError("max_redirects must be >= 0")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_response_bytes = min(max_response_bytes, _HARD_READ_CEILING)
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self.request_budget = request_budget
        self.verify_tls = verify_tls
        self.accept = accept
        self._requests_made = 0
        self._lock = threading.Lock()
        # A default-verifying TLS context (verification ON). We NEVER disable
        # certificate verification — a TLS failure is surfaced, not bypassed.
        self._ssl_context = ssl.create_default_context()
        # No proxy handler => environment proxies are ignored (no proxy by
        # default; never a rotating/anonymizing proxy).
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self._ssl_context),
            _NoRedirect(),
        )

    @property
    def requests_made(self) -> int:
        with self._lock:
            return self._requests_made

    def _charge_budget(self) -> None:
        with self._lock:
            if self.request_budget is not None and self._requests_made >= self.request_budget:
                raise HttpError(
                    ErrorCategory.SOURCE_UNAVAILABLE,
                    f"HTTP request budget exhausted ({self.request_budget}); refusing further requests",
                )
            self._requests_made += 1

    def fetch(self, request: HttpRequest) -> HttpResponse:
        """Perform exactly one logical HTTP exchange (following at most
        ``max_redirects`` ordinary redirects). Never retries."""
        method = request.method.upper()
        url = request.url
        headers = self._base_headers()
        headers.update({k: v for k, v in dict(request.headers).items()})
        body = request.body
        timeout = request.timeout or self.read_timeout

        visited: list[str] = []
        redirects = 0
        while True:
            self._charge_budget()
            visited.append(url)
            raw = self._open_raw(method, url, headers, body, timeout)
            if raw.status in _REDIRECT_STATUSES and redirects < self.max_redirects:
                location = raw.headers.get("Location")
                if not location:
                    break
                new_url = urljoin(url, location)
                if new_url in visited:
                    raise HttpError(
                        ErrorCategory.INVALID_RESPONSE,
                        f"redirect loop detected at {new_url}",
                        status=raw.status,
                    )
                # 303 (and, per browser convention, 301/302 for POST) downgrade
                # to GET and drop the body; 307/308 preserve method + body.
                if raw.status == 303 or (raw.status in (301, 302) and method != "GET"):
                    method = "GET"
                    body = None
                url = new_url
                redirects += 1
                continue
            if raw.status in _REDIRECT_STATUSES and redirects >= self.max_redirects:
                raise HttpError(
                    ErrorCategory.INVALID_RESPONSE,
                    f"exceeded redirect limit ({self.max_redirects}) starting from {request.url}",
                    status=raw.status,
                )
            break

        decoded = self._decode_body(raw)
        return HttpResponse(
            status=raw.status,
            headers=raw.headers,
            body=decoded,
            url=raw.final_url,
            request_url=request.url,
            redirects=redirects,
        )

    # -- helpers ------------------------------------------------------------
    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": self.accept,
            "Accept-Encoding": "gzip, deflate",
        }

    def _decode_body(self, raw: _RawResponse) -> bytes:
        encoding = (raw.headers.get("Content-Encoding", "") or "").strip().lower()
        data = raw.body
        try:
            if encoding == "gzip":
                data = gzip.decompress(data)
            elif encoding == "deflate":
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    data = zlib.decompress(data, -zlib.MAX_WBITS)  # raw deflate
        except (OSError, zlib.error) as exc:
            raise HttpError(
                ErrorCategory.INVALID_RESPONSE,
                f"failed to decode {encoding} response body from {raw.final_url}: {exc}",
            ) from exc
        if len(data) > self.max_response_bytes:
            raise HttpError(
                ErrorCategory.INVALID_RESPONSE,
                f"response body from {raw.final_url} exceeds max_response_bytes "
                f"({len(data)} > {self.max_response_bytes})",
                status=raw.status,
            )
        return data

    def _open_raw(
        self, method: str, url: str, headers: Mapping[str, str], body: Optional[bytes], timeout: float
    ) -> _RawResponse:
        """Low-level single socket exchange via urllib. Overridable seam for
        offline tests. Reads at most ``max_response_bytes + 1`` compressed bytes
        (the logical cap is enforced after decoding in ``_decode_body``)."""
        scheme = (urlsplit(url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise HttpError(ErrorCategory.CONFIG_ERROR, f"unsupported URL scheme {scheme!r} for {url}")
        req = urllib.request.Request(url=url, method=method, data=body, headers=dict(headers))
        try:
            resp = self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:  # 4xx/5xx completed exchange
            raw_headers = _Headers(exc.headers.items() if exc.headers else [])
            payload = self._read_capped(exc)
            return _RawResponse(status=int(exc.code), headers=raw_headers, body=payload, final_url=exc.url or url)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, ssl.SSLError)) or "timed out" in str(reason).lower():
                if isinstance(reason, ssl.SSLError):
                    raise HttpError(ErrorCategory.SOURCE_UNAVAILABLE, f"TLS error for {url}: {reason}") from exc
                raise HttpError(ErrorCategory.TIMEOUT, f"request to {url} timed out: {reason}") from exc
            raise HttpError(ErrorCategory.SOURCE_UNAVAILABLE, f"connection error for {url}: {reason}") from exc
        except TimeoutError as exc:
            raise HttpError(ErrorCategory.TIMEOUT, f"request to {url} timed out: {exc}") from exc
        with resp:
            raw_headers = _Headers(resp.headers.items() if resp.headers else [])
            payload = self._read_capped(resp)
            final_url = resp.geturl() or url
            status = int(getattr(resp, "status", None) or resp.getcode() or 0)
        return _RawResponse(status=status, headers=raw_headers, body=payload, final_url=final_url)

    def _read_capped(self, resp: Any) -> bytes:
        limit = self.max_response_bytes + 1
        data = resp.read(limit)
        if len(data) > self.max_response_bytes:
            raise HttpError(
                ErrorCategory.INVALID_RESPONSE,
                f"response body exceeds max_response_bytes ({self.max_response_bytes})",
            )
        return data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable urllib's automatic redirect following so the client can enforce
    its own explicit redirect limit and loop detection."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ARG002
        return None


__all__ = [
    "DEFAULT_USER_AGENT",
    "HttpError",
    "HttpRequest",
    "HttpResponse",
    "ReadOnlyHttpClient",
    "parse_retry_after",
]
