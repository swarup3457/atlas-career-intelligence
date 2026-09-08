# Search Policy

Status: **implemented (Phase 1B).** The Workspace search rules are imported as
**typed, validated, fingerprinted** policy under `config/policy/*.yaml`, loaded
by `atlas/policy/loader.py`. Loading is deterministic and offline; every problem
is aggregated into a single `PolicyValidationError` rather than failing on the
first issue. Policy is public — it contains no candidate PII.

## Six independent lanes

`config/policy/search_lanes.yaml` defines six lanes that are searched
independently and must never collapse into one:

`GENERAL_SOFTWARE`, `JAVA_BACKEND`, `JAVA_FULLSTACK`, `REACT_FRONTEND`,
`DOTNET`, `ENTERPRISE_HR_PAYROLL_INTEGRATION`.

Each lane carries `role_families`, `positive_titles`, `technology_terms`,
`negative_terms`, `transferable_evidence`, and `source_query_hints`. Source-
specific URL syntax lives in adapters, never in policy.

## Geography

`config/policy/geography.yaml` — India-focused, with normalized aliases and
three groups:

- **Primary:** Bengaluru/Bangalore, Hyderabad.
- **Secondary:** Remote India, Pune, Chennai, Noida, Gurugram/Gurgaon, Mumbai.
- **Expansion:** Kochi, Kolkata, Ahmedabad, Coimbatore, Indore, Jaipur,
  Chandigarh, Mysuru.

**"Remote" alone is not worldwide eligibility.** International eligibility
requires explicit wording (e.g. "remote India", visa sponsorship, relocation);
country-scoped remote (US/EU/UK-only) is treated as not eligible from India
(`atlas/policy/rules.py::international_eligibility`).

## Experience

`config/policy/experience.yaml` — preferred ranges around `1-3 / 2 / 2+`. A hard
mandatory minimum at/above **4 years** normally rejects; a suitable three-year
role may survive when overall evidence is strong. **Title never decides
eligibility** — min/max/preferred/ambiguous are extracted separately and a range
is never invented from a title (`extract_experience`, `experience_eligible`).

## Exclusions

`config/policy/exclusions.yaml` — deterministic negative families:
manual-testing-only, support-only, BPO/voice, sales, training-to-placement,
functional-consulting-only, and unrelated non-development. A genuine development
role is **not** over-filtered just because it mentions production support,
on-call, customer collaboration, or debugging.

## Company seed (not a whitelist)

`config/policy/company_seed.yaml` — exactly **108 Tier A** seed companies. The
seed is **not a whitelist or cap** (`is_whitelist: false`, enforced at load):
portal/ATS discovery adds companies and outcome signals promote them. A company
is never removed for a single empty cycle.

## Cadence

`config/policy/cadence.yaml` — recheck intervals (days) by tier:

| Tier | Delta | Deep |
|---|---|---|
| A | 1 | 2 |
| B | 2 | 3 |
| C | 2 | 5 |

The `25–40` deep-check figure is a **batch-size hint**, not a completion cap.
`Completed` is a historical cycle fact, never a permanent state.

## Source policy

`config/policy/source_policy.yaml` preserves the broad source universe **as
policy** — many families across `OFFICIAL / ATS / PORTAL / SPECIALIST /
AGGREGATOR / FALLBACK`. Phase 1B ships **no live adapters**: every entry sets
`live_adapter: false`, and the loader rejects any entry that declares a live
adapter. See [STATUS_MODEL.md](STATUS_MODEL.md) for verification and
lifecycle rules.
