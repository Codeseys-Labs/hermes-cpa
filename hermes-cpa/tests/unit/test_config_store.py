from hermes_cpa.config_store import ConfigStore


def test_initial_config_snapshot_and_in_place_rollback(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    store = ConfigStore(cfg_file)
    store.create_initial_config(8318, access_keys=["generated-client-key"],
                                management_key="generated-public-mgmt")

    raw = cfg_file.read_text()
    assert "generated-public-mgmt" not in raw
    assert "$2b$" in raw or "$2a$" in raw
    assert "generated-client-key" in raw

    snap = store.snapshot()
    rev1 = store.compute_digest()
    inode = cfg_file.stat().st_ino
    store._write_in_place(b"config-version: 8\nrouting: {strategy: fill-first}\n")
    assert store.compute_digest() != rev1
    assert cfg_file.stat().st_ino == inode

    store.rollback(snap)
    assert store.compute_digest() == rev1
    assert cfg_file.stat().st_ino == inode
    assert store.read_config()["routing"]["strategy"] == "round-robin"
