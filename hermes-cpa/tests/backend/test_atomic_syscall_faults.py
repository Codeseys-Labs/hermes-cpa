"""Actual fsync/replace syscall fault boundaries retain truthful recovery state."""
import errno
import json
import os
import stat
import pytest
from hermes_cpa.config_store import ConfigStore
from test_transactions import Owner


@pytest.mark.parametrize('target', ['snapshot', 'journal', 'config'])
@pytest.mark.parametrize('boundary', ['file_fsync', 'replace', 'directory_fsync'])
def test_enospc_at_real_atomic_boundary_is_not_success(tmp_path, monkeypatch, target, boundary):
    store = ConfigStore(tmp_path / 'config.yaml')
    store.create_initial_config(18318, 'generated-management', 'generated-inference')
    before = store.config_path.read_bytes()
    revision = store.compute_digest()
    owner = Owner()
    owner.running = False
    atomic, fsync, replace = store._atomic, os.fsync, os.replace
    active = [False]
    injected = [False]
    def fail_once():
        if active[0] and not injected[0]:
            injected[0] = True
            raise OSError(errno.ENOSPC, 'GENERATED_PRIVATE_DISK_CANARY')
    def atomic_scope(path, raw):
        active[0] = (target == 'config' and path == store.config_path or
                     target == 'snapshot' and path.suffix == '.snapshot' or
                     target == 'journal' and path.suffix == '.journal')
        try:
            return atomic(path, raw)
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
    monkeypatch.setattr(store, '_atomic', atomic_scope)
    monkeypatch.setattr(os, 'fsync', faulty_fsync)
    monkeypatch.setattr(os, 'replace', faulty_replace)
    result = store.transact(revision, {'request_retry': 4}, expected_generation=owner.generation,
                            owner=owner)
    assert injected[0] and not result['ok']
    assert 'CANARY' not in json.dumps(result)
    assert not owner.running
    if target == 'config' and boundary == 'directory_fsync':
        assert store.read_config()['routing']['retry']['request-retry'] == 4
        assert json.loads(store.config_path.with_suffix('.journal').read_bytes())['phase'] == 'applying'
    else:
        assert store.config_path.read_bytes() == before
    assert store.config_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.cpa-*'))
    journal = store.config_path.with_suffix('.journal')
    if target == 'config':
        # A directory fsync failure can follow a successful replace. The
        # implementation cannot prove write identity, so retain blocking journal.
        if boundary == 'directory_fsync':
            assert result['rollback'] == 'drift_detected'
            # Do not guess that replacement failed or auto-overwrite it. Native
            # consent acknowledges observed current bytes before exact recovery.
            restored = store.recover(expected_revision=store.compute_digest(),
                snapshot_revision=revision, expected_generation=owner.generation,
                owner=owner, native_consent=True)
            assert restored['ok'] and store.config_path.read_bytes() == before
        else:
            assert result['rollback'] == 'recovered'
