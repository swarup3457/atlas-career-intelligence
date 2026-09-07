"""Atlas Phase 0.75 production runtime shell.

This package wires the already-proven generic Phase 0 / Phase 0.5
components (configuration, run lock, LangGraph governor, SQLite state,
checkpointing, retry policy, worker/source contracts, event bus, metrics,
structured logging, graceful shutdown, intervention queue, BrowserManager,
report scaffold) into one production-shaped runtime.

IMPORTANT: no real job-search business logic exists here. `atlas run` only
supports `--demo` (deterministic fake workers) in this build. See
docs/RUNTIME.md.
"""

from __future__ import annotations

__all__ = ["states", "scheduler", "concurrency", "failure_injection", "demo_workload", "progress", "manifest", "report", "engine"]
