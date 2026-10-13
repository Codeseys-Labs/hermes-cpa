import pytest
from hermes_cpa.contracts import (
    LifecycleState, ErrorCode, ApiResponseEnvelope, StatusData,
    ConfigProjection, ConfigApplyRequest
)

def test_lifecycle_states():
    assert LifecycleState.READY.value == "ready"
    assert LifecycleState.STOPPED.value == "stopped"
    assert LifecycleState.FAILED.value == "failed"

def test_api_envelope():
    env = ApiResponseEnvelope(
        scope_id="default",
        generation=1,
        revision="abc123",
        data={"ok": True}
    )
    assert env.api_version == 1
    assert env.scope_id == "default"
    assert env.data["ok"] is True

def test_config_apply_validation():
    req = ConfigApplyRequest(
        expected_revision="a" * 64,
        expected_generation=1,
        changes={"strategy": "fill-first"},
        restart_consent=True
    )
    assert req.changes["strategy"] == "fill-first"
