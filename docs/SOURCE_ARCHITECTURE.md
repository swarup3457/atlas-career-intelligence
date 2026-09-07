# Atlas Source Architecture (Phase 1A foundation)

Status: **framework production-ready; zero real adapters shipped.** Phase 1A
builds the generic discovery/search engine *on top of* the proven Phase
0–0.95 platform (LangGraph governor, retries, checkpoints, SQLite state,
identity/reconciliation, BrowserManager, backup/restore). No candidate,
company universe, ranking weights, or search keywords are defined — those
arrive with the Workspace Atlas Agent import.

## Layers

```
SearchStrategy (candidate-agnostic)         config/ (SourceInstance descriptors, credential-free)
        │ compiles to SearchRequest                 │ validated by atlas.sources.config
        ▼                                           ▼
atlas.sources.registry  ── creates ──►  SourceAdapter (V1 typed contract)
        │  capability-based selection            │ discover / search / fetch_detail / health_check
        ▼                                        ▼
atlas.sources.worker.SourceSearchWorker  ──►  DiscoveryResult (normalized)
        │ (BaseWorker: one attempt, no retry)      │
        ▼                                          ▼
Existing LangGraph governor  ──►  retry policy · checkpoints · coverage manifest
        │                                          │
        ▼                                          ▼
atlas.sources.provenance  ── reuses Phase 0.9 IdentityResolver ──►  canonical_jobs + job_observations
```

## Module map (`atlas/sources/`)

| Module | Responsibility | Status |
|---|---|---|
| `models.py` | `SourceType`, `SourceInstance`, `Capability`, `WorkMode`, `DiscoveryResult`, typed requests/results | production |
| `adapter.py` | `SourceAdapter` V1 typed contract, `CapabilityNotSupported`, `AdapterError` | production |
| `health.py` | `SourceHealth` states + `classify_health` | production |
| `zero_result.py` | trusted/untrusted/unresolved zero + bounded sentinel | production |
| `rate_limit.py` | per-source token/interval/concurrency + Retry-After + adaptive backoff | production |
| `registry.py` | decorator registry, one adapter class per family | production |
| `config.py` | descriptor validation (credential-reference-only) | production |
| `coverage.py` | coverage manifest + `CoverageStatus` + persistence | production |
| `evidence.py` | bounded, redacting raw-evidence store | production |
| `fingerprint.py` | deterministic ATS fingerprinting | production |
| `parsing.py` | per-item parse isolation | production |
| `untrusted.py` | untrusted-content security contract | production |
| `provenance.py` | DiscoveryResult → canonical layer (reuses identity engine) | production |
| `worker.py` | adapter → governor bridge | production |
| `scaffold.py` | `atlas add-source` developer templates | production (dev tool) |
| `testing/` | FakeAdapter, FixtureAdapter, contract harness | production (test doubles) |
| `base.py` | legacy Phase 0.5 `BaseSource` | retained for compatibility |
| `ats/`, `portals/` | **empty** — no real adapters in Phase 1A | scaffold |

## Source type vs source instance

`SourceType` is a *family* (e.g. `ATS_WORKDAY`). A `SourceInstance` is a
configured deployment (one company's Workday tenant). One adapter class
serves the whole family; instances are data. We never write
`MicrosoftAdapter`/`WalmartAdapter` for shared ATS behavior.

## Persistence (SQLite migration v4)

- `coverage_records` — per company × source × lane × query terminal status.
- `source_health_history` — recent yields + health state for false-zero detection.
- `job_observations.adapter_version` / `.parser_version` — added columns so a
  parser change never mutates historical observations.

Migration is append-only, transactional, idempotent, and preserves existing
DBs (verified against a copy of the live v3 state DB).

## What MUST wait for the Workspace Atlas Agent import

Final SearchStrategy content, search lanes, company universe/tiers, cadence
(delta/deep policy), ranking weights, candidate matching, verification
vocabulary, Excel business schema, recruiter workflow, resume tailoring, and
the real India query set. Phase 1A deliberately ships none of these.

## What Atlas will NEVER inherit

Auto-apply/submission, stealth/anti-detection, `AutomationControlled`
evasion, CAPTCHA/MFA bypass, credential storage, and dependence on the
research repositories at runtime. Atlas remains discovery/intelligence only.
