"""Product-company metadata pool + deterministic stratified selector.

This is ADDITIVE product-company metadata (architecture s.3) layered on top of the
existing company registry — it does NOT create a second registry. The live five-company
pilot is a *fixed reproducible benchmark* (``FIXED_REPRODUCIBLE_BENCHMARK``); this module
also implements the *future* deterministic stratified daily selector so a normal 20-company
day can be planned reproducibly.

Nothing here performs I/O beyond reading the supplied pool YAML. Selection is pure and
deterministic: given the same pool, count, seed material, and clock, it always returns the
same ordered selection. Ties (equal priority score) are broken with a reproducible seeded
shuffle keyed by ``search_date + policy_hash + candidate_profile_hash + run_id``.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

import yaml

__all__ = [
    "ProductCompany",
    "ProductPool",
    "SelectionCandidate",
    "ProductSelection",
    "load_product_pool",
    "is_product_category",
    "infer_source_family",
    "select_product_companies",
    "select_fixed_cohort",
    "plan_daily_buckets",
    "build_seed_material",
    "ELIGIBLE_CATEGORY_TOKENS",
    "DEFAULT_BUCKETS",
]

# A company category is product-eligible when it names one of these product families
# (architecture s.3: PRODUCT / SAAS / PLATFORM / FINTECH_PRODUCT / ENTERPRISE_PRODUCT).
ELIGIBLE_CATEGORY_TOKENS = ("PRODUCT", "SAAS", "PLATFORM", "FINTECH", "ENTERPRISE")

#: A normal 20-company day approximates this bucket split (architecture s.3 / prompt s.3).
DEFAULT_BUCKETS: dict[str, int] = {
    "PRIORITY_DUE": 7,
    "OVERDUE_OR_NEVER": 5,
    "EXPLORATION": 4,
    "RETRY_DUE": 2,
    "NEWLY_ADDED": 2,
}

# Default recheck cadence (days). A company searched successfully within the cooldown is
# NOT due; a company overdue past the cadence is prioritized.
DEFAULT_COOLDOWN_DAYS = 7
DEFAULT_CADENCE_DAYS = 14


def is_product_category(category: str) -> bool:
    up = (category or "").upper()
    return any(tok in up for tok in ELIGIBLE_CATEGORY_TOKENS)


def infer_source_family(career_entry_url: str) -> str:
    u = (career_entry_url or "").lower()
    if "myworkdayjobs.com" in u or "wd5.myworkday" in u or "workday.com" in u:
        return "WORKDAY"
    if "greenhouse.io" in u or "boards.greenhouse" in u:
        return "GREENHOUSE"
    if "lever.co" in u:
        return "LEVER"
    if "ashbyhq.com" in u:
        return "ASHBY"
    if "smartrecruiters.com" in u:
        return "SMARTRECRUITERS"
    if "eightfold.ai" in u or "careers." in u:
        return "CUSTOM_CAREERS"
    return "CUSTOM_CAREERS"


@dataclass(frozen=True)
class ProductCompany:
    """Additive product-company metadata (architecture s.3 conceptual fields)."""

    name: str
    category: str
    official_domain: str
    career_entry_url: str
    india_presence: bool = True
    india_enabled_search: bool = True
    lane_affinity: tuple[str, ...] = ()
    source_family: str = ""
    # cadence / history (deterministic scheduler inputs)
    cadence_state: str = "DUE"                 # DUE | COOLDOWN | RETRY_DUE | DISABLED
    last_successful_search: Optional[str] = None  # ISO date
    historical_yield: float = 0.0             # relevant accepted rows historically
    company_priority: float = 0.0             # operator/priority weighting
    recent_velocity: float = 0.0             # recent job-posting velocity
    source_health: float = 1.0               # 0..1 (1 == healthy)
    retry_due: bool = False                   # a prior external limitation is retry-due
    recipe_age_days: int = 0                  # 0 == fresh / no recipe drift
    newly_added: bool = False
    disabled: bool = False

    @property
    def product_company(self) -> bool:
        return is_product_category(self.category)

    @property
    def resolved_source_family(self) -> str:
        return self.source_family or infer_source_family(self.career_entry_url)

    def days_since_search(self, today: datetime.date) -> Optional[int]:
        if not self.last_successful_search:
            return None
        try:
            d = datetime.date.fromisoformat(str(self.last_successful_search)[:10])
        except ValueError:
            return None
        return (today - d).days

    def is_due(self, today: datetime.date, *, cooldown_days: int = DEFAULT_COOLDOWN_DAYS) -> bool:
        if self.disabled:
            return False
        since = self.days_since_search(today)
        if since is None:
            return True  # never searched -> always due
        return since >= cooldown_days

    def in_cooldown(self, today: datetime.date, *, cooldown_days: int = DEFAULT_COOLDOWN_DAYS) -> bool:
        since = self.days_since_search(today)
        return since is not None and since < cooldown_days

    def eligible(self, today: datetime.date, *, cooldown_days: int = DEFAULT_COOLDOWN_DAYS) -> bool:
        """Product/SaaS/platform category, India presence or India-enabled search, due under
        cadence, not in cooldown, not disabled (architecture s.3 eligibility filter)."""
        if self.disabled:
            return False
        if not self.product_company:
            return False
        if not (self.india_presence or self.india_enabled_search):
            return False
        return self.is_due(today, cooldown_days=cooldown_days)


@dataclass(frozen=True)
class ProductPool:
    companies: tuple[ProductCompany, ...]
    selection_policy: str = "product_company_stratified_v1"
    source_path: str = ""
    source_sha256: str = ""

    def by_name(self, name: str) -> Optional[ProductCompany]:
        low = name.lower()
        for c in self.companies:
            if c.name.lower() == low:
                return c
        return None

    def eligible(self, today: datetime.date, *, cooldown_days: int = DEFAULT_COOLDOWN_DAYS) -> tuple[ProductCompany, ...]:
        return tuple(c for c in self.companies if c.eligible(today, cooldown_days=cooldown_days))


@dataclass(frozen=True)
class SelectionCandidate:
    company: ProductCompany
    bucket: str
    score: float
    score_breakdown: Mapping[str, float]
    tiebreak: str
    reason: str


@dataclass(frozen=True)
class ProductSelection:
    mode: str
    seed: str
    seed_material: str
    count: int
    selected: tuple[SelectionCandidate, ...]
    bucket_counts: Mapping[str, int]
    diversity_ok: bool
    notes: tuple[str, ...] = ()

    def names(self) -> tuple[str, ...]:
        return tuple(c.company.name for c in self.selected)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "seed": self.seed,
            "seed_material": self.seed_material,
            "count": self.count,
            "selected": [
                {
                    "company": c.company.name,
                    "category": c.company.category,
                    "source_family": c.company.resolved_source_family,
                    "bucket": c.bucket,
                    "score": round(c.score, 6),
                    "score_breakdown": {k: round(v, 6) for k, v in c.score_breakdown.items()},
                    "tiebreak": c.tiebreak,
                    "reason": c.reason,
                    "lane_affinity": list(c.company.lane_affinity),
                    "career_entry_url": c.company.career_entry_url,
                    "official_domain": c.company.official_domain,
                }
                for c in self.selected
            ],
            "bucket_counts": dict(self.bucket_counts),
            "diversity_ok": self.diversity_ok,
            "notes": list(self.notes),
        }


def _b(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def load_product_pool(path: Path) -> ProductPool:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, Mapping):
        raise ValueError(f"product pool must be a mapping: {p}")
    companies: list[ProductCompany] = []
    for entry in raw.get("companies", []) or []:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        career = str(entry.get("career_entry_url", "")).strip()
        companies.append(ProductCompany(
            name=name,
            category=str(entry.get("category", "")).strip(),
            official_domain=str(entry.get("official_domain", "")).strip(),
            career_entry_url=career,
            india_presence=_b(entry.get("india_presence"), True),
            india_enabled_search=_b(entry.get("india_enabled_search"), True),
            lane_affinity=tuple(str(x) for x in (entry.get("lane_affinity") or ())),
            source_family=str(entry.get("source_family", "")).strip() or infer_source_family(career),
            cadence_state=str(entry.get("cadence_state", "DUE")).strip().upper() or "DUE",
            last_successful_search=(str(entry["last_successful_search"])
                                    if entry.get("last_successful_search") else None),
            historical_yield=float(entry.get("historical_yield", 0.0) or 0.0),
            company_priority=float(entry.get("company_priority", 0.0) or 0.0),
            recent_velocity=float(entry.get("recent_velocity", 0.0) or 0.0),
            source_health=float(entry.get("source_health", 1.0) if entry.get("source_health") is not None else 1.0),
            retry_due=_b(entry.get("retry_due"), False),
            recipe_age_days=int(entry.get("recipe_age_days", 0) or 0),
            newly_added=_b(entry.get("newly_added"), False),
            disabled=_b(entry.get("disabled"), False),
        ))
    return ProductPool(
        companies=tuple(companies),
        selection_policy=str(raw.get("selection_policy", "product_company_stratified_v1")),
        source_path=str(p),
        source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def build_seed_material(
    *,
    base_seed: str,
    search_date: datetime.date,
    policy_hash: str = "",
    candidate_profile_hash: str = "",
    run_id: str = "",
) -> str:
    """seed = search_date + policy_hash + candidate_profile_hash + run_id (architecture s.3)."""
    return "|".join([
        search_date.isoformat(),
        policy_hash or "",
        candidate_profile_hash or "",
        run_id or "",
        base_seed or "",
    ])


def _tiebreak(seed_material: str, name: str) -> str:
    return hashlib.sha256(f"{seed_material}::{name.lower()}".encode("utf-8")).hexdigest()


def _lane_affinity_score(company: ProductCompany, target_lanes: Sequence[str]) -> float:
    if not target_lanes:
        return float(len(company.lane_affinity))
    tset = {l.upper() for l in target_lanes}
    return float(len({l.upper() for l in company.lane_affinity} & tset))


def _score(
    company: ProductCompany,
    today: datetime.date,
    target_lanes: Sequence[str],
    *,
    cooldown_days: int,
) -> tuple[float, dict[str, float]]:
    """Deterministic priority score (architecture s.3 priority factors)."""
    since = company.days_since_search(today)
    # due / never-searched priority
    if since is None:
        due = 6.0
    else:
        due = max(0.0, min(6.0, (since - cooldown_days) / 7.0 + 1.0)) if since >= cooldown_days else 0.0
    breakdown = {
        "due_or_never": due,
        "historical_yield": min(5.0, company.historical_yield),
        "company_priority": min(5.0, company.company_priority),
        "source_health": company.source_health * 2.0,
        "recent_velocity": min(3.0, company.recent_velocity),
        "retry_due": 2.5 if company.retry_due else 0.0,
        "recipe_age": min(2.0, company.recipe_age_days / 30.0),
        "lane_affinity": _lane_affinity_score(company, target_lanes) * 0.75,
        "recent_success_penalty": -(3.0 if company.in_cooldown(today, cooldown_days=cooldown_days) else 0.0),
        "newly_added": 1.5 if company.newly_added else 0.0,
    }
    return float(sum(breakdown.values())), breakdown


def _classify_bucket(company: ProductCompany, today: datetime.date, *, cooldown_days: int) -> str:
    if company.retry_due:
        return "RETRY_DUE"
    if company.newly_added:
        return "NEWLY_ADDED"
    since = company.days_since_search(today)
    if since is None:
        return "OVERDUE_OR_NEVER"
    if since >= DEFAULT_CADENCE_DAYS:
        return "OVERDUE_OR_NEVER"
    if company.historical_yield > 0 or company.company_priority > 0:
        return "PRIORITY_DUE"
    return "EXPLORATION"


def select_product_companies(
    pool: ProductPool,
    *,
    count: int,
    seed: str,
    today: Optional[datetime.date] = None,
    target_lanes: Sequence[str] = (),
    policy_hash: str = "",
    candidate_profile_hash: str = "",
    run_id: str = "",
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    max_per_source_family: int = 2,
    min_categories: int = 3,
    enforce_diversity: bool = True,
) -> ProductSelection:
    """Deterministic stratified selection over the eligible product pool.

    Primary ordering is by the (seed-independent) priority score; equal scores are broken by
    a reproducible seeded hash. Changing only ``seed`` therefore reorders *ties* only. A
    source-family cap and a category-diversity floor are applied greedily.
    """
    today = today or datetime.date.today()
    seed_material = build_seed_material(
        base_seed=seed, search_date=today, policy_hash=policy_hash,
        candidate_profile_hash=candidate_profile_hash, run_id=run_id,
    )
    eligible = [c for c in pool.companies if c.eligible(today, cooldown_days=cooldown_days)]

    scored: list[SelectionCandidate] = []
    for c in eligible:
        score, breakdown = _score(c, today, target_lanes, cooldown_days=cooldown_days)
        scored.append(SelectionCandidate(
            company=c,
            bucket=_classify_bucket(c, today, cooldown_days=cooldown_days),
            score=score,
            score_breakdown=breakdown,
            tiebreak=_tiebreak(seed_material, c.name),
            reason=f"eligible product company; bucket={_classify_bucket(c, today, cooldown_days=cooldown_days)}",
        ))
    # deterministic order: highest score first, ties broken by seeded hash, then name
    scored.sort(key=lambda x: (-x.score, x.tiebreak, x.company.name.lower()))

    selected: list[SelectionCandidate] = []
    fam_counts: dict[str, int] = {}
    notes: list[str] = []
    deferred: list[SelectionCandidate] = []

    def _try_add(cand: SelectionCandidate) -> bool:
        fam = cand.company.resolved_source_family
        if enforce_diversity and max_per_source_family and fam_counts.get(fam, 0) >= max_per_source_family:
            return False
        if any(s.company.name.lower() == cand.company.name.lower() for s in selected):
            return False  # no duplicates
        selected.append(cand)
        fam_counts[fam] = fam_counts.get(fam, 0) + 1
        return True

    for cand in scored:
        if len(selected) >= count:
            break
        if not _try_add(cand):
            deferred.append(cand)

    # if diversity caps blocked us from reaching count, relax the family cap (record it)
    if len(selected) < count and deferred:
        notes.append("relaxed source-family cap to reach requested count")
        for cand in deferred:
            if len(selected) >= count:
                break
            if any(s.company.name.lower() == cand.company.name.lower() for s in selected):
                continue
            selected.append(cand)
            fam = cand.company.resolved_source_family
            fam_counts[fam] = fam_counts.get(fam, 0) + 1

    categories = {c.company.category for c in selected}
    diversity_ok = (len(categories) >= min(min_categories, len(selected))) and all(
        v <= max_per_source_family for v in fam_counts.values()
    ) if enforce_diversity else True

    bucket_counts: dict[str, int] = {}
    for c in selected:
        bucket_counts[c.bucket] = bucket_counts.get(c.bucket, 0) + 1

    return ProductSelection(
        mode="STRATIFIED", seed=seed, seed_material=seed_material, count=count,
        selected=tuple(selected), bucket_counts=bucket_counts, diversity_ok=diversity_ok,
        notes=tuple(notes),
    )


def select_fixed_cohort(
    pool: ProductPool,
    company_names: Sequence[str],
    *,
    seed: str,
    today: Optional[datetime.date] = None,
    target_lanes: Sequence[str] = (),
    run_id: str = "",
) -> ProductSelection:
    """FIXED_REPRODUCIBLE_BENCHMARK selection: exactly the named cohort, in the given order,
    annotated with the same score/bucket metadata for the Selection_Audit sheet."""
    today = today or datetime.date.today()
    seed_material = build_seed_material(base_seed=seed, search_date=today, run_id=run_id)
    selected: list[SelectionCandidate] = []
    notes: list[str] = []
    for name in company_names:
        c = pool.by_name(name)
        if c is None:
            # a fixed cohort member absent from the pool is still sealed truthfully
            c = ProductCompany(name=name, category="UNKNOWN_PRODUCT", official_domain="",
                               career_entry_url="", source_family="CUSTOM_CAREERS")
            notes.append(f"{name}: not present in pool seed; sealed from config only")
        score, breakdown = _score(c, today, target_lanes, cooldown_days=DEFAULT_COOLDOWN_DAYS)
        selected.append(SelectionCandidate(
            company=c, bucket="FIXED_BENCHMARK", score=score, score_breakdown=breakdown,
            tiebreak=_tiebreak(seed_material, name),
            reason="fixed reproducible product-company benchmark cohort (sealed by config)",
        ))
    bucket_counts = {"FIXED_BENCHMARK": len(selected)}
    return ProductSelection(
        mode="FIXED_REPRODUCIBLE_BENCHMARK", seed=seed, seed_material=seed_material,
        count=len(selected), selected=tuple(selected), bucket_counts=bucket_counts,
        diversity_ok=True, notes=tuple(notes),
    )


def plan_daily_buckets(
    pool: ProductPool,
    *,
    seed: str,
    buckets: Optional[Mapping[str, int]] = None,
    today: Optional[datetime.date] = None,
    target_lanes: Sequence[str] = (),
    run_id: str = "",
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    max_per_source_family: int = 2,
) -> ProductSelection:
    """Plan a normal (e.g. 20-company) day with the configured bucket split, filling each
    bucket from its own eligible pool and enforcing diversity + no duplicates."""
    buckets = dict(buckets or DEFAULT_BUCKETS)
    today = today or datetime.date.today()
    seed_material = build_seed_material(base_seed=seed, search_date=today, run_id=run_id)

    eligible = [c for c in pool.companies if c.eligible(today, cooldown_days=cooldown_days)]
    by_bucket: dict[str, list[SelectionCandidate]] = {b: [] for b in buckets}
    for c in eligible:
        score, breakdown = _score(c, today, target_lanes, cooldown_days=cooldown_days)
        b = _classify_bucket(c, today, cooldown_days=cooldown_days)
        cand = SelectionCandidate(company=c, bucket=b, score=score, score_breakdown=breakdown,
                                  tiebreak=_tiebreak(seed_material, c.name),
                                  reason=f"bucket={b}")
        by_bucket.setdefault(b, []).append(cand)
    for b in by_bucket:
        by_bucket[b].sort(key=lambda x: (-x.score, x.tiebreak, x.company.name.lower()))

    selected: list[SelectionCandidate] = []
    fam_counts: dict[str, int] = {}
    chosen_names: set[str] = set()
    notes: list[str] = []

    order = ["PRIORITY_DUE", "OVERDUE_OR_NEVER", "EXPLORATION", "RETRY_DUE", "NEWLY_ADDED"]
    order += [b for b in buckets if b not in order]
    for b in order:
        want = buckets.get(b, 0)
        pool_b = by_bucket.get(b, [])
        # overflow from other buckets is allowed if a bucket underfills
        added = 0
        for cand in pool_b:
            if added >= want:
                break
            name = cand.company.name.lower()
            if name in chosen_names:
                continue
            fam = cand.company.resolved_source_family
            if max_per_source_family and fam_counts.get(fam, 0) >= max_per_source_family:
                continue
            selected.append(cand)
            chosen_names.add(name)
            fam_counts[fam] = fam_counts.get(fam, 0) + 1
            added += 1
        if added < want:
            notes.append(f"bucket {b} underfilled: wanted {want}, filled {added}")

    bucket_counts: dict[str, int] = {}
    for c in selected:
        bucket_counts[c.bucket] = bucket_counts.get(c.bucket, 0) + 1
    categories = {c.company.category for c in selected}
    diversity_ok = len(categories) >= 3 and all(v <= max_per_source_family for v in fam_counts.values())
    return ProductSelection(
        mode="DAILY_STRATIFIED_BUCKETS", seed=seed, seed_material=seed_material,
        count=len(selected), selected=tuple(selected), bucket_counts=bucket_counts,
        diversity_ok=diversity_ok, notes=tuple(notes),
    )
