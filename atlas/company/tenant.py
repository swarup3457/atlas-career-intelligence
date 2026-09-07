"""Deterministic ATS tenant/board extraction (Phase 1A.5).

Extracts the stable per-deployment identifier for ATS families whose tenant
is encoded in the URL — with NO network calls. Unknown/unencodable tenants
return ``None`` (never guessed).
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

from atlas.sources.models import SourceType


def _path_segments(url: str) -> list[str]:
    path = urlparse(url).path if "//" in url else urlparse("//" + url).path
    return [seg for seg in path.split("/") if seg]


def _host(url: str) -> str:
    netloc = urlparse(url if "//" in url else "//" + url).netloc
    return netloc.lower().split("@")[-1].split(":")[0]


_WORKDAY_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?:wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)


def _extract_workday(url: str) -> Optional[str]:
    host = _host(url)
    m = _WORKDAY_HOST_RE.match(host)
    if m:
        return m.group("tenant").lower()
    # <tenant>.myworkdayjobs.com fallback (no wdN label).
    if host.endswith(".myworkdayjobs.com"):
        label = host[: -len(".myworkdayjobs.com")].split(".")[0]
        return label.lower() or None
    return None


def _extract_greenhouse(url: str) -> Optional[str]:
    # boards.greenhouse.io/<board> or job-boards.greenhouse.io/<board>
    # or boards.greenhouse.io/embed/job_board?for=<board>
    query = parse_qs(urlparse(url if "//" in url else "//" + url).query)
    if "for" in query and query["for"]:
        return query["for"][0].lower() or None
    segs = _path_segments(url)
    if segs and segs[0] == "embed":
        return None
    return segs[0].lower() if segs else None


def _extract_lever(url: str) -> Optional[str]:
    # jobs.lever.co/<site>
    segs = _path_segments(url)
    return segs[0].lower() if segs else None


def _extract_smartrecruiters(url: str) -> Optional[str]:
    # careers.smartrecruiters.com/<company> or jobs.smartrecruiters.com/<company>
    segs = _path_segments(url)
    return segs[0].lower() if segs else None


_EXTRACTORS = {
    SourceType.ATS_WORKDAY: _extract_workday,
    SourceType.ATS_GREENHOUSE: _extract_greenhouse,
    SourceType.ATS_LEVER: _extract_lever,
    SourceType.ATS_SMARTRECRUITERS: _extract_smartrecruiters,
}


def extract_tenant(source_type: Optional[SourceType], url: Optional[str]) -> Optional[str]:
    """Deterministically extract the tenant/board id for a source URL, or
    ``None`` when the family has no URL-encoded tenant or the URL lacks one."""
    if source_type is None or not url:
        return None
    extractor = _EXTRACTORS.get(source_type)
    if extractor is None:
        return None
    try:
        return extractor(url)
    except (ValueError, IndexError):
        return None


__all__ = ["extract_tenant"]
