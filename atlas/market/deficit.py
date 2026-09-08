"""Adaptive coverage governor — deficit analysis + bounded wave expansion (§11).

Deterministic Python decides whether a completed wave left a coverage DEFICIT and,
if so, plans a bounded Wave N+1. An LLM never decides completion and never adds
an unbounded query. The completion rule is explicit:

    * required sealed coverage still open  -> continue/resume (even if 3 jobs found);
    * all required waves terminal           -> complete truthfully;
    * budget/time exhausted with work left   -> PARTIAL_BUDGET + continuation state;
    * never loop merely to reach a target job count.

Expansion order (build spec 11): deterministic synonyms/query variants -> alternate
geography aliases -> alternate configured source -> official follow-up for portal
leads -> optional typed QueryStrategist proposal (validated, deduped, capped).
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from atlas.market.campaign import (
    CampaignBudget,
    MarketCampaign,
    MarketWave,
    WaveTask,
    WaveTaskKind,
)
from atlas.planning.query_compiler import SearchQueryCompiler


class DeficitKind(str, enum.Enum):
    SOURCE_NOT_ATTEMPTED = "SOURCE_NOT_ATTEMPTED"
    LANE_GEO_INCOMPLETE = "LANE_GEO_INCOMPLETE"
    SOURCE_UNHEALTHY_ZERO = "SOURCE_UNHEALTHY_ZERO"
    PARSER_DRIFT = "PARSER_DRIFT"
    LOW_UNIQUE_YIELD = "LOW_UNIQUE_YIELD"
    HIGH_DUPLICATE_RATIO = "HIGH_DUPLICATE_RATIO"
    PORTAL_LEADS_UNVERIFIED = "PORTAL_LEADS_UNVERIFIED"
    DYNAMIC_COMPANY_NO_SOURCE = "DYNAMIC_COMPANY_NO_SOURCE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIME_EXHAUSTED = "TIME_EXHAUSTED"


# Deficits that budget/time exhaustion makes non-actionable (do not expand).
_BUDGET_DEFICITS = frozenset({DeficitKind.BUDGET_EXHAUSTED, DeficitKind.TIME_EXHAUSTED})


@dataclass(frozen=True)
class WaveDeficit:
    kind: DeficitKind
    lane: Optional[str] = None
    geography: Optional[str] = None
    source_family: Optional[str] = None
    detail: dict = field(default_factory=dict)

    @property
    def deficit_id(self) -> str:
        h = hashlib.sha1(
            f"{self.kind.value}::{self.lane}::{self.geography}::{self.source_family}".encode()
        ).hexdigest()[:12]
        return f"deficit::{h}"


@dataclass
class LaneSourceOutcome:
    """The observed outcome for one (source_family, lane, geography) child."""

    source_family: str
    lane: Optional[str]
    geography: Optional[str]
    attempted: bool = False
    terminal_status: str = "NOT_ATTEMPTED"
    results: int = 0
    unique: int = 0
    duplicates: int = 0
    health: str = "UNKNOWN"

    @property
    def duplicate_ratio(self) -> float:
        total = self.results
        return (self.duplicates / total) if total else 0.0


@dataclass
class WaveOutcome:
    """A deterministic summary of a wave's execution, fed to the analyzer."""

    lane_sources: list[LaneSourceOutcome] = field(default_factory=list)
    required_families: frozenset[str] = frozenset()
    attempted_families: frozenset[str] = frozenset()
    portal_leads_total: int = 0
    portal_leads_unverified: int = 0
    dynamic_companies_without_source: tuple[str, ...] = ()
    budget_exhausted: bool = False
    time_exhausted: bool = False


