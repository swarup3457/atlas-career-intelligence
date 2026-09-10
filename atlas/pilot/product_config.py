"""Load + seal the fixed five-company PRODUCT pilot config (prompt s.3/s.4).

The product pilot config is a flat YAML (``Atlas_Product_Company_5_Pilot_Config_*.yaml``).
This adapts it into the existing :class:`~atlas.pilot.config.PilotConfig` (so the whole
governor / worker / evaluator stack is reused unchanged) and carries the product-specific
fields (selection mode + seed, per-company retry budget, wall-clock + credit caps, headed
browser, browser policy) alongside it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from atlas.pilot.config import PilotConfig

__all__ = ["ProductPilotConfig", "load_product_pilot_config", "DEFAULT_QUERY_TEMPLATES"]

# Deterministic default per-lane query variants (the config supplies lanes, not queries).
DEFAULT_QUERY_TEMPLATES: dict[str, tuple[str, ...]] = {
    "JAVA_BACKEND": ("Java Developer", "Java Backend Engineer", "Software Engineer Java"),
    "JAVA_FULLSTACK": ("Java Full Stack Developer", "Full Stack Engineer Java React"),
    "REACT_FRONTEND": ("React Developer", "Frontend Engineer React"),
    "DOTNET": (".NET Developer", "C# Developer", "ASP.NET Engineer"),
    "ENTERPRISE_HR_PAYROLL_INTEGRATION": (
        "Payroll Integration Engineer", "HCM Developer", "Workday Integration Developer"),
    "GENERAL_SOFTWARE": ("Software Engineer", "Software Developer"),
}

# Default foreign-location tokens kept out of the main output (the deterministic geography
# gate is the real authority; this only feeds the report's audit column).
DEFAULT_FORBIDDEN_LOCATIONS = (
    "United States", "USA", "US", "United Kingdom", "UK", "Ireland", "Canada", "Australia",
    "Germany", "France", "Netherlands", "Singapore", "Philippines", "Poland", "Israel",
    "Japan", "China", "Dubai", "UAE",
)

DEFAULT_UNSUPPORTED_MANDATORY_BACKEND = ("Python", "Node.js", "Ruby", "Go", "PHP", "Scala", "Rust")


def _tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


@dataclass(frozen=True)
class ProductPilotConfig:
    pilot_config: PilotConfig
    pilot_name: str
    selection_mode: str
    selection_seed: str
    companies: tuple[str, ...]
    primary_lanes: tuple[str, ...]
    audit_only_lanes: tuple[str, ...]
    max_internal_retries_per_company: int
    company_search_model: str
    max_ai_credits_per_attempt: int
    max_autopilot_continues: int
    wall_clock_timeout_seconds: int
    browser_headed: bool
    browser_policy: Mapping[str, Any]
    update_latest: bool
    raw: Mapping[str, Any] = field(default_factory=dict)
    source_path: str = ""
    source_sha256: str = ""


def load_product_pilot_config(path: Path) -> ProductPilotConfig:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, Mapping):
        raise ValueError(f"product pilot config must be a mapping: {p}")

    companies = _tuple(raw.get("companies"))
    primary = _tuple(raw.get("primary_lanes")) or tuple(DEFAULT_QUERY_TEMPLATES)[:5]
    audit = _tuple(raw.get("audit_only_lanes"))
    locations = _tuple(raw.get("locations")) or ("India", "Bengaluru", "Hyderabad", "Pune",
                                                 "Chennai", "Mumbai", "Remote India")
    exp = raw.get("experience_policy") or {}
    browser_policy = dict(raw.get("browser_policy") or {})
    retries = int(raw.get("max_internal_retries_per_company", 1) or 1)

    query_templates = {lane: DEFAULT_QUERY_TEMPLATES.get(lane, (lane.replace("_", " ").title(),))
                       for lane in list(primary) + list(audit)}

    source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    config_hash = hashlib.sha256(
        (source_sha + "|".join(companies) + "|".join(primary)).encode("utf-8")
    ).hexdigest()

    pilot_config = PilotConfig(
        companies=companies,
        primary_lanes=primary,
        secondary_audit_lanes=audit,
        query_templates=query_templates,
        location_variants=locations,
        forbidden_locations=DEFAULT_FORBIDDEN_LOCATIONS,
        unsupported_mandatory_backend=DEFAULT_UNSUPPORTED_MANDATORY_BACKEND,
        experience_bands=("0-3", "3+"),
        company_search_preferred_model=str(raw.get("company_search_model", "claude-sonnet-5")),
        escalation_preferred_model="strongest_available",
        engineering_model="claude-opus-4.8",
        # concurrency ONE for this diagnostic pilot (prompt s.4)
        concurrency_company_agents=int(raw.get("concurrency_company_agents", 1) or 1),
        # one internal retry per company => max_search_rounds = 1 + retries
        max_search_rounds_per_company=1 + retries,
        # no Opus escalation in this pilot (prompt s.14)
        max_escalations_per_company=0,
        max_job_details_per_company=int(browser_policy.get("max_detail_pages_per_company", 6) or 6),
        max_pages_or_load_more_per_query=3,
        update_latest=bool(raw.get("update_latest", False)),
        config_hash=config_hash,
        raw=raw,
        source_path=str(p),
        source_sha256=source_sha,
    )

    return ProductPilotConfig(
        pilot_config=pilot_config,
        pilot_name=str(raw.get("pilot_name", "PRODUCT_COMPANY_5_V1")),
        selection_mode=str(raw.get("selection_mode", "FIXED_REPRODUCIBLE_BENCHMARK")),
        selection_seed=str(raw.get("selection_seed", "product-company-5")),
        companies=companies,
        primary_lanes=primary,
        audit_only_lanes=audit,
        max_internal_retries_per_company=retries,
        company_search_model=str(raw.get("company_search_model", "claude-sonnet-5")),
        max_ai_credits_per_attempt=int(raw.get("max_ai_credits_per_attempt", 300) or 300),
        max_autopilot_continues=int(raw.get("max_autopilot_continues", 10) or 10),
        wall_clock_timeout_seconds=int(raw.get("wall_clock_timeout_seconds", 900) or 900),
        browser_headed=bool(raw.get("browser_headed", True)),
        browser_policy=browser_policy,
        update_latest=bool(raw.get("update_latest", False)),
        raw=raw,
        source_path=str(p),
        source_sha256=source_sha,
    )
