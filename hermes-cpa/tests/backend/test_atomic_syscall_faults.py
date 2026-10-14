"""Actual write/fsync/replace syscall fault boundaries retain truthful recovery state."""
import errno
import json
import os
import stat

import pytest
from hermes_cpa.config_store import ConfigStore
from test_transactions import Owner, PORT


@pytest.mark.parametrize('target,boundary', [
    ('snapshot', 'file_fsync'), ('snapshot', 'replace'), ('snapshot', 'directory_fsync'),
    ('journal', 'file_fsync'), ('journal', 'replace'), ('journal', 'directory_fsync'),
    ('config', 'write'), ('config', 'file_fsync'),
])
def test_enospc_at_real_write_boundary_is_not_success(tmp_path, monkeypatch, target, boundary):
    store = ConfigStore(tmp_path / 'config.yaml')
    store.create_initial_config(18318, access_keys=['generated-public-client-key'])
    before = store.config_path.read_bytes()
    revision = store.compute_digest()
    owner = Owner(store)
    owner.running = False
    atomic, in_place = store._atomic, store._write_in_place
    fsync, replace, write = os.fsync, os.replace, os.write
    active = [False]
    injected = [False]

    def fail_once():
        if active[0] and not injected[0]:
            injected[0] = True
            raise OSError(errno.ENOSPC, 'GENERATED_PRIVATE_DISK_CANARY')

    def atomic_scope(path, raw):
        active[0] = (target == 'snapshot' and path.suffix == '.snapshot' or
                     target == 'journal' and path.suffix == '.journal')
        try:
            return atomic(path, raw)
        finally:
            active[0] = False

    def in_place_scope(raw):
        active[0] = target == 'config'
        try:
            return in_place(raw)
        finally:
            active[0] = False

    def faulty_fsync(fd):
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        if boundary == ('directory_fsync' if is_dir else 'file_fsync'):
            fail_once()
        return fsync(fd)

    def faulty_replace(src, dst):
        if boundary == 'replace':
            fail_once()
        return replace(src, dst)

    def faulty_write(fd, data):
        if boundary == 'write' and active[0] and not injected[0]:
            write(fd, bytes(data[:16]))  # A short write before the device fills.
            fail_once()
        return write(fd, data)

    monkeypatch.setattr(store, '_atomic', atomic_scope)
    monkeypatch.setattr(store, '_write_in_place', in_place_scope)
    monkeypatch.setattr(os, 'fsync', faulty_fsync)
    monkeypatch.setattr(os, 'replace', faulty_replace)
    monkeypatch.setattr(os, 'write', faulty_write)
    result = store.transact(revision, PORT, expected_generation=owner.generation, owner=owner)
    monkeypatch.undo()
    assert injected[0] and not result['ok']
    assert 'CANARY' not in json.dumps(result)
    assert not owner.running
    assert store.config_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.cpa-*'))
    if target != 'config':
        assert store.config_path.read_bytes() == before
        return
    journal = json.loads(store.config_path.with_suffix('.journal').read_bytes())
    if boundary == 'file_fsync':
        # Bytes were written but durability is unproven: restore the exact snapshot.
        assert result['rollback'] == 'recovered'
        assert store.config_path.read_bytes() == before
        assert journal['phase'] == 'recovered'
    else:
        # A torn document is neither base nor candidate: never guess, keep blocking.
        assert result['rollback'] == 'drift_detected'
        assert journal['phase'] == 'applying'
        restored = store.recover(expected_revision=store.compute_digest(),
            snapshot_revision=revision, expected_generation=owner.generation,
            owner=owner, native_consent=True)
        assert restored['ok'] and store.config_path.read_bytes() == before
