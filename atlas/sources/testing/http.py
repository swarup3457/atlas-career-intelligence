"""Injected fake HTTP transport for offline adapter tests (Phase 1C-A).

Lets the real ATS adapters be exercised through the SAME code path they use in
production while feeding them scripted responses — NO socket, NO network. A
``FakeTransport`` is duck-compatible with :class:`atlas.sources.http_client.ReadOnlyHttpClient`
(it exposes ``fetch(HttpRequest) -> HttpResponse`` and may raise
:class:`HttpError`), so ``MyAdapter(instance, http_client=FakeTransport(...))``
routes every request to the handler.
"""

from __future__ import annotations

import gzip
import json as _json
from typing import Any, Callable, Optional

from atlas.models import ErrorCategory
from atlas.sources.http_client import HttpError, HttpRequest, HttpResponse

Handler = Callable[[HttpRequest], HttpResponse]


class FakeTransport:
    """Routes each request to ``handler`` and records the calls."""

    def __init__(self, handler: Handler):
        self.handler = handler
        self.calls: list[HttpRequest] = []

    def fetch(self, request: HttpRequest) -> HttpResponse:
        self.calls.append(request)
        return self.handler(request)  # may raise HttpError

    @property
    def request_count(self) -> int:
        return len(self.calls)


def json_response(obj: Any, *, status: int = 200, url: str = "https://fake.local/", headers: Optional[dict] = None) -> HttpResponse:
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    return HttpResponse.build(status, body=_json.dumps(obj).encode("utf-8"), headers=hdrs, url=url)


def gzip_json_response(obj: Any, *, status: int = 200, url: str = "https://fake.local/") -> HttpResponse:
    body = gzip.compress(_json.dumps(obj).encode("utf-8"))
    return HttpResponse.build(
        status, body=body, headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}, url=url
    )


def text_response(text: str, *, status: int = 200, content_type: str = "text/html",
                  url: str = "https://fake.local/", headers: Optional[dict] = None) -> HttpResponse:
    hdrs = {"Content-Type": content_type}
    if headers:
        hdrs.update(headers)
    return HttpResponse.build(status, body=text.encode("utf-8"), headers=hdrs, url=url)


def raises(category: ErrorCategory, message: str = "fake transport error", *, retry_after: Optional[float] = None) -> Handler:
    """A handler that always raises a classified transport error."""
    def _handler(_request: HttpRequest) -> HttpResponse:
        raise HttpError(category, message, retry_after=retry_after)
    return _handler


def static(response: HttpResponse) -> Handler:
    """A handler that always returns the same response."""
    def _handler(_request: HttpRequest) -> HttpResponse:
        return response
    return _handler


__all__ = [
    "FakeTransport",
    "json_response",
    "gzip_json_response",
    "text_response",
    "raises",
    "static",
]
