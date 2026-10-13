"""Supported backend failure boundaries with real, owned/reaped children."""
import json

import pytest
from hermes_cpa.config_store import ConfigStore
from test_lifecycle_safety import owner, ready, unavailable


def transact(store, owner):
    return store.transact(store.compute_digest(), {'request_retry': 4},
        expected_generation=owner.generation, owner=owner.transaction_owner(), restart_consent=True)


@pytest.mark.parametrize('phase', ['prepared', 'stopped', 'applying', 'applied', 'committed'])
def test_journal_failure_real_owner(owner, monkeypatch, phase):
    store = ConfigStore(owner.settings.config_path)
    before = store.config_path.read_bytes()
    owner.probe = ready
    assert owner.start_child()
    initial = owner.child_process
    original = store._journal
    def fail_at(value, revision):
        if value == phase:
            raise OSError('GENERATED_PRIVATE_DISK_CANARY')
        return original(value, revision)
    monkeypatch.setattr(store, '_journal', fail_at)
    result = transact(store, owner)
    assert not result['ok'] and 'CANARY' not in str(result)
    assert store.config_path.read_bytes() == before
    if phase == 'prepared':
        assert initial.poll() is None and result['rollback'] == 'not_attempted'
    elif phase in ('stopped', 'applying'):
        assert initial.poll() is not None and owner.status()['state'] == 'stopped'
        assert result['rollback'] == 'not_attempted'
        assert transact(ConfigStore(store.config_path), owner)['code'] == 'RECOVERY_REQUIRED'
    else:
        assert initial.poll() is not None and result['rollback'] == 'recovered'
        assert owner.status()['state'] == 'ready'


@pytest.mark.parametrize('boundary', ['snapshot', 'apply', 'restore'])
def test_atomic_failure_real_owner(owner, monkeypatch, boundary):
    store = ConfigStore(owner.settings.config_path)
    before = store.config_path.read_bytes()
    owner.probe = ready
    assert owner.start_child()
    original = store._atomic
    def fail(path, raw):
        if (boundary == 'snapshot' and path.suffix == '.snapshot' or
            boundary == 'apply' and path == store.config_path and raw != before or
            boundary == 'restore' and path == store.config_path and raw == before):
            raise OSError('GENERATED_PRIVATE_WRITE_CANARY')
        return original(path, raw)
    monkeypatch.setattr(store, '_atomic', fail)
    if boundary == 'restore':
        owner.probe = unavailable
    result = transact(store, owner)
    assert not result['ok'] and 'CANARY' not in str(result)
    if boundary == 'restore':
        assert result['rollback'] == 'restore_failed'
        assert owner.child_process.poll() is not None
        assert transact(ConfigStore(store.config_path), owner)['code'] == 'RECOVERY_REQUIRED'
        assert not owner.start_child()
    else:
        assert store.config_path.read_bytes() == before
        assert result['rollback'] == ('not_attempted' if boundary == 'snapshot' else 'recovered')
        assert owner.status()['state'] == 'ready'


def test_interrupted_journal_requires_recovery_without_auto_restart(owner):
    store = ConfigStore(owner.settings.config_path)
    before = store.config_path.read_bytes()
    owner.probe = ready
    assert owner.start_child()
    assert owner.stop_child()
    store._journal('applying', store.compute_digest())
    # Reconstruct actual engine, not an invented recovery interface.
    result = transact(ConfigStore(store.config_path), owner)
    assert result['code'] == 'RECOVERY_REQUIRED'
    assert not owner.start_child()
    assert store.config_path.read_bytes() == before
    assert json.loads(store.config_path.with_suffix('.journal').read_bytes())['phase'] == 'applying'
