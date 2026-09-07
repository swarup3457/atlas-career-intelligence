"""Configurable mapping: sheet aliases, column aliases, field specs.

This layer is what keeps the whole subsystem *generic*. No downstream code
knows that ``All_Jobs`` is a sheet or that ``Match_Score`` is column 16 —
they ask the :class:`MappingConfig` to resolve a sheet name to an entity
type and a raw header to a canonical field. Aliases are matched on a
normalized key (case/whitespace/punctuation-insensitive) so real-world
header drift (``Company`` vs ``Company Name`` vs ``company_name``) resolves
cleanly.

The default Atlas config (:func:`default_mapping`) is built from the known
Phase 0.9 workbook schema, but the classes accept a plain ``dict`` (or
YAML), so a different schema is a config change, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.data_integrity.normalizers import header_key


@dataclass(frozen=True)
class FieldSpec:
    """Specification for one canonical field."""

    canonical: str
    aliases: tuple[str, ...] = ()
    normalizer: str = "text"
    required: bool = False
    domain: Optional[tuple[str, ...]] = None  # allowed normalized values
    dtype: str = "text"  # text | int | score | url | date | bool
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    def alias_keys(self) -> set[str]:
        keys = {header_key(self.canonical)}
        keys.update(header_key(a) for a in self.aliases)
        return keys


@dataclass(frozen=True)
class EntitySchema:
    """A set of fields + identity fields, keyed by one or more sheet names."""

    entity_type: str
    sheet_aliases: tuple[str, ...]
    fields: tuple[FieldSpec, ...]
    identity_fields: tuple[str, ...]
    identity_fallback: tuple[str, ...] = ()

    def sheet_keys(self) -> set[str]:
        return {header_key(s) for s in self.sheet_aliases}

    def field_index(self) -> dict[str, str]:
        """Map alias-key -> canonical field name."""
        index: dict[str, str] = {}
        for spec in self.fields:
            for key in spec.alias_keys():
                index.setdefault(key, spec.canonical)
        return index

    def spec_for(self, canonical: str) -> Optional[FieldSpec]:
        for spec in self.fields:
            if spec.canonical == canonical:
                return spec
        return None


class MappingConfig:
    """Resolves sheets -> entities and headers -> canonical fields."""

    def __init__(self, schemas: list[EntitySchema], version: str = "1"):
        self.version = version
        self.schemas = schemas
        self._by_sheet_key: dict[str, EntitySchema] = {}
        for schema in schemas:
            for key in schema.sheet_keys():
                self._by_sheet_key[key] = schema
        self._by_entity: dict[str, EntitySchema] = {s.entity_type: s for s in schemas}

    # ------------------------------------------------------------------
    def resolve_sheet(self, sheet_name: str) -> Optional[EntitySchema]:
        return self._by_sheet_key.get(header_key(sheet_name))

    def schema_for(self, entity_type: str) -> Optional[EntitySchema]:
        return self._by_entity.get(entity_type)

    def resolve_column(self, schema: EntitySchema, header: str) -> Optional[str]:
        """Return the canonical field for a raw header, or None if unknown."""
        return schema.field_index().get(header_key(header))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MappingConfig":
        """Build from a plain dict (e.g. loaded from YAML).

        Shape::

            {"version": "1", "entities": [
                {"entity_type": "job", "sheets": ["All_Jobs"],
                 "identity": ["company", "job_id"],
                 "identity_fallback": ["company", "role", "location"],
                 "fields": [
                    {"canonical": "company", "aliases": ["Company"],
                     "normalizer": "company", "required": true}, ...]}]}
        """
        schemas: list[EntitySchema] = []
        for ent in data.get("entities", []):
            specs = tuple(
                FieldSpec(
                    canonical=f["canonical"],
                    aliases=tuple(f.get("aliases", ())),
                    normalizer=f.get("normalizer", "text"),
                    required=bool(f.get("required", False)),
                    domain=tuple(f["domain"]) if f.get("domain") else None,
                    dtype=f.get("dtype", "text"),
                    min_value=f.get("min_value"),
                    max_value=f.get("max_value"),
                )
                for f in ent.get("fields", [])
            )
            schemas.append(
                EntitySchema(
                    entity_type=ent["entity_type"],
                    sheet_aliases=tuple(ent.get("sheets", ())),
                    fields=specs,
                    identity_fields=tuple(ent.get("identity", ())),
                    identity_fallback=tuple(ent.get("identity_fallback", ())),
                )
            )
        return cls(schemas, version=str(data.get("version", "1")))


# ---------------------------------------------------------------------------
# Domains (kept lenient — a domain violation FLAGS, it does not by itself
# quarantine, because real exports legitimately grow their vocab over time).
# ---------------------------------------------------------------------------
_VERIFICATION_DOMAIN = (
    "Verified Official",
    "Portal Only",
    "Manual Verification",
    "Unverified",
    "Closed",
)
_LIVE_DOMAIN = ("Active", "Closed", "Expired", "Filled", "On Hold", "Paused", "Unknown")
_RESULT_DOMAIN = ("New Jobs", "Known Unchanged", "Closed", "No Change", "Access Limited", "Manual")


def default_mapping() -> MappingConfig:
    """The known Atlas Phase 0.9 workbook schema, expressed as config."""
    job_fields = (
        FieldSpec("company", ("Company", "Company_Name"), "company", required=True),
        FieldSpec("role", ("Role", "Title", "Job_Title"), "text"),
        FieldSpec("job_id", ("Job_ID", "JobId", "Requisition_ID", "Req_ID"), "text"),
        FieldSpec("location", ("Location", "India_Locations"), "text"),
        FieldSpec("work_mode", ("Work_Mode",), "text"),
        FieldSpec("experience", ("Experience",), "text"),
        FieldSpec("search_lane", ("Search_Lane",), "text"),
        FieldSpec("company_type", ("Company_Type",), "text"),
        FieldSpec("discovery_source", ("Discovery_Source", "Source"), "text"),
        FieldSpec("source_url", ("Source_URL",), "url", dtype="url"),
        FieldSpec("official_apply_url", ("Official_Apply_URL", "Apply_URL"), "url", dtype="url"),
        FieldSpec("posted_date", ("Posted_Date",), "date", dtype="date"),
        FieldSpec("freshness", ("Freshness",), "text"),
        FieldSpec(
            "verification_status",
            ("Verification_Status",),
            "text",
            domain=_VERIFICATION_DOMAIN,
        ),
        FieldSpec("live_status", ("Live_Status",), "text", domain=_LIVE_DOMAIN),
        FieldSpec(
            "match_score",
            ("Match_Score", "Score"),
            "score",
            dtype="score",
            min_value=0,
            max_value=100,
        ),
        FieldSpec("requirements_matched", ("Requirements_Matched",), "text"),
        FieldSpec("missing_requirements", ("Missing_Requirements",), "text"),
        FieldSpec("why_it_fits", ("Why_It_Fits",), "text"),
        FieldSpec("application_recommendation", ("Application_Recommendation",), "text"),
        FieldSpec("first_seen", ("First_Seen",), "date", dtype="date"),
        FieldSpec("last_verified", ("Last_Verified",), "date", dtype="date"),
        FieldSpec("closure_reason", ("Reason",), "text"),
        FieldSpec("notes", ("Notes",), "text"),
    )

    company_fields = (
        FieldSpec("company", ("Company", "Company_Name"), "company", required=True),
        FieldSpec("official_domain", ("Official_Domain",), "domain", dtype="domain"),
        FieldSpec("careers_domain", ("Careers_Domain",), "domain", dtype="domain"),
        FieldSpec("ats", ("ATS",), "text"),
        FieldSpec("company_type", ("Company_Type",), "text"),
        FieldSpec("india_locations", ("India_Locations",), "text"),
        FieldSpec("source_discovered_from", ("Source_Discovered_From",), "text"),
        FieldSpec("reason_added", ("Reason_Added",), "text"),
        FieldSpec("suggested_tier", ("Suggested_Tier",), "text"),
        FieldSpec("next_delta_check", ("Next_Delta_Check",), "date", dtype="date"),
        FieldSpec("next_deep_check", ("Next_Deep_Check",), "date", dtype="date"),
    )

    coverage_fields = (
        FieldSpec("company", ("Company", "Company_Name"), "company", required=True),
        FieldSpec("tier", ("Tier",), "text"),
        FieldSpec("company_type", ("Company_Type",), "text"),
        FieldSpec("official_careers_domain", ("Official_Careers_Domain",), "domain", dtype="domain"),
        FieldSpec("ats", ("ATS",), "text"),
        FieldSpec("check_type", ("Check_Type",), "text"),
        FieldSpec("checked_at", ("Checked_At",), "date", dtype="date"),
        FieldSpec("result", ("Result",), "text"),
        FieldSpec("relevant_jobs", ("Relevant_Jobs",), "int", dtype="int", min_value=0),
        FieldSpec("new_jobs", ("New_Jobs",), "int", dtype="int", min_value=0),
        FieldSpec("known_unchanged", ("Known_Unchanged",), "int", dtype="int", min_value=0),
        FieldSpec("closed", ("Closed",), "int", dtype="int", min_value=0),
        FieldSpec("manual_verification", ("Manual_Verification",), "text"),
        FieldSpec("access_status", ("Access_Status",), "text"),
        FieldSpec("next_delta_check", ("Next_Delta_Check",), "date", dtype="date"),
        FieldSpec("next_deep_check", ("Next_Deep_Check",), "date", dtype="date"),
    )

    source_fields = (
        FieldSpec("source", ("Source",), "text", required=True),
        FieldSpec("attempted", ("Attempted",), "bool", dtype="bool"),
        FieldSpec("completed", ("Completed",), "bool", dtype="bool"),
        FieldSpec("searches_performed", ("Searches_Performed",), "int", dtype="int", min_value=0),
        FieldSpec("raw_results", ("Raw_Results",), "int", dtype="int", min_value=0),
        FieldSpec("relevant_results", ("Relevant_Results",), "int", dtype="int", min_value=0),
        FieldSpec("verified_official", ("Verified_Official",), "int", dtype="int", min_value=0),
        FieldSpec("portal_only", ("Portal_Only",), "int", dtype="int", min_value=0),
        FieldSpec("manual_verification", ("Manual_Verification",), "int", dtype="int", min_value=0),
        FieldSpec("closed", ("Closed",), "int", dtype="int", min_value=0),
        FieldSpec("access_limited", ("Access_Limited",), "int", dtype="int", min_value=0),
        FieldSpec("notes", ("Notes",), "text"),
    )

    resume_fields = (
        FieldSpec("company", ("Company",), "company", required=True),
        FieldSpec("role", ("Role",), "text"),
        FieldSpec("job_id", ("Job_ID",), "text"),
        FieldSpec("match_score", ("Match_Score",), "score", dtype="score", min_value=0, max_value=100),
        FieldSpec("summary_changes", ("Summary_Changes",), "text"),
        FieldSpec("skills_order", ("Skills_Order",), "text"),
        FieldSpec("experience_changes", ("Experience_Changes",), "text"),
        FieldSpec("project_changes", ("Project_Changes",), "text"),
        FieldSpec("supported_keywords", ("Supported_Keywords",), "text"),
        FieldSpec("do_not_claim", ("Do_Not_Claim",), "text"),
        FieldSpec("genuine_gaps", ("Genuine_Gaps",), "text"),
        FieldSpec("suggested_resume_filename", ("Suggested_Resume_Filename",), "text"),
    )

    recruiter_fields = (
        FieldSpec("company", ("Company",), "company", required=True),
        FieldSpec("role", ("Role",), "text"),
        FieldSpec("job_id", ("Job_ID",), "text"),
        FieldSpec("contact_name", ("Contact_Name",), "text"),
        FieldSpec("contact_title", ("Contact_Title",), "text"),
        FieldSpec("email", ("Email",), "lower"),
        FieldSpec("profile_url", ("Profile_URL",), "url", dtype="url"),
        FieldSpec("contact_type", ("Contact_Type",), "text"),
        FieldSpec("source", ("Source",), "text"),
        FieldSpec("confidence", ("Confidence",), "text"),
        FieldSpec("safe_to_contact", ("Safe_To_Contact",), "bool", dtype="bool"),
        FieldSpec("usage_restrictions", ("Usage_Restrictions",), "text"),
        FieldSpec("notes", ("Notes",), "text"),
    )

    run_fields = (
        FieldSpec("run_id", ("Run_ID",), "text", required=True),
        FieldSpec("run_date", ("Run_Date",), "date", dtype="date"),
        FieldSpec("start_time", ("Start_Time",), "text"),
        FieldSpec("end_time", ("End_Time",), "text"),
        FieldSpec("runtime", ("Runtime",), "text"),
        FieldSpec("run_status", ("Run_Status",), "text"),
        FieldSpec("companies_checked", ("Companies_Checked",), "int", dtype="int", min_value=0),
        FieldSpec("new_companies_discovered", ("New_Companies_Discovered",), "int", dtype="int", min_value=0),
        FieldSpec("official_sites_checked", ("Official_Sites_Checked",), "int", dtype="int", min_value=0),
        FieldSpec("raw_discoveries", ("Raw_Discoveries",), "int", dtype="int", min_value=0),
        FieldSpec("relevant_discoveries", ("Relevant_Discoveries",), "int", dtype="int", min_value=0),
        FieldSpec("duplicates_suppressed", ("Duplicates_Suppressed",), "int", dtype="int", min_value=0),
        FieldSpec("remaining_work", ("Remaining_Work",), "text"),
    )

    schemas = [
        EntitySchema(
            "job",
            ("All_Jobs", "Closed_or_Rejected"),
            job_fields,
            identity_fields=("company", "job_id"),
            identity_fallback=("company", "role", "location"),
        ),
        EntitySchema("company", ("New_Companies",), company_fields, identity_fields=("company",)),
        EntitySchema(
            "company_coverage",
            ("Company_Coverage",),
            coverage_fields,
            identity_fields=("company",),
        ),
        EntitySchema("source_coverage", ("Source_Coverage",), source_fields, identity_fields=("source",)),
        EntitySchema(
            "resume_tailoring",
            ("Resume_Tailoring",),
            resume_fields,
            identity_fields=("company", "job_id"),
            identity_fallback=("company", "role"),
        ),
        EntitySchema(
            "recruiter_contact",
            ("Recruiter_Contacts",),
            recruiter_fields,
            identity_fields=("company", "job_id", "email"),
            identity_fallback=("company", "contact_name"),
        ),
        EntitySchema("run_summary", ("Run_Summary",), run_fields, identity_fields=("run_id",)),
    ]
    return MappingConfig(schemas, version="1")

