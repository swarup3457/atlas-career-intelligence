"""Deterministic query signatures (Phase 1B, build spec 7.7).

Source health and yield history must be keyed by *what was actually
searched*, not merely by source instance. Otherwise a legitimate zero for
one lane/geography/mode is compared against unrelated historical searches
and falsely flagged as selector drift (P0-6).

A :class:`QuerySignature` captures the normalized dimensions of a search
and produces a stable fingerprint. Two searches with the same normalized
dimensions share a signature; changing lane, geography group, mode, the
keyword bundle, relevant filters, the cursor/page family, or the policy
version yields a different signature.

The signature carries NO candidate PII — only source/lane/geo/mode/filter
identifiers and a policy version string.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from atlas.sources.models import SourceFamily, SourceInstance


def _norm_keywords(keywords) -> tuple[str, ...]:
    """Normalize a keyword bundle: lowercase, strip, de-duplicate, sort — so
    ``["Java","java "]`` and ``["java"]`` share a signature and order does
    not matter."""
    if keywords is None:
        return ()
    if isinstance(keywords, str):
        keywords = [keywords]
    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords:
        norm = " ".join(str(kw).lower().split())
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return tuple(sorted(out))


def _norm_filters(filters: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not filters:
        return {}
    out: dict[str, Any] = {}
    for key in sorted(filters):
        val = filters[key]
        if isinstance(val, (list, tuple, set)):
            val = sorted(str(v).lower() for v in val)
        elif isinstance(val, str):
            val = val.lower().strip()
        out[str(key).lower()] = val
    return out


@dataclass(frozen=True)
class QuerySignature:
    source_instance: str
    adapter_key: SourceFamily
    lane: Optional[str] = None
    geography_group: Optional[str] = None
    search_mode: str = "DELTA"          # DELTA | DEEP
    keywords: tuple[str, ...] = ()
    filters: Mapping[str, Any] = field(default_factory=dict)
    cursor_family: Optional[str] = None  # a page/cursor *family*, not a raw page number
    policy_version: str = "unversioned"

    @classmethod
    def build(
        cls,
        *,
        source_instance: str,
        adapter_key: SourceFamily,
        lane: Optional[str] = None,
        geography_group: Optional[str] = None,
        search_mode: str = "DELTA",
        keywords=None,
        filters: Optional[Mapping[str, Any]] = None,
        cursor_family: Optional[str] = None,
        policy_version: str = "unversioned",
    ) -> "QuerySignature":
        mode = str(search_mode).upper()
        if mode not in ("DELTA", "DEEP"):
            raise ValueError(f"search_mode must be DELTA or DEEP, got {search_mode!r}")
        return cls(
            source_instance=source_instance,
            adapter_key=adapter_key,
            lane=(lane.upper() if isinstance(lane, str) else lane),
            geography_group=(geography_group.upper() if isinstance(geography_group, str) else geography_group),
            search_mode=mode,
            keywords=_norm_keywords(keywords),
            filters=_norm_filters(filters),
            cursor_family=cursor_family,
            policy_version=policy_version,
        )

    @classmethod
    def for_instance(
        cls,
        instance: SourceInstance,
        *,
        lane: Optional[str] = None,
        geography_group: Optional[str] = None,
        search_mode: str = "DELTA",
        keywords=None,
        filters: Optional[Mapping[str, Any]] = None,
        cursor_family: Optional[str] = None,
        policy_version: str = "unversioned",
    ) -> "QuerySignature":
        return cls.build(
            source_instance=instance.instance_id,
            adapter_key=instance.adapter_key,
            lane=lane,
            geography_group=geography_group,
            search_mode=search_mode,
            keywords=keywords,
            filters=filters,
            cursor_family=cursor_family,
            policy_version=policy_version,
        )

    def _canonical(self) -> dict[str, Any]:
        return {
            "source_instance": self.source_instance,
            "adapter_key": self.adapter_key.value,
            "lane": self.lane,
            "geography_group": self.geography_group,
            "search_mode": self.search_mode,
            "keywords": list(self.keywords),
            "filters": dict(self.filters),
            "cursor_family": self.cursor_family,
            "policy_version": self.policy_version,
        }

    def fingerprint(self) -> str:
        """Stable sha256 hex digest of the normalized dimensions."""
        blob = json.dumps(self._canonical(), sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def key(self) -> str:
        """Short, human-scannable key (first 16 hex chars of the fingerprint)."""
        return self.fingerprint()[:16]

    def to_dict(self) -> dict[str, Any]:
        data = self._canonical()
        data["fingerprint"] = self.fingerprint()
        return data


__all__ = ["QuerySignature"]
