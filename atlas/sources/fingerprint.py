"""Atlas ATS fingerprinting (Phase 1A).

Deterministically recognize which ATS family a careers URL / redirect /
safe HTML-marker set belongs to — using domain, path and marker rules only.
No network access, no LLM judgment. Designed to avoid false positives
(host-suffix matching, never naive substring containment) and to return an
explicit "unknown" rather than guessing.

This creates NO company records; it only classifies a source family.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence
from urllib.parse import urlparse

from atlas.sources.models import SourceType


@dataclass(frozen=True)
class ATSFingerprint:
    source_type: Optional[SourceType]
    confidence: float = 0.0
    matched_on: str = ""

    @property
    def matched(self) -> bool:
        return self.source_type is not None

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type.value if self.source_type else None,
            "confidence": self.confidence,
            "matched_on": self.matched_on,
        }


# (source_type, host_suffixes, url_path_substrings, html_markers)
_RULES: tuple[tuple[SourceType, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        SourceType.ATS_WORKDAY,
        ("myworkdayjobs.com", "myworkdaysite.com", "workday.com"),
        ("/wday/", "/en-us/careers"),
        ("workday",),
    ),
    (
        SourceType.ATS_GREENHOUSE,
        ("greenhouse.io", "grnh.se"),
        ("/embed/job_board", "boards.greenhouse.io"),
        ("greenhouse.io", "greenhouse job board", "greenhouse"),
    ),
    (
        SourceType.ATS_LEVER,
        ("lever.co",),
        ("jobs.lever.co",),
        ("lever-",),
    ),
    (
        SourceType.ATS_ASHBY,
        ("ashbyhq.com",),
        ("jobs.ashbyhq.com/",),
        ("ashby", "ashbyhq"),
    ),
    (
        SourceType.ATS_SMARTRECRUITERS,
        ("smartrecruiters.com",),
        ("/careers-api/",),
        ("smartrecruiters",),
    ),
    (
        SourceType.ATS_ORACLE,
        ("oraclecloud.com", "taleo.net", "oracle.com"),
        ("/recruitingcemsui/", "/hcmui/", "/OA_HTML/"),
        ("taleo", "oracle recruiting"),
    ),
    (
        SourceType.ATS_SUCCESSFACTORS,
        ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu"),
        ("/careersection/",),
        ("successfactors",),
    ),
    (
        SourceType.ATS_ICIMS,
        ("icims.com",),
        ("/jobs/search", "/jobs/intro"),
        ("icims",),
    ),
    (
        SourceType.ATS_PHENOM,
        ("phenompeople.com", "phenom.com"),
        ("/prod/",),
        ("phenom",),
    ),
    (
        SourceType.ATS_EIGHTFOLD,
        ("eightfold.ai",),
        ("/careershub/", "/careers/search"),
        ("eightfold",),
    ),
)

_HOST_CONFIDENCE = 0.95
_PATH_CONFIDENCE = 0.85
_MARKER_CONFIDENCE = 0.70


def _host(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc or urlparse("//" + url).netloc
    except (ValueError, TypeError):
        return ""
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host


def _host_matches_suffix(host: str, suffix: str) -> bool:
    suffix = suffix.lower().lstrip(".")
    return bool(host) and (host == suffix or host.endswith("." + suffix))


def fingerprint_ats(
    url: Optional[str] = None,
    *,
    redirect_url: Optional[str] = None,
    markers: Sequence[str] = (),
) -> ATSFingerprint:
    """Classify the ATS family from a URL, an optional redirect target, and
    optional safe HTML/script marker strings. Returns an ``ATSFingerprint``
    whose ``source_type`` is ``None`` when nothing matches (never a guess).
    Host-suffix matches win over path matches, which win over marker matches.
    """
    hosts = [h for h in (_host(url), _host(redirect_url)) if h]
    full_urls = [u.lower() for u in (url, redirect_url) if u]
    marker_blob = " ".join(m.lower() for m in markers)

    best: Optional[ATSFingerprint] = None

    def consider(candidate: ATSFingerprint) -> None:
        nonlocal best
        if best is None or candidate.confidence > best.confidence:
            best = candidate

    for source_type, host_suffixes, path_subs, html_markers in _RULES:
        # Host-suffix match (highest confidence, false-positive safe).
        for suffix in host_suffixes:
            if any(_host_matches_suffix(h, suffix) for h in hosts):
                consider(ATSFingerprint(source_type, _HOST_CONFIDENCE, f"host~{suffix}"))
        # Path/URL substring match.
        for sub in path_subs:
            if any(sub.lower() in u for u in full_urls):
                consider(ATSFingerprint(source_type, _PATH_CONFIDENCE, f"url~{sub}"))
        # HTML/script marker match.
        for marker in html_markers:
            if marker_blob and marker.lower() in marker_blob:
                consider(ATSFingerprint(source_type, _MARKER_CONFIDENCE, f"marker~{marker}"))

    if best is not None:
        return best
    return ATSFingerprint(None, 0.0, "no known ATS pattern")


def fingerprint_all(
    url: Optional[str] = None,
    *,
    redirect_url: Optional[str] = None,
    markers: Sequence[str] = (),
) -> list[ATSFingerprint]:
    """Return every family that matched (sorted by descending confidence),
    useful for diagnostics when a URL carries signals for more than one."""
    hosts = [h for h in (_host(url), _host(redirect_url)) if h]
    full_urls = [u.lower() for u in (url, redirect_url) if u]
    marker_blob = " ".join(m.lower() for m in markers)
    out: dict[SourceType, ATSFingerprint] = {}

    def consider(candidate: ATSFingerprint) -> None:
        assert candidate.source_type is not None
        cur = out.get(candidate.source_type)
        if cur is None or candidate.confidence > cur.confidence:
            out[candidate.source_type] = candidate

    for source_type, host_suffixes, path_subs, html_markers in _RULES:
        for suffix in host_suffixes:
            if any(_host_matches_suffix(h, suffix) for h in hosts):
                consider(ATSFingerprint(source_type, _HOST_CONFIDENCE, f"host~{suffix}"))
        for sub in path_subs:
            if any(sub.lower() in u for u in full_urls):
                consider(ATSFingerprint(source_type, _PATH_CONFIDENCE, f"url~{sub}"))
        for marker in html_markers:
            if marker_blob and marker.lower() in marker_blob:
                consider(ATSFingerprint(source_type, _MARKER_CONFIDENCE, f"marker~{marker}"))

    return sorted(out.values(), key=lambda f: (-f.confidence, f.source_type.value))


__all__ = ["ATSFingerprint", "fingerprint_ats", "fingerprint_all"]
