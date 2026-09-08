"""Dependency-free career-page extraction primitives (Phase 1C-B, build spec 8).

Read-only, bounded, script-free extraction from OFFICIAL public career pages.
Every function here is pure (no network, no browser) so it is exhaustively
unit-testable with fixtures. The rules encoded here mirror the ATS adapters'
safety posture and the untrusted-content contract:

    * job-posting text is DATA, never an instruction, and a link found INSIDE a
      description is NEVER surfaced as a job link to follow;
    * ``<script>``/``<style>`` is never executed and its raw text is never
      treated as content;
    * embedded application-state JSON is parsed with STRICT size and depth
      bounds (decompression/'billion-laughs'-style blowups are refused);
    * one malformed item is isolated (via :func:`atlas.sources.parsing.parse_isolated`)
      and never aborts the batch;
    * an employer POSTED date (``datePosted``) is distinguished from a crawl
      time — the crawl time is the caller's ``discovered_at`` and is never
      passed off as a posting date;
    * unknown values stay ``None`` and are NEVER invented.

The single normalized output shape is :class:`ExtractedJob`; a source adapter
maps it to a :class:`atlas.sources.models.DiscoveryResult`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlsplit

from atlas.sources.ats.base import sanitize_description, work_mode_from_text

# --- hard bounds (never trust an unbounded page) ---------------------------
MAX_EMBEDDED_JSON_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 40
MAX_JOBS_PER_PAGE = 500
MAX_LINKS_SCANNED = 5000

PARSER_VERSION = "career-extract-1.0.0"


# ---------------------------------------------------------------------------
# Normalized extracted job
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExtractedJob:
    """One normalized job extracted from an official page. Missing values stay
    ``None`` and are never invented. ``description`` is populated only on a
    detail extraction. ``posted_at`` carries ONLY an employer-declared posted
    date (never a crawl time)."""

    title: str
    url: Optional[str] = None
    source_job_id: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    posted_at: Optional[str] = None
    updated_at: Optional[str] = None
    deadline: Optional[str] = None
    employment_type: Optional[str] = None
    salary_text: Optional[str] = None
    description: Optional[str] = None
    remote: Optional[bool] = None
    is_active: Optional[bool] = None
    extraction_method: str = ""
    date_provenance: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source_job_id": self.source_job_id,
            "company": self.company,
            "location": self.location,
            "posted_at": self.posted_at,
            "updated_at": self.updated_at,
            "deadline": self.deadline,
            "employment_type": self.employment_type,
            "salary_text": self.salary_text,
            "description": self.description,
            "remote": self.remote,
            "is_active": self.is_active,
            "extraction_method": self.extraction_method,
            "date_provenance": self.date_provenance,
        }


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def host_of(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        netloc = urlsplit(url).netloc or urlsplit("//" + url).netloc
    except (ValueError, TypeError):
        return ""
    return netloc.lower().split("@")[-1].split(":")[0].rstrip(".")


def _registrable(host: str) -> str:
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def same_site(a: Optional[str], b: Optional[str]) -> bool:
    """True when two URLs share a registrable domain (host or a subdomain of
    it). Used to keep discovered job links on the OFFICIAL site."""
    ha, hb = host_of(a), host_of(b)
    if not ha or not hb:
        return False
    if ha == hb:
        return True
    ra, rb = _registrable(ha), _registrable(hb)
    return ra == rb and bool(ra)


def normalize_url(base: Optional[str], href: Optional[str]) -> Optional[str]:
    """Resolve ``href`` against ``base`` into an absolute http(s) URL, dropping
    the fragment. Returns ``None`` for empty, non-http(s), ``javascript:``,
    ``mailto:``, ``tel:``, ``data:`` and other non-navigational schemes so a
    hostile/irrelevant target is never surfaced as a job URL."""
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    low = href.lower()
    for bad in ("javascript:", "mailto:", "tel:", "data:", "vbscript:", "file:", "about:"):
        if low.startswith(bad):
            return None
    try:
        resolved = urljoin(base or "", href)
    except (ValueError, TypeError):
        return None
    parts = urlsplit(resolved)
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.netloc:
        return None
    # Drop the fragment; keep query (many boards page/identify via query).
    cleaned = parts._replace(fragment="")
    return cleaned.geturl()


# A conservative job-detail URL signature. Deliberately narrow to avoid
# classifying category/search/marketing links as jobs (false positives are
# worse than a missed link the browser path can still find).
JOB_LINK_PATTERN = re.compile(
    r"(/job[-_/]?detail|/jobs?/[0-9]|/job/(?!categor|search|alert)"
    r"|/careers?/(?:job|opening|position|vacanc)"
    r"|/opening[s]?/[0-9A-Za-z]|/position[s]?/[0-9]"
    r"|jobid=|req(?:uisition)?[-_]?id=|/vacanc(?:y|ies)/[0-9A-Za-z]"
    r"|/gh_jid=|/apply/[0-9]|/o/[0-9A-Za-z]{6,})",
    re.IGNORECASE,
)

# Obvious NON-job link markers (navigation/marketing/legal). If a URL matches
# one of these it is never treated as a job even if JOB_LINK_PATTERN also hits.
_NON_JOB_MARKERS = re.compile(
    r"(/privacy|/cookie|/terms|/legal|/benefits|/culture|/about|/contact"
    r"|/login|/sign[-_]?in|/faq|/blog|/news|/press|/events?|/team|/life|"
    r"linkedin\.com|facebook\.com|twitter\.com|instagram\.com|youtube\.com)",
    re.IGNORECASE,
)


def classify_job_link(url: Optional[str]) -> bool:
    """True when ``url`` looks like a specific job-detail page (not a category,
    search, marketing, or social link). Conservative by design."""
    if not url:
        return False
    if _NON_JOB_MARKERS.search(url):
        return False
    return bool(JOB_LINK_PATTERN.search(url))


# ---------------------------------------------------------------------------
# HTML parsing (stdlib only)
# ---------------------------------------------------------------------------
class _ScriptCollector(HTMLParser):
    """Collects the raw text of every ``<script>`` block, keyed by (type, id),
    plus ``<link rel=...>`` targets and the document ``<title>``. Never runs a
    script — it only captures text for offline JSON parsing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ld_json: list[str] = []
        self.app_json: list[tuple[str, str]] = []  # (id, text)
        self.rel_links: list[tuple[str, str]] = []  # (rel, href)
        self.title: Optional[str] = None
        self._in_script: Optional[str] = None  # the script "kind" or None
        self._script_id: str = ""
        self._buf: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            stype = a.get("type", "").lower().strip()
            self._script_id = a.get("id", "")
            if stype in ("application/ld+json",):
                self._in_script = "ld"
            elif stype in ("application/json",):
                self._in_script = "app"
            else:
                self._in_script = None
            self._buf = []
        elif tag == "link":
            rel = a.get("rel", "").lower().strip()
            href = a.get("href", "")
            if rel and href:
                self.rel_links.append((rel, href))
        elif tag == "title":
            self._in_title = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script is not None:
            text = "".join(self._buf).strip()
            if self._in_script == "ld" and text:
                self.ld_json.append(text)
            elif self._in_script == "app" and text:
                self.app_json.append((self._script_id, text))
            self._in_script = None
            self._buf = []
        elif tag == "title" and self._in_title:
            self.title = "".join(self._buf).strip() or None
            self._in_title = False
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_script is not None or self._in_title:
            self._buf.append(data)


