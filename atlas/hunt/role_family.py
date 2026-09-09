"""Deterministic role-family classifier (build spec section 10).

Classifies a posting into ONE role family from title + description, entirely
separately from technology-stack matching. Non-target families (QA/SDET,
product/program management, DevOps/SRE, support, functional consulting,
sales/BPO, data/ML, mobile) are excluded from the candidate shortlist unless a
lane explicitly allows them.

Crucially this does NOT over-filter a genuine development role that merely
mentions on-call, debugging, incident response, customer collaboration, or
production support as one responsibility (build spec section 10, last rule).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from atlas.hunt.signals import find_signals, signal_present

__all__ = ["RoleFamily", "RoleFamilyDecision", "classify_role_family", "DEVELOPMENT_FAMILIES"]


class RoleFamily(str, enum.Enum):
    SOFTWARE_DEVELOPMENT = "SOFTWARE_DEVELOPMENT"
    BACKEND_DEVELOPMENT = "BACKEND_DEVELOPMENT"
    FULLSTACK_DEVELOPMENT = "FULLSTACK_DEVELOPMENT"
    FRONTEND_DEVELOPMENT = "FRONTEND_DEVELOPMENT"
    ENTERPRISE_APPLICATION_DEVELOPMENT = "ENTERPRISE_APPLICATION_DEVELOPMENT"
    QA_MANUAL = "QA_MANUAL"
    QA_AUTOMATION_SDET = "QA_AUTOMATION_SDET"
    DEVOPS_SRE_INFRA = "DEVOPS_SRE_INFRA"
    PRODUCT_MANAGEMENT = "PRODUCT_MANAGEMENT"
    PROGRAM_MANAGEMENT = "PROGRAM_MANAGEMENT"
    DATA_ML_ANALYTICS = "DATA_ML_ANALYTICS"
    MOBILE = "MOBILE"
    SUPPORT_HELPDESK = "SUPPORT_HELPDESK"
    FUNCTIONAL_CONSULTING = "FUNCTIONAL_CONSULTING"
    SALES_BPO = "SALES_BPO"
    OTHER = "OTHER"


DEVELOPMENT_FAMILIES = frozenset(
    {
        RoleFamily.SOFTWARE_DEVELOPMENT.value,
        RoleFamily.BACKEND_DEVELOPMENT.value,
        RoleFamily.FULLSTACK_DEVELOPMENT.value,
        RoleFamily.FRONTEND_DEVELOPMENT.value,
        RoleFamily.ENTERPRISE_APPLICATION_DEVELOPMENT.value,
    }
)


@dataclass(frozen=True)
class RoleFamilyDecision:
    family: str
    matched_signals: tuple[str, ...] = ()
    reason: str = ""


# Non-development families are checked FIRST (title-weighted) so a
# "Software Development Engineer in Test" is SDET, not SOFTWARE_DEVELOPMENT.
# Each entry: (family, title_signals, body_signals_that_confirm).
_EXCLUDED_TITLE_SIGNALS: tuple[tuple[RoleFamily, tuple[str, ...]], ...] = (
    (
        RoleFamily.QA_AUTOMATION_SDET,
        (
            "sdet",
            "software development engineer in test",
            "software development engineer test",
            "test automation engineer",
            "automation test engineer",
            "qa automation engineer",
            "sdet ii",
            "automation engineer test",
        ),
    ),
    (
        RoleFamily.QA_MANUAL,
        (
            "manual test engineer",
            "manual tester",
            "manual qa",
            "quality assurance engineer",
            "qa engineer",
            "test engineer",
            "quality analyst",
        ),
    ),
    (
        RoleFamily.PRODUCT_MANAGEMENT,
        (
            "product manager",
            "technical product manager",
            "group product manager",
            "senior product manager",
            "associate product manager",
            "product owner",
            "director of product",
            "head of product",
        ),
    ),
    (
        RoleFamily.PROGRAM_MANAGEMENT,
        (
            "program manager",
            "project manager",
            "delivery manager",
            "scrum master",
            "engineering manager",
            "technical program manager",
            "release manager",
        ),
    ),
    (
        RoleFamily.DEVOPS_SRE_INFRA,
        (
            "devops engineer",
            "devops",
            "site reliability engineer",
            "site reliability",
            "sre",
            "infrastructure engineer",
            "platform reliability",
            "network engineer",
            "systems administrator",
            "system administrator",
            "cloud infrastructure engineer",
            "cloud operations engineer",
            "operations engineer",
        ),
    ),
    (
        RoleFamily.DATA_ML_ANALYTICS,
        (
            "data scientist",
            "data engineer",
            "machine learning engineer",
            "ml engineer",
            "data analyst",
            "analytics engineer",
            "ai engineer",
            "big data engineer",
            "bi developer",
        ),
    ),
    (
        RoleFamily.MOBILE,
        (
            "android developer",
            "android engineer",
            "android bsp",
            "ios developer",
            "ios engineer",
            "mobile developer",
            "mobile engineer",
            "mobile application developer",
            "react native developer",
            "flutter developer",
            "swift developer",
        ),
    ),
    (
        RoleFamily.FUNCTIONAL_CONSULTING,
        (
            "functional consultant",
            "sap functional",
            "implementation consultant",
            "functional analyst",
            "techno functional",
            "consulting engineer",
            "technical consultant",
        ),
    ),
    (
        RoleFamily.SUPPORT_HELPDESK,
        (
            "technical support engineer",
            "support engineer",
            "help desk",
            "helpdesk",
            "desktop support",
            "l1 support",
            "l2 support",
            "application support engineer",
            "production support engineer",
            "customer success engineer",
            "technical services engineer",
            "technical account manager",
            "field engineer",
            "services engineer",
        ),
    ),
    (
        RoleFamily.SALES_BPO,
        (
            "sales executive",
            "business development",
            "inside sales",
            "account executive",
            "bpo",
            "voice process",
            "customer care executive",
            "pre sales",
            "presales",
            "solutions engineer",
            "solution engineer",
            "sales engineer",
            "presales engineer",
            "pre-sales engineer",
            "solutions consultant",
            "partner engineer",
            "partnerships engineer",
        ),
    ),
)

# Development-family title signals, most specific first.
_FULLSTACK = ("full stack", "fullstack", "full-stack")
_FRONTEND = ("frontend", "front-end", "front end", "ui developer", "ui engineer", "web developer")
_BACKEND = ("backend", "back-end", "back end", "server-side", "server side", "api developer")
_ENTERPRISE = (
    "enterprise application",
    "enterprise applications",
    "business application",
    "erp developer",
    "payroll developer",
    "integration developer",
    "integration engineer",
)
_SOFTWARE = (
    "software engineer",
    "software developer",
    "software development engineer",
    "sde",
    "application developer",
    "application engineer",
    "programmer analyst",
    "programmer",
    "member of technical staff",
    "development engineer",
    "product engineer",
    "developer",
    "engineer",
)


def _title_hit(title_norm: str, signals: tuple[str, ...]) -> Optional[str]:
    for sig in signals:
        if signal_present(title_norm, sig):
            return sig
    return None


def classify_role_family(title: object, description: object = "") -> RoleFamilyDecision:
    """Classify one posting into a single role family.

    Non-development families are matched first from the TITLE so a testing or
    management title is never mistaken for a development role. If the title is
    ambiguous, a small set of body signals can still confirm an excluded family
    (e.g. an explicit "SDET" in the body). Development families are then matched
    most-specific first (full stack -> frontend -> backend -> enterprise ->
    generic software).
    """
    from atlas.hunt.signals import normalize_text

    title_norm = normalize_text(title)
    body_norm = normalize_text(description)

    # 1) excluded families, title-weighted
    for family, title_signals in _EXCLUDED_TITLE_SIGNALS:
        hit = _title_hit(title_norm, title_signals)
        if hit:
            return RoleFamilyDecision(family.value, (hit,), f"title matched excluded {family.value}")

    # 1b) a couple of high-confidence body signals for excluded families that a
    # terse title may hide (SDET / product-manager written only in the body).
    for family, body_signals in (
        (RoleFamily.QA_AUTOMATION_SDET, ("sdet", "software development engineer in test")),
        (RoleFamily.PRODUCT_MANAGEMENT, ("product manager",)),
    ):
        hit = _title_hit(body_norm, body_signals)
        if hit:
            return RoleFamilyDecision(family.value, (hit,), f"body matched excluded {family.value}")

    # 2) development families, most specific first
    for family, signals in (
        (RoleFamily.FULLSTACK_DEVELOPMENT, _FULLSTACK),
        (RoleFamily.FRONTEND_DEVELOPMENT, _FRONTEND),
        (RoleFamily.BACKEND_DEVELOPMENT, _BACKEND),
        (RoleFamily.ENTERPRISE_APPLICATION_DEVELOPMENT, _ENTERPRISE),
        (RoleFamily.SOFTWARE_DEVELOPMENT, _SOFTWARE),
    ):
        hit = _title_hit(title_norm, signals)
        if hit:
            return RoleFamilyDecision(family.value, (hit,), f"title matched {family.value}")

    # 3) development families from the body as a last resort
    for family, signals in (
        (RoleFamily.FULLSTACK_DEVELOPMENT, _FULLSTACK),
        (RoleFamily.FRONTEND_DEVELOPMENT, _FRONTEND),
        (RoleFamily.BACKEND_DEVELOPMENT, _BACKEND),
        (RoleFamily.SOFTWARE_DEVELOPMENT, _SOFTWARE),
    ):
        hit = _title_hit(body_norm, signals)
        if hit:
            return RoleFamilyDecision(family.value, (hit,), f"body matched {family.value}")

    return RoleFamilyDecision(RoleFamily.OTHER.value, (), "no role-family signal")
