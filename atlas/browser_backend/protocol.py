"""The typed backend boundary the LangGraph company node depends on.

LangGraph resolves a :class:`CompanySearchBackend` and calls
``search_company(task)``; it never sees subprocess, MCP, or Copilot-CLI details.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from atlas.browser_backend.models import BackendResult, CompanyTask


@runtime_checkable
class CompanySearchBackend(Protocol):
    """A backend that searches exactly one company and returns a typed result.

    Implementations must be safe to call concurrently for *different* companies
    with isolated browser/MCP state, and must never auto-apply, log in, or submit
    a form.
    """

    name: str

    def search_company(self, task: CompanyTask) -> BackendResult:
        ...


__all__ = ["CompanySearchBackend", "BackendResult", "CompanyTask"]
