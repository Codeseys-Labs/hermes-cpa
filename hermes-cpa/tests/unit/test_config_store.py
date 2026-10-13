import pytest
from hermes_cpa.config_store import ConfigStore

def test_config_store_initial_and_rollback(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    store = ConfigStore(cfg_file)
    store.create_initial_config(8318, "super-secret-mgmt", "client-inf-key")

    assert cfg_file.exists()
    raw = cfg_file.read_text()
    assert "super-secret-mgmt" not in raw
    assert "$2b$" in raw or "$2a$" in raw
    assert "client-inf-key" in raw

    snap = store.snapshot()
    assert snap.exists()
    rev1 = store.compute_digest()

    # Apply safe changes
    ok, rev2, code = store.apply_safe_changes(rev1, {"strategy": "fill-first", "request_retry": 5})
    assert ok is True
    assert rev2 != rev1
    data = store.read_config()
    assert data["routing"]["strategy"] == "fill-first"
    assert data["routing"]["retry"]["request-retry"] == 5

    # Test stale revision rejection
    ok_stale, _, code_stale = store.apply_safe_changes("old-rev", {"strategy": "round-robin"})
    assert ok_stale is False
    assert code_stale == "STALE_REVISION"

    # Test rollback
    store.rollback(snap)
    assert store.compute_digest() == rev1
    assert store.read_config()["routing"]["strategy"] == "round-robin"
