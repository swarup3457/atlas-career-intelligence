"""Local six-lane title/metadata prefilter (architecture s.3.5, build spec 12.2).

Recall-oriented: it runs all six lanes locally against ONE immutable board
snapshot and selects detail candidates. It may admit false positives — it must
NOT produce final recommendations. Strict qualification runs later on hydrated
evidence. The union of prefilter candidates is hydrated once per unique job.
"""

from __future__ import annotations

from typing import Iterable

from atlas.hunt.models import BoardSnapshot, LanePrefilterDecision, RawJob
from atlas.hunt.role_family import DEVELOPMENT_FAMILIES, classify_role_family
from atlas.hunt.role_intent import RoleIntentPolicy
from atlas.hunt.signals import find_signals

__all__ = ["prefilter_snapshot", "hydration_union"]


def prefilter_snapshot(
    snapshot: BoardSnapshot, policy: RoleIntentPolicy
) -> list[LanePrefilterDecision]:
    """Produce recall-oriented per-(job, lane) prefilter decisions for every raw
    job in a snapshot. One snapshot feeds all six lanes; there is NO network
    here (build spec 12.1: collect once, classify many)."""
    decisions: list[LanePrefilterDecision] = []
    for job in snapshot.raw_jobs:
        text = f"{job.title} \n {job.summary}"
        rf = classify_role_family(job.title, job.summary)
        dev_plausible = rf.family in DEVELOPMENT_FAMILIES
        for key in policy.lane_keys():
            lane = policy.lane(key)
            hits = find_signals(text, lane.title_prefilter_signals)
            # Admit for hydration when a lane title signal matches, or when the
            # role looks like a development role (broad recall for the fallback).
            candidate = bool(hits) or (dev_plausible and lane.is_fallback)
            if candidate:
                reasons = ["title_signal" if hits else "dev_role_plausible"]
                decisions.append(
                    LanePrefilterDecision(
                        snapshot_job_id=job.source_job_id,
                        lane=key,
                        candidate_for_hydration=True,
                        signals=hits,
                        reason_codes=tuple(reasons),
                        policy_hash=policy.fingerprint,
                    )
                )
    return decisions


def hydration_union(decisions: Iterable[LanePrefilterDecision]) -> tuple[str, ...]:
    """De-duplicate prefilter candidates by source job id so each unique job is
    hydrated at most once across all lanes (build spec 12.3)."""
    seen: list[str] = []
    for d in decisions:
        if d.candidate_for_hydration and d.snapshot_job_id not in seen:
            seen.append(d.snapshot_job_id)
    return tuple(seen)
