# External Research Decisions (Phase 1C-A)

Official documentation wins over third-party code. Atlas is the only writable
product repo; small concepts are **reimplemented** in Atlas style, never copied.
No external dataset, scraper service, proxy stack, or second orchestrator was
imported.

## Authoritative primary sources (official API contracts)

| Source | Used for |
|---|---|
| developers.greenhouse.io/job-board.html | Greenhouse endpoints, `updated_at` vs single-job `first_published`, no pagination, prospect filter |
| github.com/lever/postings-api | Lever `?mode=json` array, `skip`/`limit`, top-level `workplaceType`, EU base |
| developers.ashbyhq.com/docs/public-job-posting-api | Ashby list-only endpoint, `publishedAt`, `isListed`, no documented `id` |
| Public Workday careers/CXS behavior (observed) | CXS search POST + detail GET, `limit≤20`, relative `postedOn`, no-bypass |

## Reference repository decisions

| Repo | License | Decision | Rationale |
|---|---|---|---|
| MadsLorentzen/ai-job-search | MIT | IDEAS_ONLY / REIMPLEMENT | small per-source workers, search/detail split, typed output, self-description, untrusted job text, continuation on source failure — concepts realized in `ats/*`, `child_executor`, contract harness |
| Feashliaa/job-board-aggregator | MIT | REIMPLEMENT (concepts) | bounded per-platform concurrency, Workday silent-truncation detect, dead-slug idea. **Rejected:** rotating user-agents (UA spoofing), workers=50 |
| coryking/jobsearch-buddy | GPL-2.0 | IDEAS_ONLY (no code reuse) | list/detail separation, ATS URL normalization, `descriptions_in_listing` flag, registry. GPL copyleft → no copy/port |
| jonahr4/InternAtlas | NONE (all rights reserved) | IDEAS_ONLY facts only; REJECT code | best public Workday CXS behavior notes. **Rejected:** no license → no code reuse; Google-scraping reverse discovery; vendored JobSpy |
| speedyapply/JobSpy | MIT | IDEAS_ONLY, NOT a dependency | portal-track record schema idea only. **Rejected:** proxy rotation to bypass blocking; portal-ToS scraping |

## Explicitly excluded (per spec)

interviewstreet/hiring-agent, wellfound_autoApply, auto-application/messaging
repos, external company datasets, a second orchestration framework, Apify / paid
scraper runtimes, proxies/IP rotation, JobSpy as a dependency, Bun/Node for these
adapters, PostgreSQL stacks.

## Rejected patterns (never adopted)

Proxy/IP rotation, stealth / user-agent rotation, CAPTCHA/MFA/anti-bot/Cloudflare
/Zscaler bypass, login/account creation, application submission, fetching links
found inside job text, treating job text as instructions, Google-scraping reverse
discovery, wholesale copying of external repositories or datasets.

See `02_RESEARCH_AND_LICENSE_MATRIX.md` in the phase evidence pack for the full
per-source matrix (commits, useful vs rejected patterns, destinations).

## Phase 1E/F additions

### GitHub Copilot Python SDK

| Concern | Decision |
|---|---|
| Package identity | The reasoning controller imports an official SDK LAZILY via `OFFICIAL_SDK_PACKAGE` in `atlas/controllers/copilot.py`. The generic PyPI names `copilot` (0.1.9) and `copilot-sdk` are NOT GitHub's official SDK and are not used. |
| Integration model | Transport seam (`CopilotSdkTransport`): the controller never imports the SDK directly. The official transport is the ONLY place the SDK is loaded, on first real use. Deterministic runs and the entire offline suite work with `controller='none'` and injected fakes — no SDK required. |
| Enablement | OPTIONAL, disabled by default. Live Copilot reasoning requires the operator to install + pin a verified SDK version and authenticate; until a verified version is pinned, the official transport quarantines (callers fall back deterministically). This is recorded truthfully rather than fabricating an install. |
| Least privilege | Default-DENY permission handler; shell/edit/git/network always denied; agents get only a subset of typed local read-only tools. |
| Privacy | Real candidate PII is gated behind BOTH an explicit CLI flag and an account-type acknowledgement. Usage logs store only non-PII metrics. |

### ai-job-search refresh (MIT)

Refreshed to HEAD `ccf786b`. Concepts reviewed and REIMPLEMENTED Atlas-native (no
code copied): `/rank` two-tier triage (query-scoped candidate selection, ~5-job
reasoning batches, preserved deferred backlog), deterministic path-safe job key,
drafter→grounding→validate application flow, and untrusted-posting policy. Atlas
does not depend on Claude Code, Bun, its state files, or its LaTeX templates.

### wellfound_autoApply

Remains EXPLICITLY EXCLUDED as a dependency (no clear repository-level license).
Ideas-only: dedicated persistent Chrome profile, one-time visible manual sign-in,
headless normal browsing, bounded infinite scroll, read-only card/detail
extraction. Its auto-apply / form-fill / Q&A / stealth code is never adopted. A
read-only Wellfound discovery adapter is gated on an independent license
verification; absent that, it stays deferred by evidence.
