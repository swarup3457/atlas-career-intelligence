"""CareerSiteProfile / ExtractionRecipe — persisted DATA, never per-company code.

A profile records HOW Atlas successfully (or unsuccessfully) extracted jobs from
one official career entry point, so a later run can reuse the route WITHOUT
re-deriving it — but only after REVALIDATION, because a site's structure drifts.
Nothing here is executable: it is selectors, link patterns, a route kind,
bounded evidence, confidence, versions, and health. A single generic HTTP/browser
adapter reads these; there is never a bespoke Python adapter per company.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class RouteKind(str, enum.Enum):
    """The execution route chosen for an official career entry point."""

    ATS = "ATS"                       # route to an existing structured ATS adapter
    GENERIC_HTTP = "GENERIC_HTTP"     # server-rendered / JSON-LD / embedded / sitemap
    GENERIC_BROWSER = "GENERIC_BROWSER"  # JS/SPA rendering or client-side search required
    ACCESS_LIMITED = "ACCESS_LIMITED"    # anti-bot / challenge (never bypassed)
    AUTH_REQUIRED = "AUTH_REQUIRED"      # login wall (never bypassed)
    EXTRACTION_UNRESOLVED = "EXTRACTION_UNRESOLVED"  # reachable but could not parse
    UNSUPPORTED_SITE = "UNSUPPORTED_SITE"            # no viable read-only route


TERMINAL_UNRESOLVED_ROUTES: frozenset[RouteKind] = frozenset(
    {
        RouteKind.ACCESS_LIMITED,
        RouteKind.AUTH_REQUIRED,
        RouteKind.EXTRACTION_UNRESOLVED,
        RouteKind.UNSUPPORTED_SITE,
    }
)


class RecipeHealth(str, enum.Enum):
    UNVALIDATED = "UNVALIDATED"
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    DRIFT_SUSPECTED = "DRIFT_SUSPECTED"
    FAILED = "FAILED"


# Default time after which a HEALTHY recipe must be revalidated before reuse.
DEFAULT_REVALIDATE_TTL = datetime.timedelta(days=7)


@dataclass(frozen=True)
class ExtractionRecipe:
    """The reusable, revalidate-before-trust extraction plan for one entry point.

    Selectors are only meaningful for the browser route; ``job_link_patterns``
    and ``extraction_method`` drive the HTTP route. Everything is DATA."""

    extraction_method: str = ""            # jsonld | embedded_json | anchor | sitemap | browser
    job_link_patterns: tuple[str, ...] = ()
    list_selectors: tuple[str, ...] = ()
    detail_selectors: tuple[str, ...] = ()
    search_selector: Optional[str] = None
    location_selector: Optional[str] = None
    submit_selector: Optional[str] = None
    pagination_selector: Optional[str] = None
    load_more_selector: Optional[str] = None
    job_card_selector: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "extraction_method": self.extraction_method,
            "job_link_patterns": list(self.job_link_patterns),
            "list_selectors": list(self.list_selectors),
            "detail_selectors": list(self.detail_selectors),
            "search_selector": self.search_selector,
            "location_selector": self.location_selector,
            "submit_selector": self.submit_selector,
            "pagination_selector": self.pagination_selector,
            "load_more_selector": self.load_more_selector,
            "job_card_selector": self.job_card_selector,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExtractionRecipe":
        data = dict(data or {})
        return cls(
            extraction_method=data.get("extraction_method", ""),
            job_link_patterns=tuple(data.get("job_link_patterns", []) or ()),
            list_selectors=tuple(data.get("list_selectors", []) or ()),
            detail_selectors=tuple(data.get("detail_selectors", []) or ()),
            search_selector=data.get("search_selector"),
            location_selector=data.get("location_selector"),
            submit_selector=data.get("submit_selector"),
            pagination_selector=data.get("pagination_selector"),
            load_more_selector=data.get("load_more_selector"),
            job_card_selector=data.get("job_card_selector"),
        )


@dataclass
class CareerSiteProfile:
    """A persisted profile for one company career entry point.

    Keyed by ``source_instance_id`` (a company-source instance, so a company can
    have multiple current profiles — global/India/graduate). Revalidated via
    :meth:`needs_revalidation` before its recipe is trusted for reuse."""

    profile_id: str
    company_id: Optional[str]
    source_instance_id: str
    entry_url: str
    route_kind: RouteKind
    recipe: ExtractionRecipe = field(default_factory=ExtractionRecipe)
    confidence: float = 0.0
    evidence: Mapping[str, Any] = field(default_factory=dict)
    recipe_version: str = "1.0.0"
    parser_version: str = ""
    health: RecipeHealth = RecipeHealth.UNVALIDATED
    last_success_at: Optional[str] = None
    last_validated_at: Optional[str] = None
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)

    def mark_success(self, *, at: Optional[str] = None, confidence: Optional[float] = None) -> None:
        now = at or _utcnow_iso()
        self.last_success_at = now
        self.last_validated_at = now
        self.updated_at = now
        self.health = RecipeHealth.HEALTHY
        if confidence is not None:
            self.confidence = float(confidence)

    def mark_drift(self, *, at: Optional[str] = None) -> None:
        now = at or _utcnow_iso()
        self.updated_at = now
        self.last_validated_at = now
        self.health = RecipeHealth.DRIFT_SUSPECTED

    def needs_revalidation(self, *, now: Optional[datetime.datetime] = None, ttl: datetime.timedelta = DEFAULT_REVALIDATE_TTL) -> bool:
        """A recipe is never permanently trusted. It must be revalidated when it
        was never validated, is not currently HEALTHY, or its last validation is
        older than ``ttl``."""
        if self.health != RecipeHealth.HEALTHY:
            return True
        if not self.last_validated_at:
            return True
        try:
            when = datetime.datetime.fromisoformat(self.last_validated_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        now = now or datetime.datetime.now(datetime.timezone.utc)
        return (now - when) > ttl

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "company_id": self.company_id,
            "source_instance_id": self.source_instance_id,
            "entry_url": self.entry_url,
            "route_kind": self.route_kind.value,
            "recipe": self.recipe.to_dict(),
            "confidence": self.confidence,
            "evidence": dict(self.evidence),
            "recipe_version": self.recipe_version,
            "parser_version": self.parser_version,
            "health": self.health.value,
            "last_success_at": self.last_success_at,
            "last_validated_at": self.last_validated_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CareerSiteProfile":
        data = dict(data)
        return cls(
            profile_id=data["profile_id"],
            company_id=data.get("company_id"),
            source_instance_id=data["source_instance_id"],
            entry_url=data["entry_url"],
            route_kind=RouteKind(data["route_kind"]),
            recipe=ExtractionRecipe.from_dict(data.get("recipe", {})),
            confidence=float(data.get("confidence", 0.0)),
            evidence=dict(data.get("evidence", {})),
            recipe_version=data.get("recipe_version", "1.0.0"),
            parser_version=data.get("parser_version", ""),
            health=RecipeHealth(data.get("health", RecipeHealth.UNVALIDATED.value)),
            last_success_at=data.get("last_success_at"),
            last_validated_at=data.get("last_validated_at"),
            created_at=data.get("created_at", _utcnow_iso()),
            updated_at=data.get("updated_at", _utcnow_iso()),
        )


# --- persistence bridge (keeps sqlite.py free of careers imports) ----------
def save_profile(store, profile: CareerSiteProfile) -> None:
    store.upsert_career_profile(
        profile.profile_id,
        company_id=profile.company_id,
        source_instance_id=profile.source_instance_id,
        entry_url=profile.entry_url,
        route_kind=profile.route_kind.value,
        recipe=profile.recipe.to_dict(),
        confidence=profile.confidence,
        evidence=dict(profile.evidence),
        recipe_version=profile.recipe_version,
        parser_version=profile.parser_version,
        health=profile.health.value,
        last_success_at=profile.last_success_at,
        last_validated_at=profile.last_validated_at,
    )


def load_profile(store, source_instance_id: str) -> Optional[CareerSiteProfile]:
    row = store.get_career_profile(source_instance_id)
    return _row_to_profile(row) if row is not None else None


def load_profiles(store, *, company_id: Optional[str] = None) -> list[CareerSiteProfile]:
    return [_row_to_profile(r) for r in store.list_career_profiles(company_id=company_id)]


def _row_to_profile(row) -> CareerSiteProfile:
    import json

    return CareerSiteProfile(
        profile_id=row["profile_id"],
        company_id=row["company_id"],
        source_instance_id=row["source_instance_id"],
        entry_url=row["entry_url"],
        route_kind=RouteKind(row["route_kind"]),
        recipe=ExtractionRecipe.from_dict(json.loads(row["recipe_json"] or "{}")),
        confidence=float(row["confidence"] or 0.0),
        evidence=json.loads(row["evidence_json"] or "{}"),
        recipe_version=row["recipe_version"] or "1.0.0",
        parser_version=row["parser_version"] or "",
        health=RecipeHealth(row["health"] or RecipeHealth.UNVALIDATED.value),
        last_success_at=row["last_success_at"],
        last_validated_at=row["last_validated_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "RouteKind",
    "TERMINAL_UNRESOLVED_ROUTES",
    "RecipeHealth",
    "DEFAULT_REVALIDATE_TTL",
    "ExtractionRecipe",
    "CareerSiteProfile",
    "save_profile",
    "load_profile",
    "load_profiles",
]
