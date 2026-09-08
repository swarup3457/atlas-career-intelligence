"""Low-volume live canary support for the official ATS adapters (Phase 1C-A).

Two read-only, opt-in canary levels, both bounded and network-free unless
``--live`` is explicitly requested:

    * a per-family ADAPTER canary (:func:`run_adapter_canary`) — one health probe
      plus one bounded search against one configured board, reporting the board
      identity, request status, result count, pagination, health, and any
      limitation (challenge/access) truthfully;
    * a production CANARY run (:func:`build_canary_runtime`) — the SAME LangGraph
      production graph with the bounded parallel dispatcher over a handful of
      canary companies, reaching a truthful COMPLETE/PARTIAL.

Canary boards are configured (never hardcoded runtime state). Instances are
DISABLED unless the caller opts in live, so nothing here touches the network by
default and ``atlas doctor`` stays offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from atlas.planning import PlannedCompany
from atlas.sources.adapter import AdapterError
from atlas.sources.ats import ATS_FAMILIES, adapter_class_for_family, build_ats_registry
from atlas.sources.models import (
    Capability,
    SearchRequest,
    SourceFamily,
    SourceInstance,
    SourceType,
)

_FAMILY_SOURCE_TYPE = {
    SourceFamily.GREENHOUSE: SourceType.ATS_GREENHOUSE,
    SourceFamily.LEVER: SourceType.ATS_LEVER,
    SourceFamily.ASHBY: SourceType.ATS_ASHBY,
    SourceFamily.WORKDAY: SourceType.ATS_WORKDAY,
}


class CanaryConfigError(ValueError):
    """Raised when a canary config is invalid (aggregates every problem)."""


@dataclass(frozen=True)
class CanaryBoard:
    family: SourceFamily
    instance_id: str
    display_name: str
    lanes: tuple[str, ...]
    base_url: Optional[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def board_identity(self) -> str:
        md = self.metadata
        if self.family == SourceFamily.GREENHOUSE:
            return f"greenhouse:{md.get('board_token', '?')}"
        if self.family == SourceFamily.LEVER:
            return f"lever:{md.get('site', '?')}"
        if self.family == SourceFamily.ASHBY:
            return f"ashby:{md.get('board_name', '?')}"
        if self.family == SourceFamily.WORKDAY:
            return f"workday:{md.get('tenant') or self.base_url or '?'}"
        return f"{self.family.value}:{self.instance_id}"


@dataclass(frozen=True)
class CanaryConfig:
    boards: tuple[CanaryBoard, ...]
    max_companies: int = 12
    max_jobs_per_company: int = 100
    workers: int = 4
    lane: str = "CANARY"


def _coerce_board(entry: dict, idx: int, problems: list[str]) -> Optional[CanaryBoard]:
    where = f"boards[{idx}]"
    fam_raw = entry.get("family")
    try:
        family = SourceFamily(str(fam_raw))
    except ValueError:
        problems.append(f"{where}: unknown family {fam_raw!r}")
        return None
    if family not in ATS_FAMILIES:
        problems.append(f"{where}: family {family.value} is not an official ATS family")
        return None
    instance_id = str(entry.get("instance_id") or f"{family.value}-canary-{idx}")
    display_name = str(entry.get("display_name") or instance_id)
    base_url = entry.get("base_url")
    lanes = tuple(entry.get("lanes") or ("CANARY",))
    # Carry through the family-specific identity fields as adapter metadata.
    md: dict[str, Any] = {}
    for key in ("board_token", "site", "board_name", "tenant", "datacenter", "site_name",
                "locale", "eu", "include_compensation", "request_budget"):
        if entry.get(key) is not None:
            md[key] = entry[key]
    if entry.get("site_name") is not None and "site" not in md:
        md["site"] = entry["site_name"]
    return CanaryBoard(family=family, instance_id=instance_id, display_name=display_name,
                       lanes=lanes, base_url=str(base_url) if base_url else None, metadata=md)


def load_canary_config(path: Path) -> CanaryConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise CanaryConfigError("canary config must be a YAML mapping")
    problems: list[str] = []
    entries = raw.get("boards", [])
    if not isinstance(entries, list) or not entries:
        raise CanaryConfigError("canary config 'boards' must be a non-empty list")
    boards: list[CanaryBoard] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"boards[{idx}]: must be a mapping")
            continue
        board = _coerce_board(entry, idx, problems)
        if board is not None:
            boards.append(board)
    limits = raw.get("limits", {}) if isinstance(raw.get("limits"), dict) else {}
    max_companies = int(limits.get("max_companies", 12))
    if len(boards) > max_companies:
        problems.append(f"config has {len(boards)} boards but max_companies={max_companies}")
    if problems:
        raise CanaryConfigError("Invalid canary config:\n  - " + "\n  - ".join(problems))
    return CanaryConfig(
        boards=tuple(boards),
        max_companies=max_companies,
        max_jobs_per_company=int(limits.get("max_jobs_per_company", 100)),
        workers=int(limits.get("workers", 4)),
        lane=str(raw.get("lane", "CANARY")),
    )


def default_canary_config() -> CanaryConfig:
    """A built-in minimal config using only the two officially documented,
    guaranteed-live demo boards (Lever ``leverdemo`` and Ashby ``Ashby``). Used
    when the CLI canary is invoked without an explicit ``--config``."""
    return CanaryConfig(
        boards=(
            CanaryBoard(SourceFamily.LEVER, "lever-canary-leverdemo", "Lever Demo (official)",
                        ("CANARY",), None, {"site": "leverdemo"}),
            CanaryBoard(SourceFamily.ASHBY, "ashby-canary-ashby", "Ashby (official board)",
                        ("CANARY",), None, {"board_name": "Ashby", "include_compensation": True}),
        ),
        max_companies=12, max_jobs_per_company=100, workers=4, lane="CANARY",
    )


def board_to_instance(board: CanaryBoard, *, enabled: bool) -> SourceInstance:
    return SourceInstance(
        instance_id=board.instance_id,
        source_type=_FAMILY_SOURCE_TYPE[board.family],
        source_family=board.family,
        display_name=board.display_name,
        base_url=board.base_url,
        enabled=enabled,
        metadata=dict(board.metadata),
    )


def build_canary_instances(config: CanaryConfig, *, enabled: bool) -> dict[str, SourceInstance]:
    return {b.instance_id: board_to_instance(b, enabled=enabled) for b in config.boards}


def build_canary_companies(config: CanaryConfig) -> list[PlannedCompany]:
    companies: list[PlannedCompany] = []
    for b in config.boards:
        companies.append(
            PlannedCompany(
                company_id=b.instance_id, name=b.display_name, tier="A", mode="DELTA",
                source_instances=(b.instance_id,), geography_group="PRIMARY",
            )
        )
    return companies


# ---------------------------------------------------------------------------
# Per-family adapter canary
# ---------------------------------------------------------------------------
@dataclass
class JobSample:
    title: Optional[str]
    location: Optional[str]
    source_job_id: Optional[str]
    url: Optional[str]

    def to_dict(self) -> dict:
        return {"title": self.title, "location": self.location,
                "source_job_id": self.source_job_id, "url": self.url}


@dataclass
class AdapterCanaryResult:
    family: str
    instance_id: str
    board_identity: str
    live: bool
    request_status: str          # OK | ZERO | ERROR:<CATEGORY> | DRY_RUN
    result_count: int = 0
    has_more: bool = False
    zero_result_kind: Optional[str] = None
    health_state: Optional[str] = None
    limitation: Optional[str] = None
    samples: list[JobSample] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.request_status in ("OK", "ZERO")

    def to_dict(self) -> dict:
        return {
            "family": self.family, "instance_id": self.instance_id,
            "board_identity": self.board_identity, "live": self.live,
            "request_status": self.request_status, "result_count": self.result_count,
            "has_more": self.has_more, "zero_result_kind": self.zero_result_kind,
            "health_state": self.health_state, "limitation": self.limitation,
            "samples": [s.to_dict() for s in self.samples],
        }


def run_adapter_canary(board: CanaryBoard, *, live: bool, limit: int = 25, sample_cap: int = 5) -> AdapterCanaryResult:
    """One health probe + one bounded search against ``board``. When ``live`` is
    False this validates the config/identity WITHOUT any network I/O."""
    cls = adapter_class_for_family(board.family)
    instance = board_to_instance(board, enabled=live)
    result = AdapterCanaryResult(
        family=board.family.value, instance_id=board.instance_id,
        board_identity=board.board_identity(), live=live, request_status="DRY_RUN",
    )
    if not live:
        # Construct the adapter to validate identity parsing only (no request).
        try:
            cls(instance)
            result.limitation = "dry-run (no network; pass --live to hit the board)"
        except AdapterError as exc:
            result.request_status = f"ERROR:{exc.category.value}"
            result.limitation = exc.message
        return result

    adapter = cls(instance)
    try:
        health = adapter.health_check()
        result.health_state = health.state.value
    except AdapterError as exc:
        result.health_state = f"probe-error:{exc.category.value}"
    lane = board.lanes[0] if board.lanes else "CANARY"
    try:
        search = adapter.search(SearchRequest(query=lane, limit=min(limit, 100)))
    except AdapterError as exc:
        result.request_status = f"ERROR:{exc.category.value}"
        result.limitation = exc.message
        return result
    result.result_count = search.count
    result.has_more = search.has_more
    result.zero_result_kind = search.zero_result_kind.value
    result.request_status = "OK" if search.count > 0 else "ZERO"
    for r in search.results[:sample_cap]:
        result.samples.append(JobSample(r.title, r.location, r.source_job_id, r.canonical_url or r.source_url))
    return result


def run_all_adapter_canaries(config: CanaryConfig, *, live: bool, families: Optional[list[SourceFamily]] = None,
                             limit: int = 25) -> list[AdapterCanaryResult]:
    out: list[AdapterCanaryResult] = []
    for board in config.boards:
        if families is not None and board.family not in families:
            continue
        out.append(run_adapter_canary(board, live=live, limit=limit))
    return out


# ---------------------------------------------------------------------------
# Production canary runtime (full graph, parallel dispatcher)
# ---------------------------------------------------------------------------
def build_canary_runtime(settings, config: CanaryConfig, run_id: str, *, live: bool,
                         policy_dir: Optional[Path] = None, write_report: bool = True):
    """Build a :class:`ProductionSearchRuntime` for a low-volume live canary:
    real ATS adapters, the bounded parallel dispatcher, one CANARY lane per
    board, and a synthetic (never real) candidate so no PII is required."""
    from atlas.runtime.production import ProductionSearchRuntime

    registry = build_ats_registry()
    instances = build_canary_instances(config, enabled=live)
    companies = build_canary_companies(config)
    return ProductionSearchRuntime(
        settings, run_id,
        fixture_mode=True,  # synthetic candidate (no PII); adapters are REAL
        policy_dir=policy_dir,
        companies=companies,
        instances=instances,
        registry=registry,
        parallel_workers=config.workers,
        max_per_company=1, max_per_instance=1, max_per_tenant=1,
        live_canary=live,
        lane_override=[config.lane],
        write_report=write_report,
    )


__all__ = [
    "CanaryConfigError",
    "CanaryBoard",
    "CanaryConfig",
    "load_canary_config",
    "default_canary_config",
    "board_to_instance",
    "build_canary_instances",
    "build_canary_companies",
    "AdapterCanaryResult",
    "JobSample",
    "run_adapter_canary",
    "run_all_adapter_canaries",
    "build_canary_runtime",
]
