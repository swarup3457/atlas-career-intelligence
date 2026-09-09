"""Load + seal the fixed ten-company India pilot config (architecture s.12, prompt s.12)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from atlas.pilot.models import PRIMARY_LANES

__all__ = ["PilotConfig", "load_pilot_config", "DEFAULT_PILOT_CONFIG"]

DEFAULT_PILOT_CONFIG = Path(r"C:\Atlas-Agent-Import\Atlas_LLM_India_Pilot_Config_V3_20260909.yaml")


@dataclass(frozen=True)
class PilotConfig:
    companies: tuple[str, ...]
    primary_lanes: tuple[str, ...]
    secondary_audit_lanes: tuple[str, ...]
    query_templates: Mapping[str, tuple[str, ...]]
    location_variants: tuple[str, ...]
    forbidden_locations: tuple[str, ...]
    unsupported_mandatory_backend: tuple[str, ...]
    experience_bands: tuple[str, ...]
    company_search_preferred_model: str
    escalation_preferred_model: str
    engineering_model: str
    concurrency_company_agents: int
    max_search_rounds_per_company: int
    max_escalations_per_company: int
    max_job_details_per_company: int
    max_pages_or_load_more_per_query: int
    update_latest: bool
    config_hash: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)
    source_path: str = ""
    source_sha256: str = ""

    def query_for(self, lane: str) -> tuple[str, ...]:
        return tuple(self.query_templates.get(lane, ()))


def _tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def load_pilot_config(path: Optional[Path] = None) -> PilotConfig:
    p = Path(path) if path else DEFAULT_PILOT_CONFIG
    text = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, Mapping):
        raise ValueError(f"pilot config must be a mapping: {p}")

    qt_raw = raw.get("query_templates") or {}
    query_templates = {str(k): _tuple(v) for k, v in qt_raw.items()}

    stack = raw.get("stack_contract") or {}
    geo = raw.get("geography") or {}
    models = raw.get("models") or {}
    concurrency = raw.get("concurrency") or {}
    budgets = raw.get("budgets") or {}
    output = raw.get("output") or {}
    experience = raw.get("experience") or {}

    companies = _tuple(raw.get("companies"))
    primary = _tuple(raw.get("primary_lanes")) or PRIMARY_LANES

    config_hash = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return PilotConfig(
        companies=companies,
        primary_lanes=primary,
        secondary_audit_lanes=_tuple(raw.get("secondary_audit_lanes")),
        query_templates=query_templates,
        location_variants=_tuple(geo.get("allowed")),
        forbidden_locations=_tuple(geo.get("forbidden_main_output")),
        unsupported_mandatory_backend=_tuple(stack.get("unsupported_mandatory_backend")),
        experience_bands=_tuple(experience.get("normally_eligible")),
        company_search_preferred_model=str(models.get("company_search_preferred", "claude-sonnet-5")),
        escalation_preferred_model=str(models.get("escalation_preferred", "strongest_available")),
        engineering_model=str(models.get("engineering_default", "claude-opus-4.8")),
        concurrency_company_agents=int(concurrency.get("company_agents", 2)),
        max_search_rounds_per_company=int(budgets.get("max_search_rounds_per_company", 2)),
        max_escalations_per_company=int(budgets.get("max_escalations_per_company", 1)),
        max_job_details_per_company=int(budgets.get("max_job_details_per_company", 30)),
        max_pages_or_load_more_per_query=int(budgets.get("max_pages_or_load_more_per_query", 3)),
        update_latest=bool(output.get("update_latest", False)),
        config_hash=config_hash,
        raw=raw,
        source_path=str(p),
        source_sha256=source_sha,
    )
