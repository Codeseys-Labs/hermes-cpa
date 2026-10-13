"""Generated homes; owner is the independently implemented supervisor seam."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from hermes_cpa.config_store import ConfigStore


class Owner:
    def __init__(self):
        self.mutation_gate = threading.Lock()
        self.generation = 1
        self.mode = 'managed'
        self.policy_locked = False
        self.running = True
        self.calls = []
        self.starts = iter([True])

    def assert_owned(self):
        self.calls.append('owned')

    def stop_owned_and_reap(self):
        self.calls.append('stop')
        self.running = False
        return True

    def start_owned_and_poll_readiness(self):
        self.calls.append('start/probe')
        self.generation += 1
        self.running = next(self.starts)
        return self.running


@pytest.fixture
def store(tmp_path):
    store = ConfigStore(tmp_path / 'config.yaml')
    store.create_initial_config(18318, 'generated-management', 'generated-inference')
    return store


def apply(store, owner, **kwargs):
    args = dict(expected_revision=store.compute_digest(), changes={'request_retry': 4},
                expected_generation=owner.generation, owner=owner, restart_consent=True)
    args.update(kwargs)
    return store.transact(**args)


@pytest.mark.parametrize('kwargs,code', [
    ({'expected_revision': ''}, 'STALE_REVISION'),
    ({'expected_revision': 'a' * 64}, 'STALE_REVISION'),
    ({'expected_generation': 0}, 'STALE_GENERATION'),
    ({'restart_consent': False}, 'CONSENT_REQUIRED'),
    ({'changes': {'request_retry': True}}, 'INVALID_INPUT'),
    ({'changes': {'management': 'canary'}}, 'INVALID_INPUT'),
])
def test_rejected_before_stop(store, kwargs, code):
    before = store.config_path.read_bytes()
    owner = Owner()
    assert apply(store, owner, **kwargs)['code'] == code
    assert not owner.calls
    assert store.config_path.read_bytes() == before


def test_stop_apply_start_and_persistence(store):
    owner = Owner()
    result = apply(store, owner)
    assert result['ok'] and result['generation'] == 2
    assert owner.calls == ['owned', 'stop', 'start/probe']
    assert ConfigStore(store.config_path).read_config()['routing']['retry']['request-retry'] == 4
    assert store.config_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('recovery', [True, False])
def test_readiness_failure_restores_exact_bytes(store, recovery):
    before = store.config_path.read_bytes()
    owner = Owner()
    owner.starts = iter([False, recovery])
    result = apply(store, owner)
    assert not result['ok'] and result['code'] == 'RESTART_FAILED'
    assert result['rollback'] == ('recovered' if recovery else 'restart_failed')
    assert store.config_path.read_bytes() == before
    assert owner.calls == ['owned', 'stop', 'start/probe', 'stop', 'start/probe']


def test_concurrent_same_revision_only_one_applies(store):
    owner = Owner()
    revision = store.compute_digest()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: apply(store, owner, expected_revision=revision,
                                               expected_generation=1), range(2)))
    assert sum(result['ok'] for result in results) == 1
    assert owner.calls.count('stop') == 1


@pytest.mark.parametrize('mode,locked,code', [('external', False, 'READ_ONLY'),
                                            ('managed', True, 'POLICY_LOCKED')])
def test_policy(store, mode, locked, code):
    owner = Owner()
    owner.mode, owner.policy_locked = mode, locked
    assert apply(store, owner)['code'] == code
    assert not owner.calls


def test_drift_during_stop_never_overwrites_external_change(store):
    owner = Owner()
    def stop():
        owner.running = False
        store.config_path.write_bytes(b'external: change\n')
        return True
    owner.stop_owned_and_reap = stop
    result = apply(store, owner)
    assert result['code'] == 'STALE_REVISION'
    assert store.config_path.read_bytes() == b'external: change\n'


def test_symlink_rejected(store, tmp_path):
    target = tmp_path / 'target'
    store.config_path.rename(target)
    store.config_path.symlink_to(target)
    with pytest.raises(ValueError):
        store.read_config()


@pytest.mark.parametrize('data', [b'routing: {}\nrouting: {}\n', b'not: [yaml', b'- list\n',
                                 b'a: &a [1]\nb: *a\n'])
def test_invalid_yaml_refused(store, data):
    store.config_path.write_bytes(data)
    owner = Owner()
    result = apply(store, owner)
    assert result['code'] == 'INVALID_INPUT'
    assert not owner.calls
    assert store.config_path.read_bytes() == data


@pytest.mark.parametrize('phase', ['prepared', 'stopped', 'applying', 'applied', 'committed'])
def test_journal_io_failure_never_claims_success(store, monkeypatch, phase):
    before = store.config_path.read_bytes()
    owner = Owner()
    owner.starts = iter([True, True])
    original = store._journal
    def fail_at(value, revision):
        if value == phase:
            raise OSError('DISK_CANARY')
        return original(value, revision)
    monkeypatch.setattr(store, '_journal', fail_at)
    result = apply(store, owner)
    assert not result['ok']
    assert 'DISK_CANARY' not in str(result)
    assert store.config_path.read_bytes() == before


def test_failed_atomic_restore_is_truthful(store, monkeypatch):
    owner = Owner()
    owner.starts = iter([False])
    def fail(path):
        raise OSError('PRIVATE_CANARY')
    monkeypatch.setattr(store, 'rollback', fail)
    result = apply(store, owner)
    assert result['rollback'] == 'restore_failed'
    assert 'PRIVATE_CANARY' not in str(result)
    assert apply(store, owner)['code'] == 'RECOVERY_REQUIRED'


def test_unfinished_journal_blocks_after_reconstruction(store):
    store._journal('applying', store.compute_digest())
    owner = Owner()
    result = apply(ConfigStore(store.config_path), owner)
    assert result['code'] == 'RECOVERY_REQUIRED'
    assert not owner.calls


def test_unknown_fields_and_auth_store_untouched(store):
    with store.config_path.open('a') as stream:
        stream.write('future-field: preserved\n')
    auth = store.config_path.parent / 'auth' / 'generated.json'
    auth.write_bytes(b'GENERATED_AUTH_CANARY')
    owner = Owner()
    owner.starts = iter([False, True])
    before = store.config_path.read_bytes()
    assert apply(store, owner)['rollback'] == 'recovered'
    assert store.config_path.read_bytes() == before
    assert auth.read_bytes() == b'GENERATED_AUTH_CANARY'

