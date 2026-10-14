"""Generated homes; the owner seam prepares through the native wire stand-in."""
import base64
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from hermes_cpa.config_store import ConfigStore
from hermes_cpa.owner_transport import decode_prepare, encode_prepare, TransportError

import native_config_fixture as native


class Owner:
    """Independent owner seam: stop/start, native prepare and live commit."""
    def __init__(self, store):
        self.store = store
        self.mutation_gate = threading.Lock()
        self.generation = 1
        self.mode = 'managed'
        self.policy_locked = False
        self.running = True
        self.calls = []
        self.starts = iter([True])
        self.live = None  # Override the native live-commit outcome.
        self.committed = []

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

    def prepare_config(self, expected_revision, requests):
        try:
            payload = encode_prepare(expected_revision, requests)
        except TransportError as exc:
            return {'ok': False, 'code': exc.code}
        return decode_prepare(*native.prepare(self.store.config_path.read_bytes(), payload))

    def commit_live(self, expected_revision, candidate_revision, candidate):
        self.calls.append('commit')
        if self.live is not None:
            return self.live
        status, body, persist = native.commit(self.store.config_path.read_bytes(), json.dumps({
            'expected_revision': expected_revision, 'candidate_revision': candidate_revision,
            'candidate_base64': base64.b64encode(candidate).decode()}).encode())
        if persist is not None:
            native.write_in_place(self.store.config_path, persist)
            return 'OK'
        return json.loads(body)['code']

    def cancel_pending_oauth(self, reason):
        self.calls.append('cancel_oauth')

    def config_committed(self, base_revision, revision, effects):
        self.committed.append((base_revision, revision, tuple(effects)))


def put(path, value):
    return {'method': 'PUT', 'path': '/v8/management/config/' + path,
            'content_type': 'application/json',
            'body_base64': base64.b64encode(json.dumps(value).encode()).decode()}


PORT = [put('server/port', 18319)]  # Native effect: restart_required.
RETRY = [put('routing/retry/request-retry', 4)]  # Hot-reloadable natively.


@pytest.fixture
def store(tmp_path):
    store = ConfigStore(tmp_path / 'config.yaml')
    store.create_initial_config(18318, access_keys=['generated-public-client-key'])
    return store


def apply(store, owner, **kwargs):
    args = dict(expected_revision=store.compute_digest(), requests=PORT,
                expected_generation=owner.generation, owner=owner, restart_consent=True)
    args.update(kwargs)
    return store.transact(**args)


@pytest.mark.parametrize('kwargs,code', [
    ({'expected_revision': ''}, 'STALE_REVISION'),
    ({'expected_revision': 'a' * 64}, 'STALE_REVISION'),
    ({'expected_generation': 0}, 'STALE_GENERATION'),
    ({'restart_consent': False}, 'CONSENT_REQUIRED'),
    ({'requests': [put('routing/retry/request-retry', -1)]}, 'INVALID_CONFIG'),
    ({'requests': [{'method': 'PUT', 'path': '/v8/management/credentials'}]}, 'INVALID_REQUEST'),
    ({'reviewed_candidate_revision': 'b' * 64}, 'REVIEW_STALE'),
])
def test_rejected_before_stop_or_write(store, kwargs, code):
    before = store.config_path.read_bytes()
    owner = Owner(store)
    result = apply(store, owner, **kwargs)
    assert result['code'] == code
    assert not owner.calls
    assert store.config_path.read_bytes() == before
    assert not store.config_path.with_suffix('.journal').exists()


def test_native_refusal_reports_request_without_echoing_input(store):
    owner = Owner(store)
    result = apply(store, owner, requests=[put('server/port', 18319),
                                           put('routing/retry/request-retry', -1)])
    assert result['code'] == 'INVALID_CONFIG'
    assert result['detail']['request_index'] == 1 and result['detail']['status'] == 422


