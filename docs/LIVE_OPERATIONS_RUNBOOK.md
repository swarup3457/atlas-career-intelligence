# Atlas Live Operations Runbook (Phase 1E/F)

Read-only job discovery only. Atlas NEVER applies, submits, fills forms, creates
accounts, enters credentials, or bypasses a CAPTCHA/MFA/anti-bot control. Every
generated resume/cover-letter/brief is a LOCAL DRAFT and is never submitted.

All live runs publish into the stable production output contract under
`production_output_root` (default `C:\Atlas\output\production`, git-ignored):

    runs/<RUN_ID>/Atlas_Jobs_<RUN_ID>.xlsx   (8 sheets, reopen-validated)
    runs/<RUN_ID>/run_manifest.json          (sha256 of every file)
    runs/<RUN_ID>/{coverage,source_health,recommendations,portal_leads,verification_summary}.json
    runs/<RUN_ID>/discovered_leads.json      (durable leads for exact resume)
    runs/<RUN_ID>/application_packs/<JOB_KEY>/... (local drafts, versioned)
    latest/Atlas_Jobs_LATEST.xlsx + latest_run.{json,txt}  (atomic, COMPLETE-only)

## Daily production run (durable root LangGraph graph)

Plan (offline, no side effects):

    atlas daily plan --run-id <RUN_ID>

Live run — real read-only LinkedIn discovery + real portal->official follow-up
and linkage (the full Section-13 live output contract), published atomically:

    atlas daily run --live --sources linkedin --official-followup \
        --lane JAVA_BACKEND --location India --recency-days 60 --max-pages 1 \
        --run-id <RUN_ID>

Resume an interrupted run from durable checkpoint + persisted leads (exact,
no re-discovery):

    atlas daily resume --run-id <RUN_ID> --live

Status:

    atlas daily status --run-id <RUN_ID>

## Inspect outputs

    atlas outputs latest            # authoritative latest pointer + workbook path
    atlas outputs list              # all published runs
    atlas outputs show --run-id <RUN_ID>
    atlas outputs open-latest --live

## Portal auth (explicit, human-in-the-loop; Atlas never types credentials)

    atlas portals auth --family linkedin --live   # opens a VISIBLE Chrome to sign in
    atlas portals auth --family naukri  --live

Naukri's public JSON route commonly returns HTTP 406 on this network; Atlas
reports it truthfully as ACCESS_LIMITED (never a fake zero) and the browser
fallback requires an explicit, configured, visible manual sign-in.

## Market discovery (adaptive campaign; ≥2 concurrent read-only sources)

    atlas market run --live --lanes JAVA_BACKEND --portals linkedin,naukri \
        --max-waves 1 --max-browser 6 --max-pages 1 --run-id <RUN_ID>

## Official career coverage (domain-only entry points)

    atlas careers pilot --config <sealed.yaml> --live

## Optional Copilot reasoning controller (disabled by default)

The controller is the official GitHub SDK `github-copilot-sdk` (MIT), optional
and least-privilege (sessions get no tools + a default-DENY permission handler).
Deterministic runs never call a model.

    atlas copilot info              # SDK pin, license, consent state
    atlas copilot canary --live     # SYNTHETIC-data reasoning canary (real model call)

Real candidate PII is sent to a model ONLY with BOTH explicit consent flags:

    atlas daily run --live --allow-private-candidate-to-copilot   # + copilot_account_type acknowledgement

Without both, Atlas uses deterministic matching and refuses to send PII.

## Windows scheduling (dry-run by default; enabling is explicit)

    atlas daily install-task --time 07:30            # DRY-RUN: prints the schtasks command
    atlas daily install-task --time 07:30 --enable   # actually create the scheduled task
    atlas daily disable-task
    atlas daily remove-task

Scheduled runs are headless/background and never open an auth window; expired
auth yields WAITING_FOR_HUMAN with one exact manual command.

## Health

    atlas doctor
