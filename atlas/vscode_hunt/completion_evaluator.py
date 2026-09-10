from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CompletionDecision:
    action: str
    missing_obligations: tuple[str, ...] = ()
    reason: str = ""


def evaluate_completion(result: Mapping[str, Any], required_lanes: tuple[str, ...]) -> CompletionDecision:
    missing: list[str] = []
    states = result.get("result_states") or {}
    attempted = set(result.get("lanes_attempted") or [])
    if isinstance(states, dict):
        attempted.update(states)
    for lane in required_lanes:
        if lane not in attempted:
            missing.append(f"lane:{lane}")
            continue
        state = states.get(lane) if isinstance(states, dict) else next((item.get("state") for item in states if isinstance(item, dict) and item.get("lane") == lane), None)
        if isinstance(state, dict):
            state = state.get("state") or state.get("status")
        if not state:
            missing.append(f"result-state:{lane}")
    if result.get("jobs") and not result.get("detail_urls"):
        missing.append("canonical-detail-urls")
    if result.get("jobs") and not result.get("evidence_quotes"):
        missing.append("grounded-evidence-quotes")
    if result.get("completion_claim") and result.get("browser_errors") and not result.get("external_block_evidence"):
        health = result.get("source_health") or {}
        if str(health.get("status", "")).lower() not in {"healthy", "usable", "ok"} and not health.get("detail_pages_usable"):
            missing.append("usable-source-health")
    if missing:
        return CompletionDecision("FOLLOW_UP_REQUIRED", tuple(dict.fromkeys(missing)), "search contract remains incomplete")
    if result.get("external_block_evidence") and not result.get("browser_errors"):
        return CompletionDecision("EXTERNAL_ACCESS_LIMITED", reason="external access limitation is evidenced")
    if result.get("completion_claim") is False:
        return CompletionDecision("NEEDS_REPAIR", reason="worker did not claim completion after satisfying observable obligations")
    return CompletionDecision("COMPLETE", reason="all required lanes have truthful terminal states and evidence")
