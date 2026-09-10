"""Append-only, versioned, sanitized site recipes.

A *recipe* is a reusable, general navigation strategy for one company/source's
official career site — never a single job. Storage is append-only JSONL under
the output tree (additive; no risky migration of the 3k-line SQLite schema).
Every candidate is sanitized before it can be stored, and only a live canary can
promote a candidate to ``VALIDATED``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

STATUS_CANDIDATE = "CANDIDATE"
STATUS_VALIDATED = "VALIDATED"
STATUS_SUPERSEDED = "SUPERSEDED"

_JOB_ID_KEYS = ("job_id", "jobId", "requisition_id", "req_id", "posting_id")
_SECRET_KEYS = ("cookie", "cookies", "token", "authorization", "auth", "password",
                "credential", "credentials", "session_token", "bearer")
_JS_MARKERS = ("<script", "javascript:", "function(", "=>", "eval(", "document.",
               "window.", "() =>", "require(")
_APPLY_MARKERS = ("apply", "submit", "application/submit", "auto-apply", "autoapply")

_RECIPE_FIELDS = (
    "company", "source_identity", "official_domain", "career_entry_pattern",
    "search_url_template", "query_param", "location_param", "detail_url_pattern",
    "navigation_strategy", "result_observation_checks",
)


class RecipeRejected(ValueError):
    """A recipe candidate violated a non-bypassable sanitation rule."""


@dataclass
class SiteRecipe:
    company: str
    official_domain: str = ""
    source_identity: str = ""
    career_entry_pattern: str = ""
    search_url_template: str = ""
    query_param: str = ""
    location_param: str = ""
    detail_url_pattern: str = ""
    navigation_strategy: str = "canonical_href_then_direct_navigation"
    result_observation_checks: tuple[str, ...] = ()
    provenance_run_id: str = ""
    provenance_task_id: str = ""
    verified_at: str = ""
    status: str = STATUS_CANDIDATE
    version: int = 1
    supersedes_version: Optional[int] = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["result_observation_checks"] = list(self.result_observation_checks)
        return d


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_official_host(host: str, official_domain: str) -> bool:
    if not host or not official_domain:
        return False
    dom = official_domain.lower().lstrip(".")
    return host == dom or host.endswith("." + dom)


def sanitize_recipe(candidate: dict) -> SiteRecipe:
    """Validate + normalize a recipe candidate or raise :class:`RecipeRejected`.

    Rejects: a specific job ID, credentials/cookies/tokens, raw executable
    JavaScript, non-official hosts, unbounded actions, and apply/submit behavior.
    """
    if not isinstance(candidate, dict):
        raise RecipeRejected("recipe candidate is not an object")
    company = str(candidate.get("company") or "").strip()
    official = str(candidate.get("official_domain") or "").strip().lower().lstrip(".")
    if not company:
        raise RecipeRejected("recipe missing company")
    if not official:
        raise RecipeRejected("recipe missing official_domain")

    blob = json.dumps(candidate, ensure_ascii=False).lower()

    for key in _SECRET_KEYS:
        if key in candidate or f'"{key}"' in blob:
            raise RecipeRejected(f"recipe carries forbidden secret-like field: {key}")
    for key in _JOB_ID_KEYS:
        if candidate.get(key):
            raise RecipeRejected(f"recipe is job-specific (contains {key})")
    for marker in _JS_MARKERS:
        if marker in blob:
            raise RecipeRejected(f"recipe contains raw executable JavaScript ({marker!r})")

    # A search template with an embedded concrete numeric job id is job-specific.
    for tmpl_key in ("search_url_template", "detail_url_pattern", "career_entry_pattern"):
        val = str(candidate.get(tmpl_key) or "")
        if re.search(r"/\d{5,}\b", val):
            raise RecipeRejected(f"{tmpl_key} embeds a specific job id")

    # apply/submit behavior is never stored.
    nav = str(candidate.get("navigation_strategy") or "").lower()
    checks_blob = " ".join(str(c) for c in (candidate.get("result_observation_checks") or [])).lower()
    for marker in _APPLY_MARKERS:
        if marker in nav or marker in checks_blob:
            raise RecipeRejected("recipe encodes apply/submit behavior")

    # Every URL-bearing field must be on the official host (when absolute).
    for url_key in ("search_url_template", "detail_url_pattern", "career_entry_pattern"):
        val = str(candidate.get(url_key) or "")
        if val.startswith("http"):
            host = _host_of(val)
            if not _is_official_host(host, official):
                raise RecipeRejected(f"{url_key} host {host!r} is not on official domain {official!r}")

    # Bound the action surface: no free-form action list, only declarative fields.
    unexpected = set(candidate) - set(_RECIPE_FIELDS) - {
        "provenance_run_id", "provenance_task_id", "verified_at", "status",
        "version", "supersedes_version",
    }
    if "actions" in candidate or "steps" in candidate or "script" in candidate:
        raise RecipeRejected("recipe encodes unbounded actions/steps/script")

    checks = tuple(str(c) for c in (candidate.get("result_observation_checks") or ()))
    return SiteRecipe(
        company=company,
        official_domain=official,
        source_identity=str(candidate.get("source_identity") or ""),
        career_entry_pattern=str(candidate.get("career_entry_pattern") or ""),
        search_url_template=str(candidate.get("search_url_template") or ""),
        query_param=str(candidate.get("query_param") or ""),
        location_param=str(candidate.get("location_param") or ""),
        detail_url_pattern=str(candidate.get("detail_url_pattern") or ""),
        navigation_strategy=str(candidate.get("navigation_strategy") or "canonical_href_then_direct_navigation"),
        result_observation_checks=checks,
        provenance_run_id=str(candidate.get("provenance_run_id") or ""),
        provenance_task_id=str(candidate.get("provenance_task_id") or ""),
    )


class RecipeStore:
    """Append-only JSONL recipe store. Newest validated version wins on read."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (ValueError, TypeError):
                continue
        return out

    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _versions_for(self, company: str, official_domain: str) -> list[dict]:
        c = company.strip().lower()
        d = official_domain.strip().lower().lstrip(".")
        return [
            r for r in self._read_all()
            if str(r.get("company", "")).lower() == c
            and str(r.get("official_domain", "")).lower().lstrip(".") == d
        ]

    def add_candidate(self, candidate: dict) -> SiteRecipe:
        recipe = sanitize_recipe(candidate)
        existing = self._versions_for(recipe.company, recipe.official_domain)
        recipe.version = (max((int(r.get("version", 0)) for r in existing), default=0) + 1)
        recipe.status = STATUS_CANDIDATE
        recipe.verified_at = ""
        self._append(recipe.to_dict())
        return recipe

    def promote(self, company: str, official_domain: str, version: int, *, run_id: str = "") -> SiteRecipe:
        """Promote a candidate version to VALIDATED (append-only supersede)."""
        versions = self._versions_for(company, official_domain)
        match = next((r for r in versions if int(r.get("version", 0)) == version), None)
        if match is None:
            raise RecipeRejected(f"no recipe version {version} for {company}/{official_domain}")
        promoted = sanitize_recipe(match)
        promoted.version = version
        promoted.status = STATUS_VALIDATED
        promoted.verified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        promoted.provenance_run_id = run_id or match.get("provenance_run_id", "")
        self._append(promoted.to_dict())
        return promoted

    def latest_validated(self, company: str, official_domain: str) -> Optional[SiteRecipe]:
        versions = [r for r in self._versions_for(company, official_domain)
                    if r.get("status") == STATUS_VALIDATED]
        if not versions:
            return None
        best = max(versions, key=lambda r: int(r.get("version", 0)))
        return sanitize_recipe(best) if best else None

    def add_drift_version(self, candidate: dict) -> SiteRecipe:
        """Recipe drift => a NEW candidate version, never an in-place edit."""
        return self.add_candidate(candidate)


__all__ = [
    "STATUS_CANDIDATE", "STATUS_VALIDATED", "STATUS_SUPERSEDED",
    "RecipeRejected", "SiteRecipe", "sanitize_recipe", "RecipeStore",
]
