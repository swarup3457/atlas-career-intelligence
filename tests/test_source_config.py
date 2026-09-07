"""Phase 1A: source configuration validation tests."""

from __future__ import annotations

import pytest

from atlas.sources.config import SourceConfigError, demo_source_config, load_source_config, looks_like_secret_value
from atlas.sources.models import SourceType

pytestmark = pytest.mark.unit


def _entry(**kw):
    base = {"instance_id": "i1", "source_type": "ATS_WORKDAY"}
    base.update(kw)
    return {"instances": [base]}


def test_valid_config_builds_instances():
    cfg = load_source_config(
        _entry(display_name="Acme", base_url="https://acme.wd1.myworkdayjobs.com", tenant="acme",
               auth_ref="ACME_TOKEN", capability_overrides=["DETAIL"],
               rate_policy={"min_interval_seconds": 1.0, "max_concurrency": 2})
    )
    assert len(cfg.instances) == 1
    assert cfg.policy_for("i1").max_concurrency == 2


def test_duplicate_instance_id_rejected():
    raw = {"instances": [{"instance_id": "dup", "source_type": "PORTAL_LARGE"},
                         {"instance_id": "dup", "source_type": "PORTAL_LARGE"}]}
    with pytest.raises(SourceConfigError):
        load_source_config(raw)


def test_unknown_source_type_rejected():
    with pytest.raises(SourceConfigError):
        load_source_config(_entry(source_type="ATS_NOPE"))


def test_bad_url_rejected():
    with pytest.raises(SourceConfigError):
        load_source_config(_entry(base_url="not-a-url"))


def test_negative_rate_rejected():
    with pytest.raises(SourceConfigError):
        load_source_config(_entry(rate_policy={"min_interval_seconds": -1}))


def test_zero_concurrency_rejected():
    with pytest.raises(SourceConfigError):
        load_source_config(_entry(rate_policy={"max_concurrency": 0}))


def test_forbidden_secret_key_rejected():
    with pytest.raises(SourceConfigError):
        load_source_config(_entry(password="hunter2"))
    with pytest.raises(SourceConfigError):
        load_source_config(_entry(token="abc123"))


def test_auth_ref_secret_value_rejected():
    with pytest.raises(SourceConfigError):
        load_source_config(_entry(auth_ref="Bearer sk-abcdef123456"))


def test_unsupported_capability_override_rejected():
    with pytest.raises(SourceConfigError):
        load_source_config(_entry(capability_overrides=["TELEPORT"]))


def test_error_message_aggregates_all_problems():
    raw = {"instances": [{"instance_id": "", "source_type": "NOPE", "base_url": "bad"}]}
    with pytest.raises(SourceConfigError) as exc:
        load_source_config(raw)
    msg = str(exc.value)
    assert "instance_id" in msg and "source_type" in msg and "base_url" in msg


def test_require_registered_flag():
    from atlas.sources.registry import SourceRegistry
    reg = SourceRegistry()
    with pytest.raises(SourceConfigError):
        load_source_config(_entry(source_type="ATS_LEVER"), registry=reg, require_registered=True)


def test_demo_config_is_valid_and_credential_free():
    cfg = demo_source_config()
    assert {i.instance_id for i in cfg.instances} == {"fake-a", "fake-b", "fixture-a"}
    for inst in cfg.instances:
        assert inst.auth_ref is None


def test_looks_like_secret_value_heuristics():
    assert looks_like_secret_value("Bearer abc123def456")
    assert looks_like_secret_value("ghp_" + "a" * 36)
    assert not looks_like_secret_value("ACME_WORKDAY_TOKEN")
