"""Phase 0.95 — sanitized support bundle. Asserts the zip member list is
safe (no secret-looking path fragments) and that secret-like log lines are
redacted out of the log tail. Offline, tmp_path only.
"""

from __future__ import annotations

import datetime
import zipfile

import pytest

from atlas.backup.clock import FixedClock
from atlas.backup.support_bundle import (
    DOCTOR_NAME,
    ERROR_SUMMARY_NAME,
    INFO_NAME,
    LOG_TAIL_NAME,
    RUN_MANIFEST_NAME,
    create_support_bundle,
)
from tests._phase095_helpers import make_settings

pytestmark = [pytest.mark.integration]

_FIXED = datetime.datetime(2026, 9, 6, 18, 0, 0, tzinfo=datetime.timezone.utc)
_FORBIDDEN_FRAGMENTS = (".browser-profile", "cookies", "cookie", "token", "password", "secret", "credential")


def _seed_log(settings, lines):
    (settings.logs_dir / "atlas.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_support_bundle_member_list_is_safe(tmp_path):
    settings = make_settings(tmp_path)
    _seed_log(settings, ["INFO atlas: started", "ERROR atlas: something broke"])
    path = create_support_bundle(settings, tmp_path / "bundles", clock=FixedClock(_FIXED))

    assert path.exists() and path.suffix == ".zip"
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()

    # only the known-safe members
    assert set(names) <= {INFO_NAME, DOCTOR_NAME, LOG_TAIL_NAME, ERROR_SUMMARY_NAME, RUN_MANIFEST_NAME}
    assert INFO_NAME in names and DOCTOR_NAME in names and LOG_TAIL_NAME in names

    # no member NAME contains a forbidden fragment
    for name in names:
        low = name.lower()
        for frag in _FORBIDDEN_FRAGMENTS:
            assert frag not in low, f"forbidden fragment {frag!r} in member name {name!r}"


def test_support_bundle_redacts_secret_log_lines(tmp_path):
    settings = make_settings(tmp_path)
    _seed_log(
        settings,
        [
            "INFO atlas: normal line one",
            "INFO atlas: auth_token=SUPERSECRETVALUE123",
            'WARNING atlas: {"password": "hunter2"}',
            "ERROR atlas: boom happened",
            "INFO atlas: session_cookie=abcdef",
        ],
    )
    path = create_support_bundle(settings, tmp_path / "bundles", clock=FixedClock(_FIXED))

    with zipfile.ZipFile(path) as zf:
        log_tail = zf.read(LOG_TAIL_NAME).decode("utf-8")
        error_summary = zf.read(ERROR_SUMMARY_NAME).decode("utf-8")
        info = zf.read(INFO_NAME).decode("utf-8")

    # secret VALUES never appear
    assert "SUPERSECRETVALUE123" not in log_tail
    assert "hunter2" not in log_tail
    assert "abcdef" not in log_tail
    # the innocuous line survives; the error line is summarized
    assert "normal line one" in log_tail
    assert "boom happened" in error_summary
    # info.json carries a fingerprint, never raw config secrets
    assert "config_fingerprint" in info


def test_support_bundle_includes_recent_run_manifest(tmp_path):
    settings = make_settings(tmp_path)
    _seed_log(settings, ["INFO atlas: hi"])
    (settings.output_dir / "run_manifest_abc.json").write_text('{"run_id": "abc"}', encoding="utf-8")
    path = create_support_bundle(settings, tmp_path / "bundles", clock=FixedClock(_FIXED))
    with zipfile.ZipFile(path) as zf:
        assert RUN_MANIFEST_NAME in zf.namelist()
        assert "abc" in zf.read(RUN_MANIFEST_NAME).decode("utf-8")


def test_support_bundle_atomic_no_part_leftover(tmp_path):
    settings = make_settings(tmp_path)
    _seed_log(settings, ["INFO atlas: hi"])
    out = tmp_path / "bundles"
    create_support_bundle(settings, out, clock=FixedClock(_FIXED))
    assert not any(p.name.endswith(".part") for p in out.iterdir())
