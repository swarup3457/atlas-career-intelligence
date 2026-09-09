"""Sealed company campaign planner (build spec 13, architecture s.7).

Builds a deterministic, stratified company cohort from the stable 108-company
seed BEFORE the first network request, seals it with a hash, and supports
immutable append-only extension batches (never swapping a failed company for an
easier one under the same run). A company x lane obligation exists for every
company across all six lanes.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field
from typing import Optional, Sequence

from atlas.policy.loader import PolicyBundle
from atlas.policy.models import SeedCompany

__all__ = [
    "CompanyRef",
    "SealedBatch",
    "CompanyCampaign",
    "build_stratified_cohort",
    "seal_campaign",
    "extend_campaign",
]


@dataclass(frozen=True)
class CompanyRef:
    name: str
    group: str
    tier: str = "A"
    origin: str = "SEED"


@dataclass(frozen=True)
class SealedBatch:
    batch_id: str
    batch_index: int
    campaign_id: str
    companies: tuple[CompanyRef, ...]
    batch_hash: str
    sealed_at: str
    reason: str = "initial cohort"


@dataclass
class CompanyCampaign:
    campaign_id: str
    created_at: str
    role_policy_hash: str
    company_plan_hash: str
    candidate_profile_hash: str = ""
    parent_campaign_id: Optional[str] = None
    supersedes_campaign_id: Optional[str] = None
    lanes: tuple[str, ...] = ()
    min_batch: int = 30
    max_batch: int = 60
    batches: list[SealedBatch] = field(default_factory=list)
    status: str = "SEALED"

    @property
    def companies(self) -> tuple[CompanyRef, ...]:
        out: list[CompanyRef] = []
        for b in self.batches:
            out.extend(b.companies)
        return tuple(out)

    @property
    def company_count(self) -> int:
        return len(self.companies)

    @property
    def obligation_count(self) -> int:
        return self.company_count * len(self.lanes)


def _hash(parts: Sequence[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def build_stratified_cohort(seed: Sequence[SeedCompany], size: int) -> tuple[CompanyRef, ...]:
    """Round-robin across seed groups (in seed order) to build a deterministic
    stratified cohort that represents every company group."""
    groups: dict[str, list[SeedCompany]] = {}
    order: list[str] = []
    for c in seed:
        if c.group not in groups:
            groups[c.group] = []
            order.append(c.group)
        groups[c.group].append(c)
    cohort: list[CompanyRef] = []
    idx = 0
    while len(cohort) < size and any(idx < len(groups[g]) for g in order):
        for g in order:
            if idx < len(groups[g]):
                c = groups[g][idx]
                cohort.append(CompanyRef(name=c.name, group=c.group, tier=c.tier, origin=c.origin))
                if len(cohort) >= size:
                    break
        idx += 1
    return tuple(cohort)


def seal_campaign(
    policy: PolicyBundle,
    *,
    campaign_id: str,
    lanes: Sequence[str],
    role_policy_hash: str,
    cohort_size: int = 30,
    max_batch: int = 60,
    candidate_profile_hash: str = "",
    now: Optional[datetime.datetime] = None,
) -> CompanyCampaign:
    """Seal the initial stratified cohort as batch 0 with a plan hash."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cohort = build_stratified_cohort(policy.company_seed.companies, cohort_size)
    batch_hash = _hash([c.name for c in cohort] + ["batch:0"])
    plan_hash = _hash([c.name for c in cohort] + list(lanes) + [role_policy_hash])
    batch = SealedBatch(
        batch_id=f"{campaign_id}-B0", batch_index=0, campaign_id=campaign_id,
        companies=cohort, batch_hash=batch_hash, sealed_at=now.isoformat(),
        reason="initial stratified cohort",
    )
    return CompanyCampaign(
        campaign_id=campaign_id, created_at=now.isoformat(), role_policy_hash=role_policy_hash,
        company_plan_hash=plan_hash, candidate_profile_hash=candidate_profile_hash,
        lanes=tuple(lanes), min_batch=cohort_size, max_batch=max_batch, batches=[batch],
    )


def extend_campaign(
    campaign: CompanyCampaign,
    policy: PolicyBundle,
    *,
    size: int = 15,
    now: Optional[datetime.datetime] = None,
) -> Optional[SealedBatch]:
    """Append an immutable extension batch of due companies not yet in the
    campaign, up to ``max_batch`` total. Returns None when the budget is reached
    or no due companies remain. A failed company is NEVER replaced by an easier
    one — extensions only ADD new companies."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    have = {c.name for c in campaign.companies}
    remaining_budget = campaign.max_batch - campaign.company_count
    if remaining_budget <= 0:
        return None
    take = min(size, remaining_budget)
    cohort = build_stratified_cohort(
        [c for c in policy.company_seed.companies if c.name not in have],
        take,
    )
    if not cohort:
        return None
    index = len(campaign.batches)
    batch = SealedBatch(
        batch_id=f"{campaign.campaign_id}-B{index}", batch_index=index,
        campaign_id=campaign.campaign_id, companies=cohort,
        batch_hash=_hash([c.name for c in cohort] + [f"batch:{index}"]),
        sealed_at=now.isoformat(), reason="extension batch (no match, due work remains)",
    )
    campaign.batches.append(batch)
    return batch
