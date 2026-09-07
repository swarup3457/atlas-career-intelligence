# ATS Fingerprinting (Phase 1A.5)

Deterministic, network-free recognition of which ATS family a careers URL /
redirect / safe HTML-marker set belongs to, plus tenant extraction. No LLM
judgment, no fetching.

## Fingerprint (`atlas.sources.fingerprint`)

`fingerprint_ats(url, redirect_url=…, markers=…)` returns an `ATSFingerprint`
with `source_type` (or `None`) and a numeric confidence. Matching precedence:

1. **host suffix** (highest, false-positive safe — `host == suffix` or
   `host endswith "." + suffix`), e.g. `*.myworkdayjobs.com`,
   `boards.greenhouse.io`, `jobs.lever.co`, `*.smartrecruiters.com`,
   `*.icims.com`, `taleo.net`/`oraclecloud.com`, `*.successfactors.com`,
   `*.phenompeople.com`, `eightfold.ai`;
2. **URL path** substrings;
3. **HTML/script markers**.

Lookalike hosts (`myworkday.evil.com`, `notgreenhouse-evil.com`) do **not**
match — matching is host-suffix based, never naive substring containment. An
unrecognized URL returns `source_type = None` (never a guess).

## Tenant extraction (`atlas.company.tenant`)

`extract_tenant(source_type, url)` deterministically extracts the stable
per-deployment identifier where it is URL-encoded:

| Family | Source | Tenant |
|---|---|---|
| Workday | `acme.wd1.myworkdayjobs.com/…` | `acme` (host label before `.wdN`) |
| Greenhouse | `boards.greenhouse.io/acme` or `…/embed/job_board?for=acme` | `acme` |
| Lever | `jobs.lever.co/initech` | `initech` |
| SmartRecruiters | `careers.smartrecruiters.com/Hooli` | `hooli` |

Families with no URL-encoded tenant, or a URL lacking one, return `None` —
never guessed.

## Pipeline integration

`run_fingerprint_pipeline` combines fingerprint + tenant extraction into a
`SourceDiscoveryObservation` (with `detected_ats`, `tenant`, categorical
confidence, and the raw fingerprint in `detail`). A careers URL that cannot
be fingerprinted becomes `COMPANY_CAREER` (a careers page we could not
classify) rather than being dropped.

## Doctor

`atlas doctor` validates the fingerprint framework is importable alongside
company-registry integrity (schema, orphan relationships/aliases, duplicate
alias/source identities, invalid domains) — all offline.
