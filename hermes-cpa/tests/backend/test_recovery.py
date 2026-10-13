"""Explicit recovery must compare both current bytes and the selected snapshot."""
import hashlib
import json

import pytest
from test_transactions import Owner
from hermes_cpa.config_store import ConfigStore


@pytest.fixture
def recovery(tmp_path):
    store = ConfigStore(tmp_path / 'config.yaml')
    store.create_initial_config(18318, 'generated-management', 'generated-inference')
    original = store.compute_digest()
    store.snapshot()
    store.apply_safe_changes(original, {'request_retry': 4})
    store._journal('applying', original)
    owner = Owner()
    owner.running = False
    return store, owner, original


def recover(bundle, **overrides):
    store, owner, original = bundle
    args = dict(expected_revision=store.compute_digest(), snapshot_revision=original,
                expected_generation=owner.generation, owner=owner, native_consent=True)
    args.update(overrides)
    return store.recover(**args)


def test_explicit_recovery_restores_snapshot_without_starting(recovery):
    store, owner, original = recovery
    result = recover(recovery)
    assert result['ok']
    assert store.compute_digest() == original
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
    original = store._atomic
    def fail(path, raw):
        if path == store.config_path:
            raise OSError('GENERATED_PRIVATE_ERROR')
        return original(path, raw)
    monkeypatch.setattr(store, '_atomic', fail)
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
    original = store._atomic
    def revoke(path, raw):
        original(path, raw)
        if (boundary == 'restore' and path == store.config_path or
                boundary == 'commit' and path.suffix == '.journal' and
                json.loads(raw)['phase'] == 'recovered'):
            owner.policy_locked = True
    monkeypatch.setattr(store, '_atomic', revoke)
    result = recover(recovery)
    assert result['code'] == 'POLICY_LOCKED'
    assert store.compute_digest() == original_revision
    assert not owner.running
    assert json.loads(store._read_bytes(store.config_path.with_suffix('.journal')))['phase'] != 'recovered'

