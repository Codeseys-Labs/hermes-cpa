"""Console REST authentication, private host grants and large request boundary."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cpa.console_protocol import FILE_LIMIT, PREFIX
from hermes_cpa.console_service import OwnerConsoleService
from hermes_cpa.settings import CpaSettings, RequestScope

spec = importlib.util.spec_from_file_location('cpa_console_api', Path(__file__).parents[2] / 'dashboard/plugin_api.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class AcceptedHostOperations:
    def __init__(self):
        self.current = True
        self.calls = []

    def accept(self, request_id, revision, timeout):
        self.calls.append((request_id, revision))
        return {'capability': 'generated-private-host-capability-' + request_id}

    def revalidate(self, accepted, timeout):
        return self.current

    def retire(self, accepted, timeout):
        return True


class RestOwner:
    def __init__(self, settings):
        self.settings = settings
        self.generation = 17
        self.policy = lambda: True
        self.native_calls = []
        self.submissions = []
        self.responses = {}

    def operation_current(self):
        return True

    def authorized_timeout(self, timeout):
        return timeout

    def submit(self, args):
        self.submissions.append(args)
        return {'ok': True, 'operation_id': args['request_id'], 'state': 'pending', 'generation': 17}

    def request(self, method, path, **kwargs):
        self.native_calls.append((method, path, kwargs))
        if (method, path) in self.responses:
            return self.responses[method, path]
        return SimpleNamespace(status=200, headers={'content-type': 'application/json'},
                               body=b'{"credential":"GENERATED_PROVIDER_SECRET"}')


class Facade:
    mode = 'managed'
    generation = 17

    def __init__(self, service, scope, package_root):
        self.service, self.scope = service, scope
        self.settings, self.package_root = service.owner.settings, package_root

    @property
    def revision(self):
        return hashlib.sha256(self.settings.config_path.read_bytes()).hexdigest()

    def remaining(self):
        return 2.

    def console(self, op, args):
        return self.service.dispatch(op, args)


class Host:
    principal = 'generated-authenticated-principal'

    def __init__(self, controller, operations):
        self.controller, self.scope, self.operations = controller, controller.scope, operations
        self.lock = threading.RLock()

    def authenticate(self, request):
        return self if request.headers.get('Authorization') == 'Bearer generated-host-session' else None

    @contextmanager
    def guard(self):
        with self.lock:
            yield

    def assert_current(self):
        pass


@pytest.fixture
def client(tmp_path):
    settings = CpaSettings(tmp_path)
    settings.config_path.parent.mkdir(parents=True)
    settings.config_path.write_bytes(b'config-version: 8\n')
    (tmp_path / 'config.yaml').write_text('plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings:\n        mode: managed\n')
    package = tmp_path / 'package'
    package.mkdir()
    (package / 'plugin.yaml').write_text('name: hermes-cpa\n')
    owner = RestOwner(settings)
    service = OwnerConsoleService(owner, owner)
    scope = RequestScope('generated-scope', 'generated-connection', 1, tmp_path, 'managed', False)
    operations = AcceptedHostOperations()
    host = Host(Facade(service, scope, package), operations)
    app = FastAPI()
    app.state.cpa_host = host
    app.include_router(api.router)
    headers = {'Authorization': 'Bearer generated-host-session', 'X-CPA-Scope': scope.scope_id, 'X-CPA-Epoch': '1'}
    document = str(uuid.uuid4())
    with TestClient(app) as http:
        yield http, host, owner, service, headers, document
    service.close()


def unlock(client):
    http, _, _, _, headers, document = client
    response = http.post('/console/unlock', headers=headers, json={
        'request_id': 'generated-unlock', 'expected_generation': 17, 'consent': True, 'document_id': document})
    assert response.status_code == 200, response.text
    return {'document_id': document, 'lease_id': response.json()['data']['lease_id']}


@pytest.mark.parametrize('route', ['unlock', 'lock', 'prepare', 'execute', 'result', 'browser', 'plugin', 'file',
                                    'resource', 'resource/close'])
def test_all_console_routes_authenticate_and_never_cache(client, route):
    http, _, _, _, _, document = client
    response = http.post('/console/' + route, json={'document_id': document})
    assert response.status_code == 401
    assert response.headers['cache-control'] == 'no-store'


def test_no_query_capabilities_or_get_result_route(client):
    http, _, _, _, headers, document = client
    response = http.post('/console/result?lease_id=GENERATED_CAPABILITY', headers=headers,
                         json={'document_id': document})
    assert response.status_code == 422
    assert 'GENERATED_CAPABILITY' not in response.text
    assert http.get('/console/result?lease_id=GENERATED_CAPABILITY', headers=headers).status_code == 405
    assert http.get('/console/operations/generated?lease_id=GENERATED_CAPABILITY', headers=headers).status_code == 404


def test_untrusted_scope_override_is_rejected_without_echo(client):
    http, _, owner, _, headers, document = client
    response = http.post('/console/unlock', headers=headers, json={
        'document_id': document, '_scope': {'principal': 'GENERATED_FORGED_PRINCIPAL'}})
    assert response.status_code == 422
    assert 'GENERATED_FORGED' not in response.text
    assert not owner.submissions


def test_console_accepts_ten_mib_file_not_old_one_mib_envelope(client):
    http, _, owner, _, headers, _ = client
    lease = unlock(client)
    response = http.post('/console/prepare', headers=headers, json=dict(lease,
        request_id='generated-upload', request={'method': 'POST', 'path': PREFIX + '/credentials',
            'response_kind': 'json', 'body': {'kind': 'multipart', 'fields': {}, 'files': [{
                'name': 'file', 'filename': 'generated.json', 'content_type': 'application/json',
                'base64': base64.b64encode(b'g' * FILE_LIMIT).decode()}]}}))
    assert response.status_code == 200, response.text
    assert response.json()['data']['effects'] == ['credential_import']
    assert not owner.native_calls and not owner.submissions


def test_private_host_grant_revalidated_after_selection_loss(client):
    http, host, owner, _, headers, _ = client
    lease = unlock(client)
    host.operations.current = False
    response = http.post('/console/prepare', headers=headers, json=dict(lease,
        request_id='stale-request', request={'method': 'GET', 'path': PREFIX + '/credentials',
                                           'body': {'kind': 'empty'}, 'response_kind': 'json'}))
    assert response.status_code == 409
    assert response.json()['error']['code'] in ('STALE_SCOPE', 'LEASE_EXPIRED')
    assert not owner.native_calls and not owner.submissions


def test_explicit_review_accepts_only_opaque_references_and_result_is_scoped(client):
    http, _, owner, service, headers, _ = client
    lease = unlock(client)
    prepared = http.post('/console/prepare', headers=headers, json=dict(lease,
        request_id='native-secret-read', request={'method': 'GET', 'path': PREFIX + '/credentials',
                                                'body': {'kind': 'empty'}, 'response_kind': 'json'}))
    assert prepared.status_code == 200
    review = prepared.json()['data']
    accepted = http.post('/console/execute', headers=headers, json=dict(lease,
        request_id='native-secret-read', review_id=review['review_id'], approved_effects=review['effects']))
    assert accepted.status_code == 202
    assert owner.submissions
    outcome = service.execute_job(owner.submissions[-1])
    assert outcome == {'ok': True, 'code': 'OK', 'result_ready': True}
    response = http.post('/console/result', headers=headers, json=dict(lease, operation_id='native-secret-read'))
    assert response.json()['data']['result']['body']['value']['credential'] == 'GENERATED_PROVIDER_SECRET'
    assert 'GENERATED_PROVIDER_SECRET' not in json.dumps(owner.submissions)
    changed = dict(lease, document_id=str(uuid.uuid4()), operation_id='native-secret-read')
    assert http.post('/console/result', headers=headers, json=changed).status_code == 409
    assert 'generated-private-host-capability' not in response.text


def test_profile_query_reaches_authenticated_binding_without_becoming_console_authority(client):
    http, host, owner, _, headers, document = client
    observed = []
    authenticate = host.authenticate

    def capture_profile(request):
        observed.append(request.query_params.get('profile'))
        return authenticate(request)

    host.authenticate = capture_profile
    unlocked = http.post('/console/unlock?profile=generated-nondefault', headers=headers, json={
        'request_id': 'profile-unlock', 'expected_generation': 17, 'consent': True, 'document_id': document})
    assert unlocked.status_code == 200, unlocked.text
    lease = unlocked.json()['data']['lease_id']
    prepared = http.post('/console/prepare?profile=generated-nondefault', headers=headers, json={
        'lease_id': lease, 'document_id': document, 'request_id': 'profile-read',
        'request': {'method': 'GET', 'path': PREFIX + '/credentials', 'body': {'kind': 'empty'},
                    'response_kind': 'json', 'query': {'name': 'generated.json'}}})
    assert prepared.status_code == 200, prepared.text
    assert observed == ['generated-nondefault', 'generated-nondefault']
    assert prepared.json()['scope_id'] == host.scope.scope_id
    assert not owner.native_calls and not owner.submissions
    for suffix in ('&lease_id=GENERATED_PRIVATE', '&document_id=GENERATED_PRIVATE',
                   '&grant=GENERATED_PRIVATE', '&profile=duplicate'):
        rejected = http.post('/console/result?profile=generated-nondefault' + suffix,
                             headers=headers, json={'document_id': document})
        assert rejected.status_code == 422
        assert 'GENERATED_PRIVATE' not in rejected.text


def test_resource_post_uses_captured_host_context_and_a_distinct_grant_body(client):
    http, host, owner, _, headers, document = client
    lease = unlock(client)
    path = '/v0/resource/plugins/generated/index.html'
    owner.responses['GET', PREFIX + '/plugins'] = SimpleNamespace(status=200, headers={}, body=json.dumps({
        'plugins': [{'id': 'generated', 'effective_enabled': True, 'menus': [{'path': path}]}]}).encode())
    owner.responses['GET', '/hermes/plugin-routes'] = SimpleNamespace(status=200, headers={}, body=json.dumps({
        'registry_revision': '0123456789abcdef-1', 'management': [],
        'resources': [{'method': 'GET', 'path': path, 'plugin_id': 'generated'}]}).encode())
    raw = b'<form action="./index.html"><input name="op" value="save"></form>'
    owner.responses['GET', path] = SimpleNamespace(status=200, headers={
        'Content-Type': 'text/html; charset=utf-8', 'Set-Cookie': 'GENERATED_PRIVATE_COOKIE',
        'X-Hermes-Resource-Invoked': '1', 'X-Hermes-Resource-Revision': '0123456789abcdef-1'}, body=raw)
    preview = http.post('/console/plugin?profile=generated-nondefault', headers=headers,
                        json=dict(lease, plugin_id='generated', menu_index=0))
    assert preview.status_code == 200, preview.text
    review = preview.json()['data']
    opened = http.post('/console/plugin?profile=generated-nondefault', headers=headers,
                       json=dict(lease, review_id=review['review_id'], approved_effects=review['effects']))
    assert opened.status_code == 200, opened.text
    grant = opened.json()['data']
    assert grant['kind'] == 'native-resource' and 'path' not in grant and 'lease_id' not in grant
    assert grant['grant_id'] != lease['lease_id']
    resource_body = {'document_id': document, 'grant_id': grant['grant_id'], 'path': path,
                     'query': [['profile', 'native-only'], ['op', 'save'], ['key', 'one'], ['key', 'two']],
                     'accept': 'text/html'}
    fetched = http.post('/console/resource?profile=generated-nondefault', headers=headers, json=resource_body)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()['scope_id'] == host.scope.scope_id
    envelope = fetched.json()['data']
    assert base64.b64decode(envelope['body_base64']) == raw
    assert 'set-cookie' not in envelope['headers']
    assert envelope['headers']['cache-control'] == 'no-store'
    resource_calls = [(method, route, options) for method, route, options in owner.native_calls if route == path]
    assert len(resource_calls) == 1
    assert resource_calls[0][2]['query'] == [('profile', 'native-only'), ('op', 'save'), ('key', 'one'), ('key', 'two')]
    assert resource_calls[0][2]['headers'] == {
        'Accept': 'text/html', 'X-Hermes-Resource-Revision': '0123456789abcdef-1'}
    rejected = http.post('/console/resource?profile=generated-nondefault', headers=headers,
                         json=dict(resource_body, path='/v8/management/config'))
    assert rejected.status_code == 409
    assert rejected.json()['error']['effect_invoked'] is False
    assert 'GENERATED_PRIVATE_COOKIE' not in rejected.text
    closed = http.post('/console/resource/close?profile=generated-nondefault', headers=headers,
                       json={'document_id': document, 'grant_id': grant['grant_id']})
    assert closed.status_code == 200 and closed.json()['data']['closed'] is True
    again = http.post('/console/resource?profile=generated-nondefault', headers=headers, json=resource_body)
    assert again.status_code == 409 and again.json()['error']['code'] == 'RESOURCE_GONE'
    assert len([route for _, route, _ in owner.native_calls if route == path]) == 1


def test_resource_atomic_rejection_keeps_native_no_effect_proof_at_rest_boundary(client):
    http, _, owner, _, headers, document = client
    lease = unlock(client)
    path = '/v0/resource/plugins/generated/index.html'
    owner.responses['GET', PREFIX + '/plugins'] = SimpleNamespace(status=200, headers={}, body=json.dumps({
        'plugins': [{'id': 'generated', 'effective_enabled': True, 'menus': [{'path': path}]}]}).encode())
    owner.responses['GET', '/hermes/plugin-routes'] = SimpleNamespace(status=200, headers={}, body=json.dumps({
        'registry_revision': '0123456789abcdef-1', 'management': [],
        'resources': [{'method': 'GET', 'path': path, 'plugin_id': 'generated'}]}).encode())
    review = http.post('/console/plugin?profile=generated-nondefault', headers=headers,
                       json=dict(lease, plugin_id='generated', menu_index=0)).json()['data']
    grant = http.post('/console/plugin?profile=generated-nondefault', headers=headers,
                     json=dict(lease, review_id=review['review_id'], approved_effects=review['effects'])).json()['data']
    owner.responses['GET', path] = SimpleNamespace(status=412, headers={
        'X-Hermes-Resource-Invoked': '0', 'X-Hermes-Resource-Revision': '0123456789abcdef-2',
    }, body=b'{"error":"stale_resource_revision","registry_revision":"0123456789abcdef-2"}')
    body = {'document_id': document, 'grant_id': grant['grant_id'], 'path': path, 'query': []}
    rejected = http.post('/console/resource?profile=generated-nondefault', headers=headers, json=body)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()['error']['code'] == 'RESOURCE_REVOKED'
    assert rejected.json()['error']['effect_invoked'] is False
    assert rejected.json()['error']['response_withheld'] is False
    assert '0123456789abcdef' not in rejected.text
    again = http.post('/console/resource?profile=generated-nondefault', headers=headers, json=body)
    assert again.json()['error']['code'] == 'RESOURCE_GONE'
    assert len([route for _, route, _ in owner.native_calls if route == path]) == 1


def resource_grant(client):
    http, _, owner, _, headers, document = client
    lease = unlock(client)
    path = '/v0/resource/plugins/generated/index.html'
    owner.responses['GET', PREFIX + '/plugins'] = SimpleNamespace(status=200, headers={}, body=json.dumps({
        'plugins': [{'id': 'generated', 'effective_enabled': True, 'menus': [{'path': path}]}]}).encode())
    owner.responses['GET', '/hermes/plugin-routes'] = SimpleNamespace(status=200, headers={}, body=json.dumps({
        'registry_revision': '0123456789abcdef-1', 'management': [],
        'resources': [{'method': 'GET', 'path': path, 'plugin_id': 'generated'}]}).encode())
    reviewed = http.post('/console/plugin', headers=headers, json=dict(lease, plugin_id='generated', menu_index=0))
    assert reviewed.status_code == 200, reviewed.text
    review = reviewed.json()['data']
    opened = http.post('/console/plugin', headers=headers,
                       json=dict(lease, review_id=review['review_id'], approved_effects=review['effects']))
    assert opened.status_code == 200, opened.text
    return {'document_id': document, 'grant_id': opened.json()['data']['grant_id'], 'path': path, 'query': []}


def test_resource_inner_native_timeout_is_observed_as_unknown_without_retry(client):
    from hermes_cpa.owner_transport import TransportError
    http, _, owner, _, headers, _ = client
    body = resource_grant(client)
    native_request = owner.request

    def timeout(method, path, **options):
        if path == body['path']:
            owner.native_calls.append((method, path, options))
            assert 0 < options['timeout'] <= 2.
            raise TransportError('OUTCOME_UNKNOWN')
        return native_request(method, path, **options)

    owner.request = timeout
    response = http.post('/console/resource', headers=headers, json=body)
    assert response.status_code == 409, response.text
    error = response.json()['error']
    assert error['code'] == 'OUTCOME_UNKNOWN'
    assert error['effect_invoked'] is True and error['response_withheld'] is False
    assert len([path for _, path, _ in owner.native_calls if path == body['path']]) == 1


@pytest.mark.parametrize('native_status', [409, 500, 503])
def test_resource_native_http_error_is_an_observed_response_not_a_rollback(client, native_status):
    http, _, owner, _, headers, _ = client
    body = resource_grant(client)
    owner.responses['GET', body['path']] = SimpleNamespace(status=native_status, headers={
        'Content-Type': 'text/plain', 'X-Hermes-Resource-Invoked': '1',
        'X-Hermes-Resource-Revision': '0123456789abcdef-1'}, body=b'generated native error response')
    response = http.post('/console/resource', headers=headers, json=body)
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['ok'] is True and data['status'] == native_status
    assert base64.b64decode(data['body_base64']) == b'generated native error response'
    assert len([path for _, path, _ in owner.native_calls if path == body['path']]) == 1


def test_resource_delegation_exception_is_unknown_and_not_replayed(client):
    http, host, _, _, headers, document = client
    calls = []

    def lost(op, args):
        calls.append((op, args))
        raise TimeoutError('generated lost owner reply')

    host.controller.console = lost
    response = http.post('/console/resource', headers=headers, json={'document_id': document})
    assert response.status_code == 409, response.text
    error = response.json()['error']
    assert error['code'] == 'OUTCOME_UNKNOWN'
    assert error['effect_invoked'] is True and error['response_withheld'] is False
    assert len(calls) == 1


def test_resource_predelegation_refusal_stays_definitive(client):
    http, host, _, _, headers, document = client
    calls = []
    host.operations = None
    host.controller.console = lambda *args: calls.append(args)
    response = http.post('/console/resource', headers=headers, json={'document_id': document})
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'UNAVAILABLE'
    assert not calls


@pytest.mark.parametrize('observed', [False, True])
def test_resource_revocation_after_owner_reply_preserves_effect_and_withheld_metadata(client, observed):
    http, host, _, _, headers, document = client
    calls = []

    def response(op, args):
        calls.append((op, args))
        if observed:
            return {'ok': True, 'status': 200, 'headers': {},
                    'body_base64': base64.b64encode(b'GENERATED_WITHHELD_RESOURCE').decode()}
        return {'ok': False, 'code': 'OUTCOME_UNKNOWN', 'effect_invoked': True, 'response_withheld': False}

    def revoked():
        raise api.BoundaryError('STALE_SCOPE')

    host.controller.console, host.assert_current = response, revoked
    result = http.post('/console/resource', headers=headers, json={'document_id': document})
    assert result.status_code == 409, result.text
    error = result.json()['error']
    assert error['code'] == ('STALE_SCOPE' if observed else 'OUTCOME_UNKNOWN')
    assert error['effect_invoked'] is True and error['response_withheld'] is observed
    assert 'body_base64' not in result.text
    assert len(calls) == 1


def test_hosted_console_dispatch_selects_outer_budget_and_keeps_entry_deadline(client, monkeypatch):
    from hermes_cpa import host_binding
    http, host, _, _, _, document = client
    controller = host.controller
    controller.timeout, controller.request_deadline, controller.adapter = 35., None, None
    clock = SimpleNamespace(monotonic=lambda: 101.)
    monkeypatch.setattr(api, 'time', clock)
    monkeypatch.setattr(host_binding, 'time', clock)
    monkeypatch.setattr(host_binding, 'controller_from_profile', lambda settings, scope: controller)
    controller.console = lambda op, args: {
        'ok': False, 'code': 'OUTCOME_UNKNOWN', 'effect_invoked': True, 'response_withheld': False}
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(cpa_guard_homes=set())),
        state=SimpleNamespace(hermes_context={
            'version': 1, 'principal': host.principal, 'home': str(controller.settings.home),
            'host_incarnation': controller.scope.connection_id}, hermes_operations=host.operations))
    result = api.console_dispatch(request, 'resource', {'document_id': document}, deadline=104.5)
    assert result.status_code == 409
    assert controller.timeout == 4.5 and controller.request_deadline == 104.5
    assert json.loads(result.body)['error']['effect_invoked'] is True


def test_console_entry_deadline_includes_body_read_and_threadpool_handoff(monkeypatch):
    from starlette.datastructures import QueryParams
    import starlette.concurrency
    clock = SimpleNamespace(now=100.)
    monkeypatch.setattr(api, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    captured = []

    async def stream():
        clock.now += 1.
        yield json.dumps({'document_id': str(uuid.uuid4())}).encode()

    async def dispatch(fn, request, op, payload, deadline):
        captured.append((fn, op, deadline))
        return 'generated response'

    monkeypatch.setattr(starlette.concurrency, 'run_in_threadpool', dispatch)
    request = SimpleNamespace(query_params=QueryParams(), stream=stream)
    assert asyncio.run(api.console_mutation(request, 'resource')) == 'generated response'
    assert captured == [(api.console_dispatch, 'resource', 104.5)]
