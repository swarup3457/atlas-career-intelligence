# Source health & zero-result safety

A source can be **reachable yet broken**: HTTP 200 but the extractor
silently returns nothing ("site up + selector drift = 'no jobs'"). Health is
modeled as a first-class diagnostic, kept **separate** from task/run
lifecycle state (`TaskStatus`). We deliberately do NOT pollute `TaskStatus`
with every diagnostic condition.

## Health states (`SourceHealthState`)

`HEALTHY`, `DEGRADED`, `SELECTOR_DRIFT_SUSPECTED`, `AUTH_REQUIRED`,
`RATE_LIMITED`, `ACCESS_LIMITED`, `SOURCE_UNAVAILABLE`, `UNKNOWN`.

`classify_health(HealthEvidence)` is deterministic and ordered:

1. access/auth/rate: challenge → `ACCESS_LIMITED`; login redirect →
   `AUTH_REQUIRED`; 429 → `RATE_LIMITED`; 5xx → `SOURCE_UNAVAILABLE`.
2. structural drift: missing markers / schema drift / undecoded entities /
   high parse-failure ratio → `SELECTOR_DRIFT_SUSPECTED`.
3. yield collapse: zero now but non-zero historical yields →
   `SELECTOR_DRIFT_SUSPECTED`.
4. generic degradation: high null-field or duplicate ratio → `DEGRADED`.
5. otherwise `HEALTHY`.

`HealthEvidence` fields are all optional and never invented.

## Zero-result safety

A reachable source returning zero is classified (`assess_search`) as one of:

- `TRUSTED_ZERO` — genuinely no matches, trustworthy.
- `UNTRUSTED_ZERO` — zero but suspicious (untrustworthy health, or a sudden
  collapse vs historical yield, or the adapter declared it untrusted).
- `EXTRACTION_UNRESOLVED` — could not even tell (parse produced only findings).

Health/history can only *escalate* a trusted zero to untrusted, never the
reverse.

## Bounded sentinel probe

`should_run_sentinel(kind)` is true **only** for `UNTRUSTED_ZERO`. When
triggered, `run_sentinel_probe(adapter, sentinel_request)` runs **exactly
one** broad query and classifies health — then stops. There is no retry
loop.

- sentinel returns results → source structurally healthy → original zero was
  query-specific → `TRUSTED_ZERO`.
- sentinel also zero, structure intact → `TRUSTED_ZERO`.
- sentinel zero, structure missing → `SELECTOR_DRIFT_SUSPECTED` /
  `UNTRUSTED_ZERO`.
- sentinel error (429/5xx/…) → classified health (`RATE_LIMITED` / …).

The `SourceSearchWorker` runs at most one sentinel per untrusted-zero
attempt and maps the outcome to a truthful terminal `TaskStatus`.

## Health history (false-zero detection)

`StateStore.record_source_health(...)` persists `{state, result_count,
expected_structure_present, ...}` per instance; `recent_yields(instance)`
returns the most recent counts so a run can see "42, 38, 47 … then 0" and
suspect drift instead of silently concluding "no jobs".

## Freshness invariant

`NO_LONGER_OBSERVED != CLOSED`. Closure requires explicit evidence
(closed banner, 404, expired deadline). Absence from today's results is not
closure. Final closure policy waits for the Workspace import.
