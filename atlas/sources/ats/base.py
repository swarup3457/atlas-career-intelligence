"""Shared helpers for the official ATS HTTP adapters (Phase 1C-A).

Common, dependency-free building blocks used by the Greenhouse, Lever, Ashby,
and Workday adapters:

    * :class:`DateProvenance` — every date an adapter emits records exactly one
      provenance so a crawl/first-seen time is NEVER passed off as an employer
      posted date;
    * trusted-URL → source-identity parsers (board token / site / board name /
      Workday tenant+datacenter+site), which validate rather than guess;
    * ``sanitize_description`` — strips active script/style content and redacts
      secrets from UNTRUSTED posting text before it is stored/reasoned over
      (posting text is data, never an instruction, and links inside it are
      never fetched);
    * challenge/login masquerade detection (detection only — Atlas never
      bypasses a challenge, CAPTCHA, or login wall);
    * :class:`HttpAtsAdapter` — a base adapter that owns the injected read-only
      HTTP client, maps HTTP status codes to typed :class:`AdapterError`\\s, and
      provides a bounded health probe. Each concrete adapter declares its own
      capabilities, endpoints, parsing, and date semantics.
"""

from __future__ import annotations

import enum
import html
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, SourceAdapter
from atlas.sources.health import HealthEvidence, SourceHealth, SourceHealthState, classify_health
from atlas.sources.http_client import HttpError, HttpRequest, HttpResponse, ReadOnlyHttpClient
from atlas.sources.models import SourceInstance
from atlas.sources.untrusted import redact_secrets


class DateProvenance(str, enum.Enum):
    """Provenance of a date an adapter reports. A source-observed date must
    carry exactly one of these so an employer *posted* time is never conflated
    with a crawl/cache/update/first-seen time (build spec 15)."""

    EMPLOYER_POSTED_AT = "EMPLOYER_POSTED_AT"
    EMPLOYER_UPDATED_AT = "EMPLOYER_UPDATED_AT"
    RELATIVE_POSTED_TEXT = "RELATIVE_POSTED_TEXT"
    DISCOVERED_AT = "DISCOVERED_AT"
    UNKNOWN = "UNKNOWN"


# --- untrusted posting text -------------------------------------------------
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def sanitize_description(raw: Optional[str], *, keep_html: bool = False, max_chars: int = 20000) -> Optional[str]:
    """Neutralize UNTRUSTED posting description text for safe storage.

    Removes ``<script>``/``<style>`` blocks (never executed), optionally strips
    remaining tags, decodes HTML entities, redacts secrets, and bounds length.
    This is *data hygiene* only — it never follows links found in the text and
    never treats the text as instructions."""
    if raw is None:
        return None
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    if not keep_html:
        text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    text = redact_secrets(text)
    if len(text) > max_chars:
        text = text[:max_chars] + " …[truncated]"
    return text or None


def work_mode_from_text(*texts: Optional[str]):
    """Infer a :class:`atlas.sources.models.WorkMode` ONLY from an explicit
    remote/hybrid/onsite signal in the given text(s); otherwise ``UNKNOWN``
    (never invented). Kept dependency-light by importing WorkMode lazily."""
    from atlas.sources.models import WorkMode

    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return WorkMode.UNKNOWN
    if "hybrid" in blob:
        return WorkMode.HYBRID
    if "remote" in blob or "work from home" in blob or "wfh" in blob:
        return WorkMode.REMOTE
    if "on-site" in blob or "onsite" in blob or "in office" in blob or "in-office" in blob:
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "checking your browser",
    "captcha",
    "hcaptcha",
    "recaptcha",
    "access denied",
    "request blocked",
    "zscaler",
    "are you a robot",
    "enable javascript and cookies",
)
_LOGIN_MARKERS = ("sign in", "log in", "please authenticate", "login required")


