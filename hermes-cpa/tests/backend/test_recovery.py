"""Explicit recovery must compare both current bytes and the selected snapshot."""
import json

import pytest
from test_transactions import Owner
from hermes_cpa.config_store import ConfigStore


@pytest.fixture
def recovery(tmp_path):
    store = ConfigStore(tmp_path / 'config.yaml')
    store.create_initial_config(18318, access_keys=['generated-public-client-key'])
    original = store.compute_digest()
    store.snapshot()
    # An interrupted transaction: candidate bytes published, journal unfinished.
    store._write_in_place(store.config_path.read_bytes().replace(b'request-retry: 3', b'request-retry: 4'))
    store._journal('applying', original)
    owner = Owner(store)
    owner.running = False
    return store, owner, original


def recover(bundle, **overrides):
    store, owner, original = bundle
    args = dict(expected_revision=store.compute_digest(), snapshot_revision=original,
                expected_generation=owner.generation, owner=owner, native_consent=True)
    args.update(overrides)
    return store.recover(**args)


def test_explicit_recovery_restores_snapshot_in_place_without_starting(recovery):
    store, owner, original = recovery
    inode = store.config_path.stat().st_ino
    result = recover(recovery)
    assert result['ok']
    assert store.compute_digest() == original
    assert store.config_path.stat().st_ino == inode
    assert owner.calls == ['owned', 'stop']
    assert not owner.running
    assert json.loads(store._read_bytes(store.config_path.with_suffix('.journal')))['phase'] == 'recovered'


@pytest.mark.parametrize('override,code', [
    ({'native_consent': False}, 'CONSENT_REQUIRED'),
    ({'expected_revision': '0' * 64}, 'STALE_REVISION'),
    ({'snapshot_revision': '0' * 64}, 'STALE_SNAPSHOT'),
    ({'expected_generation': 0}, 'STALE_GENERATION'),
])
def test_recovery_preconditions_do_not_stop_or_write(recovery, override, code):
    store, owner, _ = recovery
    before = store.config_path.read_bytes()
    assert recover(recovery, **override)['code'] == code
    assert not owner.calls
    assert store.config_path.read_bytes() == before


def test_recovery_preserves_drift_while_stopping(recovery):
    store, owner, _ = recovery
    def stop():
        store.config_path.write_bytes(b'external: changed\n')
        return True
    owner.stop_owned_and_reap = stop
    assert recover(recovery)['code'] == 'STALE_REVISION'
    assert store.config_path.read_bytes() == b'external: changed\n'


def test_recovery_rechecks_policy_after_stop(recovery):
    store, owner, _ = recovery
    before = store.config_path.read_bytes()
    def stop():
        owner.policy_locked = True
        return True
    owner.stop_owned_and_reap = stop
    assert recover(recovery)['code'] == 'POLICY_LOCKED'
    assert store.config_path.read_bytes() == before


def test_recovery_rechecks_snapshot_after_stop(recovery):
    store, owner, _ = recovery
    before = store.config_path.read_bytes()
    def stop():
        store.config_path.with_suffix('.snapshot').write_bytes(b'external: snapshot\n')
        return True
    owner.stop_owned_and_reap = stop
    assert recover(recovery)['code'] == 'STALE_SNAPSHOT'
    assert store.config_path.read_bytes() == before


def test_recovery_io_failure_retains_blocking_journal(recovery, monkeypatch):
    store, _, _ = recovery
    def fail(raw):
        raise OSError('GENERATED_PRIVATE_ERROR')
    monkeypatch.setattr(store, '_write_in_place', fail)
    result = recover(recovery)
    assert result['code'] == 'RECOVERY_FAILED'
    assert 'GENERATED_PRIVATE_ERROR' not in str(result)
    assert json.loads(store._read_bytes(store.config_path.with_suffix('.journal')))['phase'] == 'recovering'


def test_recovery_requires_unfinished_journal(recovery):
    store, owner, _ = recovery
    store._journal('committed', store.compute_digest())
    assert recover(recovery)['code'] == 'NO_RECOVERY_REQUIRED'
    assert not owner.calls


@pytest.mark.parametrize('boundary', ['restore', 'commit'])
def test_recovery_revocation_during_restore_never_reports_success(recovery, monkeypatch, boundary):
    store, owner, original_revision = recovery
    write, atomic = store._write_in_place, store._atomic
    def revoke_on_restore(raw):
        write(raw)
        if boundary == 'restore':
            owner.policy_locked = True
    def revoke_on_commit(path, raw):
        atomic(path, raw)
        if (boundary == 'commit' and path.suffix == '.journal' and
                json.loads(raw)['phase'] == 'recovered'):
            owner.policy_locked = True
    monkeypatch.setattr(store, '_write_in_place', revoke_on_restore)
    monkeypatch.setattr(store, '_atomic', revoke_on_commit)
    result = recover(recovery)
    assert result['code'] == 'POLICY_LOCKED'
    assert store.compute_digest() == original_revision
    assert not owner.running
    assert json.loads(store._read_bytes(store.config_path.with_suffix('.journal')))['phase'] != 'recovered'
