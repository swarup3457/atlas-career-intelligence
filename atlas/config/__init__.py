"""Atlas configuration layer.

Centralizes every configurable path/setting so the rest of the codebase
never hard-codes Windows paths or machine-specific values. Settings can be
overridden via environment variables (prefixed ``ATLAS_``) or a
``config/local.yaml`` file, falling back to sensible defaults for this
machine.

Precedence (highest wins):
    1. Explicit keyword arguments passed to :func:`load_settings`.
    2. Environment variables (``ATLAS_<FIELD_NAME_UPPER>``).
    3. ``C:\\Atlas\\config\\local.yaml`` (optional, git-ignored).
    4. ``C:\\Atlas\\config\\default.yaml`` (checked in, safe defaults).
    5. Hard-coded fallback defaults in this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

import yaml

_THIS_FILE = Path(__file__).resolve()
_PROJECT_ROOT_DEFAULT = _THIS_FILE.parents[2]  # C:\Atlas
_CONFIG_DIR_DEFAULT = _PROJECT_ROOT_DEFAULT / "config"
_ENV_PREFIX = "ATLAS_"

ALLOWED_BROWSER_CHANNELS = frozenset({"chrome", "chromium", "msedge", "chrome-beta", "msedge-beta"})
ALLOWED_CONTROLLERS = frozenset({"none", "copilot", "codex"})
# Copilot account-type acknowledgement for the private-candidate consent gate
# (Phase 1E/F §7). "unspecified" means the operator has NOT acknowledged whether
# the Copilot account is a personal or organization-managed one, which — on its
# own — is never sufficient to release real candidate PII to a reasoning model.
ALLOWED_COPILOT_ACCOUNT_TYPES = frozenset({"unspecified", "personal", "organization"})


class ConfigValidationError(ValueError):
    """Raised when Settings fail validation. Message lists EVERY problem
    found (not just the first) so a single fix-and-rerun cycle can
    address all of them."""


@dataclass(frozen=True)
class Settings:
    """Immutable, fully-resolved Atlas configuration."""

    project_root: Path = _PROJECT_ROOT_DEFAULT
    browser_channel: str = "chrome"
    browser_profile: Path = _PROJECT_ROOT_DEFAULT / ".browser-profile-chrome"
    state_db: Path = _PROJECT_ROOT_DEFAULT / "state" / "atlas_state.sqlite"
    checkpoint_db: Path = _PROJECT_ROOT_DEFAULT / "state" / "atlas_checkpoints.sqlite"
    output_dir: Path = _PROJECT_ROOT_DEFAULT / "output"
    logs_dir: Path = _PROJECT_ROOT_DEFAULT / "logs"
    agents_dir: Path = _PROJECT_ROOT_DEFAULT / "agents"
    skills_dir: Path = _PROJECT_ROOT_DEFAULT / "skills"

    # Stable production output contract (Phase 1E/F §10). Every live run
    # publishes an immutable run directory under this root and atomically
    # updates a ``latest`` pointer. Git-ignored (under output/ by default).
    production_output_root: Path = _PROJECT_ROOT_DEFAULT / "output" / "production"

    # Browser execution policy (see docs/BROWSER_POLICY.md).
    default_headless: bool = True
    navigation_timeout_ms: int = 30000

    # Retry policy (see atlas/orchestration/retry.py).
    retry_budget: int = 2

    # Orchestration.
    batch_size: int = 10

    # Controller abstraction: "none" (deterministic/no-LLM), "copilot",
    # "codex". See atlas/controllers/.
    controller: str = "none"

    # Optional Copilot SDK reasoning controller settings (Phase 1E/F §7). The
    # controller is DISABLED by default; deterministic runs never call a model.
    # When enabled, Atlas prefers ``controller_model`` if the account exposes
    # it, otherwise an explicitly-configured available model.
    controller_model: str = "claude-opus-4.8"
    controller_max_session_credits: int = 50
    controller_session_timeout_s: int = 60
    controller_retry_budget: int = 1

    # Private-candidate consent gate (Phase 1E/F §7). BOTH must hold before the
    # real candidate profile may be sent to a reasoning model: this flag AND an
    # explicit account-type acknowledgement (``copilot_account_type`` !=
    # "unspecified"). Default OFF — deterministic matching and a synthetic
    # canary only.
    allow_private_candidate_to_copilot: bool = False
    copilot_account_type: str = "unspecified"

    def ensure_directories(self) -> None:
        """Create all managed directories if they do not yet exist."""
        for path in (
            self.state_db.parent,
            self.checkpoint_db.parent,
            self.output_dir,
            self.logs_dir,
            self.browser_profile,
            self.agents_dir,
            self.skills_dir,
            self.production_output_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        """Fail fast with a single, complete list of every configuration
        problem found (never just the first). Called automatically by
        :func:`load_settings`.
        """
        problems: list[str] = []

        if not str(self.project_root).strip():
            problems.append("project_root must not be empty.")
        elif not self.project_root.exists():
            problems.append(f"project_root does not exist: {self.project_root}")
        elif not self.project_root.is_dir():
            problems.append(f"project_root is not a directory: {self.project_root}")

        if not self.browser_channel or not self.browser_channel.strip():
            problems.append("browser_channel must not be empty.")
        elif self.browser_channel not in ALLOWED_BROWSER_CHANNELS:
            problems.append(
                f"browser_channel '{self.browser_channel}' is not one of the supported "
                f"channels {sorted(ALLOWED_BROWSER_CHANNELS)}."
            )

        for label, path in (
            ("browser_profile", self.browser_profile),
            ("state_db", self.state_db),
            ("checkpoint_db", self.checkpoint_db),
            ("output_dir", self.output_dir),
            ("logs_dir", self.logs_dir),
            ("agents_dir", self.agents_dir),
            ("skills_dir", self.skills_dir),
            ("production_output_root", self.production_output_root),
        ):
            if not str(path).strip():
                problems.append(f"{label} must not be empty.")
            elif not path.is_absolute():
                problems.append(f"{label} must be an absolute path, got: {path}")

        if self.retry_budget < 0:
            problems.append(f"retry_budget must be >= 0, got {self.retry_budget}.")
        if self.batch_size < 1:
            problems.append(f"batch_size must be >= 1, got {self.batch_size}.")
        if self.navigation_timeout_ms <= 0:
            problems.append(f"navigation_timeout_ms must be > 0, got {self.navigation_timeout_ms}.")

        if self.controller not in ALLOWED_CONTROLLERS:
            problems.append(
                f"controller '{self.controller}' is not one of the supported values "
                f"{sorted(ALLOWED_CONTROLLERS)}."
            )
        if self.controller_max_session_credits < 0:
            problems.append(
                f"controller_max_session_credits must be >= 0, got {self.controller_max_session_credits}."
            )
        if self.controller_session_timeout_s <= 0:
            problems.append(
                f"controller_session_timeout_s must be > 0, got {self.controller_session_timeout_s}."
            )
        if self.controller_retry_budget < 0:
            problems.append(
                f"controller_retry_budget must be >= 0, got {self.controller_retry_budget}."
            )
        if self.copilot_account_type not in ALLOWED_COPILOT_ACCOUNT_TYPES:
            problems.append(
                f"copilot_account_type '{self.copilot_account_type}' is not one of "
                f"{sorted(ALLOWED_COPILOT_ACCOUNT_TYPES)}."
            )

        if problems:
            joined = "\n  - ".join(problems)
            raise ConfigValidationError(f"Invalid Atlas configuration:\n  - {joined}")


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping.")
    return data


def _coerce(field_name: str, raw_type: type, value: Any) -> Any:
    if value is None:
        return None
    if raw_type is Path:
        return Path(str(value)).resolve() if not isinstance(value, Path) else value
    if raw_type is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if raw_type is int:
        return int(value)
    return value


def load_settings(
    config_dir: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    **overrides: Any,
) -> Settings:
    """Resolve :class:`Settings` from defaults, YAML files, env vars, and
    explicit overrides (in that increasing order of precedence)."""
    config_dir = config_dir or _CONFIG_DIR_DEFAULT
    env = os.environ if env is None else env

    merged: dict[str, Any] = {}
    merged.update(_load_yaml(config_dir / "default.yaml"))
    merged.update(_load_yaml(config_dir / "local.yaml"))

    type_map = {f.name: f.type for f in fields(Settings)}
    # Resolve string type names to real types for coercion (dataclass field.type
    # may be a string when `from __future__ import annotations` is active).
    resolved_types: dict[str, type] = {}
    for f in fields(Settings):
        if f.type in ("Path",):
            resolved_types[f.name] = Path
        elif f.type in ("bool",):
            resolved_types[f.name] = bool
        elif f.type in ("int",):
            resolved_types[f.name] = int
        else:
            resolved_types[f.name] = str

    for key in type_map:
        env_key = f"{_ENV_PREFIX}{key.upper()}"
        if env_key in env:
            merged[key] = env[env_key]

    for key, value in overrides.items():
        merged[key] = value

    kwargs: dict[str, Any] = {}
    for key, raw_type in resolved_types.items():
        if key in merged:
            kwargs[key] = _coerce(key, raw_type, merged[key])

    settings = Settings(**kwargs)
    settings.validate()
    return settings


_default_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Return the process-wide default Settings, loading it once (lazily)."""
    global _default_settings
    if _default_settings is None:
        _default_settings = load_settings()
    return _default_settings


def reset_settings_cache() -> None:
    """Clear the cached default settings (primarily for tests)."""
    global _default_settings
    _default_settings = None
