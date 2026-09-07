# Company Registry (Phase 1A.5)

The company registry is the **persistent, run-independent** store of company
IDENTITY and company↔source relationships. It contains no candidate
preference, company tiers, relevance, cadence, or ranking — those arrive with
the Workspace Atlas Agent import.

## Distinct concepts (do not conflate)

| Concept | What it answers | Where |
|---|---|---|
| **COMPANY** | Which real-world employer is this? | `company_registry` / `atlas.company.models.Company` |
| **SOURCE TYPE** | Which *family* (ATS_WORKDAY, PORTAL_LARGE, …)? | `atlas.sources.models.SourceType` |
| **SOURCE INSTANCE** | Which configured deployment (Acme's Workday tenant)? | `SourceInstance` (`instance_id`, `tenant`, `base_url`) |
| **COMPANY↔SOURCE RELATIONSHIP** | How can we reach this company's jobs, and in what lifecycle state? | `company_source_relationships` / `RelationshipState` |
| **DISCOVERY OBSERVATION** | How/when was a source discovered (provenance)? | `source_discovery_observations` / `SourceDiscoveryObservation` |
| **SOURCE HEALTH** | Can we currently *trust* a source's output? | `atlas.sources.health.SourceHealthState` |
| **TASK STATUS** | What happened to a processed task? | `atlas.models.TaskStatus` |

These are separate state machines. `RelationshipState` (DISCOVERED → VERIFIED
→ DEGRADED → REPLACED → INACTIVE) is **not** `SourceHealthState` and **not**
`TaskStatus`.

## Identity & merge safety

- `identity_key` = `company_identity_key(name)` — an aggressive match token
  that strips a small deterministic set of legal suffixes (Inc, Corp, Ltd,
  "& Co", …) so "Acme" and "Acme Inc." match. There is **no** large
  hard-coded alias database; abbreviation/expansion pairs (IBM ↔
  International Business Machines) stay distinct until an alias is registered.
- `company_id` is derived deterministically, preferring the **official
  domain** (so a display-name change never creates a new company); otherwise
  the identity key.
- **Resolution order:** official domain → explicit alias → canonical identity
  key. Merge rules:
  - same normalized name **and** same domain → same company;
  - explicit alias → same company;
  - similar name but **different** official domains → **not merged**
    (subsidiary/parent, lookalikes);
  - genuinely ambiguous (same name, no domain, multiple candidates) →
    **unresolved**, never guessed.

## Persistence (SQLite migration v5)

`company_registry`, `company_aliases` (unique per `(company_id, alias_key)`),
`company_source_relationships` (unique per `(company_id, instance_id)`,
many-to-many), and append-only `source_discovery_observations`. The migration
is transactional, idempotent, preserves v1–v4, and survives reopen +
backup/restore (verified by tests).

## Operations (`CompanyRegistry`)

`resolve`, `register_company`, `find_company`, `get_company`,
`list_companies`, `add_alias`, `set_official_domain`, `update_verification`,
`attach_source_instance`, `mark_relationship_state`, `record_observation`.
All are deterministic and idempotent; rediscovering the same tenant never
creates a second SourceInstance.

## CLI

`atlas companies` (list) and `atlas company show <id>` (identity, aliases,
sources, discovery history) — read-only diagnostics.

## What waits for the Workspace import

Company universe, tiers, candidate preference/matching, cadence, ranking,
final verification vocabulary, and the Excel business schema. None of these
are defined here.
