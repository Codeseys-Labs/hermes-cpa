"""Owner ↔ generated integration child over private Unix channels; no keys, no TCP CPA.

The child is fixture_private_cpa.py speaking the agreed owner contract. These
tests do not assert real CPA behavior; test_pinned_owner_transactions.py does.
"""
import base64
import json
import socket
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hermes_cpa import authorization
from hermes_cpa.config_store import ConfigStore
from hermes_cpa.controller import CpaController, OwnerReadAdapter
from hermes_cpa.owner_transport import CONSOLE_IPC_LIMIT, TransportError
from hermes_cpa.process_owner import socket_address
from hermes_cpa.settings import CpaSettings
from hermes_cpa.subscription_broker import SubscriptionBroker
from hermes_cpa.subscription_client import grant_token
from hermes_cpa.supervisor import CpaSupervisor

FIXTURE = Path(__file__).with_name('fixture_private_cpa.py')
TERMINAL = b'data: [DONE]\n\n'
SECRET = 'GENERATED_CONSOLE_RESULT_CANARY'


def put(path, value):
    return {'method': 'PUT', 'path': '/v8/management/config/' + path,
            'content_type': 'application/json',
            'body_base64': base64.b64encode(json.dumps(value).encode()).decode()}


@contextmanager
def private_owner(tmp_path, *, variant='normal', console=None, policy=None, readiness_timeout=5.):
    settings = CpaSettings((tmp_path / 'home').resolve())
    settings.ensure_directories()
    ConfigStore(settings.config_path).create_initial_config(
        18318, access_keys=['generated-public-client-key'])
    state = tmp_path / 'state'
    state.mkdir()
    config = str(settings.config_path)
    owner = CpaSupervisor(settings, mode='managed', policy=policy or (lambda: True), console=console,
        command=[sys.executable, str(FIXTURE), '-config', config, '--state', str(state),
                 '--variant', variant],
        prepare_command=[sys.executable, str(FIXTURE), '-config', config, '--hermes-prepare-config'],
        readiness_timeout=readiness_timeout, stop_grace=1.)
    assert owner.acquire_lock()
    try:
        yield owner, state
    finally:
        child = owner.child_process
        owner.cleanup()
        if owner._worker is not None:
            owner._worker.join(2)
        assert child is None or child.poll() is not None


def hits(state):
    path = state / 'hits.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait(owner, operation_id, timeout=10.):
    until = time.monotonic() + timeout
    while True:
        result = owner.dispatch({'cmd': 'operation', 'args': {'operation_id': operation_id}})
        if result.get('state') in ('succeeded', 'failed') or time.monotonic() >= until:
            return result
        time.sleep(.02)


def enroll(home, broker):
    (home / 'config.yaml').write_text(json.dumps({
        'providers': {'cpa': {'base_url': broker.base_url, 'key_cmd': 'generated-command'}}}))


def models(client, broker, token):
    return client.get(broker.base_url + '/models', headers={'Authorization': 'Bearer ' + token})


def test_ready_over_private_health_without_keys_or_public_listener(tmp_path):
    with private_owner(tmp_path) as (owner, state):
        assert owner.start_child(), owner.sanitized_reason
        assert owner.status()['management_ready']
        assert not (owner.settings.home / '.env').exists()
        assert owner.settings.private_dir.stat().st_mode & 0o777 == 0o700
        for path in (owner.settings.control_socket_path, owner.settings.inference_socket_path):
            assert stat.S_ISSOCK(path.lstat().st_mode) and path.stat().st_mode & 0o777 == 0o600
        response = owner.control.request('GET', '/v8/management/config',
                                         headers={'Accept': 'application/json'})
        assert response.status == 200 and json.loads(response.body)['config-version'] == 8
        revision = ConfigStore(owner.settings.config_path).compute_digest()
        assert response.headers['x-hermes-config-revision'] == revision
        assert hits(state) and all(not hit['authorization'] for hit in hits(state))
        for call, code in [
                (dict(headers={'Authorization': 'Bearer generated'}), 'INVALID_REQUEST'),
                (dict(path='/v8/management/../hermes/health'), 'INVALID_REQUEST'),
                (dict(method='PUT', path='/v8/management/config/routing/strategy'),
                 'CONFIG_TRANSACTION_REQUIRED'),
                (dict(method='PUT', path='/v8/management/%63onfig.yaml'), 'CONFIG_TRANSACTION_REQUIRED'),
                (dict(method='POST', path='/hermes/config/commit'), 'CONFIG_TRANSACTION_REQUIRED')]:
            method, path = call.pop('method', 'GET'), call.pop('path', '/v8/management/config')
            with pytest.raises(TransportError) as refused:
                owner.control.request(method, path, **call)
            assert refused.value.code == code
        with pytest.raises(TransportError):
            owner.inference.request('GET', '/v8/management/config')
        assert owner.stop_child()
        with pytest.raises(TransportError) as gone:
            owner.control.request('GET', '/v8/management/config')
        assert gone.value.code == 'UNAVAILABLE'


