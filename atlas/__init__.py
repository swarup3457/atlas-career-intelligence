"""Atlas Career Intelligence — local career-intelligence platform.

This package hosts the deterministic orchestration/runtime foundation for
Atlas together with the Phase 1B business-policy layer imported from the
Workspace Agent specification: typed search policy (six lanes, India
geography, experience, exclusions, cadence, source policy, 108-company
seed, verification), separated status axes, a private candidate evidence
ledger, a sealed coverage planner, and an explicit multi-phase production
search graph/runtime.

The platform spine remains: configuration, controller abstraction,
LangGraph orchestration, a reusable Playwright BrowserManager, durable
SQLite state, structured logging, source/company registries, and
report-only Excel generation. NO live source adapter and NO live web
search exist yet — those are deferred to Phase 1C.
"""

__version__ = "0.2.0"