def detect_challenge(resp: HttpResponse) -> Optional[ErrorCategory]:
    """Return a non-retryable :class:`ErrorCategory` if an HTML body looks like
    an anti-bot challenge or a login wall masquerading as a data response.
    Atlas NEVER bypasses these — it classifies and stops."""
    if not resp.looks_like_html() and resp.content_type not in ("", "text/plain"):
        return None
    body = resp.body[:4096].decode("utf-8", errors="replace").lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in body:
            return ErrorCategory.ANTI_BOT
    if resp.looks_like_html():
        for marker in _LOGIN_MARKERS:
            if marker in body:
                return ErrorCategory.LOGIN_WALL
    return None


# --- trusted URL → identity parsers -----------------------------------------
def _host_path(url: str) -> tuple[str, list[str]]:
    parts = urlsplit(url)
    host = (parts.netloc or "").lower().split("@")[-1].split(":")[0]
    segs = [s for s in (parts.path or "").split("/") if s]
    return host, segs


def extract_greenhouse_board_token(url: str) -> str:
    """``https://boards.greenhouse.io/{token}`` or
    ``https://job-boards.greenhouse.io/{token}`` (also boards-api hosts)."""
    host, segs = _host_path(url)
    if "greenhouse.io" not in host:
        raise ValueError(f"not a Greenhouse URL: {url!r}")
    if not segs:
        raise ValueError(f"no board token in Greenhouse URL: {url!r}")
    # boards-api.greenhouse.io/v1/boards/{token}/...
    if segs[0] in ("v1", "embed"):
        for i, s in enumerate(segs):
            if s == "boards" and i + 1 < len(segs):
                return segs[i + 1]
        raise ValueError(f"could not locate board token in API URL: {url!r}")
    return segs[0]


def extract_lever_site(url: str) -> tuple[str, str]:
    """Return ``(site, api_base)`` from a Lever URL, honoring the EU host.
    ``jobs.lever.co/{site}`` / ``api.lever.co/v0/postings/{site}`` (or ``.eu``)."""
    host, segs = _host_path(url)
    if "lever.co" not in host:
        raise ValueError(f"not a Lever URL: {url!r}")
    eu = ".eu." in f".{host}." or host.startswith("jobs.eu.") or host.startswith("api.eu.")
    api_base = "https://api.eu.lever.co/v0/postings" if eu else "https://api.lever.co/v0/postings"
    if host.startswith("api.") and "postings" in segs:
        idx = segs.index("postings")
        if idx + 1 < len(segs):
            return segs[idx + 1], api_base
        raise ValueError(f"no site in Lever API URL: {url!r}")
    if not segs:
        raise ValueError(f"no site in Lever URL: {url!r}")
    return segs[0], api_base


def extract_ashby_board_name(url: str) -> str:
    """``https://jobs.ashbyhq.com/{jobBoardName}`` (case-sensitive) or the
    posting-api URL ``.../posting-api/job-board/{name}``."""
    parts = urlsplit(url)
    host = (parts.netloc or "").lower().split("@")[-1].split(":")[0]
    segs = [s for s in (parts.path or "").split("/") if s]
    if "ashbyhq.com" not in host:
        raise ValueError(f"not an Ashby URL: {url!r}")
    if "job-board" in segs:
        idx = segs.index("job-board")
        if idx + 1 < len(segs):
            return segs[idx + 1]
    if not segs:
        raise ValueError(f"no board name in Ashby URL: {url!r}")
    return segs[0]


