"""Deterministic search-query compiler (Phase 1C-A corrective, build spec 11).

Turns a policy lane + geography GROUP + mode into REAL search terms — never the
lane enum label ("JAVA_BACKEND") or the group label ("PRIMARY") as a literal
user search string. No LLM is required for this baseline compilation; it is a
pure, deterministic function of the loaded policy so a run is reproducible and a
:class:`~atlas.sources.query_signature.QuerySignature` reflects the ACTUAL
compiled terms/geography rather than an enum label.

The compiler produces both:

    * a server-side ``primary_query`` + ``geography_terms`` for adapters that
      support real full-text/faceted search (e.g. Workday's ``searchText``); and
    * a local relevance predicate (``is_relevant`` / ``location_in_group``) for
      list-only boards (Greenhouse/Ashby/Lever) whose whole board is normalized
      once and then evaluated PER lane/geography child from that one snapshot —
      so six lanes never trigger six identical network fetches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from atlas.policy.models import GeographyPolicy, SearchLane


def _dedupe_keep_order(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        s = " ".join(str(raw).strip().split())
        if not s:
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(s)
    return tuple(out)


@dataclass(frozen=True)
class CompiledQuery:
    """The compiled, deterministic query for ONE lane × geography group."""

    lane_key: str
    geography_group: str
    mode: str
    query_terms: tuple[str, ...]          # ordered positive search phrases
    negative_terms: tuple[str, ...]       # phrases that reject a posting
    positive_signals: tuple[str, ...]     # any-of signals that make a posting on-lane
    geography_terms: tuple[str, ...]      # canonical + aliases for cities in the group
    policy_version: str = "unversioned"

    @property
    def primary_query(self) -> str:
        """A single bounded search string for server-side search (the first,
        most-specific query hint). Never the lane enum label."""
        return self.query_terms[0] if self.query_terms else ""

    def signature_keywords(self) -> list[str]:
        """Keywords for the QuerySignature — the COMPILED terms, not the enum."""
        return list(self.query_terms)

    def is_relevant(self, title: Optional[str], *, description: Optional[str] = None) -> bool:
        """Local lane relevance for a list-only board: a posting is on-lane when
        it carries at least one positive lane signal and matches no negative
        term. This keeps each lane INDEPENDENT — a Java posting is not React, and
        an unrelated sales/support posting matches no lane signal so it is
        excluded."""
        text = " ".join(t for t in (title, description) if t).lower()
        if not text:
            return False
        for neg in self.negative_terms:
            if neg.lower() in text:
                return False
        for pos in self.positive_signals:
            if pos.lower() in text:
                return True
        return False

    def location_in_group(self, location: Optional[str], geography: GeographyPolicy) -> bool:
        """True when ``location`` normalizes into THIS child's geography group
        (so Bengaluru/Bangalore both match PRIMARY, while a bare 'Remote' — which
        is not worldwide eligibility — matches no group)."""
        loc = geography.normalize(location)
        if loc is not None:
            return loc.group == self.geography_group
        # Fall back to an alias-substring check for compound location strings
        # like "Remote, Bangalore" that do not normalize as a whole.
        low = (location or "").lower()
        return any(term.lower() in low for term in self.geography_terms) if low else False

    def to_dict(self) -> dict:
        return {
            "lane_key": self.lane_key,
            "geography_group": self.geography_group,
            "mode": self.mode,
            "query_terms": list(self.query_terms),
            "negative_terms": list(self.negative_terms),
            "geography_terms": list(self.geography_terms),
            "policy_version": self.policy_version,
        }


class SearchQueryCompiler:
    """Compiles policy lanes + geography into deterministic search queries."""

    def __init__(
        self,
        lanes: Mapping[str, SearchLane],
        geography: GeographyPolicy,
        *,
        policy_version: str = "unversioned",
        global_negative_terms: Iterable[str] = (),
    ):
        self.lanes = dict(lanes)
        self.geography = geography
        self.policy_version = policy_version
        self.global_negative_terms = tuple(global_negative_terms)

    def geography_terms_for(self, group: str) -> tuple[str, ...]:
        """Canonical names + aliases for every city in a geography group."""
        group = (group or "").upper()
        terms: list[str] = []
        for loc in self.geography.locations:
            if loc.group == group:
                terms.append(loc.canonical)
                terms.extend(loc.aliases)
        return _dedupe_keep_order(terms)

    def compile(
        self,
        lane_key: str,
        geography_group: str = "PRIMARY",
        *,
        mode: str = "DELTA",
    ) -> CompiledQuery:
        lane = self.lanes.get(lane_key)
        if lane is None:
            # An unknown/aggregate lane key (e.g. a "+"-joined bundle or "ALL"):
            # compile a broad but still-real query from every known lane's hints
            # rather than emitting the label as a literal search string.
            query_terms = _dedupe_keep_order(
                h for l in self.lanes.values() for h in (l.source_query_hints or l.positive_titles)
            ) or ("software engineer",)
            positive = _dedupe_keep_order(
                s for l in self.lanes.values()
                for s in (*l.positive_titles, *l.technology_terms, *l.role_families)
            )
            negatives = _dedupe_keep_order(
                (*self.global_negative_terms, *(n for l in self.lanes.values() for n in l.negative_terms))
            )
            return CompiledQuery(
                lane_key=lane_key, geography_group=geography_group.upper(), mode=mode,
                query_terms=query_terms, negative_terms=negatives, positive_signals=positive,
                geography_terms=self.geography_terms_for(geography_group), policy_version=self.policy_version,
            )

        query_terms = _dedupe_keep_order(
            (*lane.source_query_hints, *lane.positive_titles)
        ) or (lane.display_name,)
        positive = _dedupe_keep_order(
            (*lane.positive_titles, *lane.technology_terms, *lane.role_families, *lane.source_query_hints)
        )
        negatives = _dedupe_keep_order((*self.global_negative_terms, *lane.negative_terms))
        return CompiledQuery(
            lane_key=lane_key,
            geography_group=geography_group.upper(),
            mode=mode,
            query_terms=query_terms,
            negative_terms=negatives,
            positive_signals=positive,
            geography_terms=self.geography_terms_for(geography_group),
            policy_version=self.policy_version,
        )


__all__ = ["CompiledQuery", "SearchQueryCompiler"]
