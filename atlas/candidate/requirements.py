"""Deterministic job-requirements extractor (Phase 2A official-first §11).

Turns an official job DESCRIPTION (already HTML-sanitized to text) plus any
adapter-emitted skills into a compact, evidence-bearing view the candidate
ranking operates on: mandatory / preferred requirement strings, an experience
phrase, and an eligibility / sponsorship phrase.

It is completely deterministic and treats the posting as UNTRUSTED DATA (never
instructions): it only reads the text, never follows links, never executes
anything, and is a pure function of its inputs. A description that yields no
recognizable requirement produces an EMPTY extraction — never an invented one —
so a job with no usable requirements stays MANUAL_VERIFICATION downstream rather
than getting a fabricated strong match from its title alone.

Two complementary signals are combined:

    1. A curated technology / skill vocabulary (domain data, NOT candidate
       constants) matched on word boundaries — short, readable requirement
       tokens that the token-overlap matcher can align to candidate evidence.
    2. Requirement CLAUSES: bounded sentence/bullet fragments that carry an
       explicit requirement signal ("years of experience", "proficient in",
       "strong knowledge of", ...). These capture skills outside the vocabulary.

A "nice to have" / "preferred" section marker splits mandatory from preferred;
everything before the first such marker is mandatory, everything after is
preferred.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

# --------------------------------------------------------------------------- #
# Curated technology / skill vocabulary (domain data, not candidate-specific).
# Multi-word phrases are matched first so "spring boot" wins over "spring".
# --------------------------------------------------------------------------- #
_VOCAB: tuple[str, ...] = (
    # languages
    "java", "kotlin", "scala", "groovy", "python", "golang", "go", "c++", "c#",
    "typescript", "javascript", "ruby", "php", "rust", "sql", "pl/sql", "bash",
    # jvm / backend frameworks
    "spring boot", "spring mvc", "spring security", "spring cloud", "spring",
    "hibernate", "jpa", "micronaut", "quarkus", "struts", "jax-rs", "jersey",
    "node.js", "express", "django", "flask", "fastapi", "rails", ".net", "asp.net",
    # frontend
    "react", "react.js", "angular", "vue", "next.js", "redux", "graphql",
    "html", "css", "sass", "tailwind",
    # data / messaging
    "postgresql", "postgres", "mysql", "oracle", "mongodb", "cassandra", "redis",
    "elasticsearch", "kafka", "rabbitmq", "activemq", "hadoop", "spark", "hive",
    "snowflake", "dynamodb", "sqlserver", "mariadb",
    # cloud / devops
    "aws", "azure", "gcp", "google cloud", "kubernetes", "docker", "terraform",
    "jenkins", "gitlab ci", "github actions", "ansible", "helm", "openshift",
    "ci/cd", "linux", "unix",
    # architecture / concepts
    "microservices", "restful", "rest", "grpc", "soap", "apis",
    "distributed systems", "event-driven", "message queue", "oauth", "jwt",
    "junit", "mockito", "tdd", "agile", "scrum", "design patterns",
    "multithreading", "concurrency", "data structures", "algorithms",
    "object-oriented", "oop", "system design", "web services", "soa",
    "workday", "payroll", "sap", "peoplesoft", "hcm", "etl", "integration",
)

# Requirement signal words: a clause that contains one is treated as a genuine
# requirement fragment even when it uses no vocabulary term.
_SIGNALS: tuple[str, ...] = (
    "experience", "years", "proficient", "proficiency", "knowledge of",
    "familiarity", "familiar with", "expertise", "hands-on", "hands on",
    "understanding of", "ability to", "degree in", "bachelor", "master",
    "b.tech", "b.e.", "m.tech", "skilled", "background in", "working with",
    "fluent in", "must have", "required", "strong", "solid", "demonstrated",
    "track record", "competency", "competencies", "well-versed",
)

_PREFERRED_MARKERS: tuple[str, ...] = (
    "nice to have", "nice-to-have", "nice if you", "preferred qualifications",
    "preferred qualification", "preferred skills", "preferred experience",
    "preferred:", "bonus points", "bonus if", "good to have", "great to have",
    "would be a plus", "is a plus", "are a plus", "pluses", "desired skills",
    "desirable", "it would be great", "even better", "extra credit",
)

_ELIGIBILITY_MARKERS: tuple[str, ...] = (
    "visa", "sponsorship", "sponsor", "work authorization", "authorized to work",
    "work permit", "right to work", "citizen", "citizenship", "security clearance",
    "clearance", "relocat", "eligible to work", "work eligibility",
)

_EXPERIENCE_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:to\s*\d{1,2}\s*)?year", re.IGNORECASE
)

# Residual HTML tags / entities can survive upstream sanitizing (encoded tags
# unescaped after tag-strip). Strip them so requirements are clean text.
_TAG_RE = re.compile(r"<[^>]{0,120}>")
_ENTITY_RE = re.compile(r"&[a-z#0-9]{1,8};", re.IGNORECASE)

# Clause splitting: bullets, sentence ends, semicolons, common list separators.
_CLAUSE_SPLIT_RE = re.compile(r"[\u2022\u2023\u25CF\u25AA\u2043\*\n\r;]+|(?<=[a-z0-9\)])\.\s+|\s[-\u2013\u2014]\s")
_WS_RE = re.compile(r"\s+")

_MAX_CLAUSE_CHARS = 200
_MIN_CLAUSE_CHARS = 8
_MAX_MANDATORY = 25
_MAX_PREFERRED = 15


@dataclass(frozen=True)
class RequirementExtraction:
    """Compact requirement view extracted from an official description."""

    mandatory: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()
    experience_text: str = ""
    eligibility_text: str = ""
    signals: dict = field(default_factory=dict)

    @property
    def has_requirements(self) -> bool:
        return bool(self.mandatory or self.preferred)

    def to_dict(self) -> dict:
        return {
            "mandatory": list(self.mandatory),
            "preferred": list(self.preferred),
            "experience_text": self.experience_text,
            "eligibility_text": self.eligibility_text,
            "signals": dict(self.signals),
        }


def _norm_ws(text: str) -> str:
    # Strip residual HTML tags/entities then collapse whitespace, so a clause is
    # clean readable text even when upstream sanitizing left encoded markup.
    text = _TAG_RE.sub(" ", text)
    text = _ENTITY_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _dedupe_preserve(items: Iterable[str], limit: int) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        norm = _norm_ws(item)
        if len(norm) < 2:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
        if len(out) >= limit:
            break
    return tuple(out)


def _vocab_hits(region_lower: str) -> list[str]:
    """Vocabulary terms present in the region, word-boundary matched, in the
    canonical vocabulary order (longest-phrase-first is preserved by ordering
    multi-word entries before their prefixes in _VOCAB)."""
    hits: list[str] = []
    for term in _VOCAB:
        # Build a boundary-aware pattern; escape regex metacharacters in the term.
        pat = r"(?<![a-z0-9\+\#\.])" + re.escape(term) + r"(?![a-z0-9\+\#])"
        if re.search(pat, region_lower):
            hits.append(term)
    return hits


def _clause_requirements(region: str) -> list[str]:
    """Bounded requirement clauses carrying an explicit requirement signal."""
    out: list[str] = []
    for raw in _CLAUSE_SPLIT_RE.split(region):
        clause = _norm_ws(raw)
        low = clause.lower()
        if not (_MIN_CLAUSE_CHARS <= len(clause) <= _MAX_CLAUSE_CHARS):
            continue
        if not any(sig in low for sig in _SIGNALS):
            continue
        out.append(clause)
    return out


def _first_marker_index(text_lower: str, markers: Sequence[str]) -> Optional[int]:
    best: Optional[int] = None
    for marker in markers:
        idx = text_lower.find(marker)
        if idx != -1 and (best is None or idx < best):
            best = idx
    return best


def _experience_phrase(description: str, explicit: str) -> str:
    if explicit and explicit.strip():
        return _norm_ws(explicit)[:120]
    m = _EXPERIENCE_RE.search(description or "")
    if not m:
        return ""
    # Start at the number and extend forward for readable context ("5+ years of
    # experience with ..."); trim any trailing partial word.
    start = m.start()
    end = min(len(description), m.end() + 40)
    phrase = _norm_ws(description[start:end])
    if len(phrase) > 60 and " " in phrase:
        phrase = phrase[:60].rsplit(" ", 1)[0]
    return phrase[:120]


def _eligibility_phrase(description: str) -> str:
    low = (description or "").lower()
    for marker in _ELIGIBILITY_MARKERS:
        idx = low.find(marker)
        if idx == -1:
            continue
        start = max(0, idx - 30)
        end = min(len(description), idx + 90)
        return _norm_ws(description[start:end])[:160]
    return ""


def extract_requirements(
    description: Optional[str],
    *,
    skills: Sequence[str] = (),
    experience_text: str = "",
    extra_vocabulary: Sequence[str] = (),
) -> RequirementExtraction:
    """Extract mandatory / preferred requirements from an official description.

    ``skills`` are adapter-emitted skill tokens (treated as mandatory). Returns
    an EMPTY extraction when nothing recognizable is found (never invented)."""
    description = (description or "").strip()
    # Strip residual HTML tags/entities up front so section detection, vocab
    # matching, and clause extraction all operate on clean text.
    description = _ENTITY_RE.sub(" ", _TAG_RE.sub(" ", description))
    skills = tuple(s for s in (skills or ()) if s and str(s).strip())

    if not description and not skills:
        return RequirementExtraction()

    low = description.lower()
    split_at = _first_marker_index(low, _PREFERRED_MARKERS)
    if split_at is None:
        mandatory_region, preferred_region = description, ""
    else:
        mandatory_region, preferred_region = description[:split_at], description[split_at:]

    mand_low = mandatory_region.lower()
    pref_low = preferred_region.lower()

    # Vocabulary hits (short, readable requirement tokens).
    vocab = tuple(_VOCAB) + tuple(str(v).lower() for v in (extra_vocabulary or ()))
    mand_vocab = [t for t in vocab if re.search(
        r"(?<![a-z0-9\+\#\.])" + re.escape(t) + r"(?![a-z0-9\+\#])", mand_low)]
    pref_vocab = [t for t in vocab if re.search(
        r"(?<![a-z0-9\+\#\.])" + re.escape(t) + r"(?![a-z0-9\+\#])", pref_low)]

    # Adapter skills are mandatory signals.
    mandatory_items = list(skills) + mand_vocab + _clause_requirements(mandatory_region)
    preferred_items = pref_vocab + _clause_requirements(preferred_region)

    # A preferred token that also appears in mandatory stays mandatory only.
    mandatory = _dedupe_preserve(mandatory_items, _MAX_MANDATORY)
    mand_keys = {m.lower() for m in mandatory}
    preferred = _dedupe_preserve(
        (p for p in preferred_items if p.lower() not in mand_keys), _MAX_PREFERRED
    )

    return RequirementExtraction(
        mandatory=mandatory,
        preferred=preferred,
        experience_text=_experience_phrase(description, experience_text),
        eligibility_text=_eligibility_phrase(description),
        signals={
            "had_preferred_section": split_at is not None,
            "vocab_mandatory": len(mand_vocab),
            "vocab_preferred": len(pref_vocab),
            "adapter_skills": len(skills),
            "description_len": len(description),
        },
    )


__all__ = ["RequirementExtraction", "extract_requirements"]
