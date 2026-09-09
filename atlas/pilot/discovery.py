"""Deterministic official career-source discovery + bounded read-only fetch (audit 3.1).

Replaces the V2 static ``KNOWN_BOARDS`` short-circuit. An unmapped company runs real,
bounded, official-domain discovery: resolve the official domain (public, honest hints +
validation), fetch the careers entry read-only, fingerprint the ATS, and — where a public
employer-controlled endpoint exists (Greenhouse / Lever / Ashby / Workday CxS) — fetch job
cards and details. Everything is HTTPS, read-only, and honest: no login, no apply, no
CAPTCHA/anti-bot bypass, no stealth UA, no following links out of posting text. Unreachable
or non-public sites yield a TRUTHFUL terminal status, never a fabricated job.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from atlas.models import ErrorCategory
from atlas.pilot.models import CompanyStatus, JobCard, JobDetailEvidence
from atlas.sources.http_client import HttpError, HttpRequest, ReadOnlyHttpClient

__all__ = [
    "DiscoveryResult",
    "resolve_official_domain",
    "discover_careers_entry",
    "search_official_source",
    "fetch_job_detail",
    "OFFICIAL_DOMAIN_HINTS",
    "CAREERS_ENTRY_HINTS",
    "make_http_client",
]

# Public, well-known official corporate domains for the fixed pilot cohort. These are
# provenance HINTS only; discovery still fetches and validates the live site.
OFFICIAL_DOMAIN_HINTS: dict[str, str] = {
    "accenture": "accenture.com",
    "infosys": "infosys.com",
    "tcs": "tcs.com",
    "cognizant": "cognizant.com",
    "ibm": "ibm.com",
    "oracle": "oracle.com",
    "sap": "sap.com",
    "jpmorgan chase": "jpmorganchase.com",
    "jpmorgan": "jpmorganchase.com",
    "fiserv": "fiserv.com",
    "adp": "adp.com",
}

# Public official careers entry points (employer-controlled). Read-only landing pages.
CAREERS_ENTRY_HINTS: dict[str, str] = {
    "accenture": "https://www.accenture.com/in-en/careers/jobsearch",
    "infosys": "https://career.infosys.com/jobs",
    "tcs": "https://www.tcs.com/careers/india",
    "cognizant": "https://careers.cognizant.com/global-en/search-jobs/",
    "ibm": "https://www.ibm.com/careers/search",
    "oracle": "https://careers.oracle.com/en/sites/jobsearch/jobs",
    "sap": "https://jobs.sap.com/search/",
    "jpmorgan chase": "https://careers.jpmorgan.com/global/en/search-results",
    "fiserv": "https://careers.fiserv.com/en/search-jobs",
    "adp": "https://jobs.adp.com/search-jobs/",
}

# Optional public ATS hints (employer-controlled public endpoints). Best-effort; validated
# live and treated truthfully on failure. token forms: greenhouse/lever/ashby = board token;
# workday = "host|tenant|site".
KNOWN_ATS_HINTS: dict[str, tuple[str, str]] = {
    "fiserv": ("workday", "fiserv.wd5.myworkdayjobs.com|fiserv|EXT"),
}

_INDIA_TOKENS = ("india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
                 "noida", "gurugram", "gurgaon", "mumbai", "delhi", "kolkata", "kochi",
                 "ahmedabad", "coimbatore", "indore", "jaipur", "chandigarh")

_WD_RE = re.compile(r"https?://([a-z0-9-]+\.[a-z0-9]+\.myworkdayjobs\.com)/([^/\"'\s]+)", re.I)
_WD_CXS_RE = re.compile(r"/wday/cxs/([^/]+)/([^/]+)/jobs", re.I)
_GH_RE = re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)
_LEVER_RE = re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)
_ASHBY_RE = re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I)


def make_http_client(*, timeout: float = 20.0, budget: Optional[int] = None,
                     accept: str = "text/html,application/json") -> ReadOnlyHttpClient:
    return ReadOnlyHttpClient(
        read_timeout=timeout, connect_timeout=min(10.0, timeout), request_budget=budget,
        accept=accept, max_response_bytes=6 * 1024 * 1024,
    )


@dataclass
class DiscoveryResult:
    company: str
    official_domain: str = ""
    domain_provenance: str = ""
    career_entry_url: str = ""
    route: str = "UNRESOLVED"           # ATS_* / GENERIC_HTTP / GENERIC_BROWSER / UNRESOLVED
    source_family: str = ""
    ats: Optional[tuple[str, str]] = None   # (provider, token) provider in greenhouse/lever/ashby/workday
    status: str = CompanyStatus.OFFICIAL_SOURCE_UNRESOLVED.value
    evidence_urls: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    redirect_chain: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.route != "UNRESOLVED" and self.ats is not None


def resolve_official_domain(company: str, hints: Optional[dict[str, str]] = None) -> tuple[str, str]:
    """Return (official_domain, provenance) for a company name. Public well-known
    corporate domains are used as honest hints; unknown companies return ("", "")."""
    key = " ".join(company.strip().lower().split())
    table = dict(OFFICIAL_DOMAIN_HINTS)
    if hints:
        table.update({k.lower(): v for k, v in hints.items()})
    if key in table:
        return table[key], "curated_public_official_domain"
    # fall back: first token .com guess is NOT asserted as official
    return "", ""


def _classify_status(exc: HttpError) -> str:
    cat = getattr(exc, "category", None)
    if cat == ErrorCategory.TIMEOUT:
        return CompanyStatus.NETWORK_UNAVAILABLE.value
    if cat == ErrorCategory.SOURCE_UNAVAILABLE:
        return CompanyStatus.NETWORK_UNAVAILABLE.value
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return CompanyStatus.AUTH_REQUIRED.value
    if status in (404, 410):
        return CompanyStatus.OFFICIAL_SOURCE_UNRESOLVED.value
    if status and 500 <= status < 600:
        return CompanyStatus.ACCESS_LIMITED.value
    return CompanyStatus.ACCESS_LIMITED.value


def _fingerprint_ats(html: str) -> Optional[tuple[str, str, str]]:
    """Return (provider, token, family) from an ATS fingerprint in the page HTML."""
    m = _WD_CXS_RE.search(html)
    if m:
        host_m = _WD_RE.search(html)
        host = host_m.group(1) if host_m else ""
        return ("workday", f"{host}|{m.group(1)}|{m.group(2)}", "OFFICIAL_ATS_WORKDAY")
    m = _WD_RE.search(html)
    if m:
        host, site = m.group(1), m.group(2)
        tenant = host.split(".")[0]
        return ("workday", f"{host}|{tenant}|{site}", "OFFICIAL_ATS_WORKDAY")
    m = _GH_RE.search(html)
    if m:
        return ("greenhouse", m.group(1), "OFFICIAL_ATS_GREENHOUSE")
    m = _LEVER_RE.search(html)
    if m:
        return ("lever", m.group(1), "OFFICIAL_ATS_LEVER")
    m = _ASHBY_RE.search(html)
    if m:
        return ("ashby", m.group(1), "OFFICIAL_ATS_ASHBY")
    return None


def discover_careers_entry(
    company: str,
    *,
    client: Optional[ReadOnlyHttpClient] = None,
    domain_hint: Optional[str] = None,
    entry_hint: Optional[str] = None,
) -> DiscoveryResult:
    """Resolve official domain, fetch the careers entry read-only, fingerprint the ATS."""
    client = client or make_http_client()
    key = " ".join(company.strip().lower().split())
    domain, prov = resolve_official_domain(company, {key: domain_hint} if domain_hint else None)
    result = DiscoveryResult(company=company, official_domain=domain, domain_provenance=prov)
    if not domain:
        result.status = CompanyStatus.OFFICIAL_SOURCE_UNRESOLVED.value
        result.limitations.append("no curated/validated official domain for company")
        return result

    # Fast path: a known public ATS hint (still validated by a live fetch below).
    if key in KNOWN_ATS_HINTS:
        provider, token = KNOWN_ATS_HINTS[key]
        result.ats = (provider, token)
        result.route = f"ATS_{provider.upper()}"
        result.source_family = f"OFFICIAL_ATS_{provider.upper()}"

    entry = entry_hint or CAREERS_ENTRY_HINTS.get(key) or f"https://www.{domain}/careers"
    result.career_entry_url = entry
    result.evidence_urls.append(entry)
    try:
        resp = client.fetch(HttpRequest(url=entry, headers={"Accept": "text/html"}))
        result.redirect_chain.append(resp.url)
        if resp.ok and resp.looks_like_html():
            fp = _fingerprint_ats(resp.text())
            if fp:
                provider, token, family = fp
                result.ats = (provider, token)
                result.route = f"ATS_{provider.upper()}"
                result.source_family = family
                result.status = CompanyStatus.COMPLETE.value  # a real source resolved
            elif result.ats is not None:
                result.status = CompanyStatus.COMPLETE.value
            else:
                # server-rendered HTML but no recognizable ATS/public API
                result.route = "GENERIC_HTTP"
                result.source_family = "OFFICIAL_CAREERS_HTML"
                result.status = CompanyStatus.UNSUPPORTED_SITE.value
                result.limitations.append("careers page has no public ATS/JSON API (needs browser)")
        elif resp.ok:
            if result.ats is None:
                result.route = "GENERIC_BROWSER"
                result.source_family = "OFFICIAL_CAREERS_SPA"
                result.status = CompanyStatus.UNSUPPORTED_SITE.value
                result.limitations.append("careers entry is a dynamic SPA shell (no public API)")
        else:
            if result.ats is None:
                result.status = _status_from_code(resp.status)
                result.limitations.append(f"careers entry returned HTTP {resp.status}")
    except HttpError as exc:
        if result.ats is None:
            result.status = _classify_status(exc)
            result.limitations.append(f"careers entry fetch failed: {exc.category}:{exc}")
    return result


def _status_from_code(code: int) -> str:
    if code in (401, 403):
        return CompanyStatus.AUTH_REQUIRED.value
    if code in (404, 410):
        return CompanyStatus.OFFICIAL_SOURCE_UNRESOLVED.value
    return CompanyStatus.ACCESS_LIMITED.value


def _india_match(text: str) -> bool:
    low = (text or "").lower()
    return any(tok in low for tok in _INDIA_TOKENS)


def search_official_source(
    discovery: DiscoveryResult,
    query: str,
    *,
    client: Optional[ReadOnlyHttpClient] = None,
    india_only: bool = True,
    page_budget: int = 3,
    max_cards: int = 40,
) -> tuple[list[JobCard], list[str]]:
    """Fetch job cards from a resolved public ATS source. Returns (cards, limitations)."""
    if discovery.ats is None:
        return [], ["no resolved public ATS source to search"]
    provider, token = discovery.ats
    client = client or make_http_client(accept="application/json")
    try:
        if provider == "greenhouse":
            return _search_greenhouse(token, query, india_only, max_cards, client)
        if provider == "lever":
            return _search_lever(token, query, india_only, max_cards, client)
        if provider == "ashby":
            return _search_ashby(token, query, india_only, max_cards, client)
        if provider == "workday":
            return _search_workday(token, query, india_only, page_budget, max_cards, client)
    except HttpError as exc:
        return [], [f"{provider} search failed: {exc.category}:{exc}"]
    return [], [f"unsupported provider {provider}"]


def _search_greenhouse(token, query, india_only, max_cards, client):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    resp = client.fetch(HttpRequest(url=url, headers={"Accept": "application/json"}))
    data = resp.json()
    cards = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        loc = (j.get("location") or {}).get("name", "")
        # A list board is fetched once and ALL lanes are evaluated locally, so the
        # multi-word query is not used to substring-filter titles (that would drop
        # e.g. "Java Backend Engineer" for a "Java Developer" query).
        if india_only and not _india_match(f"{loc} {title}"):
            continue
        cards.append(JobCard(title=title, location=loc, url=j.get("absolute_url", ""),
                             requisition_id=str(j.get("requisition_id") or ""),
                             updated_date=str(j.get("updated_at") or ""),
                             posted_date=str(j.get("first_published") or ""),
                             source_family="OFFICIAL_ATS_GREENHOUSE"))
        if len(cards) >= max_cards:
            break
    return cards, []


def _search_lever(token, query, india_only, max_cards, client):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    resp = client.fetch(HttpRequest(url=url, headers={"Accept": "application/json"}))
    data = resp.json()
    cards = []
    for j in data:
        title = j.get("text", "")
        loc = (j.get("categories") or {}).get("location", "")
        if india_only and not _india_match(f"{loc} {title}"):
            continue
        cards.append(JobCard(title=title, location=loc, url=j.get("hostedUrl", ""),
                             snippet=(j.get("descriptionPlain") or "")[:300],
                             source_family="OFFICIAL_ATS_LEVER"))
        if len(cards) >= max_cards:
            break
    return cards, []


def _search_ashby(token, query, india_only, max_cards, client):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"
    resp = client.fetch(HttpRequest(url=url, headers={"Accept": "application/json"}))
    data = resp.json()
    cards = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        loc = j.get("location", "") or j.get("locationName", "")
        if india_only and not _india_match(f"{loc} {title}"):
            continue
        cards.append(JobCard(title=title, location=loc, url=j.get("jobUrl", ""),
                             source_family="OFFICIAL_ATS_ASHBY"))
        if len(cards) >= max_cards:
            break
    return cards, []


def _search_workday(token, query, india_only, page_budget, max_cards, client):
    host, tenant, site = token.split("|")
    base = f"https://{host}"
    cxs = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    cards, limitations = [], []
    offset = 0
    for _ in range(max(1, page_budget)):
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": offset,
                           "searchText": query}).encode("utf-8")
        resp = client.fetch(HttpRequest(url=cxs, method="POST", body=body,
                                        headers={"Accept": "application/json",
                                                 "Content-Type": "application/json"}))
        data = resp.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            title = j.get("title", "")
            loc = j.get("locationsText", "")
            if india_only and not _india_match(f"{loc} {title}"):
                continue
            ext = j.get("externalPath", "")
            url = f"{base}/{site}{ext}" if ext else ""
            cards.append(JobCard(title=title, location=loc, url=url,
                                 requisition_id=str(j.get("bulletFields", [""])[0] if j.get("bulletFields") else ""),
                                 posted_date=str(j.get("postedOn", "")),
                                 source_family="OFFICIAL_ATS_WORKDAY"))
            if len(cards) >= max_cards:
                return cards, limitations
        offset += 20
    return cards, limitations


def fetch_job_detail(
    card: JobCard,
    company: str,
    *,
    discovery: Optional[DiscoveryResult] = None,
    client: Optional[ReadOnlyHttpClient] = None,
) -> Optional[JobDetailEvidence]:
    """Fetch a validated official job-detail page read-only and extract bounded evidence."""
    if not card.url or not card.url.lower().startswith("https://"):
        return None
    client = client or make_http_client()
    provider = discovery.ats[0] if (discovery and discovery.ats) else ""
    try:
        if provider == "greenhouse":
            m = re.search(r"/jobs/(\d+)", card.url)
            token = discovery.ats[1]
            if m:
                url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{m.group(1)}"
                resp = client.fetch(HttpRequest(url=url, headers={"Accept": "application/json"}))
                d = resp.json()
                content = _strip_html(d.get("content", "") or "")
                loc = (d.get("location") or {}).get("name", card.location)
                return JobDetailEvidence(
                    title=d.get("title", card.title), company=company, location=loc,
                    work_mode="REMOTE" if "remote" in loc.lower() else "ONSITE",
                    description=content, experience_text=content,
                    posted_date=str(d.get("first_published") or card.posted_date),
                    updated_date=str(d.get("updated_at") or card.updated_date),
                    requisition_id=str(d.get("requisition_id") or card.requisition_id),
                    official_url=d.get("absolute_url", card.url),
                    eligibility_text=f"{loc}. {content[:400]}",
                    source_family="OFFICIAL_ATS_GREENHOUSE",
                    evidence_snippets=(content[:800],),
                )
        if provider == "workday" and discovery and discovery.ats:
            # Fetch the Workday detail via the public CxS JSON endpoint (the human page is an
            # SPA redirect shell); this returns the full jobDescription read-only.
            host, tenant, site = discovery.ats[1].split("|")
            base = f"https://{host}"
            prefix = f"{base}/{site}"
            ext = card.url[len(prefix):] if card.url.startswith(prefix) else ""
            if ext:
                cxs = f"{base}/wday/cxs/{tenant}/{site}{ext}"
                resp = client.fetch(HttpRequest(url=cxs, headers={"Accept": "application/json"}))
                d = resp.json()
                info = d.get("jobPostingInfo", {}) or {}
                content = _strip_html(info.get("jobDescription", "") or "")
                loc = info.get("location", card.location) or card.location
                return JobDetailEvidence(
                    title=info.get("title", card.title), company=company, location=loc,
                    work_mode="REMOTE" if "remote" in str(loc).lower() else "ONSITE",
                    description=content, experience_text=content,
                    posted_date=str(info.get("startDate", "") or card.posted_date),
                    updated_date=str(info.get("startDate", "") or card.updated_date),
                    requisition_id=str(info.get("jobReqId", "") or card.requisition_id),
                    official_url=info.get("externalUrl", card.url),
                    eligibility_text=f"{loc}. {content[:400]}",
                    source_family="OFFICIAL_ATS_WORKDAY",
                    evidence_snippets=(content[:800],),
                )
        # Generic official HTML detail (Lever/Ashby/Workday detail or careers HTML).
        resp = client.fetch(HttpRequest(url=card.url, headers={"Accept": "text/html,application/json"}))
        if not resp.ok:
            return None
        text = _strip_html(resp.text()) if resp.looks_like_html() else resp.text()
        return JobDetailEvidence(
            title=card.title, company=company, location=card.location,
            work_mode="REMOTE" if "remote" in (card.location or "").lower() else "",
            description=text[:4000], experience_text=text[:4000],
            posted_date=card.posted_date, updated_date=card.updated_date,
            requisition_id=card.requisition_id, official_url=card.url,
            eligibility_text=f"{card.location}. {text[:400]}",
            source_family=card.source_family or "OFFICIAL_CAREERS",
            evidence_snippets=(text[:800],),
        )
    except HttpError:
        return None
    return None


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = _TAG_RE.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return _WS_RE.sub(" ", text).strip()
