"""Typed technology-evidence model + React/full-stack backend gate (audit 3.4, prompt s.8).

The V2 React lane accepted ANY full-stack role that merely mentioned React, even when
the mandatory backend was Python/Node/Ruby/Go/PHP. This module extracts a typed
technology-evidence view of a posting that distinguishes:

* mandatory vs preferred vs incidental technologies,
* the dominant/mandatory *backend*,
* the supported backend (Java/.NET), the frontend stack, and any
  *unsupported mandatory backend*.

:func:`react_fullstack_verdict` then decides whether a React/full-stack role is
candidate-relevant: a pure React frontend is fine; a Java/.NET full stack is fine; a
full-stack role whose mandatory backend is an unsupported language (and no supported
backend is present) is rejected. Genuinely ambiguous cases are flagged for the optional
ambiguity agent — deterministic code never silently accepts an unsupported full stack.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = [
    "TechnologyEvidence",
    "StackVerdict",
    "analyze_technology_evidence",
    "react_fullstack_verdict",
    "SUPPORTED_BACKEND",
    "UNSUPPORTED_BACKEND",
    "FRONTEND_TECH",
]

# token -> canonical family label
SUPPORTED_BACKEND: dict[str, str] = {
    "java": "Java", "jvm": "Java", "spring": "Java", "spring boot": "Java",
    "spring mvc": "Java", "j2ee": "Java", "jakarta ee": "Java", "javaee": "Java",
    "c#": ".NET", ".net": ".NET", ".net core": ".NET", "asp.net": ".NET",
    "asp.net core": ".NET", "dotnet": ".NET",
}

UNSUPPORTED_BACKEND: dict[str, str] = {
    "python": "Python", "django": "Python", "flask": "Python", "fastapi": "Python",
    "node.js": "Node.js", "nodejs": "Node.js", "node js": "Node.js", "nestjs": "Node.js",
    "express.js": "Node.js", "express": "Node.js",
    "ruby": "Ruby", "ruby on rails": "Ruby", "rails": "Ruby",
    "go": "Go", "golang": "Go", "rust": "Rust",
    "php": "PHP", "laravel": "PHP", "scala": "Scala", "elixir": "Elixir",
}

FRONTEND_TECH: tuple[str, ...] = (
    "react", "reactjs", "react.js", "angular", "vue", "vue.js", "javascript", "typescript",
)

_OPTIONAL_CTX = re.compile(
    r"(nice[\s-]*to[\s-]*have|good[\s-]*to[\s-]*have|a plus|is a plus|bonus|optional|"
    r"preferred|desirable|desired|familiarity|exposure to|knowledge of|would be great|ideally|"
    r"advantage|added advantage)",
    re.I,
)
_BACKEND_ROLE_RE = re.compile(r"\b(backend|back[\s-]?end|server[\s-]?side)\b", re.I)


def _norm(text: Optional[str]) -> str:
    if not text:
        return ""
    return " ".join(str(text).strip().lower().split())


def _present(token: str, low: str) -> bool:
    return re.search(r"(?<![a-z0-9+.#])" + re.escape(token) + r"(?![a-z0-9+#])", low) is not None


def _near_optional(token: str, low: str, window: int = 45) -> bool:
    for m in re.finditer(re.escape(token), low):
        s = max(0, m.start() - window)
        e = min(len(low), m.end() + window)
        if _OPTIONAL_CTX.search(low[s:e]):
            return True
    return False


@dataclass(frozen=True)
class TechnologyEvidence:
    mandatory: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()
    incidental: tuple[str, ...] = ()
    supported_backend: tuple[str, ...] = ()
    unsupported_mandatory_backend: tuple[str, ...] = ()
    unsupported_optional_backend: tuple[str, ...] = ()
    frontend: tuple[str, ...] = ()
    dominant_backend: Optional[str] = None
    ambiguous_backend: bool = False
    evidence_spans: tuple[str, ...] = ()

    @property
    def supported_stack_summary(self) -> str:
        parts = list(self.supported_backend) + list(self.frontend)
        return ", ".join(dict.fromkeys(parts))


class StackVerdict(str, enum.Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    AMBIGUOUS = "AMBIGUOUS"


def analyze_technology_evidence(
    title: str,
    description: str = "",
    mandatory_requirements: Sequence[str] = (),
    preferred_requirements: Sequence[str] = (),
) -> TechnologyEvidence:
    title_low = _norm(title)
    desc_low = _norm(description)
    mand_low = _norm(" \n ".join(mandatory_requirements))
    pref_low = _norm(" \n ".join(preferred_requirements))
    body = " \n ".join(p for p in (title_low, desc_low, mand_low) if p)

    supported: list[str] = []
    unsupported_mand: list[str] = []
    unsupported_opt: list[str] = []
    frontend: list[str] = []
    spans: list[str] = []
    ambiguous = False

    for token, fam in SUPPORTED_BACKEND.items():
        if _present(token, body) or _present(token, pref_low):
            if fam not in supported:
                supported.append(fam)
                spans.append(token)

    for token in FRONTEND_TECH:
        if _present(token, body) or _present(token, pref_low):
            if token not in frontend:
                frontend.append(token)

    for token, fam in UNSUPPORTED_BACKEND.items():
        in_mand_req = _present(token, mand_low)
        in_pref_req = _present(token, pref_low)
        in_title = _present(token, title_low)
        in_desc = _present(token, desc_low)
        if not (in_mand_req or in_pref_req or in_title or in_desc):
            continue
        spans.append(token)
        # explicit optional/preferred context => optional
        if in_pref_req or _near_optional(token, desc_low) or (_near_optional(token, mand_low)):
            if fam not in unsupported_opt:
                unsupported_opt.append(fam)
            continue
        # explicit requirement or title or "<lang> backend" => mandatory
        if in_mand_req or in_title or _BACKEND_ROLE_RE.search(_context(token, body)):
            if fam not in unsupported_mand:
                unsupported_mand.append(fam)
            continue
        # bare description mention with no requirement list and no optional cue
        if in_desc:
            if fam not in unsupported_mand:
                unsupported_mand.append(fam)
            # only ambiguous when there is no explicit requirement list at all
            if not mandatory_requirements and not _BACKEND_ROLE_RE.search(desc_low):
                ambiguous = True

    dominant = unsupported_mand[0] if unsupported_mand else (supported[0] if supported else None)

    mandatory = tuple(dict.fromkeys(list(supported) + list(unsupported_mand)))
    preferred = tuple(dict.fromkeys(list(unsupported_opt)))
    return TechnologyEvidence(
        mandatory=mandatory,
        preferred=preferred,
        incidental=tuple(unsupported_opt),
        supported_backend=tuple(supported),
        unsupported_mandatory_backend=tuple(unsupported_mand),
        unsupported_optional_backend=tuple(unsupported_opt),
        frontend=tuple(frontend),
        dominant_backend=dominant,
        ambiguous_backend=ambiguous,
        evidence_spans=tuple(dict.fromkeys(spans)),
    )


def _context(token: str, low: str, window: int = 30) -> str:
    m = re.search(re.escape(token), low)
    if not m:
        return ""
    return low[max(0, m.start() - window): min(len(low), m.end() + window)]


def react_fullstack_verdict(evidence: TechnologyEvidence) -> tuple[StackVerdict, str]:
    """Decide whether a React/full-stack role is candidate-relevant on stack grounds."""
    if not evidence.unsupported_mandatory_backend:
        return StackVerdict.ACCEPT, "no unsupported mandatory backend"
    if evidence.supported_backend:
        return (
            StackVerdict.ACCEPT,
            f"supported backend {evidence.supported_backend} present alongside "
            f"{evidence.unsupported_mandatory_backend}",
        )
    if evidence.ambiguous_backend:
        return (
            StackVerdict.AMBIGUOUS,
            f"backend {evidence.unsupported_mandatory_backend} present but mandatory-vs-optional "
            "is unclear from deterministic evidence",
        )
    return (
        StackVerdict.REJECT,
        f"mandatory unsupported backend {evidence.unsupported_mandatory_backend} and no supported backend",
    )
