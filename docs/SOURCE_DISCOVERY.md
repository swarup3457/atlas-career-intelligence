# Source Discovery (Phase 1A.5)

Deterministic, **offline** discovery of a company's career sources. No live
web search, no LLM-invented URLs, no auto-apply.

## Pipeline

```
company observation (name [+ domain] [+ careers/candidate/redirect URL] [+ markers])
    → register/resolve company identity (merge-safe)
    → [trusted method only] fingerprint ATS (atlas.sources.fingerprint)
    → tenant extraction (atlas.company.tenant)
    → SourceDiscoveryObservation (append-only provenance)
    → SourceInstance (factory: type, stable id, base_url, tenant, family caps)
    → company↔source relationship (DISCOVERED)
```

`register_employer(registry, observation)` runs the whole flow and returns an
`EmployerRegistrationResult`.

## Domain safety

Career domains/sources may only be registered from **trusted** discovery
methods: `EXPLICIT_CONFIG`, `CONFIRMED_IDENTITY`, `CANDIDATE_URL` (user
supplied), `VALIDATED_REDIRECT`, `FINGERPRINT` (from an already-trusted
careers URL).

A URL found inside job-posting text (`UNTRUSTED_POSTING`) is **never** used
to register an official domain or source. Such an observation registers only
the company *name* (domain/careers stripped) and is recorded as a `REJECTED`
discovery observation. This is regression-tested.

## Confidence (categorical, not fake precision)

Relationship confidence is categorical — `CONFIRMED` (explicit config /
confirmed identity / user URL), `STRONG` (host-level ATS fingerprint /
validated redirect), `TENTATIVE` (path/marker fingerprint), `UNKNOWN`.
Numeric fingerprint scores are mapped into these buckets by
`confidence_from_fingerprint`. Categorical confidence avoids implying a
precision (0.8734…) the heuristics do not have.

## SourceInstance factory

`build_source_instance` produces a **disabled** `SourceInstance` (no runnable
adapter exists in Phase 1A.5) with a deterministic `instance_id` (stable per
company + family + tenant), the resolved base URL, the extracted tenant, and
the family's default capabilities. Rediscovering the same tenant yields the
same `instance_id` (idempotent).

## Multiple sources & ATS migration

- A company may have **multiple simultaneously current** relationships
  (global ATS + India ATS + internship portal). Discovery never forces a
  single "current source".
- ATS migration is **non-destructive**: `mark_source_replaced` sets the old
  relationship to `REPLACED` / non-current and retains it as history; the new
  relationship stays current. Nothing is deleted.

## Career endpoint discovery contract

`discover_career_endpoints(name, known_domain=…, candidate_url=…)` returns
candidate endpoints constructed only from an already-known official domain or
a user-supplied URL — it never fetches, never invents random URLs, and never
consults an LLM. It is a contract for a future, policy-supplied discovery
mechanism.

## Company discovery queue

`CompanyDiscoveryTask` is a generic queue item (company name, reason, known
URL, neutral `priority=0`, attempt count). Real prioritization is a Workspace
policy and is intentionally absent.