def test_resource_revision_precondition_and_invocation_facts_pass_through(tmp_path):
    with private_owner(tmp_path) as (owner, state):
        assert owner.start_child()
        item = '/v0/resource/plugins/generated/item'
        stale = owner.control.request('GET', item,
            headers={'X-Hermes-Resource-Revision': '0123456789abcdef-7'})
        assert stale.status == 412
        assert stale.headers['x-hermes-resource-invoked'] == '0'
        assert stale.headers['x-hermes-resource-revision'] == 'none-0'
        current = owner.control.request('GET', item, headers={'X-Hermes-Resource-Revision': 'none-0'})
        assert current.status == 200 and current.headers['x-hermes-resource-invoked'] == '1'
        # A duplicated host fact is ambiguous: reported absent, never one of the values.
        forged = owner.control.request('GET', item + '/forged',
                                       headers={'X-Hermes-Resource-Revision': 'none-0'})
        assert 'x-hermes-resource-invoked' not in forged.headers
        sent = [hit for hit in hits(state) if hit['path'].startswith('/v0/resource/')]
        assert len(sent) == 3 and not any(hit['authorization'] for hit in sent)


@pytest.mark.parametrize('variant', ['grandchild', 'wrong-service'])
def test_only_the_launched_child_can_satisfy_readiness(tmp_path, variant):
    with private_owner(tmp_path, variant=variant, readiness_timeout=1.) as (owner, state):
        assert not owner.start_child()
        assert owner.sanitized_reason == 'readiness_timeout'
        assert owner.child_process.poll() is not None
        if variant == 'grandchild':
            # Peer identity fails before any request byte reaches the impostor.
            assert hits(state) == []


def test_legacy_apply_commits_live_through_ledger_without_restart(tmp_path):
    with private_owner(tmp_path) as (owner, state):
        assert owner.start_child()
        child = owner.child_process
        store = ConfigStore(owner.settings.config_path)
        inode = store.config_path.stat().st_ino
        args = {'action': 'config.apply', 'request_id': 'generated-apply',
                'expected_generation': owner.generation, 'expected_revision': store.compute_digest(),
                'changes': {'request_retry': 4}, 'restart_consent': True}
        assert owner.submit(args)['state'] == 'pending'
        result = wait(owner, 'generated-apply')
        assert result['state'] == 'succeeded', result
        assert owner.child_process is child and child.poll() is None, 'hot change restarted CPA'
        assert store.read_config()['routing']['retry']['request-retry'] == 4
        assert store.config_path.stat().st_ino == inode
        assert [hit['path'] for hit in hits(state) if hit['path'].startswith('/hermes/config')] == [
            '/hermes/config/prepare', '/hermes/config/commit']
        assert owner.submit(dict(args, changes={'request_retry': 5}))['code'] == 'REQUEST_CONFLICT'


def test_stopped_transaction_prepares_offline_and_stays_stopped(tmp_path):
    with private_owner(tmp_path) as (owner, state):
        store = ConfigStore(owner.settings.config_path)
        inode = store.config_path.stat().st_ino
        result = store.transact(store.compute_digest(), [put('server/port', 18401)],
                                expected_generation=owner.generation, owner=owner.transaction_owner())
        assert result['ok'], result
        assert owner.child_process is None
        assert store.read_config()['server']['port'] == 18401
        assert store.config_path.stat().st_ino == inode
        assert hits(state) == []


