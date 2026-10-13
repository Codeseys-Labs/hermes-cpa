import importlib.util
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hermes_cpa.settings import RequestScope

spec = importlib.util.spec_from_file_location('cpa_plugin_api', Path(__file__).parents[2] / 'dashboard/plugin_api.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class Facade:
    generation = 1
    revision = 'a' * 64

    def __init__(self, scope):
        self.scope = scope
        self.submissions = []

    def get_status(self):
        return {'instance_id': self.scope.scope_id, 'management_ready': False}

    def get_config(self):
        return {'revision': self.revision, 'strategy': 'round-robin'}

    def get_metrics(self):
        return {'available': False, 'total_success': None}

    def submit_config(self, payload):
        self.submissions.append(payload)
        return {'operation_id': 'generated-config', 'state': 'pending'}

    def submit_operation(self, payload):
        self.submissions.append(payload)
        return {'operation_id': 'generated-operation', 'state': 'pending'}

    def get_operation(self, operation_id):
        return None

    def get_events(self, cursor, limit):
        assert limit == 100
        return {'events': [], 'cursor': '1:0', 'reset': cursor != '1:0'}


class Host:
    def __init__(self, home):
        self.lock = threading.RLock()
        self.epoch = 0
        self.home = home
        self.select('A')

    def select(self, name):
        with self.lock:
            self.epoch += 1
            self.scope = RequestScope(name, 'connection-one', self.epoch,
                                      self.home / name, 'managed', False)
            self.controller = Facade(self.scope)

    def authenticate(self, request):
        if request.headers.get('Authorization') != 'Bearer generated-host':
            return None
        return self

    @contextmanager
    def guard(self):
        with self.lock:
            yield

    def assert_current(self):
        pass


@pytest.fixture
def setup(tmp_path):
    host = Host(tmp_path)
    app = FastAPI()
    app.state.cpa_host = host
    app.include_router(api.router, prefix='/api/plugins/hermes-cpa')
    return TestClient(app), host


def headers(host, **extra):
    return {'Authorization': 'Bearer generated-host', 'X-CPA-Scope': host.scope.scope_id,
            'X-CPA-Epoch': str(host.epoch), **extra}


@pytest.mark.parametrize('path', ['/status', '/metrics', '/config', '/events', '/operations/old', '/console/asset'])
def test_all_reads_authenticated(setup, path):
    client, host = setup
    response = client.get('/api/plugins/hermes-cpa' + path)
    assert response.status_code == 401
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['error']['code'] == 'UNAUTHORIZED'


def test_scope_and_aba(setup):
    client, host = setup
    old = headers(host)
    response = client.get('/api/plugins/hermes-cpa/status', headers=old)
    assert response.json()['data']['instance_id'] == 'A'
    host.select('B')
    assert client.get('/api/plugins/hermes-cpa/status', headers=old).status_code == 409
    assert client.get('/api/plugins/hermes-cpa/status', headers=headers(host)).json()['scope_id'] == 'B'
    host.select('A')
    assert client.get('/api/plugins/hermes-cpa/status', headers=old).status_code == 409


def test_no_host_fails_closed():
    app = FastAPI()
    app.include_router(api.router)
    assert TestClient(app).get('/status').status_code == 503


@pytest.mark.parametrize('body', [
    {'expected_generation': 1, 'changes': {'request_retry': 2}},
    {'expected_revision': '', 'expected_generation': 1, 'changes': {}},
    {'expected_revision': 'a' * 64, 'expected_generation': True, 'changes': {'request_retry': 2}},
    {'expected_revision': 'a' * 64, 'expected_generation': 1, 'changes': {'secret': 'CANARY'}},
    {'expected_revision': 'a' * 64, 'expected_generation': 1, 'changes': {'request_retry': True}},
])
def test_typed_input_no_echo(setup, body):
    client, host = setup
    response = client.put('/api/plugins/hermes-cpa/config', json=body, headers=headers(host))
    assert response.status_code == 422
    assert 'CANARY' not in response.text
    assert not host.controller.submissions


def test_submission_is_pending_not_file_success(setup):
    client, host = setup
    body = {'expected_revision': 'a' * 64, 'expected_generation': 1,
            'changes': {'request_retry': 2}, 'restart_consent': True}
    response = client.put('/api/plugins/hermes-cpa/config', json=body, headers=headers(host))
    assert response.status_code == 202
    assert response.json()['data']['state'] == 'pending'
    old = headers(host)
    host.select('B')
    host.select('A')
    assert client.put('/api/plugins/hermes-cpa/config', json=body, headers=old).status_code == 409
    assert not host.controller.submissions


def test_consent_rejected_before_owner_submission(setup):
    client, host = setup
    response = client.put('/api/plugins/hermes-cpa/config', headers=headers(host), json={
        'expected_revision': 'a' * 64, 'expected_generation': 1,
        'changes': {'request_retry': 2}, 'restart_consent': False})
    assert response.status_code == 403
    assert not host.controller.submissions


def test_external_readonly(setup):
    client, host = setup
    host.scope = RequestScope('A', 'connection-one', host.epoch, host.home)
    response = client.post('/api/plugins/hermes-cpa/operations', headers=headers(host),
                           json={'request_id': 'test', 'expected_generation': 1, 'action': 'stop'})
    assert response.status_code == 403
    assert not host.controller.submissions


def test_telemetry_events_and_unknown_operation(setup):
    client, host = setup
    h = headers(host)
    assert client.get('/api/plugins/hermes-cpa/metrics', headers=h).json()['data']['total_success'] is None
    assert client.get('/api/plugins/hermes-cpa/events?cursor=old', headers=h).json()['data']['reset']
    assert client.get('/api/plugins/hermes-cpa/operations/old', headers=h).json()['error']['code'] == 'RECONCILE_REQUIRED'


def test_exception_redaction(setup):
    client, host = setup
    def fail():
        raise RuntimeError('SECRET_CANARY')
    host.controller.get_status = fail
    response = client.get('/api/plugins/hermes-cpa/status', headers=headers(host))
    assert response.status_code == 503
    assert 'SECRET_CANARY' not in response.text