class _AnchorCollector(HTMLParser):
    """Collects ``<a href>`` anchors with their visible text, EXCLUDING anchors
    that appear inside a description/article/main-content region so a link found
    inside posting text is never surfaced as a job link (build spec 8)."""

    _SKIP_CONTAINERS = frozenset(
        {"script", "style", "template", "noscript"}
    )
    # Regions that typically hold posting BODY text; links inside are ignored.
    _DESCRIPTION_CONTAINERS = frozenset({"article"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []  # (text, href)
        self._skip_depth = 0
        self._desc_depth = 0
        self._href: Optional[str] = None
        self._text: list[str] = []
        self._count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in self._SKIP_CONTAINERS:
            self._skip_depth += 1
            return
        if tag in self._DESCRIPTION_CONTAINERS:
            self._desc_depth += 1
        if tag == "a" and self._skip_depth == 0 and self._count < MAX_LINKS_SCANNED:
            a = {k.lower(): (v or "") for k, v in attrs}
            self._href = a.get("href")
            self._text = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # self-closing tags cannot open an anchor region
        return

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_CONTAINERS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in self._DESCRIPTION_CONTAINERS and self._desc_depth > 0:
            self._desc_depth -= 1
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            # A link inside a description region is ignored as a job link.
            if self._desc_depth == 0:
                self.anchors.append((text, self._href))
                self._count += 1
            self._href = None
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None and self._skip_depth == 0:
            self._text.append(data)


def extract_scripts(html: str) -> _ScriptCollector:
    collector = _ScriptCollector()
    try:
        collector.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must not crash extraction
        pass
    return collector


def looks_like_spa_shell(html: str, *, extracted_jobs: int = 0) -> bool:
    """Heuristic: a mostly-empty HTML shell whose content is rendered client-side
    (React/Vue/Angular root with little server-rendered job content). Used to
    justify (with evidence) a browser fallback — NEVER to browse a structured
    page that merely has a parser bug."""
    if extracted_jobs > 0:
        return False
    low = html.lower()
    shell_markers = (
        'id="root"', "id='root'", 'id="__next"', 'id="app"', "ng-version",
        "data-reactroot", "window.__nuxt__", "__next_data__",
    )
    has_shell = any(m in low for m in shell_markers)
    # Very little textual content between tags is another SPA signal.
    text_only = re.sub(r"<[^>]+>", " ", html)
    text_only = re.sub(r"\s+", " ", text_only).strip()
    return bool(has_shell and len(text_only) < 1500)


# ---------------------------------------------------------------------------
# JSON-LD JobPosting
# ---------------------------------------------------------------------------
def _iter_jsonld_objects(blocks: Iterable[str]) -> list[dict]:
    """Parse each ``application/ld+json`` block and yield every dict node,
    flattening ``@graph`` and top-level arrays. One malformed block is isolated
    and skipped."""
    out: list[dict] = []
    for raw in blocks:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        _collect_jsonld_nodes(data, out, depth=0)
    return out


def _collect_jsonld_nodes(data: Any, out: list[dict], *, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        return
    if isinstance(data, list):
        for item in data:
            _collect_jsonld_nodes(item, out, depth=depth + 1)
        return
    if not isinstance(data, dict):
        return
    out.append(data)
    graph = data.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            _collect_jsonld_nodes(item, out, depth=depth + 1)


def _type_matches(node: dict, wanted: str) -> bool:
    t = node.get("@type")
    if isinstance(t, str):
        return t.lower() == wanted.lower()
    if isinstance(t, list):
        return any(isinstance(x, str) and x.lower() == wanted.lower() for x in t)
    return False


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        # schema.org often nests {"@value": ...} or {"name": ...}
        for key in ("@value", "name", "value", "text"):
            if key in value:
                return _text(value[key])
    if isinstance(value, list):
        parts = [_text(v) for v in value]
        parts = [p for p in parts if p]
        return ", ".join(parts) or None
    return None


def _jsonld_location(node: dict) -> Optional[str]:
    """Assemble a human location string from schema.org ``jobLocation``.
    Multiple locations are joined; missing pieces are simply omitted (never
    invented)."""
    loc = node.get("jobLocation")
    locations: list[str] = []

    def _one(item: Any) -> Optional[str]:
        if not isinstance(item, dict):
            return _text(item)
        addr = item.get("address")
        if isinstance(addr, str):
            return addr.strip() or None
        if not isinstance(addr, dict):
            return _text(item.get("name"))
        pieces = [
            _text(addr.get("addressLocality")),
            _text(addr.get("addressRegion")),
            _text(addr.get("addressCountry")),
        ]
        pieces = [p for p in pieces if p]
        return ", ".join(pieces) or None

    if isinstance(loc, list):
        for item in loc:
            one = _one(item)
            if one:
                locations.append(one)
    elif loc is not None:
        one = _one(loc)
        if one:
            locations.append(one)

    # Deduplicate preserving order.
    seen: set[str] = set()
    unique = [x for x in locations if not (x in seen or seen.add(x))]
    return " | ".join(unique) or None


def _jsonld_remote(node: dict) -> Optional[bool]:
    jlt = _text(node.get("jobLocationType"))
    if jlt and "telecommute" in jlt.lower():
        return True
    if node.get("applicantLocationRequirements"):
        # explicit remote-eligibility requirement present
        return True
    return None


def _jsonld_identifier(node: dict) -> Optional[str]:
    ident = node.get("identifier")
    if isinstance(ident, dict):
        return _text(ident.get("value")) or _text(ident.get("name"))
    return _text(ident)


def jobposting_from_jsonld(node: dict, *, base_url: Optional[str] = None) -> Optional[ExtractedJob]:
    """Normalize ONE schema.org ``JobPosting`` node. Returns ``None`` if the
    node is not a JobPosting or has no usable title. Raises ``ValueError`` for a
    structurally-broken node so :func:`parse_isolated` can record a finding."""
    if not _type_matches(node, "JobPosting"):
        return None
    title = _text(node.get("title"))
    if not title:
        raise ValueError("JobPosting missing title")
    url = normalize_url(base_url, _text(node.get("url")))
    hiring = node.get("hiringOrganization")
    company = _text(hiring.get("name")) if isinstance(hiring, dict) else _text(hiring)
    posted_at = _text(node.get("datePosted"))
    valid_through = _text(node.get("validThrough"))
    emp_type = _text(node.get("employmentType"))
    desc = node.get("description")
    description = sanitize_description(desc) if isinstance(desc, str) else None
    location = _jsonld_location(node)
    remote = _jsonld_remote(node)
    salary = None
    base_salary = node.get("baseSalary")
    if isinstance(base_salary, dict):
        salary = _text(base_salary.get("value")) or _text(base_salary)
    return ExtractedJob(
        title=title,
        url=url,
        source_job_id=_jsonld_identifier(node),
        company=company,
        location=location,
        posted_at=posted_at,
        deadline=valid_through,
        employment_type=emp_type,
        salary_text=salary,
        description=description,
        remote=remote,
        # A JobPosting present on a live official page implies it is being
        # advertised; absence of a closed banner is NOT proof, so stay None
        # unless the caller/detail says otherwise.
        is_active=None,
        extraction_method="jsonld",
        date_provenance="EMPLOYER_POSTED_AT" if posted_at else "UNKNOWN",
    )


def extract_jsonld_jobs(html: str, *, base_url: Optional[str] = None) -> list[ExtractedJob]:
    """Extract every schema.org ``JobPosting`` from a page's JSON-LD blocks.
    Isolated per node; a malformed node is skipped, never fatal."""
    from atlas.sources.parsing import parse_isolated

    collector = extract_scripts(html)
    nodes = _iter_jsonld_objects(collector.ld_json)
    parsed = parse_isolated(nodes, lambda n: jobposting_from_jsonld(n, base_url=base_url))
    return list(parsed.results)[:MAX_JOBS_PER_PAGE]


# ---------------------------------------------------------------------------
# Embedded application-state JSON (bounded)
# ---------------------------------------------------------------------------
_ASSIGN_RE = re.compile(
    r"(?:window\.)?(?:__INITIAL_STATE__|__NUXT__|__APOLLO_STATE__|__NEXT_DATA__"
    r"|__PRELOADED_STATE__|APP_STATE|__DATA__)\s*=\s*",
)


def _scan_balanced_json(text: str, start: int) -> Optional[str]:
    """From ``start`` (which must be at a ``{`` or ``[``), return the balanced
    JSON substring, honoring strings/escapes, bounded by
    :data:`MAX_EMBEDDED_JSON_BYTES`. Returns ``None`` if unbalanced or too big."""
    if start >= len(text) or text[start] not in "{[":
        return None
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    limit = min(len(text), start + MAX_EMBEDDED_JSON_BYTES)
    i = start
    while i < limit:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def parse_embedded_state(html: str) -> list[dict]:
    """Extract bounded embedded application-state JSON objects.

    Handles two shapes: a pure ``application/json`` script (e.g. Next.js
    ``__NEXT_DATA__``) and a ``window.__X__ = {...}`` assignment. The parsed
    size is capped and nothing is executed. Returns the top-level dict(s); the
    caller mines them for job-like nodes via :func:`find_job_nodes`."""
    out: list[dict] = []
    collector = extract_scripts(html)
    for _id, text in collector.app_json:
        if len(text.encode("utf-8", errors="ignore")) > MAX_EMBEDDED_JSON_BYTES:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    # Inline assignments inside ordinary <script> blocks are not captured by the
    # collector (it only keeps ld+json / application/json), so scan the raw HTML
    # for the well-known assignment prefixes with a bounded balanced scan.
    for m in _ASSIGN_RE.finditer(html):
        # Skip past whitespace to the opening brace/bracket.
        j = m.end()
        while j < len(html) and html[j] in " \t\r\n":
            j += 1
        blob = _scan_balanced_json(html, j)
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            out.append(data)
        if len(out) >= 20:  # bound the number of embedded blobs considered
            break
    return out


_TITLE_KEYS = ("title", "jobtitle", "name", "positionname", "roletitle")
_URL_KEYS = ("url", "absolute_url", "absoluteurl", "joburl", "canonicalurl", "applyurl", "hosted_url", "link")
_ID_KEYS = ("id", "jobid", "job_id", "requisitionid", "reqid", "shortcode", "slug")
_LOCATION_KEYS = ("location", "locationname", "city", "office", "primarylocation")
_DATE_KEYS = ("datePosted", "dateposted", "posted_at", "postedat", "publishdate", "createdat", "first_published")


def _lower_keys(node: dict) -> dict:
    return {str(k).lower(): v for k, v in node.items()}


def _looks_like_job(node: dict) -> bool:
    lk = _lower_keys(node)
    has_title = any(k in lk and isinstance(lk[k], (str,)) and lk[k].strip() for k in _TITLE_KEYS)
    has_anchor = any(k in lk for k in _URL_KEYS) or any(k in lk for k in _ID_KEYS)
    return has_title and has_anchor


def find_job_nodes(data: Any, *, max_depth: int = MAX_JSON_DEPTH, limit: int = MAX_JOBS_PER_PAGE) -> list[dict]:
    """Recursively (bounded depth) find job-like dicts in embedded JSON: a node
    with a title-ish key AND a url/id anchor, OR a schema.org JobPosting.
    Conservative — a non-job config dict is not returned."""
    found: list[dict] = []

    def _walk(node: Any, depth: int) -> None:
        if len(found) >= limit or depth > max_depth:
            return
        if isinstance(node, dict):
            if _type_matches(node, "JobPosting") or _looks_like_job(node):
                found.append(node)
            else:
                for v in node.values():
                    _walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)

    _walk(data, 0)
    return found


def job_from_embedded_node(node: dict, *, base_url: Optional[str] = None) -> Optional[ExtractedJob]:
    """Normalize an embedded job-like dict. JSON-LD JobPosting nodes defer to
    :func:`jobposting_from_jsonld`. Returns ``None`` when no title is present."""
    if _type_matches(node, "JobPosting"):
        return jobposting_from_jsonld(node, base_url=base_url)
    lk = _lower_keys(node)

    def _first(keys: tuple[str, ...]) -> Optional[str]:
        for k in keys:
            if k in lk:
                val = _text(lk[k])
                if val:
                    return val
        return None

    title = _first(_TITLE_KEYS)
    if not title:
        return None
    raw_url = _first(_URL_KEYS)
    url = normalize_url(base_url, raw_url) if raw_url else None
    posted = _first(_DATE_KEYS)
    location = _first(_LOCATION_KEYS)
    remote = None
    for rk in ("remote", "isremote", "is_remote"):
        if rk in lk and isinstance(lk[rk], bool):
            remote = lk[rk]
            break
    return ExtractedJob(
        title=title,
        url=url,
        source_job_id=_first(_ID_KEYS),
        location=location,
        posted_at=posted,
        remote=remote,
        extraction_method="embedded_json",
        date_provenance="EMPLOYER_POSTED_AT" if posted else "UNKNOWN",
    )


def extract_embedded_jobs(html: str, *, base_url: Optional[str] = None) -> list[ExtractedJob]:
    """Extract jobs from bounded embedded application-state JSON. Isolated per
    node."""
    from atlas.sources.parsing import parse_isolated

    blobs = parse_embedded_state(html)
    nodes: list[dict] = []
    for blob in blobs:
        nodes.extend(find_job_nodes(blob))
    parsed = parse_isolated(nodes, lambda n: job_from_embedded_node(n, base_url=base_url))
    # De-dup by (title, url) preserving order.
    out: list[ExtractedJob] = []
    seen: set[tuple[str, Optional[str]]] = set()
    for job in parsed.results:
        key = (job.title, job.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out[:MAX_JOBS_PER_PAGE]


# ---------------------------------------------------------------------------
# Anchor / list-page job links
# ---------------------------------------------------------------------------
def extract_job_links(html: str, *, base_url: Optional[str], same_host_only: bool = True) -> list[ExtractedJob]:
    """Extract job-detail links from a server-rendered list page. Only anchors
    OUTSIDE description regions are considered (build spec 8), each URL is
    normalized and classified with :func:`classify_job_link`, and (by default)
    off-site links are dropped so a cross-domain link in a posting can never be
    surfaced as a job."""
    parser = _AnchorCollector()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        pass
    out: list[ExtractedJob] = []
    seen: set[str] = set()
    for text, href in parser.anchors:
        url = normalize_url(base_url, href)
        if not url or not classify_job_link(url):
            continue
        if same_host_only and base_url and not same_site(base_url, url):
            continue
        if url in seen:
            continue
        seen.add(url)
        title = text.strip()
        if not title or len(title) < 3:
            # A job link without a usable anchor text still counts, but we do
            # not invent a title — use a bounded slug from the URL path.
            slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            title = slug.replace("-", " ").replace("_", " ").strip() or url
        out.append(
            ExtractedJob(
                title=title[:200],
                url=url,
                extraction_method="anchor",
            )
        )
        if len(out) >= MAX_JOBS_PER_PAGE:
            break
    return out


# ---------------------------------------------------------------------------
# Pagination (ordinary rel=next)
# ---------------------------------------------------------------------------
def find_next_page(html: str, *, base_url: Optional[str]) -> Optional[str]:
    """Return the ordinary next-page URL from a ``<link rel="next">`` or an
    anchor ``rel="next"``/labelled 'next'. Only same-site targets are returned.
    Never fabricates a ``?page=N+1`` URL (that is the adapter's explicit choice,
    not an inferred link)."""
    collector = extract_scripts(html)
    for rel, href in collector.rel_links:
        if "next" in rel.split():
            url = normalize_url(base_url, href)
            if url and (not base_url or same_site(base_url, url)):
                return url
    # Anchor rel=next.
    for m in re.finditer(r"<a\b[^>]*rel=[\"']?[^\"'>]*next[^\"'>]*[\"']?[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        href_m = re.search(r"href=[\"']([^\"']+)[\"']", tag, re.IGNORECASE)
        if href_m:
            url = normalize_url(base_url, href_m.group(1))
            if url and (not base_url or same_site(base_url, url)):
                return url
    return None


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


@dataclass(frozen=True)
class SitemapResult:
    is_index: bool
    urls: tuple[str, ...] = ()

    def job_like(self) -> tuple[str, ...]:
        return tuple(u for u in self.urls if _CAREER_URL_SIGNAL.search(u))


_CAREER_URL_SIGNAL = re.compile(r"(job|career|opening|position|vacanc|/o/|requisition)", re.IGNORECASE)
_SITEMAP_SIGNAL = re.compile(r"(job|career|opening|position|vacanc)", re.IGNORECASE)


def parse_sitemap(xml: str, *, base_url: Optional[str] = None, max_urls: int = 50000) -> SitemapResult:
    """Parse a sitemap or sitemap index. Returns the ``<loc>`` URLs and whether
    the document is a ``<sitemapindex>`` (so a caller can descend into child
    sitemaps that carry job/career signals). URLs are normalized and bounded."""
    is_index = "<sitemapindex" in xml.lower()
    urls: list[str] = []
    for m in _LOC_RE.finditer(xml):
        raw = m.group(1).strip()
        url = normalize_url(base_url, raw) if base_url else (raw if raw.lower().startswith("http") else None)
        if url:
            urls.append(url)
        if len(urls) >= max_urls:
            break
    return SitemapResult(is_index=is_index, urls=tuple(urls))


def child_sitemaps_with_job_signal(result: SitemapResult) -> tuple[str, ...]:
    """From a sitemap INDEX, the child sitemap URLs whose own URL hints at
    job/career content (so we descend selectively, not into every child)."""
    if not result.is_index:
        return ()
    return tuple(u for u in result.urls if _SITEMAP_SIGNAL.search(u))


# ---------------------------------------------------------------------------
# robots.txt (sitemap discovery only — never a bypass)
# ---------------------------------------------------------------------------
def sitemaps_from_robots(robots_txt: str, *, base_url: Optional[str] = None) -> list[str]:
    """Return ``Sitemap:`` URLs declared in robots.txt. Atlas reads robots for
    sitemap DISCOVERY and to DOCUMENT crawl policy — it never uses robots to
    bypass anything."""
    out: list[str] = []
    for line in robots_txt.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            ref = line.split(":", 1)[1].strip()
            url = normalize_url(base_url, ref) if base_url else (ref if ref.lower().startswith("http") else None)
            if url:
                out.append(url)
    return out


@dataclass(frozen=True)
class RobotsPolicy:
    """A minimal read of robots.txt for DOCUMENTATION. Atlas does not crawl
    broadly, so this is advisory context, not an enforcement engine."""

    sitemaps: tuple[str, ...] = ()
    disallow_all_for_star: bool = False
    raw_lines: int = 0


def summarize_robots(robots_txt: str, *, base_url: Optional[str] = None) -> RobotsPolicy:
    sitemaps = tuple(sitemaps_from_robots(robots_txt, base_url=base_url))
    disallow_all = False
    ua_star = False
    for line in robots_txt.splitlines():
        s = line.strip().lower()
        if s.startswith("user-agent:"):
            ua_star = s.split(":", 1)[1].strip() == "*"
        elif ua_star and s.startswith("disallow:"):
            if s.split(":", 1)[1].strip() == "/":
                disallow_all = True
    return RobotsPolicy(sitemaps=sitemaps, disallow_all_for_star=disallow_all,
                        raw_lines=len(robots_txt.splitlines()))


# ---------------------------------------------------------------------------
# Career nav-link discovery (from a homepage)
# ---------------------------------------------------------------------------
_CAREER_LABELS = re.compile(
    r"\b(careers?|jobs?|openings?|opportunities|vacanc(?:y|ies)|"
    r"work with us|join us|join our team|we'?re hiring|life at)\b",
    re.IGNORECASE,
)
_CAREER_PATH = re.compile(r"(/careers?|/jobs?|/opening|/opportunit|/vacanc|/join|/work-with-us)", re.IGNORECASE)


@dataclass(frozen=True)
class CareerLinkCandidate:
    url: str
    label: str
    score: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "label": self.label, "score": round(self.score, 3), "reason": self.reason}


def score_career_links(html: str, *, base_url: str, same_host_only: bool = True) -> list[CareerLinkCandidate]:
    """Score homepage anchors by how strongly they point at a careers section,
    using BOTH the visible label and the URL path. Deterministic scoring; only
    same-site targets by default. Returned sorted by descending score."""
    parser = _AnchorCollector()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        pass
    out: list[CareerLinkCandidate] = []
    seen: set[str] = set()
    for text, href in parser.anchors:
        url = normalize_url(base_url, href)
        if not url:
            continue
        if same_host_only and not same_site(base_url, url):
            continue
        label = " ".join(text.split())
        score = 0.0
        reasons = []
        if _CAREER_LABELS.search(label):
            score += 0.6
            reasons.append("label")
        if _CAREER_PATH.search(urlsplit(url).path):
            score += 0.5
            reasons.append("path")
        # A careers subdomain is a strong signal.
        h = host_of(url)
        if h.split(".")[0] in ("careers", "jobs", "job", "work"):
            score += 0.4
            reasons.append("subdomain")
        if score <= 0:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(CareerLinkCandidate(url=url, label=label[:120], score=min(score, 1.0), reason="+".join(reasons)))
    return sorted(out, key=lambda c: (-c.score, c.url))


__all__ = [
    "ExtractedJob",
    "PARSER_VERSION",
    "MAX_EMBEDDED_JSON_BYTES",
    "MAX_JSON_DEPTH",
    "MAX_JOBS_PER_PAGE",
    "host_of",
    "same_site",
    "normalize_url",
    "classify_job_link",
    "JOB_LINK_PATTERN",
    "extract_scripts",
    "looks_like_spa_shell",
    "jobposting_from_jsonld",
    "extract_jsonld_jobs",
    "parse_embedded_state",
    "find_job_nodes",
    "job_from_embedded_node",
    "extract_embedded_jobs",
    "extract_job_links",
    "find_next_page",
    "SitemapResult",
    "parse_sitemap",
    "child_sitemaps_with_job_signal",
    "sitemaps_from_robots",
    "RobotsPolicy",
    "summarize_robots",
    "CareerLinkCandidate",
    "score_career_links",
]
