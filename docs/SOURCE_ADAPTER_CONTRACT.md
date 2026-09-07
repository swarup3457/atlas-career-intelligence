# SourceAdapter V1 contract

Every production adapter subclasses `atlas.sources.adapter.SourceAdapter`.
It supersedes the untyped Phase 0.5 `BaseSource` (retained for
compatibility). Real adapters are **not** part of Phase 1A.

## Declarations

```python
class MyAdapter(SourceAdapter):
    source_type = SourceType.ATS_GREENHOUSE      # family
    CAPABILITIES = frozenset({Capability.SEARCH, Capability.DETAIL, ...})
    adapter_version = "1.0.0"
    parser_version = "1.0.0"
    concurrency_class = ConcurrencyClass.HTTP     # scheduling lane
```

An adapter is constructed with a configured `SourceInstance` whose
`source_type` must match the class.

## Operations (typed in, typed out)

| Method | Request | Result |
|---|---|---|
| `capabilities()` | – | `frozenset[Capability]` (class set ∪ instance overrides) |
| `health_check()` | – | `SourceHealth` (read-only; no search/extraction) |
| `discover(req)` | `DiscoverRequest` | `DiscoverResult` (entry points for a company) |
| `search(req)` | `SearchRequest` | `SearchResult` (list of `DiscoveryResult`) |
| `fetch_detail(req)` | `DetailRequest` | `DiscoveryResult` (hydrated) |

Rules:

- **No `list[dict]`** as the contract — always typed objects.
- **Unsupported functionality raises `CapabilityNotSupported`** — never a
  silent `[]`. Call `self._require(Capability.X)` at the top of an op.
- **One attempt per call.** Adapters never retry, sleep, or set budgets.
  Classify failures by raising `AdapterError(ErrorCategory.X, msg)`; the
  governor + `atlas.orchestration.retry` decide retry vs escalate.
- **Never invent data.** Unknown fields stay `None` / `UNKNOWN`.
- **Local dedupe only.** Global canonicalization happens after normalization
  in `atlas.sources.provenance` (reusing the Phase 0.9 identity engine).

## Failure taxonomy → retry

| `ErrorCategory` | Retryable? | Terminal (on exhaustion / non-retry) |
|---|---|---|
| `TRANSIENT_NAVIGATION`, `TIMEOUT`, `HTTP_5XX` | yes | `SOURCE_UNAVAILABLE` / `PERMANENT_FAILURE` |
| `HTTP_429` | yes (honor Retry-After via rate limiter) | `RATE_LIMITED` |
| `PARSE_FAILURE`, `INVALID_RESPONSE`, `SELECTOR_UNCERTAINTY` | no | `EXTRACTION_UNRESOLVED` |
| `CONFIG_ERROR`, `UNKNOWN` | no | `PERMANENT_FAILURE` |
| `SOURCE_UNAVAILABLE` | no | `SOURCE_UNAVAILABLE` |
| `CAPTCHA`, `MFA`, `ANTI_BOT`, `LOGIN_WALL` | **never (non-bypass)** | human escalation / `ACCESS_LIMITED` |

## DiscoveryResult

Normalized shape carrying identity, provenance (`adapter_version`,
`parser_version`, `provenance`), core fields (nullable), status
(`is_active`, `verification_level`), and a `raw_observation_ref` (a hash
pointer — never the full page). `content_hash()` excludes observational
fields so re-seeing an unchanged posting is idempotent.

## Registry & config

Register with `@register_source` (or an isolated `SourceRegistry`). Configure
instances via `atlas.sources.config.load_source_config` — credentials are
**references only** (`auth_ref`, e.g. an env-var name); embedded secret
values or forbidden keys are rejected.

## Adding a source

`atlas add-source "Name" --type <SourceType>` prints a dry-run scaffold plan
(adapter, descriptor, contract test, fixture, docs). Pass `--out DIR --write`
to emit templates. Implement, add sanitized fixtures, and pass the reusable
contract harness (`atlas.sources.testing.contract.run_contract_checks`).
