# Atlas Controller Abstraction

## Why this exists

Atlas will initially be built using Copilot and/or Codex, but the
codebase must not bake either product into deterministic orchestration.
Switching the reasoning controller in the future must never require
changing LangGraph, Playwright, SQLite, workers, state, source adapters,
Excel, or GitHub persistence code.

## The interface

`atlas/controllers/base.py` defines:

```python
class Controller(Protocol):
    def reason(self, request: ControllerRequest) -> ControllerResponse: ...
    def classify(self, request: ControllerRequest) -> ControllerResponse: ...
    def extract(self, request: ControllerRequest) -> ControllerResponse: ...
    def review(self, request: ControllerRequest) -> ControllerResponse: ...
```

`ControllerRequest` is a generic `{prompt, context}` payload and
`ControllerResponse` is a generic `{content, raw, metadata}` result — both
defined in `atlas/controllers/base.py`. Using one generic request/response
shape (rather than bespoke parameters per method) keeps every controller
adapter interchangeable.

- `BaseController` — an ABC other controllers may subclass for shared
  behavior (currently just response-shape helpers).
- `NullController` — **IMPLEMENTED**. Returns clearly-marked no-op
  `ControllerResponse` objects (`metadata={"noop": True}`) for every
  method. This is what makes `controller=none` fully deterministic test
  runs possible — no code path secretly requires an LLM to run.
- `CopilotController` (`atlas/controllers/copilot.py`) — **SCAFFOLDED**.
  Every method raises `NotImplementedError` with a docstring explaining
  that no supported programmatic integration exists yet. This is
  deliberate: the spec forbids "unsupported hacks to programmatically
  invoke" Copilot.
- `CodexController` (`atlas/controllers/codex.py`) — **SCAFFOLDED**, same
  shape/reasoning as `CopilotController`.

## Factory / isolation boundary

`atlas.controllers.base.get_controller(name)` is the only place that
imports `CopilotController`/`CodexController`, and it imports them lazily
(only when actually requested). No other module in the codebase imports
`atlas.controllers.copilot` or `atlas.controllers.codex` directly — this
keeps every product-specific integration point isolated under
`atlas/controllers/`, satisfying spec section 21.

## What "deterministic workers must remain usable without either
controller" means concretely

Every worker built so far (`atlas/workers/career_page.py`,
`atlas/workers/company.py`, `atlas/workers/verification.py`) and the
entire orchestration graph (`atlas/orchestration/graph.py`) run correctly
with `controller="none"` and no `Controller` instance in the loop at all
— see `tests/test_foundation_suite.py` section I, which runs a full
LangGraph queue-processing cycle with zero LLM involvement.

## Adding a real controller integration later

When a supported integration path exists for Copilot or Codex:

1. Implement the real logic inside `atlas/controllers/copilot.py` or
   `codex.py` only.
2. Do not change the `Controller` protocol shape unless a genuinely new
   capability is needed (and if so, add it to the protocol so all
   controllers, including `NullController`, implement it consistently).
3. Do not add controller-specific branching anywhere in
   `atlas/orchestration/`, `atlas/workers/`, or `atlas/browser/` — those
   layers should only ever call the `Controller` protocol methods.
