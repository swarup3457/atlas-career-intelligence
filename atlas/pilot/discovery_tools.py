"""Constrained public-web discovery tools for the agentic company agent (prompt s.5).

The V3 SDK session ran with ``available_tools=[]``, so the model had no way to
locate an official indexed job page when a company SPA resisted automation — a
large part of the native agent's advantage.

The installed Copilot SDK (see :func:`probe_builtin_web_tools`) exposes NO
``web_search`` / ``web_fetch`` built-in, so per prompt s.5 this module supplies a
SINGLE constrained public-web adapter built on the existing read-only
:class:`~atlas.sources.http_client.ReadOnlyHttpClient` — no paid service, no new
credential. It is deliberately weak on authority:

* results are **leads only**; Python (not the model) validates that a final URL
  is HTTPS on the verified official employer domain or a trusted ATS host before
  any job is accepted;
* everything is GET / read-only; it never logs in or bypasses a challenge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from atlas.pilot.browser_actor import _TRUSTED_ATS_SUFFIXES, actor_registrable_domain
from atlas.pilot.normalize import normalize_source_text

__all__ = ["WebDiscoveryTools", "SearchLead", "probe_builtin_web_tools", "is_trusted_official_url"]


def _host(url: str) -> str:
    return (urlsplit(url or "").netloc or "").split("@")[-1].split(":")[0].lower().rstrip(".")


def is_trusted_official_url(url: str, official_domain: str, *, extra_hosts: tuple[str, ...] = ()) -> bool:
    """True only for an HTTPS URL on the verified official employer domain or a
    trusted ATS host. This is the Python trust gate every lead must pass."""
    if not url or not url.lower().startswith("https://"):
        return False
    h = _host(url)
    if not h:
        return False
    if h.endswith(".myworkdayjobs.com") or h == "myworkdayjobs.com":
        return True
    for suf in _TRUSTED_ATS_SUFFIXES:
        if h == suf or h.endswith("." + suf):
            return True
    dom = (official_domain or "").lower().lstrip(".")
    if dom and (h == dom or h.endswith("." + dom) or actor_registrable_domain(h) == actor_registrable_domain(dom)):
        return True
    for e in extra_hosts:
        e = e.lower()
        if h == e or h.endswith("." + e):
            return True
    return False


def probe_builtin_web_tools() -> dict:
    """Inspect the installed Copilot SDK for a safe ``web_search`` / ``web_fetch``
    built-in (prompt s.5). Returns availability so the launcher/evidence can
    record the exact finding rather than assuming."""
    result = {"web_search": False, "web_fetch": False, "builtin_isolated": [], "sdk_version": ""}
    try:
        import copilot

        result["sdk_version"] = getattr(copilot, "__version__", "")
        isolated = list(getattr(copilot, "BUILTIN_TOOLS_ISOLATED", []) or [])
        result["builtin_isolated"] = isolated
        low = {str(x).lower() for x in isolated}
        result["web_search"] = "web_search" in low
        result["web_fetch"] = "web_fetch" in low
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["fallback_adapter"] = not (result["web_search"] or result["web_fetch"])
    return result


@dataclass
class SearchLead:
    title: str
    url: str
    snippet: str = ""
    trusted: bool = False

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet, "trusted": self.trusted}


# DuckDuckGo HTML endpoint result anchors + redirect unwrap. Tolerant of either
# quote style so it works across HTML engines and fixtures.
_RESULT_A_RE = re.compile(r'<a[^>]+class=["\'][^"\']*result__a[^"\']*["\'][^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_ANY_A_RE = re.compile(r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _unwrap_ddg(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if "duckduckgo.com" in (parts.netloc or "") and "/l/" in (parts.path or ""):
        q = parse_qs(parts.query)
        if "uddg" in q:
            return unquote(q["uddg"][0])
    return href


@dataclass
class WebDiscoveryTools:
    """Two constrained, read-only discovery tools bound to one company's search."""

    official_domain: str = ""
    extra_trusted_hosts: tuple[str, ...] = ()
    fetcher: Optional[Callable[[str, str], tuple[int, str, str]]] = None  # (url, accept) -> (status, ct, text)
    search_endpoint: str = "https://html.duckduckgo.com/html/"
    calls: list[dict] = field(default_factory=list)
    max_leads: int = 12

    def _fetch(self, url: str, accept: str) -> tuple[int, str, str]:
        if self.fetcher is not None:
            return self.fetcher(url, accept)
        # default: the bounded read-only Atlas client
        from atlas.sources.http_client import HttpRequest, ReadOnlyHttpClient

        client = ReadOnlyHttpClient(
            user_agent="Atlas-Career-Intelligence/1.0 (read-only career research)",
            accept=accept, request_budget=40,
        )
        resp = client.fetch(HttpRequest(url=url, headers={"Accept": accept}))
        return resp.status, resp.content_type, resp.text()

    # -- tool: web_fetch_official -------------------------------------------
    def web_fetch_official(self, url: str) -> dict:
        """Fetch a page ONLY if it is on the verified official / trusted-ATS host,
        returning bounded normalized text + same-domain job links as leads."""
        self.calls.append({"tool": "web_fetch_official", "url": url})
        if not is_trusted_official_url(url, self.official_domain, extra_hosts=self.extra_trusted_hosts):
            return {"error": f"refused untrusted or non-HTTPS URL: {url}", "url": url}
        try:
            status, ct, text = self._fetch(url, "text/html,application/xhtml+xml,*/*;q=0.8")
        except Exception as exc:  # noqa: BLE001
            return {"error": f"fetch failed: {type(exc).__name__}: {exc}", "url": url}
        norm = normalize_source_text(text)
        links = []
        for m in _ANY_A_RE.finditer(text):
            href = m.group(1)
            if is_trusted_official_url(href, self.official_domain, extra_hosts=self.extra_trusted_hosts) \
                    and re.search(r"(job|career|requisition|posting|vacanc|opening|position)", href, re.I):
                if href not in [l["url"] for l in links]:
                    links.append({"url": href, "text": _TAG_RE.sub("", m.group(2)).strip()[:120]})
            if len(links) >= 40:
                break
        return {"url": url, "status": status, "content_type": ct, "text": norm[:8000],
                "official_job_links": links}

    # -- tool: web_search_leads ---------------------------------------------
    def web_search_leads(self, query: str, *, site_scope: bool = True) -> dict:
        """Return public-web search LEADS for a query. When an official domain is
        known, the query is scoped with ``site:`` to that domain / a trusted ATS,
        and every lead is flagged with whether Python's trust gate accepts it. The
        model may ONLY treat trusted leads as candidate official pages."""
        self.calls.append({"tool": "web_search_leads", "query": query})
        q = query
        if site_scope and self.official_domain:
            q = f"{query} site:{self.official_domain}"
        try:
            status, ct, text = self._fetch(f"{self.search_endpoint}?q={_urlq(q)}", "text/html")
        except Exception as exc:  # noqa: BLE001
            return {"query": query, "leads": [], "limitation": f"search unavailable: {type(exc).__name__}"}
        leads: list[SearchLead] = []
        matches = list(_RESULT_A_RE.finditer(text)) or list(_ANY_A_RE.finditer(text))
        seen: set[str] = set()
        for m in matches:
            url = _unwrap_ddg(m.group(1))
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            title = _TAG_RE.sub("", m.group(2)).strip()[:160]
            trusted = is_trusted_official_url(url, self.official_domain, extra_hosts=self.extra_trusted_hosts)
            leads.append(SearchLead(title=title, url=url, trusted=trusted))
            if len(leads) >= self.max_leads:
                break
        return {"query": q, "leads": [l.to_dict() for l in leads],
                "trusted_count": sum(1 for l in leads if l.trusted),
                "note": "leads only; Python trust-validates official/ATS before acceptance"}


def _urlq(s: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(s)
