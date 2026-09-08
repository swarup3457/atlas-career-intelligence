"""Atlas separated status axes + legacy aliases (Phase 1B, build spec 17).

Legacy Workspace documents overloaded a single "status" across discovery,
lifecycle, verification, recommendation, source health, coverage, and task
outcomes. Phase 1B keeps these as DISTINCT typed axes so no single field is
overloaded, and provides legacy aliases (spacing/case variants) that map
onto the canonical states WITHOUT creating duplicate canonical values.

Other axes already exist and are re-exported for a single import point:
    * SourceHealthState — atlas.sources.health
    * CoverageStatus    — atlas.sources.coverage
    * TaskStatus        — atlas.models
"""

from __future__ import annotations

import enum
from typing import Optional

from atlas.models import TaskStatus  # noqa: F401 (re-exported)
from atlas.sources.coverage import CoverageStatus  # noqa: F401 (re-exported)
from atlas.sources.health import SourceHealthState  # noqa: F401 (re-exported)


# --- A. DiscoveryStatus: where a lead is in the processing pipeline --------
class DiscoveryStatus(str, enum.Enum):
    DISCOVERY_CANDIDATE = "DISCOVERY_CANDIDATE"
    NORMALIZED = "NORMALIZED"
    DEDUPLICATED = "DEDUPLICATED"
    VERIFICATION_PENDING = "VERIFICATION_PENDING"
    VERIFIED = "VERIFIED"
    MATCHED = "MATCHED"
    REPORTED = "REPORTED"
    REJECTED = "REJECTED"


# --- B. JobLifecycleStatus: is the vacancy itself open? --------------------
class JobLifecycleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


# --- C. VerificationLevel: strength of current evidence --------------------
# CLOSED belongs to lifecycle, not verification (build spec 17.C/G).
class VerificationLevel(str, enum.Enum):
    VERIFIED_OFFICIAL = "VERIFIED_OFFICIAL"
    OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED = "OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED"
    PORTAL_CURRENT_LEAD = "PORTAL_CURRENT_LEAD"
    MANUAL_VERIFICATION = "MANUAL_VERIFICATION"
    SUSPICIOUS_REJECTED = "SUSPICIOUS_REJECTED"


# --- D. RecommendationStatus: the candidate action after fit analysis ------
class RecommendationStatus(str, enum.Enum):
    PRIORITY_APPLY = "PRIORITY_APPLY"
    STRONG_APPLY = "STRONG_APPLY"
    APPLY_AFTER_TAILORING = "APPLY_AFTER_TAILORING"
    STRETCH = "STRETCH"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    MONITOR = "MONITOR"
    REJECT = "REJECT"
    CLOSED = "CLOSED"


# ---------------------------------------------------------------------------
# Legacy aliases → canonical. Includes spacing/case variants from the
# Workspace documents. Aliases NEVER add a new canonical state; they only
# resolve to an existing one.
# ---------------------------------------------------------------------------
def _norm(label: str) -> str:
    return " ".join(str(label).strip().lower().replace("-", " ").replace("_", " ").split())


_VERIFICATION_ALIASES: dict[str, VerificationLevel] = {
    _norm("VERIFIED_OFFICIAL"): VerificationLevel.VERIFIED_OFFICIAL,
    _norm("Verified Official"): VerificationLevel.VERIFIED_OFFICIAL,
    _norm("VERIFIED_LIVE"): VerificationLevel.VERIFIED_OFFICIAL,
    _norm("Verified Authorized Recruiter"): VerificationLevel.VERIFIED_OFFICIAL,
    _norm("OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED"): VerificationLevel.OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED,
    _norm("Official Page Found Apply Path Unconfirmed"): VerificationLevel.OFFICIAL_PAGE_FOUND_APPLY_PATH_UNCONFIRMED,
    _norm("PORTAL_CURRENT_LEAD"): VerificationLevel.PORTAL_CURRENT_LEAD,
    _norm("Portal Current Lead"): VerificationLevel.PORTAL_CURRENT_LEAD,
    _norm("PORTAL_ONLY_UNVERIFIED"): VerificationLevel.PORTAL_CURRENT_LEAD,
    _norm("Portal-Only Unverified"): VerificationLevel.PORTAL_CURRENT_LEAD,
    _norm("MANUAL_VERIFICATION"): VerificationLevel.MANUAL_VERIFICATION,
    _norm("Manual Verification"): VerificationLevel.MANUAL_VERIFICATION,
    _norm("Live — Date Unknown"): VerificationLevel.MANUAL_VERIFICATION,
    _norm("SUSPICIOUS_REJECTED"): VerificationLevel.SUSPICIOUS_REJECTED,
    _norm("Suspicious"): VerificationLevel.SUSPICIOUS_REJECTED,
    _norm("SUSPICIOUS"): VerificationLevel.SUSPICIOUS_REJECTED,
}