class WorkdayIdentity:
    """Parsed Workday careers identity. The datacenter shard (``wd1``/``wd3``/…)
    is part of the hostname and CANNOT be guessed — it must come from the real
    employer careers URL."""

    __slots__ = ("tenant", "datacenter", "site", "locale", "host")

    def __init__(self, tenant: str, datacenter: str, site: str, locale: str, host: str):
        self.tenant = tenant
        self.datacenter = datacenter
        self.site = site
        self.locale = locale
        self.host = host

    def cxs_search_url(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    def cxs_detail_url(self, external_path: str) -> str:
        path = external_path if external_path.startswith("/") else "/" + external_path
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}{path}"

    def public_job_url(self, external_path: str) -> str:
        path = external_path if external_path.startswith("/") else "/" + external_path
        return f"https://{self.host}/{self.locale}/{self.site}{path}"

    def to_dict(self) -> dict:
        return {"tenant": self.tenant, "datacenter": self.datacenter, "site": self.site,
                "locale": self.locale, "host": self.host}


_WD_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)


def parse_workday_url(url: str) -> WorkdayIdentity:
    """Parse ``https://{tenant}.{dc}.myworkdayjobs.com/{locale}/{site}`` into a
    :class:`WorkdayIdentity`. Also accepts a ``/wday/cxs/{tenant}/{site}`` URL."""
    parts = urlsplit(url)
    host = (parts.netloc or "").lower().split("@")[-1].split(":")[0]
    m = _WD_HOST_RE.match(host)
    if not m:
        raise ValueError(f"not a Workday myworkdayjobs URL host: {host!r}")
    tenant = m.group("tenant")
    dc = m.group("dc").lower()
    segs = [s for s in (parts.path or "").split("/") if s]
    locale = "en-US"
    site = ""
    if segs and segs[0] == "wday" and "cxs" in segs:
        idx = segs.index("cxs")
        # /wday/cxs/{tenant}/{site}/...
        if idx + 2 < len(segs):
            site = segs[idx + 2]
    else:
        # /{locale}/{site}
        if len(segs) >= 1 and re.match(r"^[a-z]{2}-[A-Za-z]{2}$", segs[0]):
            locale = segs[0]
            if len(segs) >= 2:
                site = segs[1]
        elif segs:
            site = segs[0]
    if not site:
        raise ValueError(f"could not determine Workday site from URL: {url!r}")
    return WorkdayIdentity(tenant=tenant, datacenter=dc, site=site, locale=locale, host=host)


# --- base HTTP adapter -------------------------------------------------------
def _client_from_instance(instance: SourceInstance) -> ReadOnlyHttpClient:
    md = instance.metadata or {}
    return ReadOnlyHttpClient(
        connect_timeout=float(md.get("connect_timeout", 10.0)),
        read_timeout=float(md.get("read_timeout", 20.0)),
        max_response_bytes=int(md.get("max_response_bytes", 10 * 1024 * 1024)),
        max_redirects=int(md.get("max_redirects", 5)),
        request_budget=(int(md["request_budget"]) if md.get("request_budget") is not None else None),
    )


