"""Policy loader + validation + bundle (Phase 1B, build spec section 8).

Loads the public YAML policy files under ``config/policy/`` into typed,
validated, fingerprinted models. Loading is deterministic and offline; a
missing/invalid file aggregates every problem into a single
:class:`PolicyValidationError` rather than failing on the first issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from atlas.policy.models import (
    CadencePolicy,
    CompanySeedPolicy,
    ExclusionPolicy,
    ExperiencePolicy,
    GeographyPolicy,
    SearchLane,
    SourcePolicy,
    VerificationPolicy,
)
from atlas.policy.versioning import fingerprint_payload, short

DEFAULT_POLICY_DIR = Path(__file__).resolve().parents[2] / "config" / "policy"

# The six independent lanes REQUIRED to exist and stay distinct (build spec 9).
REQUIRED_LANES: tuple[str, ...] = (
    "GENERAL_SOFTWARE",
    "JAVA_BACKEND",
    "JAVA_FULLSTACK",
    "REACT_FRONTEND",
    "DOTNET",
    "ENTERPRISE_HR_PAYROLL_INTEGRATION",
)


class PolicyValidationError(ValueError):
    """Aggregates every policy problem found (not just the first)."""


@dataclass(frozen=True)
class LoadedPolicy:
    """One loaded policy file: typed model + version + fingerprint + source."""

    name: str
    schema_version: int
    policy_version: str
    fingerprint: str
    source_path: str
    model: Any


@dataclass(frozen=True)
class PolicyBundle:
    lanes: Mapping[str, SearchLane]
    geography: GeographyPolicy
    experience: ExperiencePolicy
    exclusions: ExclusionPolicy
    cadence: CadencePolicy
    source_policy: SourcePolicy
    company_seed: CompanySeedPolicy
    verification: VerificationPolicy
    loaded: Mapping[str, LoadedPolicy] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Combined deterministic fingerprint across all loaded policies."""
        return fingerprint_payload({name: lp.fingerprint for name, lp in sorted(self.loaded.items())})

    @property
    def short_fingerprint(self) -> str:
        return short(self.fingerprint)

    def lane(self, key: str) -> SearchLane:
        return self.lanes[key]


def _read_yaml(path: Path, problems: list[str]) -> Mapping[str, Any]:
    if not path.exists():
        problems.append(f"missing policy file: {path.name}")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        problems.append(f"{path.name}: invalid YAML: {exc}")
        return {}
    if not isinstance(data, Mapping):
        problems.append(f"{path.name}: top level must be a mapping")
        return {}
    return data


def _versioned(name: str, raw: Mapping[str, Any], path: Path, model: Any) -> LoadedPolicy:
    return LoadedPolicy(
        name=name,
        schema_version=int(raw.get("schema_version", 1)),
        policy_version=str(raw.get("policy_version", "unversioned")),
        fingerprint=fingerprint_payload({k: v for k, v in raw.items()}),
        source_path=str(path),
        model=model,
    )


def load_policy(policy_dir: Optional[Path] = None) -> PolicyBundle:
    """Load and validate the full public policy bundle."""
    policy_dir = Path(policy_dir) if policy_dir else DEFAULT_POLICY_DIR
    problems: list[str] = []
    loaded: dict[str, LoadedPolicy] = {}

    # --- lanes ---
    lanes_raw = _read_yaml(policy_dir / "search_lanes.yaml", problems)
    lanes: dict[str, SearchLane] = {}
    for key, body in (lanes_raw.get("lanes", {}) or {}).items():
        if not isinstance(body, Mapping):
            problems.append(f"search_lanes.yaml: lane {key!r} must be a mapping")
            continue
        lanes[str(key)] = SearchLane.from_dict(str(key), body)
    for required in REQUIRED_LANES:
        if required not in lanes:
            problems.append(f"search_lanes.yaml: required lane {required} missing")
    loaded["lanes"] = _versioned("lanes", lanes_raw, policy_dir / "search_lanes.yaml", lanes)

    # --- geography ---
    geo_raw = _read_yaml(policy_dir / "geography.yaml", problems)
    geography = GeographyPolicy.from_dict(geo_raw)
    if not geography.locations:
        problems.append("geography.yaml: no locations defined")
    loaded["geography"] = _versioned("geography", geo_raw, policy_dir / "geography.yaml", geography)

    # --- experience ---
    exp_raw = _read_yaml(policy_dir / "experience.yaml", problems)
    experience = ExperiencePolicy.from_dict(exp_raw)
    loaded["experience"] = _versioned("experience", exp_raw, policy_dir / "experience.yaml", experience)

    # --- exclusions ---
    exc_raw = _read_yaml(policy_dir / "exclusions.yaml", problems)
    exclusions = ExclusionPolicy.from_dict(exc_raw)
    if not exclusions.families:
        problems.append("exclusions.yaml: no exclusion families defined")
    loaded["exclusions"] = _versioned("exclusions", exc_raw, policy_dir / "exclusions.yaml", exclusions)

    # --- cadence ---
    cad_raw = _read_yaml(policy_dir / "cadence.yaml", problems)
    cadence = CadencePolicy.from_dict(cad_raw)
    for tier in ("A", "B", "C"):
        if tier not in cadence.tiers:
            problems.append(f"cadence.yaml: tier {tier} missing")
    loaded["cadence"] = _versioned("cadence", cad_raw, policy_dir / "cadence.yaml", cadence)

    # --- source policy ---
    src_raw = _read_yaml(policy_dir / "source_policy.yaml", problems)
    source_policy = SourcePolicy.from_dict(src_raw)
    if not source_policy.entries:
        problems.append("source_policy.yaml: no sources defined")
    for e in source_policy.entries:
        if e.live_adapter:
            problems.append(f"source_policy.yaml: {e.family} declares a live adapter — forbidden in Phase 1B")
    loaded["source_policy"] = _versioned("source_policy", src_raw, policy_dir / "source_policy.yaml", source_policy)

    # --- company seed ---
    seed_raw = _read_yaml(policy_dir / "company_seed.yaml", problems)
    company_seed = CompanySeedPolicy.from_dict(seed_raw)
    if company_seed.is_whitelist:
        problems.append("company_seed.yaml: seed must NOT be a whitelist (is_whitelist must be false)")
    loaded["company_seed"] = _versioned("company_seed", seed_raw, policy_dir / "company_seed.yaml", company_seed)

    # --- verification ---
    ver_raw = _read_yaml(policy_dir / "verification.yaml", problems)
    verification = VerificationPolicy.from_dict(ver_raw)
    if verification.require_final_apply_submission:
        problems.append("verification.yaml: require_final_apply_submission must be false (build spec 18)")
    loaded["verification"] = _versioned("verification", ver_raw, policy_dir / "verification.yaml", verification)

    if problems:
        joined = "\n  - ".join(problems)
        raise PolicyValidationError(f"Invalid Atlas policy:\n  - {joined}")

    return PolicyBundle(
        lanes=lanes,
        geography=geography,
        experience=experience,
        exclusions=exclusions,
        cadence=cadence,
        source_policy=source_policy,
        company_seed=company_seed,
        verification=verification,
        loaded=loaded,
    )


__all__ = [
    "REQUIRED_LANES",
    "PolicyValidationError",
    "LoadedPolicy",
    "PolicyBundle",
    "load_policy",
    "DEFAULT_POLICY_DIR",
]