def test_public_listener_and_key_changes_keep_broker_endpoint_and_enrollment(tmp_path):
    with private_owner(tmp_path) as (owner, state):
        home = owner.settings.home
        assert owner.start_child()
        with SubscriptionBroker(home, owner.inference, port=0) as broker:
            enroll(home, broker)
            endpoint = broker.base_url
            with httpx.Client(trust_env=False, timeout=5) as client:
                first = models(client, broker, grant_token(broker.control_path, home, enrolled=True))
                assert first.status_code == 200
                before_pid = first.json()['data'][0]['owned_by']
                store = ConfigStore(owner.settings.config_path)
                inode = store.config_path.stat().st_ino
                result = store.transact(store.compute_digest(), [
                    put('server/port', 18402), put('access/api-keys', ['rotated-public-client-key'])],
                    expected_generation=owner.generation, owner=owner.transaction_owner(),
                    restart_consent=True)
                assert result['ok'], result
                assert {'restart_required', 'access_keys_changed'} <= set(result['effects'])
                config = store.read_config()
                assert config['server']['port'] == 18402
                assert list(config['access']['api-keys']) == ['rotated-public-client-key'], \
                    'no private broker key may be reinserted into user access keys'
                assert store.config_path.stat().st_ino == inode
                second = models(client, broker, grant_token(broker.control_path, home, enrolled=True))
                assert second.status_code == 200
                assert second.json()['data'][0]['owned_by'] != before_pid
                assert broker.base_url == endpoint
        inference = [hit for hit in hits(state) if hit['channel'] == 'inference']
        assert len(inference) == 2 and not any(hit['authorization'] for hit in inference)


def test_admitted_private_stream_stops_at_authorization_expiry(tmp_path, monkeypatch):
    clock = SimpleNamespace(wall=time.time(), ticks=time.monotonic())
    monkeypatch.setattr(authorization, 'time', SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.ticks))
    deadline = authorization.AuthorizationDeadline(clock.wall + 60)
    with private_owner(tmp_path, variant='stream-hold') as (owner, state):
        home = owner.settings.home
        assert owner.start_child()
        with SubscriptionBroker(home, owner.inference, port=0, authorization=deadline) as broker:
            token = grant_token(broker.control_path, home)
            with httpx.Client(trust_env=False, timeout=5) as client:
                with client.stream('POST', broker.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + token},
                        json={'model': 'generated-model', 'messages': [], 'stream': True}) as response:
                    assert response.status_code == 200
                    parts = response.iter_bytes()
                    received = bytearray(next(parts))
                    clock.ticks += 60
                    (state / 'release').touch()
                    with pytest.raises(httpx.RemoteProtocolError):
                        for part in parts:
                            received.extend(part)
                    assert TERMINAL not in received
            with pytest.raises(RuntimeError):
                grant_token(broker.control_path, home)


def test_broker_revocation_cuts_admitted_private_stream(tmp_path):
    with private_owner(tmp_path, variant='stream-hold') as (owner, state):
        home = owner.settings.home
        assert owner.start_child()
        broker = SubscriptionBroker(home, owner.inference, port=0).__enter__()
        closer = threading.Thread(target=broker.__exit__, args=(None, None, None))
        try:
            token = grant_token(broker.control_path, home)
            with httpx.Client(trust_env=False, timeout=5) as client:
                with client.stream('POST', broker.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + token},
                        json={'model': 'generated-model', 'messages': [], 'stream': True}) as response:
                    parts = response.iter_bytes()
                    received = bytearray(next(parts))
                    closer.start()  # Owner cleanup path: stop admissions, cut transports.
                    with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
                        for part in parts:
                            received.extend(part)
                    assert TERMINAL not in received
        finally:
            if closer.ident is None:
                broker.__exit__(None, None, None)
            closer.join(5)
            (state / 'release').touch()
        assert owner.child_process.poll() is None, 'revoking grants never stops the owned service'


class RecordingConsole:
    """Generated console service: echoes frames, blocks jobs, leaks a canary if allowed."""
    def __init__(self, owner, transport):
        self.owner, self.transport = owner, transport
        self.release = threading.Event()
        self.rejected, self.cancelled, self.ran = [], [], []

    def dispatch(self, op, args):
        if op == 'echo':
            blob = args['file']['base64']
            return {'ok': True, 'bytes': len(blob), 'tail': blob[-5:]}
        if op == 'result':
            return {'ok': True, 'receipt': 'original-scope'}
        return {'ok': False, 'code': 'INVALID_INPUT'}

    def execute_job(self, args):
        self.ran.append(args['request_id'])
        self.release.wait(10)
        return {'ok': self.owner.operation_current(), 'code': 'OK', 'result_ready': True,
                'native': SECRET, 'headers': {'authorization': SECRET}}

    def job_rejected(self, args, code):
        self.rejected.append((args['request_id'], code))

    def cancel_pending_oauth(self, reason):
        # Recorded pending OAuth must be cancelled while the child can still answer.
        self.cancelled.append((reason, self.owner.private_peer() is not None))

    def config_committed(self, base_revision, revision, effects):
        pass

    def close(self):
        pass


