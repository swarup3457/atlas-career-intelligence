"""Atlas typed business-policy models (Phase 1B, build spec section 8).

Deterministic, versioned, fingerprintable policy imported from the Workspace
Agent package. Every model is a frozen dataclass built from a plain mapping
(parsed YAML) so the same structures validate, fingerprint, and drive the
planner/matcher without any free-form prose governing execution.

Public policy contains only public company/source/search information — never
candidate PII.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


def _tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


# ---------------------------------------------------------------------------
# Search lanes (build spec 9)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SearchLane:
    key: str
    display_name: str
    role_families: tuple[str, ...] = ()
    positive_titles: tuple[str, ...] = ()
    technology_terms: tuple[str, ...] = ()
    negative_terms: tuple[str, ...] = ()
    transferable_evidence: tuple[str, ...] = ()
    source_query_hints: tuple[str, ...] = ()
    detail_hydration_hints: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, key: str, raw: Mapping[str, Any]) -> "SearchLane":
        return cls(
            key=key,
            display_name=str(raw.get("display_name", key)),
            role_families=_tuple(raw.get("role_families")),
            positive_titles=_tuple(raw.get("positive_titles")),
            technology_terms=_tuple(raw.get("technology_terms")),
            negative_terms=_tuple(raw.get("negative_terms")),
            transferable_evidence=_tuple(raw.get("transferable_evidence")),
            source_query_hints=_tuple(raw.get("source_query_hints")),
            detail_hydration_hints=_tuple(raw.get("detail_hydration_hints")),
        )


# ---------------------------------------------------------------------------
# Geography (build spec 10)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeoLocation:
    canonical: str
    group: str  # PRIMARY | SECONDARY | EXPANSION
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeographyPolicy:
    locations: tuple[GeoLocation, ...] = ()
    # Evidence tokens that alone grant international eligibility for a
    # candidate located in India. "Remote" ALONE is deliberately excluded.
    international_eligibility_signals: tuple[str, ...] = ()

    def normalize(self, text: Optional[str]) -> Optional[GeoLocation]:
        if not text:
            return None
        low = " ".join(str(text).strip().lower().split())
        for loc in self.locations:
            if low == loc.canonical.lower() or low in {a.lower() for a in loc.aliases}:
                return loc
        return None

    def group_for(self, text: Optional[str]) -> Optional[str]:
        loc = self.normalize(text)
        return loc.group if loc else None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GeographyPolicy":
        locs: list[GeoLocation] = []
        for group in ("primary", "secondary", "expansion"):
            for entry in raw.get(group, []) or []:
                if isinstance(entry, Mapping):
                    canonical = str(entry.get("canonical"))
                    aliases = _tuple(entry.get("aliases"))
                else:
                    canonical = str(entry)
                    aliases = ()
                locs.append(GeoLocation(canonical=canonical, group=group.upper(), aliases=aliases))
        return cls(
            locations=tuple(locs),
            international_eligibility_signals=_tuple(raw.get("international_eligibility_signals")),
        )


# ---------------------------------------------------------------------------
# Experience (build spec 11)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExperiencePolicy:
    preferred_ranges: tuple[str, ...] = ()
    hard_reject_min_years: int = 4
    allow_three_year_when_strong: bool = True

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExperiencePolicy":
        return cls(
            preferred_ranges=_tuple(raw.get("preferred_ranges")),
            hard_reject_min_years=int(raw.get("hard_reject_min_years", 4)),
            allow_three_year_when_strong=bool(raw.get("allow_three_year_when_strong", True)),
        )


# ---------------------------------------------------------------------------
# Exclusions (build spec 12)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExclusionFamily:
    key: str
    terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExclusionPolicy:
    families: tuple[ExclusionFamily, ...] = ()
    # Terms that must NOT by themselves exclude a legitimate development role.
    do_not_overfilter: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExclusionPolicy":
        families = tuple(
            ExclusionFamily(key=str(k), terms=_tuple(v))
            for k, v in (raw.get("families", {}) or {}).items()
        )
        return cls(families=families, do_not_overfilter=_tuple(raw.get("do_not_overfilter")))


# ---------------------------------------------------------------------------
# Cadence (build spec 13)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CadenceTier:
    tier: str
    delta_days: int
    deep_days: int


@dataclass(frozen=True)
class CadencePolicy:
    tiers: Mapping[str, CadenceTier] = field(default_factory=dict)
    batch_size_hint: int = 40         # 25-40 is a batch hint, never a completion cap
    promotion_signals: tuple[str, ...] = ()
    outcome_promotion_hold_days: int = 30

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CadencePolicy":
        tiers = {
            str(k): CadenceTier(tier=str(k), delta_days=int(v["delta_days"]), deep_days=int(v["deep_days"]))
            for k, v in (raw.get("tiers", {}) or {}).items()
        }
        return cls(
            tiers=tiers,
            batch_size_hint=int(raw.get("batch_size_hint", 40)),
            promotion_signals=_tuple(raw.get("promotion_signals")),
            outcome_promotion_hold_days=int(raw.get("outcome_promotion_hold_days", 30)),
        )


# ---------------------------------------------------------------------------
# Source policy (build spec 14) — policy, NOT live adapters
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourcePolicyEntry:
    family: str
    category: str
    display_name: str
    role: str = ""
    live_adapter: bool = False   # Phase 1B ships NO live adapters


@dataclass(frozen=True)
class SourcePolicy:
    entries: tuple[SourcePolicyEntry, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SourcePolicy":
        entries = tuple(
            SourcePolicyEntry(
                family=str(e.get("family")),
                category=str(e.get("category")),
                display_name=str(e.get("display_name", e.get("family"))),
                role=str(e.get("role", "")),
                live_adapter=bool(e.get("live_adapter", False)),
            )
            for e in (raw.get("sources", []) or [])
        )
        return cls(entries=entries)


# ---------------------------------------------------------------------------
# Company seed (build spec 13) — 108 Tier A seed, NOT a whitelist
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SeedCompany:
    name: str
    group: str
    tier: str = "A"
    origin: str = "SEED"


@dataclass(frozen=True)
class CompanySeedPolicy:
    companies: tuple[SeedCompany, ...] = ()
    is_whitelist: bool = False

    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.companies)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CompanySeedPolicy":
        companies: list[SeedCompany] = []
        for group in raw.get("groups", []) or []:
            gname = str(group.get("name", ""))
            for cname in group.get("companies", []) or []:
                companies.append(SeedCompany(name=str(cname), group=gname, tier="A", origin="SEED"))
        return cls(companies=tuple(companies), is_whitelist=bool(raw.get("is_whitelist", False)))


# ---------------------------------------------------------------------------
# Verification / freshness / closure (build spec 18)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VerificationPolicy:
    levels: tuple[str, ...] = ()
    freshness_bands: tuple[str, ...] = ()
    closure_evidence_terms: tuple[str, ...] = ()
    non_closure_conditions: tuple[str, ...] = ()
    require_final_apply_submission: bool = False
    forbidden_posted_date_sources: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "VerificationPolicy":
        return cls(
            levels=_tuple(raw.get("levels")),
            freshness_bands=_tuple(raw.get("freshness_bands")),
            closure_evidence_terms=_tuple(raw.get("closure_evidence_terms")),
            non_closure_conditions=_tuple(raw.get("non_closure_conditions")),
            require_final_apply_submission=bool(raw.get("require_final_apply_submission", False)),
            forbidden_posted_date_sources=_tuple(raw.get("forbidden_posted_date_sources")),
        )


__all__ = [
    "SearchLane",
    "GeoLocation",
    "GeographyPolicy",
    "ExperiencePolicy",
    "ExclusionFamily",
    "ExclusionPolicy",
    "CadenceTier",
    "CadencePolicy",
    "SourcePolicyEntry",
    "SourcePolicy",
    "SeedCompany",
    "CompanySeedPolicy",
    "VerificationPolicy",
]
