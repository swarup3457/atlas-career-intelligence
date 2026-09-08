"""Market search campaign + append-only sealed waves (Phase 1D §10).

A market campaign is an append-only record of one adaptive market-discovery run:
its run id, policy/candidate fingerprint, hard budgets, status, current wave, and
terminal reason. Work is organized into SEALED, fingerprinted WAVES. Wave 0
carries the required baseline coverage (portal × lane/geography, due official
companies, and domain-only discovery). A sealed wave is NEVER mutated — a
detected deficit creates Wave N+1 that references the parent wave and the
deficit, so the whole expansion history is durable and auditable.

These are pure DATA models + deterministic sealing/persistence. Loops, budgets,
and completion live in Python (:mod:`atlas.market.runtime` /
:mod:`atlas.market.deficit`) — never in prose, never in an LLM.
"""

from __future__ import annotations

import datetime
import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class CampaignStatus(str, enum.Enum):
    SEALED = "SEALED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    PARTIAL_BUDGET = "PARTIAL_BUDGET"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    FAILED = "FAILED"


class WaveStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    SEALED = "SEALED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class WaveTaskKind(str, enum.Enum):
    PORTAL = "PORTAL"                       # portal (linkedin/naukri) × lane × geo query
    OFFICIAL_COMPANY = "OFFICIAL_COMPANY"   # a known company × official source × lane
    DOMAIN_DISCOVERY = "DOMAIN_DISCOVERY"   # domain-only official source discovery
    OFFICIAL_FOLLOWUP = "OFFICIAL_FOLLOWUP"  # portal lead -> official verification follow-up


@dataclass(frozen=True)
class CampaignBudget:
    """Campaign-wide HARD budgets (build spec 11). All configurable + enforced;
    exhaustion is PARTIAL_BUDGET, never COMPLETE."""

    max_waves: int = 3
    max_variants_per_lane: int = 3
    max_portal_pages: int = 2
    max_portal_cards: int = 40
    max_total_tasks: int = 200
    max_http_calls: int = 300
    max_browser_calls: int = 40
    max_llm_calls: int = 20
    max_results: int = 500
    max_wall_clock_s: float = 900.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_waves": self.max_waves,
            "max_variants_per_lane": self.max_variants_per_lane,
            "max_portal_pages": self.max_portal_pages,
            "max_portal_cards": self.max_portal_cards,
            "max_total_tasks": self.max_total_tasks,
            "max_http_calls": self.max_http_calls,
            "max_browser_calls": self.max_browser_calls,
            "max_llm_calls": self.max_llm_calls,
            "max_results": self.max_results,
            "max_wall_clock_s": self.max_wall_clock_s,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "CampaignBudget":
        data = dict(data or {})
        base = cls()
        return cls(**{k: data.get(k, getattr(base, k)) for k in base.to_dict()})


@dataclass(frozen=True)
class WaveTask:
    """One planned unit of market work. Its ``canonical`` form is what a wave
    seal-hashes, so an identical plan seals identically and a changed plan seals
    differently."""

    kind: WaveTaskKind
    task_id: str
    lane: Optional[str] = None
    geography: Optional[str] = None
    source_family: Optional[str] = None
    query: Optional[str] = None
    company_id: Optional[str] = None
    company_name: Optional[str] = None
    official_domain: Optional[str] = None
    instance_id: Optional[str] = None
    recency_days: Optional[int] = None
    origin: str = "BASELINE"
    portal_lead_id: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "task_id": self.task_id,
            "lane": self.lane,
            "geography": self.geography,
            "source_family": self.source_family,
            "query": self.query,
            "company_id": self.company_id,
            "official_domain": self.official_domain,
            "instance_id": self.instance_id,
            "recency_days": self.recency_days,
            "origin": self.origin,
            "portal_lead_id": self.portal_lead_id,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.canonical()
        d["company_name"] = self.company_name
        d["detail"] = dict(self.detail)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "WaveTask":
        return cls(
            kind=WaveTaskKind(data["kind"]), task_id=data["task_id"], lane=data.get("lane"),
            geography=data.get("geography"), source_family=data.get("source_family"),
            query=data.get("query"), company_id=data.get("company_id"),
            company_name=data.get("company_name"), official_domain=data.get("official_domain"),
            instance_id=data.get("instance_id"), recency_days=data.get("recency_days"),
            origin=data.get("origin", "BASELINE"), portal_lead_id=data.get("portal_lead_id"),
            detail=dict(data.get("detail", {})),
        )