class CoverageDeficitAnalyzer:
    """Deterministic deficit detection (build spec 11). No LLM, no completion
    authority — it only reports what remains unmet."""

    def __init__(self, *, low_yield_threshold: int = 1, high_dup_ratio: float = 0.6):
        self.low_yield_threshold = low_yield_threshold
        self.high_dup_ratio = high_dup_ratio

    def analyze(self, outcome: WaveOutcome) -> list[WaveDeficit]:
        deficits: list[WaveDeficit] = []
        # Required source not attempted at all.
        for fam in sorted(outcome.required_families - outcome.attempted_families):
            deficits.append(WaveDeficit(DeficitKind.SOURCE_NOT_ATTEMPTED, source_family=fam))
        for ls in outcome.lane_sources:
            if not ls.attempted:
                deficits.append(WaveDeficit(
                    DeficitKind.LANE_GEO_INCOMPLETE, lane=ls.lane, geography=ls.geography,
                    source_family=ls.source_family, detail={"status": ls.terminal_status}))
                continue
            status = (ls.terminal_status or "").upper()
            if status in ("EXTRACTION_UNRESOLVED",) or ls.health == "SELECTOR_DRIFT_SUSPECTED":
                deficits.append(WaveDeficit(
                    DeficitKind.PARSER_DRIFT, lane=ls.lane, geography=ls.geography,
                    source_family=ls.source_family, detail={"health": ls.health}))
            elif status in ("ACCESS_LIMITED", "RATE_LIMITED", "SOURCE_UNAVAILABLE", "AUTH_REQUIRED"):
                deficits.append(WaveDeficit(
                    DeficitKind.SOURCE_UNHEALTHY_ZERO, lane=ls.lane, geography=ls.geography,
                    source_family=ls.source_family, detail={"status": status}))
            elif status in ("PARTIAL_BUDGET",):
                deficits.append(WaveDeficit(
                    DeficitKind.LANE_GEO_INCOMPLETE, lane=ls.lane, geography=ls.geography,
                    source_family=ls.source_family, detail={"status": status}))
            else:
                if ls.results and ls.duplicate_ratio >= self.high_dup_ratio:
                    deficits.append(WaveDeficit(
                        DeficitKind.HIGH_DUPLICATE_RATIO, lane=ls.lane, geography=ls.geography,
                        source_family=ls.source_family, detail={"dup_ratio": round(ls.duplicate_ratio, 2)}))
                if ls.unique < self.low_yield_threshold:
                    deficits.append(WaveDeficit(
                        DeficitKind.LOW_UNIQUE_YIELD, lane=ls.lane, geography=ls.geography,
                        source_family=ls.source_family, detail={"unique": ls.unique}))
        if outcome.portal_leads_unverified > 0:
            deficits.append(WaveDeficit(
                DeficitKind.PORTAL_LEADS_UNVERIFIED,
                detail={"unverified": outcome.portal_leads_unverified, "total": outcome.portal_leads_total}))
        for cid in outcome.dynamic_companies_without_source:
            deficits.append(WaveDeficit(DeficitKind.DYNAMIC_COMPANY_NO_SOURCE, detail={"company_id": cid}))
        if outcome.budget_exhausted:
            deficits.append(WaveDeficit(DeficitKind.BUDGET_EXHAUSTED))
        if outcome.time_exhausted:
            deficits.append(WaveDeficit(DeficitKind.TIME_EXHAUSTED))
        return deficits


# A conservative validator for any query-variant term (deterministic OR from a
# reasoning agent): it must be a short, plain search phrase — no URL, no scheme,
# no angle brackets/instructions, no control characters.
_UNSAFE_VARIANT_RE = re.compile(r"(https?://|://|www\.|[<>{}\[\]`]|\b(ignore|disregard|system|prompt)\b)", re.IGNORECASE)


def validate_variant_term(term: Optional[str]) -> Optional[str]:
    """Return a cleaned variant term, or None if it is not a safe search phrase.
    Rejects URLs/instructions harvested from untrusted posting text (build spec
    11) and bounds length."""
    if not term or not isinstance(term, str):
        return None
    t = " ".join(term.strip().split())
    if not (2 <= len(t) <= 80):
        return None
    if _UNSAFE_VARIANT_RE.search(t):
        return None
    if not any(c.isalnum() for c in t):
        return None
    return t


# Optional reasoning transport: returns candidate variant strings for a lane; the
# planner VALIDATES/DEDUPES/CAPS everything it returns. Never authoritative.
QueryStrategist = Callable[[str, str, list[str]], list[str]]


