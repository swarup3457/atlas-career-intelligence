# Phase 1B — Privacy & Public-Repository Gate

Status: **implemented (Phase 1B).** This repository is treated as **potentially
public**. Everything candidate-identifying stays local; only schemas,
validators, synthetic fixtures, and public policy are committed.

## Never commit

- Candidate email, phone, or personal identifiers.
- Real resume or `Profile.pdf` (or any candidate PDF).
- Raw candidate evidence rows / a populated candidate ledger.
- Any content of `C:\Atlas-Agent-Import`.
- Real job/application history containing private data.
- Browser profile / session data.
- Local SQLite operational state.
- Secrets, tokens, or credentials.

## What the public tree *may* contain

- Schemas and validators.
- Synthetic fixtures (e.g. `fixtures/candidate/synthetic_claims.json`).
- Public policy definitions (`config/policy/*.yaml`).
- Sanitized examples and the PII-free migration matrix.

Real candidate evidence goes only to local SQLite or a gitignored private file
(`config/private/*.private.json`). The importer
(`atlas/candidate/importer.py`) writes populated evidence **only** to gitignored
paths.

## `.gitignore` protections

The repository `.gitignore` blocks the private surface:

```
config/private/
private/
imports/private/
candidate_data/
*.private.yaml
*.private.json
*.pdf
```

These are in addition to the existing exclusions for local state, output, logs,
and browser-profile directories (`state/`, `output/`, `logs/`, `*.sqlite*`,
`.browser-profile*/`).

## Remote audit is sanitized

The optional remote audit (`atlas/persistence/remote_audit.py`) sanitizes every
event before export, dropping PII-ish keys (email, phone, resume, profile,
candidate, name, token, secret, …). GitHub availability is never required for a
run, and no candidate-private data leaves the local machine.

## Enforcement

- `tests/test_phase1b_import_matrix.py` — asserts no PDFs or private candidate
  files are tracked, that all required `.gitignore` protections are present, and
  that the candidate PII sources are classed `PRIVATE_PII` / local-only.
- `tests/test_phase1b_candidate.py` — asserts the synthetic fixture contains no
  PII and that the importer writes only to a gitignored `*.private.json` path.

See [CANDIDATE_EVIDENCE_LEDGER.md](CANDIDATE_EVIDENCE_LEDGER.md) for the private
evidence model and [PHASE1B_WORKSPACE_IMPORT_AUDIT.md](PHASE1B_WORKSPACE_IMPORT_AUDIT.md)
for the overall import decision.
