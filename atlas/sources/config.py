"""Atlas source configuration + validation (Phase 1A).

Builds validated :class:`SourceInstance` objects (and their optional
:class:`RatePolicy`) from a plain mapping (e.g. parsed YAML/JSON). No real
candidate/company sources are defined here — only a safe fixture/demo
configuration for tests and the validation rules every future config must
pass.

Security rule: credentials are NEVER stored in source config. Only an
``auth_ref`` (a *reference* such as an env-var name) is allowed; a literal
secret value — or any forbidden secret key — is rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from atlas.sources.models import Capability, SourceInstance, SourceType
from atlas.sources.rate_limit import RatePolicy, RateLimitConfigError
from atlas.sources.registry import SourceRegistry


class SourceConfigError(ValueError):
    """Raised when source configuration is invalid. The message lists EVERY
    problem found (not just the first), matching atlas.config style."""


# Keys that must never appear in a source config entry — they would embed a
# secret directly instead of a reference.
_FORBIDDEN_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "auth_value",
        "cookie",
        "cookies",
        "authorization",
        "access_token",
        "refresh_token",
        "private_key",
        "session",
    }
)

# Heuristic patterns that indicate a *value* is a literal secret rather than
# a reference. Used to guard auth_ref specifically.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^bearer\s+", re.IGNORECASE),
    re.compile(r"^basic\s+[A-Za-z0-9+/=]{8,}", re.IGNORECASE),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"^eyJ[A-Za-z0-9_-]{10,}\."),  # JWT-ish
    re.compile(r"^xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack
)


def looks_like_secret_value(value: str) -> bool:
    """Deterministic heuristic: does ``value`` look like an embedded secret
    rather than a reference (env-var name)? Never logs the value."""
    if not value:
        return False
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            return True
    # A plausible env-var reference is short-ish and has no whitespace.
    if len(value) > 128:
        return True
    return False


@dataclass
class SourceConfigResult:
    instances: tuple[SourceInstance, ...] = ()
    policies: Mapping[str, RatePolicy] = field(default_factory=dict)

    def policy_for(self, instance_id: str) -> RatePolicy:
        return self.policies.get(instance_id, RatePolicy())


def _valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _build_policy(raw: Mapping[str, Any], problems: list[str], where: str) -> RatePolicy:
    kwargs: dict[str, Any] = {}
    for key in ("min_interval_seconds", "max_concurrency", "backoff_initial_seconds",
                "backoff_max_seconds", "backoff_multiplier"):
        if key in raw:
            kwargs[key] = raw[key]
    try:
        policy = RatePolicy(**kwargs)
        policy.validate()
        return policy
    except (RateLimitConfigError, TypeError, ValueError) as exc:
        problems.append(f"{where}: invalid rate_policy: {exc}")
        return RatePolicy()


def load_source_config(
    raw: Mapping[str, Any],
    *,
    registry: Optional[SourceRegistry] = None,
    require_registered: bool = False,
) -> SourceConfigResult:
    """Validate and build source instances from a raw mapping.

    Raises :class:`SourceConfigError` (aggregating every problem) if the
    config is invalid. When ``require_registered`` is set and a ``registry``
    is given, each instance's source type must have a registered adapter.
    """
    problems: list[str] = []
    entries = raw.get("instances", []) if isinstance(raw, Mapping) else []
    if not isinstance(entries, list):
        raise SourceConfigError("source config 'instances' must be a list")

    instances: list[SourceInstance] = []
    policies: dict[str, RatePolicy] = {}
    seen_ids: set[str] = set()

    for idx, entry in enumerate(entries):
        where = f"instances[{idx}]"
        if not isinstance(entry, Mapping):
            problems.append(f"{where}: must be a mapping")
            continue

        instance_id = entry.get("instance_id")
        if not instance_id or not str(instance_id).strip():
            problems.append(f"{where}: missing instance_id")
            instance_id = None
        else:
            instance_id = str(instance_id)
            if instance_id in seen_ids:
                problems.append(f"{where}: duplicate instance_id {instance_id!r}")
            seen_ids.add(instance_id)

        # Forbidden secret keys anywhere in the entry.
        for key in entry:
            if str(key).lower() in _FORBIDDEN_SECRET_KEYS:
                problems.append(
                    f"{where}: forbidden secret key {key!r} — use auth_ref (a reference) only, "
                    "never an embedded credential value"
                )

        # Source type.
        source_type_raw = entry.get("source_type")
        source_type: Optional[SourceType] = None
        try:
            source_type = SourceType(str(source_type_raw))
        except ValueError:
            problems.append(f"{where}: unknown source_type {source_type_raw!r}")
        if source_type is not None and require_registered and registry is not None:
            if not registry.is_registered(source_type):
                problems.append(
                    f"{where}: no adapter registered for source_type {source_type.value}"
                )

        # base_url.
        base_url = entry.get("base_url")
        if base_url is not None and not _valid_url(str(base_url)):
            problems.append(f"{where}: bad base_url {base_url!r} (must be http(s) with a host)")

        # auth_ref must be a reference, not a secret value.
        auth_ref = entry.get("auth_ref")
        if auth_ref is not None:
            if not isinstance(auth_ref, str) or not auth_ref.strip():
                problems.append(f"{where}: auth_ref must be a non-empty reference string")
            elif looks_like_secret_value(auth_ref):
                problems.append(
                    f"{where}: auth_ref looks like an embedded secret value; it must be a "
                    "reference (e.g. an environment variable name)"
                )

        # Capability overrides.
        overrides_raw = entry.get("capability_overrides", []) or []
        overrides: set[Capability] = set()
        if not isinstance(overrides_raw, (list, tuple)):
            problems.append(f"{where}: capability_overrides must be a list")
        else:
            for cap in overrides_raw:
                try:
                    overrides.add(Capability(str(cap)))
                except ValueError:
                    problems.append(f"{where}: unsupported capability override {cap!r}")

        # Rate policy.
        rate_raw = entry.get("rate_policy")
        if rate_raw is not None:
            if not isinstance(rate_raw, Mapping):
                problems.append(f"{where}: rate_policy must be a mapping")
            elif instance_id is not None:
                policies[instance_id] = _build_policy(rate_raw, problems, where)

        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            problems.append(f"{where}: enabled must be a boolean")

        if instance_id is not None and source_type is not None:
            instances.append(
                SourceInstance(
                    instance_id=instance_id,
                    source_type=source_type,
                    display_name=str(entry.get("display_name", "")),
                    base_url=str(base_url) if base_url is not None else None,
                    tenant=str(entry["tenant"]) if entry.get("tenant") is not None else None,
                    company_id=str(entry["company_id"]) if entry.get("company_id") is not None else None,
                    enabled=bool(enabled),
                    capability_overrides=frozenset(overrides),
                    rate_policy=str(entry["rate_policy_ref"]) if entry.get("rate_policy_ref") else None,
                    auth_ref=str(auth_ref) if auth_ref is not None else None,
                    metadata=dict(entry.get("metadata", {})),
                )
            )

    if problems:
        joined = "\n  - ".join(problems)
        raise SourceConfigError(f"Invalid Atlas source configuration:\n  - {joined}")

    return SourceConfigResult(instances=tuple(instances), policies=policies)


def demo_source_config() -> SourceConfigResult:
    """A safe, credential-free fixture/demo configuration (test doubles
    only). Contains NO real company/candidate sources."""
    raw = {
        "instances": [
            {
                "instance_id": "fake-a",
                "source_type": SourceType.FAKE.value,
                "display_name": "Fake Source A",
                "rate_policy": {"min_interval_seconds": 0.0, "max_concurrency": 4},
            },
            {
                "instance_id": "fake-b",
                "source_type": SourceType.FAKE.value,
                "display_name": "Fake Source B",
                "enabled": True,
            },
            {
                "instance_id": "fixture-a",
                "source_type": SourceType.FIXTURE.value,
                "display_name": "Fixture Source A",
            },
        ]
    }
    # NOTE: two FAKE instances share the FAKE family — that is expected;
    # multiple *instances* of one family are fine (one adapter *class* per
    # family, many configured instances).
    return load_source_config(raw)


__all__ = [
    "SourceConfigError",
    "SourceConfigResult",
    "looks_like_secret_value",
    "load_source_config",
    "demo_source_config",
]
