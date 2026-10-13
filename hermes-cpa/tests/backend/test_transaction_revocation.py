"""Revocation at durable boundaries must never report committed success."""
import pytest
from test_transactions import Owner, apply, store


@pytest.mark.parametrize('phase', ['applying', 'applied', 'committed'])
@pytest.mark.parametrize('running', [True, False])
def test_revocation_at_journal_boundary_stops_and_never_restarts(store, monkeypatch, phase, running):
    owner = Owner()
    owner.running = running
    owner.starts = iter([True, True])
    before = store.config_path.read_bytes()
    journal = store._journal

    def revoke(value, revision):
        journal(value, revision)
        if value == phase:
            owner.policy_locked = True
    monkeypatch.setattr(store, '_journal', revoke)
    result = apply(store, owner)
    assert not result['ok'], result
    assert result['code'] == 'POLICY_LOCKED', result
    assert not owner.running
    assert store.config_path.read_bytes() == before
    # A start before revocation at commit is stopped, never restarted by rollback.
    assert owner.calls.count('start/probe') <= (1 if phase == 'committed' and running else 0)


def test_revoke_during_failed_restart_restores_but_does_not_restart(store):
    owner = Owner()
    before = store.config_path.read_bytes()
    def revoked_start():
        owner.calls.append('start/probe')
        owner.policy_locked = True
        return False
    owner.start_owned_and_poll_readiness = revoked_start
    result = apply(store, owner)
    assert not result['ok']
    assert result['rollback'] == 'restored_stopped'
    assert owner.calls.count('start/probe') == 1
    assert store.config_path.read_bytes() == before
