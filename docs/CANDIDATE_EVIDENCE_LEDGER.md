# Candidate Evidence Ledger

Status: **implemented (Phase 1B).** The candidate evidence ledger
(`atlas/candidate/`) records *what the evidence actually supports* about a
candidate's skills and history, preserving conflicts instead of guessing.
**Populated evidence is private** and never enters the public tree — see
[PHASE1B_PRIVACY.md](PHASE1B_PRIVACY.md).

## Evidence classes

Every claim carries exactly one evidence class
(`atlas/candidate/models.py::EvidenceClass`):

| Class | Meaning |
|---|---|
| `PROFESSIONAL` | Demonstrated in paid employment |
| `PROJECT_PRODUCT` | Demonstrated in a real project/product |
| `CANDIDATE_CONFIRMED` | Candidate-stated knowledge, not proven ownership |
| `SKILLS_LIST_ONLY` | Appears only in a skills list |
| `UNRESOLVED_CONFLICT` | Sources disagree; preserved, not resolved |
| `UNSUPPORTED` | Asserted elsewhere but not established by evidence |

## Claim schema

`CandidateClaim` fields: `claim_id`, `topic`, `normalized_value`, `scope`,
`evidence_class`, `source_document_id` (a **logical id, never a filename**),
`source_date`, `confidence`, `conflict_state`, and `notes`. Claims serialize to
and from JSON for the private store.

## Invariants

- **Never promote a weaker class into a stronger one.** A `SKILLS_LIST_ONLY`
  entry (e.g. C# listed in a skills section) is **not** `PROFESSIONAL` C#;
  `CANDIDATE_CONFIRMED` microservices *knowledge* is **not** `PROFESSIONAL`
  production *ownership*. `assert_no_illegal_promotion` raises `IllegalPromotion`
  on any upgrade the evidence does not support.
- **Conflicts are preserved, not guessed.** When two sources disagree on a
  `(topic, scope)` and no explicit resolution exists, the resolved view surfaces
  an `UNRESOLVED_CONFLICT` rather than picking a value.
- **History is append-only.** An explicit, candidate-confirmed resolution
  *appends* a resolving claim (`CandidateLedger.resolve`) — it never edits or
  deletes historical claims.

## Privacy model

- Populated evidence lives **only** in gitignored `config/private/*.private.json`.
- The public tree contains the **schema** plus a **synthetic fixture**
  (`fixtures/candidate/synthetic_claims.json`) — synthetic topics only, no PII.
- The importer (`atlas/candidate/importer.py`) reads private Workspace sources
  and writes the populated ledger **only** to gitignored paths (defaulting to
  `config/private/`). It records the source SHA-256 for provenance and stores
  technology/evidence classifications, never identity PII.

## Tests

`tests/test_phase1b_candidate.py` verifies the evidence classes stay distinct,
conflicts remain unresolved, a weaker class cannot become professional, explicit
resolution does not edit history, and the synthetic fixture contains no PII.
`tests/test_phase1b_import_matrix.py` verifies no PDF or private candidate file
is ever tracked in Git.
