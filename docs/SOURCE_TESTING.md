# Source testing

## Reusable adapter contract harness

`atlas.sources.testing.contract.run_contract_checks(make_adapter)` is the
suite every future adapter must satisfy. It is driven by a factory
`make_adapter(scenario) -> SourceAdapter | None`; returning `None` for an
unrepresentable scenario SKIPs that check (never fails it).

Covered categories: registration/identity, capabilities, typed result
schema, query validation, pagination, recency, normalization, missing
fields, malformed-item isolation, 429/5xx/timeout mapping, 404 behavior,
trusted/untrusted zero, sentinel behavior, selector drift, closed-state,
Unicode, provenance, adapter/parser version, and no-secret evidence.

Example:

```python
def _factory(scenario):
    if scenario == "results": return MyAdapter(instance_with_results)
    if scenario == "zero":    return MyAdapter(instance_with_zero)
    # ... return None to skip a scenario your adapter cannot represent
    return None

def test_my_adapter_contract():
    report = run_contract_checks(_factory)
    assert report.ok, report.render()
```

## Test doubles

- `FakeAdapter` (`SourceType.FAKE`) — scripts every scenario (results, zero,
  untrusted zero, 429, 5xx, timeout, parse failure, access-limited, selector
  drift, transient-then-success) with no network. Scenario comes from
  instance metadata or an explicit `FakeScenario`.
- `FixtureAdapter` (`SourceType.FIXTURE`) — parses sanitized local JSON
  fixtures via `parse_isolated`, proving parsing/normalization,
  one-bad-item isolation, Unicode, schema drift, zero classification, and
  closed-state.

Between the two, every required contract category is covered.

## Fixture strategy

Fixtures are minimal, sanitized, and generic (`{"items": [...]}`), never a
copy of a real site's proprietary markup, and never contain sessions,
cookies, credentials, or private profile data. Live tests (against real
sites) are kept separate from CI and are not part of Phase 1A.

## Persistence, coverage & crash/resume tests

- `test_source_coverage.py` — manifest completion (PLANNED vs TERMINAL) and
  SQLite round-trip.
- `test_source_provenance.py` — multi-source dedupe, duplicate idempotency,
  repost flagging, version persistence (reusing the Phase 0.9 identity
  engine).
- `test_source_langgraph_integration.py` — the framework wired into the real
  governor with mixed outcomes, plus a crash/resume that proves no task is
  repeated and observations are not duplicated.

## Security regressions

`test_source_security_regression.py` guards that the atlas package never
references the research repos at runtime, contains no auto-apply/submission
code, never executes source content, keeps browser profiles + runtime DBs
gitignored, rejects plaintext credentials in config, and redacts secrets
from raw evidence.
