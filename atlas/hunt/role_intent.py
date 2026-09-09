"""Typed Role-Intent V2 policy (build spec section 8).

Loads ``config/policy/role_intent_v2.yaml`` into frozen dataclasses and exposes
a stable fingerprint (policy hash) used in run lineage. This is the versioned
contract that FINAL qualification uses — distinct from the discovery-only
``search_lanes.yaml``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

__all__ = [
    "AnchorGroup",
    "LaneContract",
    "RoleIntentPolicy",
    "load_role_intent_policy",
    "DEFAULT_ROLE_INTENT_PATH",
]

DEFAULT_ROLE_INTENT_PATH = Path("config/policy/role_intent_v2.yaml")


def _tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


@dataclass(frozen=True)
class AnchorGroup:
    group: str
    any_of: tuple[str, ...]


@dataclass(frozen=True)
class LaneContract:
    key: str
    priority: int
    display_name: str
    allowed_role_families: tuple[str, ...]
    excluded_role_families: tuple[str, ...]
    required_anchor_groups: tuple[AnchorGroup, ...]
    support_signals: tuple[str, ...] = ()
    wrong_stack_signals: tuple[str, ...] = ()
    reject_when_wrong_stack_dominant: bool = True
    is_fallback: bool = False
    require_transferable_or_neutral: bool = False
    transferable_or_neutral_any: tuple[str, ...] = ()
    title_prefilter_signals: tuple[str, ...] = ()
    query_templates: tuple[str, ...] = ()
    # Tie-breaker for choosing a job's PRIMARY display lane when it qualifies
    # for several lanes. A named-domain lane (enterprise) or a multi-anchor lane
    # (Java full stack) is more specific than a single-tech lane; the fallback
    # is least specific. Defaults to the number of required anchor groups.
    selection_specificity: int = -1

    @property
    def required_group_count(self) -> int:
        return len(self.required_anchor_groups)

    @property
    def specificity(self) -> int:
        return self.selection_specificity if self.selection_specificity >= 0 else len(self.required_anchor_groups)

    @classmethod
    def from_dict(cls, key: str, raw: Mapping[str, Any]) -> "LaneContract":
        groups = tuple(
            AnchorGroup(group=str(g.get("group", "")), any_of=_tuple(g.get("any_of")))
            for g in (raw.get("required_anchor_groups") or [])
        )
        return cls(
            key=key,
            priority=int(raw.get("priority", 999)),
            display_name=str(raw.get("display_name", key)),
            allowed_role_families=_tuple(raw.get("allowed_role_families")),
            excluded_role_families=_tuple(raw.get("excluded_role_families")),
            required_anchor_groups=groups,
            support_signals=_tuple(raw.get("support_signals")),
            wrong_stack_signals=_tuple(raw.get("wrong_stack_signals")),
            reject_when_wrong_stack_dominant=bool(raw.get("reject_when_wrong_stack_dominant", True)),
            is_fallback=bool(raw.get("is_fallback", False)),
            require_transferable_or_neutral=bool(raw.get("require_transferable_or_neutral", False)),
            transferable_or_neutral_any=_tuple(raw.get("transferable_or_neutral_any")),
            title_prefilter_signals=_tuple(raw.get("title_prefilter_signals")),
            query_templates=_tuple(raw.get("query_templates")),
            selection_specificity=int(raw.get("selection_specificity", -1)),
        )


@dataclass(frozen=True)
class RoleIntentPolicy:
    policy_version: str
    lanes: Mapping[str, LaneContract]
    max_display_per_company: int = 5
    max_display_per_lane_per_company: int = 3
    max_shortlist_total: int = 15
    fingerprint: str = ""

    def lane(self, key: str) -> LaneContract:
        return self.lanes[key]

    def lane_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self.lanes, key=lambda k: self.lanes[k].priority))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RoleIntentPolicy":
        lanes = {
            str(k): LaneContract.from_dict(str(k), v)
            for k, v in (raw.get("lanes") or {}).items()
        }
        disp = raw.get("display_defaults") or {}
        fingerprint = _fingerprint(raw)
        return cls(
            policy_version=str(raw.get("policy_version", "unknown")),
            lanes=lanes,
            max_display_per_company=int(disp.get("max_display_per_company", 5)),
            max_display_per_lane_per_company=int(disp.get("max_display_per_lane_per_company", 3)),
            max_shortlist_total=int(disp.get("max_shortlist_total", 15)),
            fingerprint=fingerprint,
        )


def _fingerprint(raw: Mapping[str, Any]) -> str:
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_role_intent_policy(path: Optional[Path] = None, *, root: Optional[Path] = None) -> RoleIntentPolicy:
    """Load and fingerprint the Role-Intent V2 policy. ``root`` allows tests to
    point at a temporary policy tree."""
    p = Path(path) if path else DEFAULT_ROLE_INTENT_PATH
    if not p.is_absolute():
        p = (Path(root) if root else Path.cwd()) / p
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"role_intent policy must be a mapping: {p}")
    policy = RoleIntentPolicy.from_dict(raw)
    if len(policy.lanes) != 6:
        raise ValueError(
            f"role_intent policy must define exactly six lanes, found {len(policy.lanes)}"
        )
    return policy