def raw_exchange(settings, data):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        with socket_address(settings.run_dir) as address:
            client.connect(address)
        client.sendall(data)
        reply = bytearray()
        while not reply.endswith(b'\n'):
            chunk = client.recv(65536)
            if not chunk:
                break
            reply.extend(chunk)
        return json.loads(reply)


def test_console_ipc_frames_ten_mib_and_keeps_results_private(tmp_path):
    with private_owner(tmp_path, console=RecordingConsole) as (owner, state):
        service = owner.console
        assert owner.start_child()
        loop = threading.Thread(target=owner.run_ipc)
        loop.start()
        try:
            until = time.monotonic() + 5
            while not owner.settings.identity_path.exists() and time.monotonic() < until:
                time.sleep(.02)
            controller = CpaController(owner.settings, mode='managed', timeout=15.)
            scope = {'home': str(owner.settings.home)}
            blob = 'A' * (14 * 1024 * 1024 - 5) + 'TAIL!'  # One 10 MiB file, base64-encoded.
            echoed = controller.console('echo', {'_scope': scope, 'file': {'base64': blob}})
            assert echoed == {'ok': True, 'bytes': len(blob), 'tail': 'TAIL!'}
            assert controller.console('echo', {'_scope': {'home': '/elsewhere'}})['code'] == 'STALE_SCOPE'
            assert controller.console('Not An Op', {'_scope': scope})['code'] == 'INVALID_INPUT'
            # Frame lengths are bounded before allocation; ordinary IPC keeps 64 KiB.
            header = json.dumps({'cmd': 'console.echo', 'length': CONSOLE_IPC_LIMIT + 1}).encode()
            assert raw_exchange(owner.settings, header + b'\n')['code'] == 'UNAVAILABLE'
            padded = json.dumps({'cmd': 'status', 'args': {'pad': 'x' * 70000}}).encode() + b'\n'
            assert raw_exchange(owner.settings, padded)['code'] == 'UNAVAILABLE'
            assert controller._call_supervisor('status', {'pad': 'x' * 70000}) is None
            # Managed passive reads use the owner's private channel, no key.
            controller.adapter = OwnerReadAdapter(controller)
            assert controller.get_feature('providers')['available'] is True
            # Four console jobs at most; they never block lifecycle operations.
            for index in range(4):
                assert owner.submit({'action': 'console.execute', 'request_id': f'job-{index}',
                                     'expected_generation': owner.generation, 'review_id': 'r'})['ok']
            assert owner.submit({'action': 'console.execute', 'request_id': 'job-5',
                                 'expected_generation': owner.generation, 'review_id': 'r'})['code'] == 'BUSY'
            old_child = owner.child_process
            assert owner.submit({'action': 'restart', 'request_id': 'restart-1',
                                 'expected_generation': owner.generation})['ok']
            assert wait(owner, 'restart-1')['state'] == 'succeeded'
            assert old_child.poll() is not None and owner.child_process.poll() is None
            assert service.cancelled == [('child_stopping', True)]
            service.release.set()
            for index in range(4):
                assert wait(owner, f'job-{index}')['state'] == 'succeeded'
            receipt = controller.get_operation('job-0')
            assert receipt['ok'] is True and receipt['result_ready'] is True and receipt['code'] == 'OK'
            assert SECRET not in json.dumps(receipt)
            assert SECRET not in json.dumps(controller.get_events())
            assert controller.get_operation('job-0') == receipt
        finally:
            service.release.set()
            owner.running = False
            loop.join(5)


def test_rejected_console_job_releases_service_and_receipts_outlive_revocation(tmp_path):
    answers = iter([True])
    with private_owner(tmp_path, console=RecordingConsole,
                       policy=lambda: next(answers, False)) as (owner, state):
        service = owner.console
        receipt = owner.submit({'action': 'console.execute', 'request_id': 'revoked-job',
                                'expected_generation': owner.generation, 'review_id': 'r'})
        assert receipt['ok']
        result = wait(owner, 'revoked-job')
        assert result['state'] == 'failed' and result['code'] == 'POLICY_LOCKED'
        assert service.rejected == [('revoked-job', 'POLICY_LOCKED')] and service.ran == []
        scope = {'_scope': {'home': str(owner.settings.home)}}
        assert owner.dispatch({'cmd': 'console.echo', 'args': scope})['code'] == 'POLICY_LOCKED'
        assert owner.dispatch({'cmd': 'console.result', 'args': scope}) == {
            'ok': True, 'receipt': 'original-scope'}
