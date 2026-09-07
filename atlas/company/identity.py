"""Deterministic company identity utilities (Phase 1A.5).

Reuses the proven Phase 0.9 normalizers (`identity_token`, `normalize_url`)
rather than inventing a second normalization scheme. Provides:

    * ``company_identity_key`` — an aggressive match token that additionally
      strips a small, deterministic set of legal suffixes (Inc, Corp, Ltd,
      "& Co", …) so "Acme" and "Acme Inc." match, WITHOUT any large
      hard-coded alias database.
    * ``normalize_domain`` — a registrable-host form for domain identity.
    * stable, deterministic ID derivations for companies, source instances,
      relationships, and observations (so rediscovery is idempotent and a
      display-name change never creates a new company).
    * categorical confidence mapping (no fake numeric precision).

Ambiguity is never guessed — unresolved identity stays unresolved.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from atlas.data_integrity.normalizers import identity_token, normalize_text
from atlas.company.models import DiscoveryMethod, SourceConfidence

# A small, deterministic set of legal/organizational suffix tokens removed
# from the *identity* token (never from the display name). This is a
# normalization rule, not an alias database.
_LEGAL_SUFFIX_TOKENS = frozenset(
    {
        "inc", "incorporated", "corp", "corporation", "co", "company",
        "ltd", "limited", "llc", "llp", "lp", "plc", "gmbh", "ag", "sa",
        "nv", "bv", "oy", "ab", "as", "pvt", "private", "holdings", "group",
        "and",  # trailing connector left over from "& Co"
    }
)


def company_identity_key(name: str) -> str:
    """Aggressive, deterministic identity token for a company name, with
    trailing legal suffixes stripped. Returns ``""`` for empty input."""
    token = identity_token(name)
    if not token:
        return ""
    parts = token.split(" ")
    while len(parts) > 1 and parts[-1] in _LEGAL_SUFFIX_TOKENS:
        parts.pop()
    return " ".join(parts)


def canonical_company_name(name: str) -> str:
    """Display-normalized canonical name (whitespace/control normalized,
    case preserved). Falls back to a trimmed string."""
    normalized = normalize_text(name)
    return normalized if normalized else (name or "").strip()


def normalize_domain(value: Optional[str]) -> Optional[str]:
    """Return the registrable host form of a domain/URL, or None.

    Lowercases, strips scheme/path/port/userinfo and a leading ``www.``.
    Requires at least one dot so a bare company name is not mistaken for a
    domain."""
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    from urllib.parse import urlparse

    parsed = urlparse(raw if "//" in raw else "//" + raw)
    host = (parsed.netloc or parsed.path).lower()
    host = host.split("@")[-1].split(":")[0].split("/")[0]
    if host.startswith("www."):
        host = host[4:]
    if "." not in host:
        return None
    return host or None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value.lower()).strip("-")


def _hash(*parts: str) -> str:
    blob = "|".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def derive_company_id(identity_key: str, official_domain: Optional[str] = None) -> str:
    """Deterministic company id. Prefers the official domain (stable across
    display-name changes); falls back to the identity key."""
    domain = normalize_domain(official_domain)
    if domain:
        return "co-" + _hash("domain:" + domain)
    return "co-" + _hash("name:" + (identity_key or ""))


def derive_instance_id(
    company_id: str,
    source_type_value: str,
    *,
    tenant: Optional[str] = None,
    base_url: Optional[str] = None,
) -> str:
    """Deterministic SourceInstance id for a company's source. Rediscovering
    the same tenant/host yields the same id (idempotent)."""
    anchor = None
    if tenant:
        anchor = tenant.strip().lower()
    elif base_url:
        anchor = normalize_domain(base_url) or base_url.strip().lower()
    anchor = anchor or "default"
    return f"{company_id}--{source_type_value.lower()}--{_slug(anchor) or 'default'}"


def derive_relationship_id(company_id: str, instance_id: str) -> str:
    return "rel-" + _hash(company_id, instance_id)


def derive_alias_id(company_id: str, alias_key: str) -> str:
    return "al-" + _hash(company_id, alias_key)


def derive_observation_id(
    company_id: str,
    method_value: str,
    input_url: Optional[str],
    resolved_url: Optional[str],
    detected_ats: Optional[str],
) -> str:
    """Deterministic observation id: identical rediscovery is idempotent, a
    genuinely different discovery event is a new observation."""
    return "obs-" + _hash(
        company_id, method_value, input_url or "", resolved_url or "", detected_ats or ""
    )


def confidence_from_fingerprint(numeric: float) -> SourceConfidence:
    """Map a numeric ATS-fingerprint score to a categorical confidence."""
    if numeric >= 0.9:
        return SourceConfidence.STRONG
    if numeric >= 0.7:
        return SourceConfidence.TENTATIVE
    return SourceConfidence.UNKNOWN


def confidence_for_method(method: DiscoveryMethod) -> SourceConfidence:
    """Baseline confidence implied by a discovery method (fingerprint
    confidence is refined separately from the numeric score)."""
    if method in (
        DiscoveryMethod.EXPLICIT_CONFIG,
        DiscoveryMethod.CONFIRMED_IDENTITY,
        DiscoveryMethod.CANDIDATE_URL,
    ):
        return SourceConfidence.CONFIRMED
    if method == DiscoveryMethod.VALIDATED_REDIRECT:
        return SourceConfidence.STRONG
    if method == DiscoveryMethod.FINGERPRINT:
        return SourceConfidence.TENTATIVE
    return SourceConfidence.UNKNOWN


__all__ = [
    "company_identity_key",
    "canonical_company_name",
    "normalize_domain",
    "derive_company_id",
    "derive_instance_id",
    "derive_relationship_id",
    "derive_alias_id",
    "derive_observation_id",
    "confidence_from_fingerprint",
    "confidence_for_method",
]