class WaveExpansionPlanner:
    """Plans a bounded Wave N+1 from a parent wave's deficits (build spec 11)."""

    def __init__(
        self,
        compiler: SearchQueryCompiler,
        geography,
        budget: CampaignBudget,
        *,
        portal_families: tuple[str, ...] = ("linkedin", "naukri"),
        query_strategist: Optional[QueryStrategist] = None,
    ):
        self.compiler = compiler
        self.geography = geography
        self.budget = budget
        self.portal_families = portal_families
        self.query_strategist = query_strategist

    def plan_next_wave(
        self,
        campaign: MarketCampaign,
        parent_wave: MarketWave,
        deficits: list[WaveDeficit],
        *,
        prior_queries: set[tuple[str, str, str]],
        secondary_group: str = "SECONDARY",
    ) -> Optional[MarketWave]:
        """Return the next (unsealed) wave, or None when no bounded expansion is
        warranted (max_waves reached, only budget/time deficits, or no new
        variant survives validation/dedupe)."""
        next_index = parent_wave.wave_index + 1
        if next_index >= self.budget.max_waves:
            return None
        actionable = [d for d in deficits if d.kind not in _BUDGET_DEFICITS]
        if not actionable:
            return None

        tasks: list[WaveTask] = []
        seen_queries: set[tuple[str, str, str]] = set(prior_queries)
        per_lane_variant_count: dict[str, int] = {}

        def _emit_portal(family: str, lane: Optional[str], geo: Optional[str], query: str,
                         origin: str, reason: str, recency_days: Optional[int] = 7) -> None:
            q = validate_variant_term(query)
            if q is None or lane is None:
                return
            key = (family, lane, q.lower())
            if key in seen_queries:
                return
            if per_lane_variant_count.get(lane, 0) >= self.budget.max_variants_per_lane:
                return
            if len(tasks) >= self.budget.max_total_tasks:
                return
            seen_queries.add(key)
            per_lane_variant_count[lane] = per_lane_variant_count.get(lane, 0) + 1
            tid = "wtask::" + hashlib.sha1(
                f"{campaign.campaign_id}::{next_index}::{family}::{lane}::{geo}::{q}".encode()
            ).hexdigest()[:14]
            tasks.append(WaveTask(
                kind=WaveTaskKind.PORTAL, task_id=tid, lane=lane, geography=geo,
                source_family=family, query=q, recency_days=recency_days, origin=origin,
                detail={"reason": reason},
            ))

        for d in actionable:
            if d.kind == DeficitKind.PORTAL_LEADS_UNVERIFIED:
                continue  # handled by the OFFICIAL_FOLLOWUP path in the runtime
            if d.kind == DeficitKind.DYNAMIC_COMPANY_NO_SOURCE:
                cid = d.detail.get("company_id")
                if cid:
                    tid = "wtask::" + hashlib.sha1(
                        f"{campaign.campaign_id}::{next_index}::domdisc::{cid}".encode()).hexdigest()[:14]
                    tasks.append(WaveTask(
                        kind=WaveTaskKind.DOMAIN_DISCOVERY, task_id=tid, company_id=cid,
                        origin="OFFICIAL_FOLLOWUP", detail={"reason": d.kind.value}))
                continue
            lane = d.lane
            fam = d.source_family
            if lane is None or fam not in self.portal_families:
                continue
            geo = d.geography or "PRIMARY"
            try:
                compiled = self.compiler.compile(lane, geo, mode="DELTA")
            except Exception:  # noqa: BLE001
                continue
            # 1. deterministic synonyms / query variants (alternate phrasings).
            for variant in compiled.query_terms[1:]:
                _emit_portal(fam, lane, geo, variant, "SYNONYM", d.kind.value)
            # 2. alternate geography: a bounded SECONDARY-group probe for an
            #    unhealthy / low-yield lane (city aliases within a group are
            #    already applied by the query compiler at execution time).
            if d.kind in (DeficitKind.SOURCE_UNHEALTHY_ZERO, DeficitKind.LOW_UNIQUE_YIELD,
                          DeficitKind.LANE_GEO_INCOMPLETE):
                _emit_portal(fam, lane, secondary_group, compiled.primary_query, "GEO_EXPANSION", d.kind.value)
            # 3. alternate configured source (linkedin<->naukri) for the lane/geo.
            for other in self.portal_families:
                if other != fam:
                    _emit_portal(other, lane, geo, compiled.primary_query, "ALT_SOURCE", d.kind.value)
            # 5. optional typed QueryStrategist proposal (validated + capped).
            if self.query_strategist is not None:
                try:
                    suggestions = self.query_strategist(lane, geo, list(compiled.query_terms))
                except Exception:  # noqa: BLE001 - the LLM never breaks the loop
                    suggestions = []
                for s in suggestions or []:
                    _emit_portal(fam, lane, geo, s, "LLM_STRATEGIST", "query_strategist")

        if not tasks:
            return None
        reason = ", ".join(sorted({d.kind.value for d in actionable}))[:240]
        return MarketWave(
            campaign_id=campaign.campaign_id, run_id=campaign.run_id, wave_index=next_index,
            tasks=tasks, parent_wave_id=parent_wave.wave_id, deficit_reason=reason,
        )


__all__ = [
    "DeficitKind",
    "WaveDeficit",
    "LaneSourceOutcome",
    "WaveOutcome",
    "CoverageDeficitAnalyzer",
    "WaveExpansionPlanner",
    "validate_variant_term",
    "QueryStrategist",
]
