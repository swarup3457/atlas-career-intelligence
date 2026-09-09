"""Per-company + total model usage capture for the pilot (architecture s.10, prompt s.10).

Consumes the official SDK's ``AssistantUsageData`` events (and context-info events) into a
reconcilable usage ledger: model, input / cached-input / output / reasoning tokens, AI
credits (nano-AIU), context utilization, tool calls, session id, and wall time. Everything
here degrades safely when no SDK event is available (deterministic / NullController path):
counters simply stay zero and are reported truthfully.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class ModelUsage:
    model: str
    api_calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_write_tokens: int = 0
    ai_credits: float = 0.0
    cost: float = 0.0
    tool_calls: int = 0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "api_calls": self.api_calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "ai_credits": round(self.ai_credits, 6),
            "cost": round(self.cost, 6),
            "tool_calls": self.tool_calls,
            "elapsed_s": round(self.elapsed_s, 3),
        }


@dataclass
class UsageMeter:
    """Thread-safe usage accumulator. One meter aggregates the whole pilot; a per-company
    child meter is created with :meth:`child`."""

    label: str = "pilot"
    by_model: dict[str, ModelUsage] = field(default_factory=dict)
    session_ids: set[str] = field(default_factory=set)
    tool_calls: int = 0
    context_max_tokens: int = 0
    context_limit: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _model(self, model: str) -> ModelUsage:
        mu = self.by_model.get(model)
        if mu is None:
            mu = ModelUsage(model=model)
            self.by_model[model] = mu
        return mu

    def record_usage_event(self, data: Any, *, model_hint: str = "") -> None:
        """Record one ``AssistantUsageData``-shaped event."""
        with self._lock:
            model = str(getattr(data, "model", "") or model_hint or "unknown")
            mu = self._model(model)
            mu.api_calls += 1
            mu.input_tokens += _i(getattr(data, "input_tokens", 0))
            mu.cached_input_tokens += _i(getattr(data, "cache_read_tokens", 0))
            mu.cache_write_tokens += _i(getattr(data, "cache_write_tokens", 0))
            mu.output_tokens += _i(getattr(data, "output_tokens", 0))
            mu.cost += _f(getattr(data, "cost", 0.0))
            dur = getattr(data, "duration", None)
            if dur is not None and hasattr(dur, "total_seconds"):
                mu.elapsed_s += dur.total_seconds()
            cu = getattr(data, "copilot_usage", None)
            if cu is not None:
                mu.ai_credits += _f(getattr(cu, "total_nano_aiu", 0.0)) / 1e9
                for td in (getattr(cu, "_token_details", None) or []):
                    ttype = str(getattr(td, "token_type", "")).lower()
                    if "reason" in ttype:
                        mu.reasoning_tokens += _i(getattr(td, "token_count", 0))
            sid = getattr(data, "api_call_id", None)
            if sid:
                self.session_ids.add(str(sid))

    def record_context_info(self, data: Any) -> None:
        with self._lock:
            self.context_max_tokens = max(self.context_max_tokens, _i(getattr(data, "current_tokens", 0)))
            self.context_limit = max(self.context_limit, _i(getattr(data, "token_limit", 0)))

    def record_tool_call(self, model: str = "") -> None:
        with self._lock:
            self.tool_calls += 1
            if model:
                self._model(model).tool_calls += 1

    def add_session_id(self, sid: str) -> None:
        with self._lock:
            if sid:
                self.session_ids.add(sid)

    def child(self, label: str) -> "UsageMeter":
        return UsageMeter(label=label)

    def merge(self, other: "UsageMeter") -> None:
        with self._lock:
            for model, mu in other.by_model.items():
                dst = self._model(model)
                dst.api_calls += mu.api_calls
                dst.input_tokens += mu.input_tokens
                dst.cached_input_tokens += mu.cached_input_tokens
                dst.cache_write_tokens += mu.cache_write_tokens
                dst.output_tokens += mu.output_tokens
                dst.reasoning_tokens += mu.reasoning_tokens
                dst.ai_credits += mu.ai_credits
                dst.cost += mu.cost
                dst.tool_calls += mu.tool_calls
                dst.elapsed_s += mu.elapsed_s
            self.tool_calls += other.tool_calls
            self.session_ids |= other.session_ids
            self.context_max_tokens = max(self.context_max_tokens, other.context_max_tokens)
            self.context_limit = max(self.context_limit, other.context_limit)

    def totals(self) -> dict:
        with self._lock:
            t = {
                "input_tokens": sum(m.input_tokens for m in self.by_model.values()),
                "cached_input_tokens": sum(m.cached_input_tokens for m in self.by_model.values()),
                "output_tokens": sum(m.output_tokens for m in self.by_model.values()),
                "reasoning_tokens": sum(m.reasoning_tokens for m in self.by_model.values()),
                "cache_write_tokens": sum(m.cache_write_tokens for m in self.by_model.values()),
                "ai_credits": round(sum(m.ai_credits for m in self.by_model.values()), 6),
                "cost": round(sum(m.cost for m in self.by_model.values()), 6),
                "api_calls": sum(m.api_calls for m in self.by_model.values()),
                "tool_calls": self.tool_calls,
                "models": sorted(self.by_model.keys()),
                "session_ids": sorted(self.session_ids),
                "context_max_tokens": self.context_max_tokens,
                "context_limit": self.context_limit,
                "context_utilization": (
                    round(self.context_max_tokens / self.context_limit, 4)
                    if self.context_limit else 0.0
                ),
            }
            return t

    def snapshot(self) -> dict:
        return {
            "label": self.label,
            "totals": self.totals(),
            "by_model": {k: v.to_dict() for k, v in sorted(self.by_model.items())},
        }


__all__ = ["UsageMeter", "ModelUsage"]
