"""OfficialUrlTrustPolicy — trusted-source gate for career discovery (Phase 1C-B).

Before Atlas will search a career entry point, its URL must be provably
OFFICIAL. This policy is the single authority for that decision. It is
deliberately conservative and FAILS CLOSED:

    * a URL is trusted only when its host EXACTLY equals the verified official
      domain, is a genuine subdomain of it, is an explicitly approved
      subdomain, or is a KNOWN ATS tenant host (Greenhouse/Lever/Ashby/Workday
      and the other fingerprinted families);
    * host matching is suffix-exact (``host == apex`` or ``host.endswith('.'+apex)``)
      so a typo-squat / look-alike (``evilgreenhouse.io``, ``acme.com.evil.example``)
      is REJECTED, never accepted by a substring test;
    * a URL discovered INSIDE job-posting text is never trusted on that basis
      (posting content is untrusted data);
    * a redirect chain is trusted only when EVERY hop stays on an approved host
      and the FINAL target is itself trusted; the whole chain is preserved as
      evidence;
    * ambiguity (no official domain, an unparseable host, a downgrade) is
      UNTRUSTED, never a guess.

This module performs NO network I/O — it classifies URLs/redirect-chains that a
bounded fetcher already obtained.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional, Sequence
from urllib.parse import urlsplit

from atlas.careers.extract import host_of
from atlas.sources.ats.base import host_trusted_for
from atlas.sources.fingerprint import fingerprint_ats
from atlas.sources.models import SourceType


class TrustKind(str, enum.Enum):
    OFFICIAL_DOMAIN = "OFFICIAL_DOMAIN"
    OFFICIAL_SUBDOMAIN = "OFFICIAL_SUBDOMAIN"
    APPROVED_SUBDOMAIN = "APPROVED_SUBDOMAIN"
    KNOWN_ATS = "KNOWN_ATS"
    UNTRUSTED = "UNTRUSTED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class TrustDecision:
    trusted: bool
    kind: TrustKind
    url: Optional[str]
    host: str = ""
    reason: str = ""
    ats_source_type: Optional[SourceType] = None
    redirect_chain: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "trusted": self.trusted,
            "kind": self.kind.value,
            "url": self.url,
            "host": self.host,
            "reason": self.reason,
            "ats_source_type": self.ats_source_type.value if self.ats_source_type else None,
            "redirect_chain": list(self.redirect_chain),
        }


# The KNOWN ATS apex hosts Atlas recognizes (mirrors the fingerprint rules).
# A host on one of these apexes is a legitimate employer-controlled ATS tenant
# and is trusted as an OFFICIAL source route.
KNOWN_ATS_APEXES: frozenset[str] = frozenset(
    {
        "myworkdayjobs.com", "myworkdaysite.com",
        "greenhouse.io", "grnh.se",
        "lever.co",
        "ashbyhq.com",
        "smartrecruiters.com",
        "oraclecloud.com", "taleo.net",
        "successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu",
        "icims.com",
        "phenompeople.com",
        "eightfold.ai",
    }
)


def _normalize_domain(domain: Optional[str]) -> str:
    if not domain:
        return ""
    d = domain.strip().lower()
    if "://" in d:
        d = host_of(d)
    d = d.split("/")[0].split("@")[-1].split(":")[0].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return d


class OfficialUrlTrustPolicy:
    """Decides whether a career-entry URL is a trusted official source.

    Constructed with the company's VERIFIED official domain (from the Company
    Registry) plus any explicitly approved extra subdomains/hosts (e.g. a
    known ``careers.acme-cdn.com`` the operator confirmed). Nothing else is
    trusted.
    """

    def __init__(
        self,
        official_domain: Optional[str],
        *,
        approved_hosts: Sequence[str] = (),
        allow_known_ats: bool = True,
    ):
        self.official_domain = _normalize_domain(official_domain)
        self.approved_hosts = frozenset(_normalize_domain(h) for h in approved_hosts if h)
        self.allow_known_ats = allow_known_ats

    # -- single URL ---------------------------------------------------------
    def classify(self, url: Optional[str], *, from_posting_text: bool = False) -> TrustDecision:
        """Classify one URL. ``from_posting_text=True`` (a URL found inside a job
        description) is ALWAYS untrusted — posting content is data, never a
        source of truth for an official domain."""
        if not url:
            return TrustDecision(False, TrustKind.AMBIGUOUS, url, reason="empty url")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return TrustDecision(False, TrustKind.UNTRUSTED, url, reason=f"non-http scheme {parts.scheme!r}")
        host = host_of(url)
        if not host:
            return TrustDecision(False, TrustKind.AMBIGUOUS, url, reason="unparseable host")
        if from_posting_text:
            return TrustDecision(
                False, TrustKind.UNTRUSTED, url, host=host,
                reason="URL found inside untrusted posting text; never trusted on that basis",
            )

        # 1. Exact official domain / genuine subdomain.
        if self.official_domain:
            if host == self.official_domain:
                return TrustDecision(True, TrustKind.OFFICIAL_DOMAIN, url, host=host,
                                     reason=f"exact official domain {self.official_domain}")
            if host_trusted_for(host, self.official_domain):
                return TrustDecision(True, TrustKind.OFFICIAL_SUBDOMAIN, url, host=host,
                                     reason=f"subdomain of official domain {self.official_domain}")

        # 2. Explicitly approved extra host.
        for approved in self.approved_hosts:
            if approved and (host == approved or host_trusted_for(host, approved)):
                return TrustDecision(True, TrustKind.APPROVED_SUBDOMAIN, url, host=host,
                                     reason=f"approved host {approved}")

        # 3. Known ATS tenant host (employer-controlled deployment).
        if self.allow_known_ats:
            for apex in KNOWN_ATS_APEXES:
                if host == apex or host_trusted_for(host, apex):
                    fp = fingerprint_ats(url)
                    return TrustDecision(
                        True, TrustKind.KNOWN_ATS, url, host=host,
                        reason=f"known ATS host on {apex}",
                        ats_source_type=fp.source_type,
                    )

        # 4. No official domain configured at all → ambiguous, fail closed.
        if not self.official_domain and not self.approved_hosts:
            return TrustDecision(False, TrustKind.AMBIGUOUS, url, host=host,
                                 reason="no verified official domain to compare against")

        return TrustDecision(False, TrustKind.UNTRUSTED, url, host=host,
                             reason=f"host {host!r} is not the official domain, an approved host, or a known ATS")

    # -- redirect chain -----------------------------------------------------
    def classify_redirect_chain(self, chain: Sequence[str]) -> TrustDecision:
        """Trust a redirect chain only when EVERY hop stays on an approved host
        AND the final target is itself trusted. An HTTPS→HTTP downgrade anywhere
        in the chain is rejected. The whole chain is preserved as evidence."""
        hops = [h for h in chain if h]
        if not hops:
            return TrustDecision(False, TrustKind.AMBIGUOUS, None, reason="empty redirect chain")
        # Downgrade check across the chain.
        prev_scheme = None
        for hop in hops:
            scheme = (urlsplit(hop).scheme or "").lower()
            if prev_scheme == "https" and scheme == "http":
                return TrustDecision(
                    False, TrustKind.UNTRUSTED, hops[-1], host=host_of(hops[-1]),
                    reason="HTTPS→HTTP downgrade in redirect chain", redirect_chain=tuple(hops),
                )
            prev_scheme = scheme
        # Every hop must be individually trusted (stays official / known ATS).
        for hop in hops:
            decision = self.classify(hop)
            if not decision.trusted:
                return TrustDecision(
                    False, decision.kind, hops[-1], host=host_of(hops[-1]),
                    reason=f"redirect hop {hop!r} not trusted: {decision.reason}",
                    redirect_chain=tuple(hops),
                )
        final = self.classify(hops[-1])
        return TrustDecision(
            final.trusted, final.kind, hops[-1], host=host_of(hops[-1]),
            reason=f"redirect chain trusted; final={final.reason}",
            ats_source_type=final.ats_source_type, redirect_chain=tuple(hops),
        )


__all__ = [
    "TrustKind",
    "TrustDecision",
    "KNOWN_ATS_APEXES",
    "OfficialUrlTrustPolicy",
]
