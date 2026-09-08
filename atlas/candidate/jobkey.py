"""Stable candidate-facing job key (Phase 1E/F §8.D).

Application packages live under ``application_packs/<JOB_KEY>/`` and ranking rows
are keyed by ``<JOB_KEY>``. That key MUST be a pure deterministic function of the
job's Atlas *canonical identity* — path safe, Unicode safe, length bounded, and
collision resistant — and must NOT introduce a second, conflicting identity
system. This module therefore derives a path-safe VIEW of the existing canonical
identity (the same requisition-first / company+title+location contract used by
:mod:`atlas.runtime.canonicalize`), it never invents a new identity.

The colon-delimited canonical id (``canon::<hex>``, ``reqid::…``, ``ctl::…``) is
not itself a legal Windows path component, so :func:`stable_job_key` hashes the
canonical identity into a short, filesystem-safe token.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Optional

from atlas.data_integrity.normalizers import identity_token

# ATS families whose confirmed requisition id yields a requisition-first identity
# (mirrors atlas.runtime.canonicalize._FAMILY_APEX keys).
OFFICIAL_FAMILIES = frozenset({"greenhouse", "lever", "ashby", "workday"})

_SAFE_RE = re.compile(r"[^a-z0-9]+")


def canonical_identity(
    *,
    company: str,
    title: str,
    location: str = "",
    source_family: Optional[str] = None,
    source_job_id: Optional[str] = None,
    official: bool = False,
    posted_at: Optional[str] = None,
) -> str:
    """Return the Atlas canonical identity STRING for a job, matching the
    requisition-first contract in :mod:`atlas.runtime.canonicalize`:

    * a confirmed official requisition -> ``reqid::{company}::{family}::{job_id}``;
    * otherwise the fallback ``ctl::{company}::{title}::{location}`` (plus the
      posted date when supplied, so a probable repost stays distinct).

    URL is never part of identity. ``identity_token`` provides the Unicode-safe
    normalization shared with canonicalization, so this never diverges from the
    established identity."""
    fam = (source_family or "").strip().lower()
    jid = (source_job_id or "").strip().lower()
    if official and fam in OFFICIAL_FAMILIES and jid:
        return f"reqid::{identity_token(company)}::{fam}::{jid}"
    base = f"ctl::{identity_token(company)}::{identity_token(title)}::{identity_token(location)}"
    posted = (posted_at or "").strip()
    return f"{base}::{posted}" if posted else base


def stable_job_key(identity: str, *, prefix: str = "job") -> str:
    """Derive a path-safe, Unicode-safe, length-bounded, collision-resistant key
    from an Atlas canonical identity string.

    The output is ``{prefix}_{sha256(identity)[:24]}`` — pure lowercase hex plus
    one underscore, so it is a valid path component on every OS, is deterministic
    for identical input, is bounded (<= len(prefix)+25 chars), and inherits
    SHA-256 collision resistance (96 bits of digest). Passing an existing
    ``canon::``/``reqid::``/``ctl::`` identity is the intended use."""
    norm = unicodedata.normalize("NFKC", str(identity)).strip()
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]
    safe_prefix = _SAFE_RE.sub("", (prefix or "job").lower()) or "job"
    return f"{safe_prefix}_{digest}"


def job_key_for(
    *,
    company: str,
    title: str,
    location: str = "",
    source_family: Optional[str] = None,
    source_job_id: Optional[str] = None,
    official: bool = False,
    posted_at: Optional[str] = None,
) -> str:
    """Convenience: compute the canonical identity for a job's fields and return
    its :func:`stable_job_key`."""
    return stable_job_key(
        canonical_identity(
            company=company, title=title, location=location,
            source_family=source_family, source_job_id=source_job_id,
            official=official, posted_at=posted_at,
        )
    )


__all__ = ["OFFICIAL_FAMILIES", "canonical_identity", "stable_job_key", "job_key_for"]