def test_restart_required_candidate_stops_writes_in_place_and_restarts(store):
    owner = Owner(store)
    inode = store.config_path.stat().st_ino
    expected = store.compute_digest()
    review = owner.prepare_config(expected, PORT)
    result = apply(store, owner, reviewed_candidate_revision=review['candidate_revision'])
    assert result['ok'] and result['generation'] == 2, result
    assert 'restart_required' in result['effects'] and result['changed_paths'] == ['server/port']
    assert owner.calls == ['owned', 'stop', 'start/probe']
    assert store.config_path.read_bytes() == review['candidate']
    assert store.config_path.stat().st_ino == inode, 'native persistence keeps the inode'
    assert store.config_path.stat().st_mode & 0o777 == 0o600
    assert owner.committed == [(expected, review['candidate_revision'], tuple(result['effects']))]
    assert json.loads(store.config_path.with_suffix('.journal').read_bytes())['phase'] == 'committed'
    assert not store.candidate_path.exists()


def test_hot_candidate_commits_live_without_restart(store):
    owner = Owner(store)
    inode = store.config_path.stat().st_ino
    result = apply(store, owner, requests=RETRY, restart_consent=False)
    assert result['ok'], result
    assert owner.calls == ['owned', 'commit'] and owner.running
    assert ConfigStore(store.config_path).read_config()['routing']['retry']['request-retry'] == 4
    assert store.config_path.stat().st_ino == inode


def test_stopped_child_is_written_and_left_stopped(store):
    owner = Owner(store)
    owner.running = False
    result = apply(store, owner, restart_consent=False)
    assert result['ok']
    assert owner.calls == ['owned']
    assert ConfigStore(store.config_path).read_config()['server']['port'] == 18319


def test_unchanged_candidate_is_success_without_side_effects(store):
    owner = Owner(store)
    result = apply(store, owner, requests=[put('server/port', 18318)])
    assert result['ok'] and 'no_change' in result['effects']
    assert not owner.calls and not owner.committed


def test_live_stale_commit_aborts_without_stop(store):
    owner = Owner(store)
    owner.live = 'STALE_REVISION'
    before = store.config_path.read_bytes()
    result = apply(store, owner, requests=RETRY)
    assert result['code'] == 'STALE_REVISION' and result['rollback'] == 'not_attempted'
    assert owner.calls == ['owned', 'commit'] and owner.running
    assert store.config_path.read_bytes() == before
    assert apply(store, owner, requests=RETRY, expected_revision=store.compute_digest())['code'] != 'RECOVERY_REQUIRED'


def test_uncertain_live_commit_blocks_until_explicit_recovery(store):
    owner = Owner(store)
    owner.live = 'OUTCOME_UNKNOWN'
    before = store.config_path.read_bytes()
    result = apply(store, owner, requests=RETRY)
    assert result['code'] == 'APPLY_FAILED' and result['rollback'] == 'outcome_unknown'
    assert owner.calls == ['owned', 'commit'] and owner.running, 'never resubmitted or restarted'
    assert store.config_path.read_bytes() == before
    assert apply(store, owner, requests=RETRY)['code'] == 'RECOVERY_REQUIRED'


@pytest.mark.parametrize('recovery', [True, False])
def test_readiness_failure_restores_exact_bytes(store, recovery):
    before = store.config_path.read_bytes()
    owner = Owner(store)
    owner.starts = iter([False, recovery])
    result = apply(store, owner)
    assert not result['ok'] and result['code'] == 'RESTART_FAILED'
    assert result['rollback'] == ('recovered' if recovery else 'restart_failed')
    assert store.config_path.read_bytes() == before
    assert owner.calls == ['owned', 'stop', 'start/probe', 'stop', 'start/probe']


def test_concurrent_same_revision_only_one_applies(store):
    owner = Owner(store)
    revision = store.compute_digest()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: apply(store, owner, expected_revision=revision,
                                               expected_generation=1), range(2)))
    assert sum(result['ok'] for result in results) == 1
    assert owner.calls.count('stop') == 1


@pytest.mark.parametrize('mode,locked,code', [('external', False, 'READ_ONLY'),
                                            ('managed', True, 'POLICY_LOCKED')])
def test_policy(store, mode, locked, code):
    owner = Owner(store)
    owner.mode, owner.policy_locked = mode, locked
    assert apply(store, owner)['code'] == code
    assert not owner.calls


def test_drift_during_stop_never_overwrites_external_change(store):
    owner = Owner(store)
    def stop():
        owner.running = False
        store.config_path.write_bytes(b'external: change\n')
        return True
    owner.stop_owned_and_reap = stop
    result = apply(store, owner)
    assert result['code'] == 'STALE_REVISION'
    assert store.config_path.read_bytes() == b'external: change\n'