class HttpAtsAdapter(SourceAdapter):
    """Base for the official ATS HTTP adapters. Owns the injected read-only HTTP
    client and the HTTP-status → :class:`AdapterError` mapping."""

    def __init__(self, instance: SourceInstance, *, http_client: Optional[ReadOnlyHttpClient] = None):
        super().__init__(instance)
        self.http = http_client or _client_from_instance(instance)

    # -- HTTP plumbing ------------------------------------------------------
    def _fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
        accept: str = "application/json",
    ) -> HttpResponse:
        req_headers = {"Accept": accept}
        if headers:
            req_headers.update(dict(headers))
        try:
            return self.http.fetch(HttpRequest(url, method=method, body=body, headers=req_headers))
        except HttpError as exc:
            raise AdapterError(exc.category, exc.message, retry_after=exc.retry_after) from exc

    def _raise_for_status(self, resp: HttpResponse, *, context: str, not_found: ErrorCategory) -> None:
        """Map a non-2xx HTTP status to a typed AdapterError. A masquerading
        challenge/login page is detected first so it becomes a non-retryable
        access condition rather than a parse error."""
        challenge = detect_challenge(resp)
        if challenge is not None:
            raise AdapterError(challenge, f"{context}: challenge/login page detected (no bypass) at {resp.url}")
        status = resp.status
        if 200 <= status < 300:
            return
        if status == 429:
            raise AdapterError(
                ErrorCategory.HTTP_429, f"{context}: HTTP 429 rate limited",
                retry_after=resp.retry_after_seconds(),
            )
        if status in (401,):
            raise AdapterError(ErrorCategory.LOGIN_WALL, f"{context}: HTTP 401 (auth required; no bypass)")
        if status == 403:
            raise AdapterError(ErrorCategory.ANTI_BOT, f"{context}: HTTP 403 (access limited; no bypass)")
        if status == 404:
            raise AdapterError(not_found, f"{context}: HTTP 404 not found")
        if 500 <= status < 600:
            raise AdapterError(
                ErrorCategory.HTTP_5XX, f"{context}: HTTP {status}",
                retry_after=resp.retry_after_seconds(),
            )
        raise AdapterError(ErrorCategory.INVALID_RESPONSE, f"{context}: unexpected HTTP {status}")

    def _get_json(self, url: str, *, context: str, not_found: ErrorCategory, method: str = "GET",
                  body: Optional[bytes] = None, headers: Optional[Mapping[str, str]] = None) -> Any:
        resp = self._fetch(url, method=method, body=body, headers=headers)
        self._raise_for_status(resp, context=context, not_found=not_found)
        try:
            return resp.json()
        except HttpError as exc:
            # A 200 body that is not JSON (often an HTML challenge/login page).
            challenge = detect_challenge(resp)
            if challenge is not None:
                raise AdapterError(challenge, f"{context}: {exc.message}") from exc
            raise AdapterError(exc.category, f"{context}: {exc.message}") from exc

    def _probe(self, url: str, *, method: str = "GET", body: Optional[bytes] = None,
               headers: Optional[Mapping[str, str]] = None,
               expected_keys: tuple[str, ...] = ()) -> SourceHealth:
        """A bounded, read-only health probe. Reports HEALTHY only on a 2xx JSON
        response with the expected top-level structure; otherwise a truthful
        degraded/limited/unavailable state — never a false HEALTHY."""
        try:
            resp = self._fetch(url, method=method, body=body, headers=headers)
        except AdapterError as exc:
            state = {
                ErrorCategory.HTTP_429: SourceHealthState.RATE_LIMITED,
                ErrorCategory.HTTP_5XX: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.SOURCE_UNAVAILABLE: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.TIMEOUT: SourceHealthState.SOURCE_UNAVAILABLE,
                ErrorCategory.ANTI_BOT: SourceHealthState.ACCESS_LIMITED,
                ErrorCategory.LOGIN_WALL: SourceHealthState.AUTH_REQUIRED,
            }.get(exc.category, SourceHealthState.UNKNOWN)
            return SourceHealth(state, f"probe failed: {exc.message}")
        challenge = detect_challenge(resp)
        evidence = HealthEvidence(
            http_status=resp.status,
            challenge_detected=challenge == ErrorCategory.ANTI_BOT,
            login_redirect=challenge == ErrorCategory.LOGIN_WALL,
        )
        if resp.status != 200:
            return classify_health(evidence)
        try:
            data = resp.json()
        except HttpError:
            return SourceHealth(SourceHealthState.SELECTOR_DRIFT_SUSPECTED, "probe: 200 but body is not JSON")
        if expected_keys:
            present = isinstance(data, dict) and any(k in data for k in expected_keys)
            evidence = HealthEvidence(http_status=200, expected_structure_present=present or isinstance(data, list))
        return classify_health(evidence)


__all__ = [
    "DateProvenance",
    "sanitize_description",
    "work_mode_from_text",
    "detect_challenge",
    "extract_greenhouse_board_token",
    "extract_lever_site",
    "extract_ashby_board_name",
    "parse_workday_url",
    "WorkdayIdentity",
    "HttpAtsAdapter",
]