@dataclass
class MarketWave:
    """A sealed, fingerprinted set of tasks. Append-only: once SEALED its task
    set / index / seal-hash are immutable; only its status/terminal_reason
    advance."""

    campaign_id: str
    run_id: str
    wave_index: int
    tasks: list[WaveTask] = field(default_factory=list)
    parent_wave_id: Optional[str] = None
    deficit_reason: Optional[str] = None
    status: WaveStatus = WaveStatus.PLANNED
    terminal_reason: Optional[str] = None
    seal_hash: Optional[str] = None
    sealed_at: Optional[str] = None
    created_at: str = field(default_factory=_utcnow)

    @property
    def wave_id(self) -> str:
        return f"wave::{self.campaign_id}::{self.wave_index}"

    def compute_seal_hash(self) -> str:
        payload = {
            "campaign_id": self.campaign_id,
            "wave_index": self.wave_index,
            "parent_wave_id": self.parent_wave_id,
            "tasks": sorted((t.canonical() for t in self.tasks), key=lambda d: d["task_id"]),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def seal(self) -> str:
        if self.status != WaveStatus.PLANNED:
            raise ValueError(f"cannot seal wave in status {self.status}")
        self.seal_hash = self.compute_seal_hash()
        self.sealed_at = _utcnow()
        self.status = WaveStatus.SEALED
        return self.seal_hash

    @property
    def is_sealed(self) -> bool:
        return self.seal_hash is not None and self.status in (
            WaveStatus.SEALED, WaveStatus.RUNNING, WaveStatus.COMPLETE,
            WaveStatus.PARTIAL, WaveStatus.FAILED,
        )

    def persist(self, store) -> None:
        store.upsert_market_wave(
            self.wave_id, self.campaign_id, self.run_id, self.wave_index,
            parent_wave_id=self.parent_wave_id, seal_hash=self.seal_hash,
            status=self.status.value, deficit_reason=self.deficit_reason,
            tasks=[t.to_dict() for t in self.tasks], terminal_reason=self.terminal_reason,
            sealed_at=self.sealed_at,
        )

    @classmethod
    def from_row(cls, row) -> "MarketWave":
        try:
            status = WaveStatus(row["status"])
        except (ValueError, KeyError):
            status = WaveStatus.PLANNED
        try:
            tasks = [WaveTask.from_dict(t) for t in json.loads(row["tasks_json"] or "[]")]
        except (ValueError, TypeError):
            tasks = []
        return cls(
            campaign_id=row["campaign_id"], run_id=row["run_id"], wave_index=int(row["wave_index"]),
            tasks=tasks, parent_wave_id=row["parent_wave_id"], deficit_reason=row["deficit_reason"],
            status=status, terminal_reason=row["terminal_reason"], seal_hash=row["seal_hash"],
            sealed_at=row["sealed_at"], created_at=row["created_at"],
        )


@dataclass
class MarketCampaign:
    """Append-only campaign record."""

    campaign_id: str
    run_id: str
    policy_fingerprint: str = ""
    candidate_fingerprint: str = ""
    mode: str = "DELTA"
    budget: CampaignBudget = field(default_factory=CampaignBudget)
    status: CampaignStatus = CampaignStatus.SEALED
    current_wave: int = 0
    terminal_reason: Optional[str] = None
    config: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_utcnow)

    def seal_hash(self) -> str:
        payload = {
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "policy_fingerprint": self.policy_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "mode": self.mode,
            "budget": self.budget.to_dict(),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def persist(self, store) -> None:
        store.upsert_market_campaign(
            self.campaign_id, self.run_id, policy_fingerprint=self.policy_fingerprint,
            candidate_fingerprint=self.candidate_fingerprint, mode=self.mode,
            budgets=self.budget.to_dict(), status=self.status.value, current_wave=self.current_wave,
            terminal_reason=self.terminal_reason, config=self.config,
        )

    @classmethod
    def from_row(cls, row) -> "MarketCampaign":
        try:
            status = CampaignStatus(row["status"])
        except (ValueError, KeyError):
            status = CampaignStatus.SEALED
        return cls(
            campaign_id=row["campaign_id"], run_id=row["run_id"],
            policy_fingerprint=row["policy_fingerprint"] or "",
            candidate_fingerprint=row["candidate_fingerprint"] or "",
            mode=row["mode"] or "DELTA",
            budget=CampaignBudget.from_dict(json.loads(row["budgets_json"] or "{}")),
            status=status, current_wave=int(row["current_wave"] or 0),
            terminal_reason=row["terminal_reason"],
            config=json.loads(row["config_json"] or "{}"), created_at=row["created_at"],
        )


__all__ = [
    "CampaignStatus",
    "WaveStatus",
    "WaveTaskKind",
    "CampaignBudget",
    "WaveTask",
    "MarketWave",
    "MarketCampaign",
]