def test_symlink_rejected(store, tmp_path):
    revision = store.compute_digest()
    before = store.config_path.read_bytes()
    target = tmp_path / 'target'
    store.config_path.rename(target)
    store.config_path.symlink_to(target)
    with pytest.raises(ValueError):
        store.read_config()
    owner = Owner(store)
    result = store.transact(expected_revision=revision, requests=PORT,
                            expected_generation=owner.generation, owner=owner, restart_consent=True)
    assert result['code'] in ('UNAVAILABLE', 'STALE_REVISION')
    assert target.read_bytes() == before
    assert not owner.calls


@pytest.mark.parametrize('phase', ['prepared', 'stopped', 'applying', 'applied', 'committed'])
def test_journal_io_failure_never_claims_success(store, monkeypatch, phase):
    before = store.config_path.read_bytes()
    owner = Owner(store)
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


def test_failed_restore_is_truthful(store, monkeypatch):
    owner = Owner(store)
    owner.starts = iter([False])
    def fail(path):
        raise OSError('PRIVATE_CANARY')
    monkeypatch.setattr(store, 'rollback', fail)
    result = apply(store, owner)
    assert result['rollback'] == 'restore_failed'
    assert 'PRIVATE_CANARY' not in str(result)
    assert apply(store, owner)['code'] == 'RECOVERY_REQUIRED'


def test_torn_in_place_write_requires_explicit_recovery(store, monkeypatch):
    owner = Owner(store)
    owner.running = False
    before = store.config_path.read_bytes()
    original = store.compute_digest()
    write = os.write
    def torn(fd, data):
        write(fd, bytes(data[:7]))
        raise OSError('TORN_CANARY')
    monkeypatch.setattr(os, 'write', torn)
    result = apply(store, owner)
    monkeypatch.setattr(os, 'write', write)
    assert result['code'] == 'APPLY_FAILED' and result['rollback'] == 'drift_detected'
    assert 'TORN_CANARY' not in str(result)
    assert apply(store, owner, expected_revision=store.compute_digest())['code'] == 'RECOVERY_REQUIRED'
    restored = store.recover(expected_revision=store.compute_digest(), snapshot_revision=original,
                             expected_generation=owner.generation, owner=owner, native_consent=True)
    assert restored['ok'] and store.config_path.read_bytes() == before


def test_unfinished_journal_blocks_after_reconstruction(store):
    store._journal('applying', store.compute_digest())
    owner = Owner(store)
    result = apply(ConfigStore(store.config_path), owner)
    assert result['code'] == 'RECOVERY_REQUIRED'
    assert not owner.calls


def test_unknown_fields_and_auth_store_untouched(store):
    with store.config_path.open('a') as stream:
        stream.write('future-field: preserved\n')
    auth = store.config_path.parent / 'auth' / 'generated.json'
    auth.write_bytes(b'GENERATED_AUTH_CANARY')
    owner = Owner(store)
    owner.starts = iter([False, True])
    before = store.config_path.read_bytes()
    assert apply(store, owner)['rollback'] == 'recovered'
    assert store.config_path.read_bytes() == before
    assert auth.read_bytes() == b'GENERATED_AUTH_CANARY'


def test_initial_config_holds_only_user_public_keys(tmp_path):
    store = ConfigStore(tmp_path / 'config.yaml')
    with pytest.raises(ValueError):
        store.create_initial_config(18318, access_keys=[])
    store.create_initial_config(18318, access_keys=['generated-public-client-key'],
                                management_key='generated-public-management')
    data = store.read_config()
    assert data['access']['api-keys'] == ['generated-public-client-key']
    secret = data['management']['secret-key']
    assert secret.startswith('$2') and 'generated-public-management' not in store.config_path.read_text()
    assert data['server']['host'] == '127.0.0.1'
    assert store.config_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        store.create_initial_config(18318, access_keys=['other'])
    plain = ConfigStore(tmp_path / 'plain.yaml')
    plain.create_initial_config(18318, access_keys=['generated-public-client-key'])
    assert 'secret-key' not in plain.read_config()['management']
