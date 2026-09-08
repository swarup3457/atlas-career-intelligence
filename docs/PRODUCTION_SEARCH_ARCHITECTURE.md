# Production Search Architecture

Status: **implemented (Phase 1B).** This document records the architecture
corrections that make production search correct at market scale. Phase 1B ships
**no live adapters and performs no live web search** — every guarantee below is
exercised with fake/fixture sources.

## Source taxonomy split

A single `SourceType` could not host both LinkedIn and Naukri (both "portals")
without collapsing their identity. The taxonomy is now three orthogonal axes
(`atlas/sources/models.py`):

- **`SourceCategory`** — the broad role a source plays:
  `OFFICIAL / ATS / PORTAL / SPECIALIST / AGGREGATOR / FALLBACK / MANUAL / TEST`.
- **`SourceFamily`** (the `adapter_key`) — the concrete adapter identity:
  `workday, greenhouse, lever, ashby, linkedin, naukri, …`.
- **`SourceInstance`** — a configured tenant/deployment/market of a family.

The registry (`atlas/sources/registry.py`) is keyed by **`adapter_key`**, so
LinkedIn and Naukri coexist under `PORTAL` as distinct families. A backward-
compatible `SourceType` lookup still works when a type maps to exactly one
registered family. **Ashby** is a first-class ATS family, with deterministic
fingerprinting (`atlas/sources/fingerprint.py`) and tenant extraction
(`atlas/company/tenant.py`).

## Capability model (additions *and* removals)

Planning branches on declared capabilities, never on hard-coded source names
(`atlas/sources/models.py::Capability`, `atlas/sources/adapter.py`). **`DISCOVER`
is a distinct capability from `SEARCH`** — a source may enumerate a company's
roles without keyword search, or vice versa. A `SourceInstance` may both **add**
capabilities (`capability_overrides`) and **remove** them
(`capability_removals`) relative to its adapter class default.

## First-class source instances

Source instances are persisted (`atlas/persistence/sqlite.py` migration **v6**,
`atlas/sources/instance_store.py`) with `adapter_key`, category, lifecycle
state, and capability add/remove sets. Workday is addressed by **tenant + site**
(a tenant host alone cannot locate the CXS/search endpoint). Credentials are
never stored — only an `auth_ref` reference.

## Deterministic query signatures

Source health and yield history are keyed by **what was actually searched**, not
merely by instance (`atlas/sources/query_signature.py`). A `QuerySignature`
normalizes source instance, `adapter_key`, lane, geography group, mode, keyword
bundle, filters, cursor family, and policy version into a stable fingerprint, so
a legitimate zero for one lane/geo is never compared against unrelated history
and falsely flagged as selector drift.

## Sealed coverage plans

Coverage plans have an explicit lifecycle `BUILDING / SEALED / FAILED`
(`atlas/sources/coverage.py`). Only a **sealed** plan is production-executable;
an empty plan can only be sealed as **`NO_WORK_DUE`** (never a silent
"complete"), a planning failure is distinct from `NO_WORK_DUE`, a conflicting
duplicate `coverage_id` is rejected, and the plan carries a deterministic
fingerprint. Attempt history is stored in an **append-only, immutable
`coverage_attempts`** table so diagnostics survive the resolved-state upsert.

## Compact checkpoints & separate runtime

Production checkpoints are **compact** (`atlas/orchestration/production_state.py`):
run id, policy/plan fingerprints, phase, task IDs, counters, cursors, retry
refs, and short status summaries only — never job descriptions or payloads. A
size guard proves the checkpoint stays bounded across thousands of discoveries.
The `ProductionSearchRuntime` (`atlas/runtime/production.py`) is **separate**
from the demo `AtlasRuntime`, which is preserved for regression. Production
reasoning uses typed controller operations
(`atlas/controllers/operations.py`) rather than free-text responses, and runs
end-to-end under `NullController`.

See [PRODUCTION_PHASE_GRAPH.md](PRODUCTION_PHASE_GRAPH.md) for the phase order
that consumes this architecture.