_LIFECYCLE_ALIASES: dict[str, JobLifecycleStatus] = {
    _norm("ACTIVE"): JobLifecycleStatus.ACTIVE,
    _norm("Open"): JobLifecycleStatus.ACTIVE,
    _norm("Live"): JobLifecycleStatus.ACTIVE,
    _norm("CLOSED"): JobLifecycleStatus.CLOSED,
    _norm("Closed"): JobLifecycleStatus.CLOSED,
    _norm("Expired"): JobLifecycleStatus.CLOSED,
    _norm("Filled"): JobLifecycleStatus.CLOSED,
    _norm("Removed"): JobLifecycleStatus.CLOSED,
    _norm("No Longer Accepting"): JobLifecycleStatus.CLOSED,
    _norm("UNKNOWN"): JobLifecycleStatus.UNKNOWN,
    _norm("Unknown"): JobLifecycleStatus.UNKNOWN,
}

_RECOMMENDATION_ALIASES: dict[str, RecommendationStatus] = {
    _norm("PRIORITY_APPLY"): RecommendationStatus.PRIORITY_APPLY,
    _norm("Priority Apply"): RecommendationStatus.PRIORITY_APPLY,
    _norm("Apply Now"): RecommendationStatus.PRIORITY_APPLY,
    _norm("STRONG_APPLY"): RecommendationStatus.STRONG_APPLY,
    _norm("Strong Apply"): RecommendationStatus.STRONG_APPLY,
    _norm("APPLY_AFTER_TAILORING"): RecommendationStatus.APPLY_AFTER_TAILORING,
    _norm("Apply After Tailoring"): RecommendationStatus.APPLY_AFTER_TAILORING,
    _norm("Tailoring Needed"): RecommendationStatus.APPLY_AFTER_TAILORING,
    _norm("STRETCH"): RecommendationStatus.STRETCH,
    _norm("Stretch"): RecommendationStatus.STRETCH,
    _norm("MANUAL_REVIEW"): RecommendationStatus.MANUAL_REVIEW,
    _norm("Manual Review"): RecommendationStatus.MANUAL_REVIEW,
    _norm("MONITOR"): RecommendationStatus.MONITOR,
    _norm("Monitor"): RecommendationStatus.MONITOR,
    _norm("REJECT"): RecommendationStatus.REJECT,
    _norm("Reject"): RecommendationStatus.REJECT,
    _norm("CLOSED"): RecommendationStatus.CLOSED,
    _norm("Closed"): RecommendationStatus.CLOSED,
}


class UnknownStatusAlias(KeyError):
    pass


def normalize_verification(label: str) -> Optional[VerificationLevel]:
    return _VERIFICATION_ALIASES.get(_norm(label))


def normalize_lifecycle(label: str) -> Optional[JobLifecycleStatus]:
    return _LIFECYCLE_ALIASES.get(_norm(label))


def normalize_recommendation(label: str) -> Optional[RecommendationStatus]:
    return _RECOMMENDATION_ALIASES.get(_norm(label))


# A combined human-facing "verification status" column may DERIVE CLOSED from
# lifecycle, but the internal axes stay separate.
def combined_verification_display(
    verification: VerificationLevel, lifecycle: JobLifecycleStatus
) -> str:
    if lifecycle == JobLifecycleStatus.CLOSED:
        return "CLOSED"
    return verification.value


__all__ = [
    "DiscoveryStatus",
    "JobLifecycleStatus",
    "VerificationLevel",
    "RecommendationStatus",
    "SourceHealthState",
    "CoverageStatus",
    "TaskStatus",
    "UnknownStatusAlias",
    "normalize_verification",
    "normalize_lifecycle",
    "normalize_recommendation",
    "combined_verification_display",
]
