"""Atlas Career Intelligence — local platform foundation.

This package hosts the deterministic orchestration/runtime foundation for
Atlas. It intentionally does NOT contain final job-search business logic
(search lanes, company universe, ATS rules, candidate profile, Excel
schema, etc.) — that specification will be imported later from the
existing ChatGPT Workspace Atlas Agent.

Today's scope is the PLATFORM: configuration, controller abstraction,
LangGraph orchestration primitives, a reusable Playwright BrowserManager,
durable SQLite state, structured logging, and scaffolding for GitHub
persistence / Excel reporting.
"""

__version__ = "0.1.0-foundation"
