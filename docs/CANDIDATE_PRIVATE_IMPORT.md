# Candidate Private Import — Phase 1B.1 (REDACTED)

Canonical code: `atlas/candidate/`, `docs/CANDIDATE_EVIDENCE_LEDGER.md`.
This file is a **redacted** audit record. It contains only generic claim
**categories** and document **hashes** — never personal names, contact details,
timelines, employer names, or any private text.

## Privacy handling (non-negotiable)

- Real resume / profile / verified-profile content is **never** copied into the
  public Git tree.
- Direct extraction for this review was written **only** to the gitignored path
  `private/phase1b1/` (covered by `.gitignore` `private/`), confirmed with
  `git check-ignore`.
- Production runs load candidate evidence from a **gitignored** private snapshot
  and record only its **hash** in checkpoints/manifests
  (`ProductionSearchRuntime`, `fixture_mode=False`). Synthetic evidence is used
  **only** in explicit fixture mode and is reported as
  `synthetic_candidate_evidence: true`.

## Mandatory direct review (build spec §3 / P0-20)

Each file below was reviewed **directly and read-only** in Phase 1B.1 by
extracting text locally with `pypdf`/plain read into `private/phase1b1/`
(gitignored). The prior run relied on a Markdown representation; this run reads
the source documents themselves.

| Document | Directly reviewed | Method | SHA-256 |
|---|---|---|---|
| Resume PDF | YES | `pypdf` text extract → gitignored | `3a76617c3dd88a7a9db5e4dcf8f26e91153f4f50d5fb659078b8ed93d12b7f6c` |
| Profile PDF | YES | `pypdf` text extract → gitignored | `f8fa4d14ab3b4ab77d8c4f40138f51ba9589f257677c8c192ca8e394c28cb109` |
| Verified profile Markdown | YES | plain read → gitignored | `fb9e95a3177e5d96fc8366112d7200061fc6c27356361494563bd0c6c6966742` |

Private candidate data committed to the public tree: **NO**.

## Generic category presence (no PII)

Presence flags only — no values, dates, names, or free text.

| Category | Resume PDF | Profile PDF | Verified MD |
|---|---|---|---|
| Java / Spring backend | ✔ | ✔ | ✔ |
| React / frontend (JS/TS) | ✔ | ✔ | ✔ |
| .NET / C# | ✔ | ✔ | ✔ |
| Microservices | ✔ | ✔ | ✔ |
| Enterprise payroll / HR | ✔ | — | ✔ |
| Internship | ✔ | ✔ | ✔ |
| Experience/employment section | ✔ | ✔ | ✔ |
| Projects section | — | ✔ | ✔ |
| Skills section | ✔ | ✔ | ✔ |
| Education section | ✔ | ✔ | — |

## Preserved conflicts (conservative, generic)

Consistent with the synthetic ledger structure
(`atlas/candidate/importer.build_synthetic_ledger`). These remain **UNRESOLVED**
and are never guessed away:

- **current-role title / timeline** — representation differs between documents.
- **project-vs-professional provenance** — a "projects" emphasis appears in the
  profile/MD but not identically in the resume; project-product evidence must
  not silently become professional provenance.
- **microservices production ownership** — candidate-confirmed knowledge vs. no
  documentary production-ownership evidence.
- **additional / prior internship period** — wording and dates differ across
  documents.
- **`.NET` / C# depth** — appears as a listed/skills-level signal, not
  professional provenance; must not upgrade.

Resolution policy: candidate confirmation may resolve a **value** conflict but
cannot by itself establish **PROFESSIONAL** provenance (enforced by
`CandidateLedger.resolve` + the explicit evidence-transition matrix in
`atlas/candidate/models.py`).
