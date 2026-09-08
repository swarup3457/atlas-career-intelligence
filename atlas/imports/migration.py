"""Phase 1B legacy migration matrix (machine-readable, PII-free).

Every file under the private Workspace Agent import package
(``C:\\Atlas-Agent-Import``) is accounted for here with a deterministic
migration decision. The build specification requires that *no imported
file is omitted*; :mod:`tests.test_phase1b_import_matrix` enforces that,
that the two distinct ``06_`` files are never collapsed, and that the
known broken references (missing recruiter policy, stale resume filename,
hardcoded workbook) are explicitly flagged.

This module deliberately holds only NON-SENSITIVE accounting metadata:
relative paths inside the read-only import package, sha256 digests, and
decisions. It contains no candidate PII and none of the imported content.
The populated private candidate evidence lives only in a gitignored local
store (see :mod:`atlas.candidate`).
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class Decision(str, enum.Enum):
    """Allowed migration decisions (build spec section 3)."""

    AUTHORITATIVE = "AUTHORITATIVE"
    KEEP_AS_POLICY = "KEEP_AS_POLICY"
    KEEP_AS_SKILL = "KEEP_AS_SKILL"
    MOVE_TO_PYTHON = "MOVE_TO_PYTHON"
    MOVE_TO_LANGGRAPH = "MOVE_TO_LANGGRAPH"
    SPLIT = "SPLIT"
    REWRITE = "REWRITE"
    REGRESSION_ONLY = "REGRESSION_ONLY"
    LEGACY_IMPORT_ONLY = "LEGACY_IMPORT_ONLY"
    BLOCKED_PENDING_POLICY = "BLOCKED_PENDING_POLICY"
    RETIRE = "RETIRE"


class PrivacyClass(str, enum.Enum):
    """How a file may (or may not) enter the public Git tree."""

    PUBLIC_POLICY = "PUBLIC_POLICY"          # public company/source/search info only
    PRIVATE_PII = "PRIVATE_PII"              # candidate PII — never committed
    PRIVATE_TEMPLATE = "PRIVATE_TEMPLATE"    # local/private report fixture — never committed
    META = "META"                            # delivery/kit metadata (spec, manifest, readme)


@dataclass(frozen=True)
class MigrationRecord:
    relative_path: str
    sha256: str
    size: int
    purpose: str
    authority: str
    conflicts: tuple[str, ...]
    decision: Decision
    new_implementation_home: str
    migration_action: str
    privacy_class: PrivacyClass
    tests_required: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size": self.size,
            "purpose": self.purpose,
            "authority": self.authority,
            "conflicts": list(self.conflicts),
            "decision": self.decision.value,
            "new_implementation_home": self.new_implementation_home,
            "migration_action": self.migration_action,
            "privacy_class": self.privacy_class.value,
            "tests_required": list(self.tests_required),
        }


def _r(*args, **kwargs) -> MigrationRecord:
    return MigrationRecord(*args, **kwargs)


# NOTE: sha256/size values are the recorded pre-flight input manifest of the
# read-only import package. They are non-sensitive integrity digests.
MIGRATION_MATRIX: tuple[MigrationRecord, ...] = (
    # ------------------------------------------------------------------
    # Root specification / policy files (15)
    # ------------------------------------------------------------------
    _r(
        "00_CURRENT_LIVE_AGENT_INSTRUCTIONS.md",
        "97025ADC833460AD00CEEE01C3EB9F919FCF1FFC449F2BB63DB72E512E850D2F", 18709,
        "Current production objective, request modes, six lanes, India geography, "
        "source breadth, dynamic universe, verification and reporting.",
        "CURRENT_OBJECTIVE",
        ("GitHub-first persistence vs local SQLite architecture", "file 00 references a stale/mismatched resume filename (name-bearing filename redacted for privacy)"),
        Decision.SPLIT,
        "config/policy/*.yaml + production graph + thin AGENTS.md pointer",
        "Split into typed policy config, production phase graph, and a short AGENTS.md pointer. "
        "Replace GitHub-first operational language with the local SQLite invariant.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_policy_lanes", "test_phase1b_agents_pointer"),
    ),
    _r(
        "01_AGENT_EDIT_INSTRUCTIONS_V5.md",
        "0BEFB0D5CA93F10E1360AD837177A3916ED3A2D1B7FCFDEF9F1EF601DB889B72", 1823,
        "Obsolete editor/builder map assuming a single active Excel workbook.",
        "OBSOLETE",
        ("Excel-first active-workbook operational model contradicts report-only Excel",),
        Decision.RETIRE,
        "docs/LEGACY_MIGRATION_MATRIX.md (historical note only)",
        "Retire runtime role; retain only useful field semantics as a historical note.",
        PrivacyClass.PUBLIC_POLICY,
        (),
    ),
    _r(
        "02_CORE_OPERATING_POLICY_V5.md",
        "8CC07B30B68B00AB79E2F48F81818CB5C03163BA65399A394D1C4C3521265DED", 5377,
        "Recurring-monitoring mission, safety boundaries, six lanes, evidence classes, "
        "persistence and report goals.",
        "SUPPORTING_POLICY",
        ("GitHub as sole source of truth vs local SQLite operational truth",),
        Decision.KEEP_AS_POLICY,
        "config/policy/ + atlas/policy/ + production run rules",
        "Adapt principles into typed policy; replace GitHub-only operational state with SQLite.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_policy_loads",),
    ),
    _r(
        "03_DAILY_SCHEDULE_PROMPT_V5.md",
        "65841D3FE4D06565C803BE32C07D25688FEE745AF69D5AB69E4FB71ECD2EF75A", 4932,
        "Daily delta/deep flow, source sequence, broad coverage.",
        "SUPPORTING_POLICY",
        ("interleaves search with GitHub writes — superseded by file 13 phase isolation",),
        Decision.MOVE_TO_PYTHON,
        "atlas/planning cadence planner + production graph phases",
        "Move scheduling to Python/LangGraph; keep only concise cadence policy.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_cadence",),
    ),
    _r(
        "04_COMPANY_RECHECK_CADENCE_V5.md",
        "EB7D0D3F514167BCA464728784D1E0919094D4548AFC70E24D1175515E07421D", 2470,
        "Tiering, due selection, deep-sweep ordering, no permanent Completed.",
        "SUPPORTING_POLICY",
        ("25-40 deep checks is a batch hint, not a completion cap",),
        Decision.KEEP_AS_POLICY,
        "config/policy/cadence.yaml + atlas/planning scheduler",
        "Convert to cadence.yaml; 25-40 becomes batch size; Completed is never permanent.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_cadence", "test_phase1b_completed_not_permanent"),
    ),
    _r(
        "05_SOURCE_AND_ATS_REGISTRY_V5.md",
        "AD0346C1B31FE562BFD1EFD1DA9471A52C9A0714D7139091F3133C94E0D1FF07", 2030,
        "ATS/portal source universe and portal-to-official flow.",
        "SUPPORTING_POLICY",
        ("broad SourceType cannot host LinkedIn+Naukri — needs adapter-key registry",),
        Decision.KEEP_AS_POLICY,
        "config/policy/source_policy.yaml + adapter registry (AdapterKey)",
        "Convert to source policy config; register adapters by AdapterKey, not broad category.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_source_policy", "test_source_registry_adapter_key"),
    ),
    _r(
        "06_CANDIDATE_PROFILE_VERIFIED.md",
        "FB9E95A3177E5D96FC8366112D7200061FC6C27356361494563BD0C6C6966742", 3052,
        "Conservative candidate evidence baseline and unresolved facts.",
        "PRIVATE_AUTHORITY",
        ("preserved unresolved candidate conflicts (generic labels; real employer names kept private): current-role title/timeline, prior-internship dates, additional internship, microservices production ownership, C#/.NET scope",),
        Decision.LEGACY_IMPORT_ONLY,
        "atlas/candidate private importer -> gitignored local SQLite/JSON",
        "Import into a private candidate evidence ledger; preserve conflicts; NEVER commit PII.",
        PrivacyClass.PRIVATE_PII,
        ("test_phase1b_candidate_ledger", "test_phase1b_privacy_no_pii"),
    ),
    _r(
        "06_COMPANY_SEARCH_UNIVERSE_V5.md",
        "E5EBF0DE39185BE4C6BE7935D6777DE4F8F78629E739108609A3D7BF77BAFA92", 2449,
        "108-company Tier A seed universe and dynamic-universe rule.",
        "SUPPORTING_POLICY",
        ("seed must not become a whitelist/cap",),
        Decision.KEEP_AS_POLICY,
        "config/policy/company_seed.yaml (versioned, public)",
        "Import exactly 108 Tier A seed companies as public seed; seed is not a whitelist.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_company_seed_108", "test_phase1b_seed_not_whitelist"),
    ),
    _r(
        "07_TRACKER_SCHEMA_V5.md",
        "91F4A0EEEA5601E3A35285C59DEDA9D6A1EC279F68A67BD3222F1375920DF903", 2373,
        "Old workbook operational tracker schema (Applications/Company_Coverage/Search_Queue).",
        "OBSOLETE",
        ("workbook-as-operational-state contradicts report-only Excel + SQLite truth",),
        Decision.RETIRE,
        "field-mapping reference only; SQLite state + report mapper own implementation",
        "Retire the operational workbook model; retain field semantics for the report mapping.",
        PrivacyClass.PUBLIC_POLICY,
        (),
    ),
    _r(
        "08_COMPANY_SCHEDULER_LIVE_VERIFICATION_STEP2.md",
        "93B19890462FD087F8C36094FD46B5F2BD4BADFEB5FAD8590DE045027D549E25", 6232,
        "Fixed ten-company scheduler smoke test, old CSV state, strict apply-path verification.",
        "REGRESSION_REFERENCE",
        ("fixed ten-company scope must never narrow production", "requires active Apply path — superseded by file 12"),
        Decision.REGRESSION_ONLY,
        "regression fixtures/tests (never production policy)",
        "Move the ten-company scope into regression fixtures; production derives its own manifest.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_ten_company_regression_only",),
    ),
    _r(
        "09_APPEND_ONLY_GITHUB_EVENT_STORE_STEP2C.md",
        "E89D7DB3B9A3739248216D2C8E0D0195E8C30D3A1772775B4AD4646896E71E9A", 6898,
        "Legacy baseline CSV + immutable GitHub event architecture and fixed smoke scope.",
        "LEGACY_REFERENCE",
        ("GitHub as operational DB contradicts local SQLite truth",),
        Decision.LEGACY_IMPORT_ONLY,
        "optional sanitized remote-audit exporter (not local runtime state)",
        "Do not port as operational DB; expose optional sanitized run-event export; degrade gracefully.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_remote_audit_optional",),
    ),
    _r(
        "10_STEP3_HIGH_COVERAGE_INDIA_DISCOVERY.md",
        "F5E7741CF8D2A5916E2EDD055EFB1496D0BBEE410DBF939C13504F29FF3477A6", 12221,
        "Broad India discovery, six lanes, dynamic employer growth, source batches.",
        "SEARCH_INTENT",
        ("its old verification labels/persistence terms are superseded",),
        Decision.KEEP_AS_POLICY,
        "config/policy search policy + atlas/planning planner",
        "Keep broad search-first market-coverage intent; supersede old verification/persistence vocabulary.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_policy_lanes", "test_phase1b_geography"),
    ),
    _r(
        "11_LIVE_CHANNEL_AND_EXCEL_OUTPUT_CONTRACT.md",
        "5CE88B9F47C884B1C8D816EEC465E7DAB29E1D5DB06D22B6F7752CC6E31AE7DB", 5725,
        "Eight-sheet user-facing workbook and truthful coverage reporting.",
        "SUPPORTING_POLICY",
        ("Official_Apply_URL/Verification_Status labels differ from file 12 canonical names",),
        Decision.KEEP_AS_POLICY,
        "config/policy/report_mapping.yaml + atlas/reporting mapper",
        "Adapt into report schema mapping; canonical internal fields map to workbook columns.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_report_mapping",),
    ),
    _r(
        "12_STEP3_VERIFICATION_HARDENING.md",
        "75BA255593512F223C5C337601551EC4DD46FB62A9B574E0DACDA9F6201B89AC", 8653,
        "Best verification/freshness/closure policy in the package.",
        "AUTHORITATIVE",
        ("overrides older apply-path verification (files 08/09)",),
        Decision.AUTHORITATIVE,
        "config/policy/verification.yaml + atlas/policy verification rules + status axes",
        "Adopt as the authoritative verification/lifecycle/recommendation rules and tests.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_verification", "test_phase1b_status_axes"),
    ),
    _r(
        "13_PRODUCTION_SEARCH_PHASE_ISOLATION_HOTFIX.md",
        "B241766CF9A78A31EC6810A505529060FF6D4BA5764BB386B0856978D7579848", 8473,
        "Search-first phase isolation and runtime allocation.",
        "AUTHORITATIVE",
        ("supersedes any guidance interleaving search with persistence/Excel",),
        Decision.AUTHORITATIVE,
        "atlas/orchestration production multi-phase graph",
        "Deterministically implement as the explicit multi-phase production graph with phase isolation.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_production_graph_phase_order", "test_phase1b_search_isolation"),
    ),
    # ------------------------------------------------------------------
    # Report + candidate evidence artifacts (3)
    # ------------------------------------------------------------------
    _r(
        "Atlas_Jobs_TEMPLATE.xlsx",
        "D465428E3C9DE7334BF41EF28DA84C77558B95D3E98345FD56289B54F47F9DF8", 25775,
        "Eight-sheet report layout template.",
        "REPORT_TEMPLATE",
        ("must never be operational state",),
        Decision.KEEP_AS_POLICY,
        "config/policy/report_mapping.yaml (schema) + private local fixture",
        "Keep as a private/local report template; encode its sheet/column schema publicly as a mapping.",
        PrivacyClass.PRIVATE_TEMPLATE,
        ("test_phase1b_report_mapping",),
    ),
    _r(
        "candidate_resume.pdf",  # real filename is name-bearing PII — redacted; sha256 identifies it
        "3A76617C3DD88A7A9DB5E4DCF8F26E91153F4F50D5FB659078B8ED93D12B7F6C", 157406,
        "Candidate resume evidence (PII). Real filename is name-bearing and kept private.",
        "PRIVATE_AUTHORITY",
        ("shipped resume filename does not match the resume filename referenced in file 00 (both name-bearing, redacted)",),
        Decision.LEGACY_IMPORT_ONLY,
        "atlas/candidate private importer -> gitignored local store",
        "Local-only candidate evidence source; NEVER commit the PDF. Reference by logical id, not filename.",
        PrivacyClass.PRIVATE_PII,
        ("test_phase1b_privacy_no_pii", "test_phase1b_stale_resume_filename"),
    ),
    _r(
        "Profile.pdf",
        "F8FA4D14AB3B4AB77D8C4F40138F51BA9589F257677C8C192CA8E394C28CB109", 47154,
        "LinkedIn-export profile evidence (PII).",
        "PRIVATE_AUTHORITY",
        (),
        Decision.LEGACY_IMPORT_ONLY,
        "atlas/candidate private importer -> gitignored local store",
        "Local-only candidate evidence source; NEVER commit the PDF.",
        PrivacyClass.PRIVATE_PII,
        ("test_phase1b_privacy_no_pii",),
    ),
    # ------------------------------------------------------------------
    # Skills (10 x SKILL.md + 10 x openai.yaml + 1 reference)
    # ------------------------------------------------------------------
    _r(
        "skills/application-brief-generator/SKILL.md",
        "16777E8167248B0B6A6D0960585CB3C0E8648D133616B099515D1CC63DED92A6", 1319,
        "Decision-ready application brief methodology.",
        "SKILL_METHODOLOGY", (),
        Decision.SPLIT,
        "agents/APPLICATION_BRIEF_WRITER.agent.md + typed renderer in atlas/reporting",
        "Keep reasoning as a skill; move deterministic brief schema/rendering to Python.",
        PrivacyClass.PUBLIC_POLICY, ("test_phase1b_skill_no_workbook",),
    ),
    _r(
        "skills/application-brief-generator/agents/openai.yaml",
        "65EA9C4BDE4D2D2E5F8A19238E0A6576DEB9A55D531BBBA1D56C0FD8EFB11A87", 343,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.KEEP_AS_SKILL, "agents/*.agent.md front matter",
        "Recreate equivalent local metadata after rewrite; do not copy stale default prompts.",
        PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/application-brief-generator/references/APPLICATION_BRIEF_TEMPLATE.md",
        "2DAAB11425BF5622EC973CF03A09DD4E732CDB0723DB9875C51E325D7EE55350", 528,
        "Application-brief presentation template.", "PRESENTATION_TEMPLATE", (),
        Decision.KEEP_AS_SKILL, "skills reference template (populated from typed data)",
        "Retain as presentation template; populate from typed application-brief data.",
        PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/application-response-optimizer/SKILL.md",
        "11F99F6743F1F225AA19477C7291F483333570715E624A2330787E6D18465D2B", 889,
        "Outcome-driven source/company prioritization.", "SKILL_METHODOLOGY", (),
        Decision.SPLIT, "atlas analytics (deterministic) + TREND_ANALYST skill",
        "Deterministic aggregation/weights in Python; interpretation in an LLM skill.",
        PrivacyClass.PUBLIC_POLICY, ("test_phase1b_skill_no_workbook",),
    ),
    _r(
        "skills/application-response-optimizer/agents/openai.yaml",
        "99B81D1816525534218229F24794515ACB1593CAA0E904EFCDC2614CE36A0960", 322,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.KEEP_AS_SKILL, "agents/*.agent.md front matter",
        "Recreate equivalent local metadata after rewrite.", PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/application-tracker-deduper/SKILL.md",
        "335581E711A4B35261E6567FA3C228B1F17B5438C289DD9ADD8B201120EEC475", 1868,
        "Old Atlas_MASTER_ACTIVE_ workbook dedupe/state mechanics.", "OBSOLETE_MECHANICS",
        ("requires an active workbook — contradicts SQLite-owned dedupe/state",),
        Decision.RETIRE,
        "atlas/persistence + data_integrity (Python/SQLite own dedupe)",
        "Retire workbook mechanics; canonicalization/dedupe/state belong to Python/SQLite.",
        PrivacyClass.PUBLIC_POLICY, ("test_phase1b_skill_no_workbook",),
    ),
    _r(
        "skills/application-tracker-deduper/agents/openai.yaml",
        "820760FB9105AF26A085D639C19C616E12A6509863DCD45FDE50E4B61AA76126", 151,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.RETIRE, "n/a", "Retire with its skill.", PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/company-cadence-orchestrator/SKILL.md",
        "C3F7A22AF6EFEE0B16D7EAD4A3A462C09C9335F7DD42EEB16ADA7D15A8C1A654", 3238,
        "Tier/due calculations and queue selection.", "SKILL_METHODOLOGY",
        ("scheduling loops must be code, not prose",),
        Decision.MOVE_TO_PYTHON,
        "atlas/planning scheduler + thin policy skill",
        "Tier/due/queue calculations move to the scheduler; retain concise policy explanation only.",
        PrivacyClass.PUBLIC_POLICY, ("test_phase1b_skill_no_loops",),
    ),
    _r(
        "skills/company-cadence-orchestrator/agents/openai.yaml",
        "078216C0B040DB4166FBE20AFBA7841F9638BAB00C24C3FF279E6658A3DBA234", 240,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.KEEP_AS_SKILL, "agents/*.agent.md front matter",
        "Recreate equivalent local metadata.", PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/company-career-page-sweeper/SKILL.md",
        "4980423EEBA93013D874EAFF8D3E3A85E9D1D18AD6F9EA2CF708BC5555A2D080", 793,
        "Career-page/ATS search execution across lanes/locations.", "SKILL_METHODOLOGY",
        ("about 60 domains is a batch hint, not completion",),
        Decision.MOVE_TO_LANGGRAPH,
        "atlas/planning planner + source adapters (Phase 1C)",
        "Execution/coverage owned by planner/adapters; keep only thin methodology; 60 is a batch hint.",
        PrivacyClass.PUBLIC_POLICY, ("test_phase1b_skill_no_loops",),
    ),
    _r(
        "skills/company-career-page-sweeper/agents/openai.yaml",
        "55283B7933FC8FB316E2C6B4E97FEEA88AD6D1A740595F53E4D902E22BFDCD5D", 363,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.KEEP_AS_SKILL, "agents/*.agent.md front matter",
        "Recreate equivalent local metadata.", PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/international-eligibility-check/SKILL.md",
        "4E1163D429786CB804317BD787975ADA7251C14C38E69D77E78172594EDE3F9F", 1305,
        "Country/sponsorship/remote eligibility rules.", "SKILL_METHODOLOGY", (),
        Decision.SPLIT,
        "atlas/policy eligibility rules + INTERNATIONAL_ELIGIBILITY_REVIEWER skill",
        "Deterministic country/sponsorship rules first; LLM only for ambiguous language.",
        PrivacyClass.PUBLIC_POLICY, ("test_phase1b_geography_remote_not_worldwide",),
    ),
    _r(
        "skills/international-eligibility-check/agents/openai.yaml",
        "C877C63774D34455958D1662914B647D6B2FC0BE95A66AD4F9EB1D87235A9EDE", 363,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.KEEP_AS_SKILL, "agents/*.agent.md front matter",
        "Recreate equivalent local metadata.", PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/job-discovery-verification/SKILL.md",
        "741497A2D629A9590D3B3110F492A23B9E2A041890709DE5967EA135C0B42A5F", 1001,
        "Official-first multi-source discovery + verification methodology.", "SKILL_METHODOLOGY", (),
        Decision.SPLIT,
        "adapters/planner (execution) + VERIFICATION_REVIEWER skill",
        "Source execution in adapters/governor; verification methodology in a reviewer skill.",
        PrivacyClass.PUBLIC_POLICY, ("test_phase1b_skill_no_loops",),
    ),
    _r(
        "skills/job-discovery-verification/agents/openai.yaml",
        "5B416BF3EB3EF78F17075FECF0240E2F31A74312BEF018B4F23DCF1A34D03F59", 347,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.KEEP_AS_SKILL, "agents/*.agent.md front matter",
        "Recreate equivalent local metadata.", PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/job-market-trend-analyzer/SKILL.md",
        "CC4B857FC4600BE9D4FEDBD785A4D1E75508DA347EF02AFA915B09D0F1E346B8", 1410,
        "Market trend analytics across lanes/sources.", "SKILL_METHODOLOGY",
        ("hardcodes Atlas_MASTER_ACTIVE_20260808.xlsx",),
        Decision.REWRITE,
        "atlas analytics (deterministic aggregates) + TREND_ANALYST skill",
        "Remove hardcoded workbook; deterministic analytics in Python, narrative insights in LLM.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_skill_no_workbook", "test_phase1b_hardcoded_workbook_rejected"),
    ),
    _r(
        "skills/job-market-trend-analyzer/agents/openai.yaml",
        "5BFD0E4867707753507FD4C2C9EA7C889FE49E1AD94DD3BC9D4678EED0003BD3", 428,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.KEEP_AS_SKILL, "agents/*.agent.md front matter",
        "Recreate equivalent local metadata.", PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/recruiter-outreach-prep/SKILL.md",
        "27C676EE881C4F260D3B23151BD658E58526D22C147ADCBF5A8B85353C474A71", 630,
        "Public recruiter-contact research methodology.", "SKILL_METHODOLOGY",
        ("references missing 05_PUBLIC_RECRUITER_CONTACT_POLICY.md",),
        Decision.BLOCKED_PENDING_POLICY,
        "skills/recruiter-outreach-prep (blocked stub) pending approved policy",
        "Keep blocked until an authoritative recruiter contact policy is approved.",
        PrivacyClass.PUBLIC_POLICY,
        ("test_phase1b_recruiter_blocked", "test_phase1b_missing_recruiter_policy"),
    ),
    _r(
        "skills/recruiter-outreach-prep/agents/openai.yaml",
        "8FB6FCDB1B6F6299518C39D7966622A34EF94AA6FDAE497F35681FD08DDA4D6B", 343,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.BLOCKED_PENDING_POLICY, "n/a",
        "Blocked with its skill.", PrivacyClass.PUBLIC_POLICY, (),
    ),
    _r(
        "skills/resume-job-matcher/SKILL.md",
        "1551018B016B92261E2B2A86B145ACE2AB9C61A38A4E75D30F210D1C7769F879", 2729,
        "Evidence-based match scoring and truthful resume tailoring.", "SKILL_METHODOLOGY", (),
        Decision.SPLIT,
        "atlas/policy matching (deterministic gates) + MATCH_ANALYST skill",
        "Evidence extraction/scoring constraints in Python; semantic fit/tailoring in typed LLM ops.",
        PrivacyClass.PUBLIC_POLICY, ("test_phase1b_matching_gates",),
    ),
    _r(
        "skills/resume-job-matcher/agents/openai.yaml",
        "2FE34AB382A0957681F36C687ACE74574164C2A812F0D649E04F1D0A4B11EBAE", 452,
        "Skill interface/display metadata.", "SKILL_METADATA", (),
        Decision.KEEP_AS_SKILL, "agents/*.agent.md front matter",
        "Recreate equivalent local metadata.", PrivacyClass.PUBLIC_POLICY, (),
    ),
    # ------------------------------------------------------------------
    # Kit / delivery metadata (7)
    # ------------------------------------------------------------------
    _r(
        "Atlas_Agent_Import_Manifest.sha256",
        "645DEE1FEC89090636EC6FFDA2A2E5B99232D24A1238C4167F7BA324BB33DE0A", 5566,
        "SHA-256 manifest of the delivered import package.", "META", (),
        Decision.LEGACY_IMPORT_ONLY, "docs/migration/input_manifest.json (recomputed)",
        "Recorded as the pre-flight integrity reference.", PrivacyClass.META, (),
    ),
    _r(
        "Atlas_Phase1B_Copilot_Build_Prompt_20260908.txt",
        "C9282D3234156E19E9BF92B86616FA5E0E9724105DA45B739853477AF6BAE29B", 41868,
        "The Phase 1B build specification prompt.", "SPECIFICATION", (),
        Decision.LEGACY_IMPORT_ONLY, "this Phase 1B implementation",
        "Executed as the Phase 1B build specification.", PrivacyClass.META, (),
    ),
    _r(
        "Atlas_Phase1B_Deep_Audit_20260908.md",
        "57A5C3137EB532FC67578D0F89499DE081D28492E3559B8F13D8FEAE2139BD79", 28783,
        "Independent deep audit of the package and current code.", "AUTHORITATIVE_AUDIT", (),
        Decision.AUTHORITATIVE, "docs/PHASE1B_WORKSPACE_IMPORT_AUDIT.md (adopted decisions)",
        "Adopted as the precedence/decision reference for this migration.", PrivacyClass.META, (),
    ),
    _r(
        "README_Atlas_Phase1B_Kit.txt",
        "28BD1F279960D2F95AC8FE0A14CE75267416CC46A13E3B04D3EAAEEB4C2271DC", 1045,
        "Kit readme.", "META", (),
        Decision.LEGACY_IMPORT_ONLY, "n/a", "Delivery metadata only.", PrivacyClass.META, (),
    ),
    _r(
        "Start_Atlas_Phase1B_Copilot.ps1",
        "EA019054B922949077C716B7914D492A998E1AF98EF853571648C29F8A548418", 1846,
        "Kit launcher script.", "META", (),
        Decision.LEGACY_IMPORT_ONLY, "n/a", "Delivery metadata only.", PrivacyClass.META, (),
    ),
)


def matrix_as_dicts() -> list[dict]:
    return [rec.to_dict() for rec in MIGRATION_MATRIX]


def record_for(relative_path: str) -> Optional[MigrationRecord]:
    key = relative_path.replace("\\", "/")
    for rec in MIGRATION_MATRIX:
        if rec.relative_path.replace("\\", "/") == key:
            return rec
    return None


def export_json(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "phase": "1B",
        "file_count": len(MIGRATION_MATRIX),
        "records": matrix_as_dicts(),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


__all__ = [
    "Decision",
    "PrivacyClass",
    "MigrationRecord",
    "MIGRATION_MATRIX",
    "matrix_as_dicts",
    "record_for",
    "export_json",
]
