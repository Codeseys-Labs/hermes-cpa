"""Public console behavior with generated native-transport fixtures.

The native socket is the injected I/O seam. These tests exercise the real
session, registry, effect review, immutable acceptance and private result code.
"""
import base64
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from hermes_cpa.console_protocol import FILE_LIMIT, PREFIX
from hermes_cpa.console_service import OwnerConsoleService


class NativeFixture:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.failure = None

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.failure:
            raise self.failure
        value = self.responses.get((method, path), {'native': 'GENERATED_PROVIDER_SECRET'})
        if isinstance(value, SimpleNamespace):
            response = value
        elif isinstance(value, bytes):
            response = SimpleNamespace(status=200, headers={'content-type': 'application/octet-stream'}, body=value)
        else:
            response = SimpleNamespace(status=200, headers={'content-type': 'application/json'},
                                       body=json.dumps(value).encode())
        if path.startswith('/v0/resource/plugins/'):
            headers = dict(response.headers)
            for name, value in {
                'x-hermes-resource-invoked': '1',
                'x-hermes-resource-revision': kwargs.get('headers', {}).get(
                    'X-Hermes-Resource-Revision', '0123456789abcdef-1'),
            }.items():
                if not any(key.lower() == name for key in headers):
                    headers[name] = value
            response = SimpleNamespace(status=response.status, headers=headers, body=response.body)
        return response


class OwnerFixture:
    def __init__(self, home, native):
        config = home / 'config.yaml'
        config.write_bytes(b'config-version: 8\n')
        self.settings = SimpleNamespace(home=home, config_path=config)
        self.generation = 10
        self.allowed = True
        self.policy = lambda: self.allowed
        self.inference = NativeFixture()
        self.submissions = []
        self.receipts = {}
        self.preparations = []
        self.transactions = []
        self.effects = ['restart_required', 'public_listener_changed', 'pending_oauth_invalidated']
        self.changed_paths = ['server/host', 'server/port']

    def operation_current(self):
        return self.allowed

    def authorized_timeout(self, value):
        return value

    def submit(self, args):
        self.submissions.append(args)
        receipt = {'ok': True, 'operation_id': args['request_id'], 'state': 'pending',
                   'generation': self.generation, 'action': 'console.execute'}
        self.receipts[args['request_id']] = receipt
        return receipt.copy()

    def dispatch(self, request):
        return self.receipts.get(request['args']['operation_id'], {'ok': False, 'code': 'RECONCILE_REQUIRED'})

    def prepare_config(self, revision, requests, timeout):
        self.preparations.append((revision, requests, timeout))
        return {'ok': True, 'base_revision': revision, 'candidate_revision': 'b' * 64,
                'candidate': b'generated native canonical candidate',
                'effects': self.effects, 'changed_paths': self.changed_paths}

    def config_transaction(self, revision, requests, **kwargs):
        self.transactions.append((revision, requests, kwargs))
        return {'ok': True, 'code': 'OK', 'revision': 'b' * 64, 'generation': self.generation}


@pytest.fixture
def console(tmp_path):
    native = NativeFixture()
    owner = OwnerFixture(tmp_path, native)
    service = OwnerConsoleService(owner, native)
    scope = {'principal': 'generated-principal', 'home': str(tmp_path), 'scope_id': 'generated-scope',
             'connection_id': 'generated-host', 'selection_epoch': 1, 'document_id': str(uuid.uuid4())}
    unlock = service.dispatch('unlock', {'_scope': scope, 'document_id': scope['document_id'],
        'request_id': 'unlock-generated', 'expected_generation': 10, 'consent': True})
    assert unlock['ok']
    args = {'_scope': scope, 'document_id': scope['document_id'], 'lease_id': unlock['lease_id']}
    yield service, owner, native, args
    service.close()


def request(path, method='GET', value=None, **kwargs):
    return {'method': method, 'path': path if path.startswith('/v') else PREFIX + path,
            'body': {'kind': 'empty'} if value is None else {'kind': 'json', 'value': value},
            'response_kind': 'json', **kwargs}


def prepare(console, wire, request_id='generated-operation', **kwargs):
    service, _, _, args = console
    return service.dispatch('prepare', dict(args, request_id=request_id, request=wire, **kwargs))


def execute(console, review, request_id='generated-operation', approve=True):
    service, _, _, args = console
    return service.dispatch('execute', dict(args, request_id=request_id, review_id=review['review_id'],
        approved_effects=review['effects'] if approve else []))


def finish(console):
    service, owner, _, args = console
    job = owner.submissions[-1]
    public = service.execute_job(job)
    state = 'succeeded' if public['ok'] else 'unknown' if public['code'] == 'OUTCOME_UNKNOWN' else 'failed'
    owner.receipts[job['request_id']].update(public, state=state)
    private = service.dispatch('result', dict(args, operation_id=job['request_id']))
    return public, private


def test_native_data_is_private_and_acceptance_contains_only_references(console):
    service, owner, native, args = console
    preview = prepare(console, request('/credentials'))
    assert preview['effects'] == []
    assert not native.calls
    receipt = execute(console, preview)
    assert receipt['state'] == 'pending'
    assert not native.calls
    public, private = finish(console)
    assert private['result']['body']['value'] == {'native': 'GENERATED_PROVIDER_SECRET'}
    assert public == {'ok': True, 'code': 'OK', 'result_ready': True}
    assert 'GENERATED_PROVIDER_SECRET' not in json.dumps(owner.submissions + list(owner.receipts.values()))
    assert set(owner.submissions[0]) == {'action', 'request_id', 'expected_generation', 'lease_id', 'review_id', '_scope'}
    different_document = dict(args, _scope=dict(args['_scope'], document_id=str(uuid.uuid4())))
    different_document['document_id'] = different_document['_scope']['document_id']
    assert service.dispatch('result', dict(different_document, operation_id=receipt['operation_id']))['code'] == 'STALE_SCOPE'


@pytest.mark.parametrize('change,code', [
    ({'consent': False}, 'CONSENT_REQUIRED'),
    ({'expected_generation': 9}, 'STALE_GENERATION'),
])
def test_unlock_is_explicit_and_generation_bound(console, change, code):
    service, _, _, args = console
    result = service.dispatch('unlock', dict(args, request_id='other-unlock', expected_generation=10, consent=True) | change)
    assert result['code'] == code


def test_scope_epoch_generation_and_policy_reject_before_native_execution(console):
    service, owner, native, args = console
    preview = prepare(console, request('/credentials'))
    changed = dict(args, _scope=dict(args['_scope'], selection_epoch=2))
    assert service.dispatch('execute', dict(changed, request_id='generated-operation',
        review_id=preview['review_id'], approved_effects=[]))['code'] == 'STALE_SCOPE'
    owner.generation += 1
    assert execute(console, preview)['code'] == 'STALE_GENERATION'
    owner.generation -= 1
    owner.allowed = False
    assert execute(console, preview)['code'] == 'POLICY_LOCKED'
    assert not native.calls


def test_lock_revokes_unaccepted_reviews(console):
    service, owner, native, args = console
    preview = prepare(console, request('/credentials', 'DELETE', {'names': ['generated.json']}))
    assert service.dispatch('lock', args)['locked']
    assert execute(console, preview)['code'] in ('LEASE_EXPIRED', 'STALE_SCOPE')
    assert not owner.submissions and not native.calls


def test_ordered_config_save_uses_one_native_prepare_and_commit(console):
    service, owner, native, args = console
    revision = hashlib.sha256(owner.settings.config_path.read_bytes()).hexdigest()
    requests = [request('/config', 'PATCH', {'server': {'host': '127.0.0.1'}}),
                request('/config/access/api-keys', 'PUT', []),
                request('/config/oauth/model-alias/claude', 'DELETE')]
    preview = service.dispatch('prepare', dict(args, request_id='transaction', requests=requests, expected_revision=revision))
    assert preview['ok']
    assert {'config_write', 'restart_required', 'public_exposure', 'authentication_changed'} <= set(preview['effects'])
    assert not native.calls and not owner.transactions
    assert [r['method'] for r in owner.preparations[0][1]] == ['PATCH', 'PUT', 'DELETE']
    assert execute(console, preview, request_id='transaction')['ok']
    public, private = finish(console)
    assert public['ok']
    assert len(owner.transactions) == 1
    assert owner.transactions[0][1] == owner.preparations[0][1]
    assert owner.transactions[0][2] == {'reviewed_candidate_revision': 'b' * 64, 'restart_consent': True}
    assert private['result']['revision'] == 'b' * 64
    assert 'candidate' not in json.dumps(owner.submissions)


def test_config_requires_cas_and_detects_external_drift(console):
    _, owner, native, _ = console
    assert prepare(console, request('/config/access/api-keys', 'PUT', []))['code'] == 'STALE_REVISION'
    revision = hashlib.sha256(owner.settings.config_path.read_bytes()).hexdigest()
    preview = prepare(console, request('/config/access/api-keys', 'PUT', [], expected_revision=revision))
    owner.settings.config_path.write_bytes(b'config-version: 8\nchanged: true\n')
    assert execute(console, preview)['code'] == 'STALE_REVISION'
    assert not native.calls and not owner.transactions


@pytest.mark.parametrize('url,effect', [
    ('https://api.x.ai/v1/chat/completions', 'paid_inference'),
    ('https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume', 'credit_consume'),
    ('https://api.anthropic.com/api/organizations/generated/reset_rate_limits', 'entitlement_claim'),
    ('https://generated.example/custom', 'external_mutation'),
])
def test_read_looking_quota_workflows_receive_real_effect_review(console, url, effect):
    _, owner, native, _ = console
    preview = prepare(console, request('/requests/api-call', 'POST', {
        'method': 'POST', 'url': url, 'auth_index': 'generated-index',
        'header': {'Authorization': 'Bearer $TOKEN$'}, 'data': '{}'}))
    assert {effect, 'network_destination'} <= set(preview['effects'])
    assert execute(console, preview, approve=False)['code'] == 'CONSENT_REQUIRED'
    assert not owner.submissions and not native.calls


def test_executing_transport_receives_exact_egress_grant(console):
    _, _, native, _ = console
    preview = prepare(console, request('/requests/api-call', 'POST', {
        'method': 'POST', 'url': 'https://generated.example/start?secret=GENERATED_QUERY',
        'proxy_url': 'socks5://proxy.example:1080', 'auth_index': 'generated-index',
        'header': {'Host': 'virtual.example', 'Authorization': 'Bearer $TOKEN$'}, 'data': '{}'}),
        egress_destinations=[{'scheme': 'https', 'host': 'redirect.example'}])
    assert 'GENERATED_QUERY' not in preview['summary']
    assert 'proxy.example' in preview['summary'] and 'virtual.example' in preview['summary']
    assert 'cross_origin_redirect' in preview['effects']
    execute(console, preview)
    finish(console)
    policy = native.calls[-1][2]['headers']['X-Hermes-Egress-Policy']
    policy = json.loads(base64.urlsafe_b64decode(policy + '=' * (-len(policy) % 4)))
    assert policy['destinations'] == [
        {'scheme': 'https', 'host': 'generated.example', 'port': 443, 'path_prefix': '/'},
        {'scheme': 'https', 'host': 'redirect.example', 'port': 443, 'path_prefix': '/'}]
    assert policy['redirects'] == 'allowed' and policy['max_redirects'] == 5
    assert policy['methods'] == ['POST', 'GET']
    assert policy['proxy'] == {'mode': 'explicit', 'url': 'socks5://proxy.example:1080'}
    assert policy['host_header'] == ['virtual.example'] and policy['auth_index'] == 'generated-index'


def test_unknown_outcome_is_not_replayed_even_after_generic_receipt_retirement(console):
    _, owner, native, _ = console
    error = RuntimeError('GENERATED_PRIVATE_NETWORK_ERROR')
    error.code = 'OUTCOME_UNKNOWN'
    native.failure = error
    preview = prepare(console, request('/credentials/refresh', 'POST', {'name': 'generated.json'}))
    execute(console, preview)
    public, private = finish(console)
    assert public['code'] == 'OUTCOME_UNKNOWN'
    assert private['state'] == 'unknown' and 'GENERATED_PRIVATE' not in json.dumps(private)
    owner.receipts.clear()
    assert execute(console, preview)['code'] == 'RECONCILE_REQUIRED'
    assert len(owner.submissions) == 1 and len(native.calls) == 1


def test_resource_conflict_and_released_terminal_reservation(console):
    one = prepare(console, request('/credentials/refresh', 'POST', {'name': 'one.json'}), 'one')
    two = prepare(console, request('/credentials/refresh', 'POST', {'name': 'two.json'}), 'two')
    assert execute(console, one, 'one')['ok']
    assert execute(console, two, 'two')['code'] == 'RESOURCE_CONFLICT'
    finish(console)
    assert execute(console, two, 'two')['code'] == 'RESOURCE_CONFLICT'
    three = prepare(console, request('/credentials/refresh', 'POST', {'name': 'three.json'}), 'three')
    assert execute(console, three, 'three')['ok']


def test_ten_mib_file_roundtrip_and_batch_split(console):
    _, _, native, _ = console
    raw = b'g' * FILE_LIMIT
    file = {'name': 'file', 'filename': 'generated.json', 'content_type': 'application/json',
            'base64': base64.b64encode(raw).decode()}
    wire = request('/credentials', 'POST')
    wire['body'] = {'kind': 'multipart', 'fields': {}, 'files': [file]}
    preview = prepare(console, wire)
    assert preview['ok']
    execute(console, preview)
    finish(console)
    assert raw in native.calls[-1][2]['body']
    assert native.calls[-1][2]['content_type'].startswith('multipart/form-data; boundary=')
    wire['body']['files'] = [file, file]
    assert prepare(console, wire, 'batch')['code'] == 'BATCH_SPLIT_REQUIRED'
    file['base64'] = base64.b64encode(raw + b'x').decode()
    wire['body']['files'] = [file]
    assert prepare(console, wire, 'oversize')['code'] == 'PAYLOAD_TOO_LARGE'


def test_native_binary_download_preserves_bytes(console):
    _, _, native, _ = console
    raw = bytes(range(256)) * 4096
    native.responses['GET', PREFIX + '/credentials/download'] = raw
    preview = prepare(console, request('/credentials/download', query={'name': 'generated.json'}, response_kind='bytes'))
    assert 'credential_export' in preview['effects']
    execute(console, preview)
    _, result = finish(console)
    assert base64.b64decode(result['result']['body']['base64']) == raw


def test_oauth_state_callback_browser_and_lock_are_owned(console):
    service, _, native, args = console
    native.responses['GET', PREFIX + '/oauth/auth-url'] = {
        'url': 'https://login.example/authorize?state=generated-state', 'state': 'generated-state',
        'user_code': 'GENERATED-DEVICE-CODE', 'expires_in': 1800}
    start = prepare(console, request('/oauth/auth-url', query={'provider': 'codex'}), 'oauth-start')
    assert 'oauth_start' in start['effects']
    execute(console, start, 'oauth-start')
    finish(console)
    assert prepare(console, request('/oauth/status', query={'state': 'not-owned'}), 'bad-state')['code'] == 'OAUTH_STATE_MISMATCH'
    assert prepare(console, request('/oauth/callback', 'POST', {
        'provider': 'anthropic', 'redirect_url': 'http://127.0.0.1/callback?state=generated-state&code=generated'}),
        'wrong-provider')['code'] == 'OAUTH_PROVIDER_MISMATCH'
    callback = prepare(console, request('/oauth/callback', 'POST', {
        'provider': 'codex', 'redirect_url': 'http://127.0.0.1/callback?state=generated-state&code=generated'}), 'callback')
    assert callback['ok']
    browser = service.dispatch('browser', dict(args, action='open', value='https://login.example/authorize?state=generated-state'))
    assert browser['requires_confirmation'] is False
    copied = service.dispatch('browser', dict(args, action='copy', value='GENERATED_PROVIDER_SECRET'))
    assert copied['effects'] == ['data_export']
    service.dispatch('lock', args)
    assert ('DELETE', PREFIX + '/oauth/session', {'state': 'generated-state'}) in [
        (m, p, a.get('query')) for m, p, a in native.calls]


def test_plugin_routes_are_native_declared_and_effect_reviewed(console):
    service, _, native, args = console
    page = '/v0/resource/plugins/generated/index.html'
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': '0123456789abcdef-1',
        'management': [{'method': 'POST', 'path': '/v0/management/generated/run', 'plugin_id': 'generated'}],
        'resources': [{'method': 'GET', 'path': page, 'plugin_id': 'generated'},
                      {'method': 'GET', 'path': '/v0/resource/plugins/generated/app.js', 'plugin_id': 'generated'}]}
    native.responses['GET', PREFIX + '/plugins'] = {'plugins': [
        {'id': 'generated', 'effective_enabled': True, 'menus': [{'path': page}]}]}
    preview = prepare(console, request('/v0/management/generated/run', 'POST', {}))
    assert {'plugin_code', 'network_destination'} <= set(preview['effects'])
    assert prepare(console, request('/v0/management/unregistered/run', 'POST', {}), 'unregistered')['code'] == 'ROUTE_NOT_ALLOWED'
    plugin = service.dispatch('plugin', dict(args, plugin_id='generated', menu_index=0))
    assert plugin['effects'] == ['plugin_code']
    result = service.dispatch('plugin', dict(args, review_id=plugin['review_id'], approved_effects=plugin['effects']))
    assert result['entry_path'] == page and result['kind'] == 'native-resource'
    assert isinstance(result['grant_id'], str)
    assert 'path' not in result and 'resources' not in result
    assert 'lease_id' not in result


def test_inference_models_use_separate_private_inference_channel(console):
    _, owner, native, _ = console
    preview = prepare(console, request('/v1/models'))
    execute(console, preview)
    finish(console)
    assert not native.calls
    assert owner.inference.calls[0][:2] == ('GET', '/v1/models')


@pytest.mark.parametrize('wire', [
    request('/v1/chat/completions', 'POST', {}),
    request('/v8/management/../../hermes/health'),
    request('/v8/management/config%2f..%2fcredentials'),
    request('/v0/management/config', 'PUT', {}),
    request('/credentials') | {'headers': {'Authorization': 'GENERATED_PRIVATE_KEY'}},
    request('/credentials') | {'path': 'https://arbitrary.example'},
])
def test_no_blanket_proxy_or_caller_authority(console, wire):
    _, owner, native, _ = console
    result = prepare(console, wire)
    assert result['ok'] is False
    assert 'GENERATED_PRIVATE_KEY' not in json.dumps(result)
    assert not owner.submissions
    assert not native.calls or all(path == '/hermes/plugin-routes' for _, path, _ in native.calls)


def test_original_config_receipt_survives_generation_change(console):
    service, owner, _, args = console
    revision = hashlib.sha256(owner.settings.config_path.read_bytes()).hexdigest()
    preview = prepare(console, request('/config/server/port', 'PUT', 8320, expected_revision=revision))
    execute(console, preview)
    original_commit = owner.config_transaction

    def restarting_commit(*args, **kwargs):
        outcome = original_commit(*args, **kwargs)
        owner.generation += 1
        outcome['generation'] = owner.generation
        return outcome

    owner.config_transaction = restarting_commit
    public, private = finish(console)
    assert public['ok'] is True and private['state'] == 'succeeded'
    assert private['requires_unlock'] is True
    assert private['result']['body']['value']['code'] == 'OK'
    assert private['result']['revision'] == 'b' * 64
    assert prepare(console, request('/credentials'), 'after-restart')['code'] in ('STALE_GENERATION', 'LEASE_EXPIRED')
    wrong_scope = dict(args, _scope=dict(args['_scope'], principal='another-principal'))
    assert service.dispatch('result', dict(wrong_scope, operation_id='generated-operation'))['code'] == 'STALE_SCOPE'


def test_observed_native_success_not_rewritten_after_inflight_revocation(console):
    service, owner, native, args = console
    preview = prepare(console, request('/credentials/refresh', 'POST', {'name': 'generated.json'}))
    execute(console, preview)
    original_request = native.request

    def revoke_after_effect(*args, **kwargs):
        response = original_request(*args, **kwargs)
        owner.allowed = False
        return response

    native.request = revoke_after_effect
    public, private = finish(console)
    assert public == {'ok': True, 'code': 'OK', 'result_ready': True}
    assert private['state'] == 'succeeded'
    assert private['requires_unlock'] and private['result_withheld']
    assert 'result' not in private
    assert len(native.calls) == 1


def test_request_id_collision_cannot_reassign_private_result_to_another_lease(console):
    service, owner, _, args = console
    preview = prepare(console, request('/credentials'))
    execute(console, preview)
    other_scope = dict(args['_scope'], connection_id='second-connection', document_id=str(uuid.uuid4()))
    unlocked = service.dispatch('unlock', {'_scope': other_scope, 'document_id': other_scope['document_id'],
        'request_id': 'second-unlock', 'expected_generation': 10, 'consent': True})
    other = {'_scope': other_scope, 'document_id': other_scope['document_id'], 'lease_id': unlocked['lease_id']}
    second = service.dispatch('prepare', dict(other, request_id='generated-operation', request=request('/credentials')))
    rejected = service.dispatch('execute', dict(other, request_id='generated-operation',
        review_id=second['review_id'], approved_effects=second['effects']))
    assert rejected['code'] == 'REQUEST_CONFLICT'
    finish(console)
    assert service.dispatch('result', dict(other, operation_id='generated-operation'))['code'] == 'RECONCILE_REQUIRED'
    assert len(owner.submissions) == 1


def test_lease_expiry_rejects_new_native_work(console, monkeypatch):
    import hermes_cpa.console_service as module
    service, owner, native, args = console
    monkeypatch.setattr(module, 'LEASE_SECONDS', 0)
    scope = dict(args['_scope'], connection_id='expired-connection', document_id=str(uuid.uuid4()))
    unlocked = service.dispatch('unlock', {'_scope': scope, 'document_id': scope['document_id'],
        'request_id': 'expired-unlock', 'expected_generation': 10, 'consent': True})
    expired = {'_scope': scope, 'document_id': scope['document_id'], 'lease_id': unlocked['lease_id']}
    result = service.dispatch('prepare', dict(expired, request_id='expired-read', request=request('/credentials')))
    assert result['code'] in ('LEASE_EXPIRED', 'STALE_SCOPE')
    assert not owner.submissions and not native.calls


def test_real_supervisor_ledger_and_events_never_expose_native_results(tmp_path):
    import threading
    import time
    from hermes_cpa.settings import CpaSettings
    from hermes_cpa.supervisor import CpaSupervisor

    settings = CpaSettings(tmp_path)
    settings.config_path.parent.mkdir(parents=True)
    settings.config_path.write_bytes(b'config-version: 8\n')
    native = NativeFixture()
    owner = CpaSupervisor(settings, mode='managed', policy=lambda: True,
                          console=lambda supervisor, transport: OwnerConsoleService(supervisor, native))
    scope = {'principal': 'generated-principal', 'home': str(tmp_path), 'scope_id': 'generated-scope',
             'connection_id': 'generated-host', 'selection_epoch': 1, 'document_id': str(uuid.uuid4())}

    def call(op, payload):
        return owner.dispatch({'cmd': 'console.' + op, 'args': dict(payload, _scope=scope, document_id=scope['document_id'])})

    try:
        unlocked = call('unlock', {'request_id': 'unlock', 'expected_generation': owner.generation, 'consent': True})
        lease = {'lease_id': unlocked['lease_id']}
        preview = call('prepare', dict(lease, request_id='native-read', request=request('/credentials')))
        accepted = call('execute', dict(lease, request_id='native-read', review_id=preview['review_id'],
                                       approved_effects=preview['effects']))
        assert accepted['state'] in ('pending', 'running', 'succeeded')
        deadline = time.monotonic() + 2
        pause = threading.Event()
        while True:
            generic = owner.dispatch({'cmd': 'operation', 'args': {'operation_id': 'native-read'}})
            if generic.get('state') not in ('pending', 'running'):
                break
            assert time.monotonic() < deadline, generic
            pause.wait(.005)
        assert generic['state'] == 'succeeded'
        events = owner.dispatch({'cmd': 'events', 'args': {}})
        assert 'GENERATED_PROVIDER_SECRET' not in json.dumps([generic, events, accepted])
        private = call('result', dict(lease, operation_id='native-read'))
        assert private['result']['body']['value']['native'] == 'GENERATED_PROVIDER_SECRET'
    finally:
        owner.dispatch({'cmd': 'shutdown', 'args': {}})
        owner.console.close()


def test_native_revision_and_plugin_headers_survive_error_response(console):
    _, _, native, _ = console
    revision = 'a' * 64

    def absent_config(method, path, **kwargs):
        return SimpleNamespace(status=404, headers={
            'X-Hermes-Config-Revision': revision, 'X-CPA-SUPPORT-PLUGIN': 'true',
            'X-CPA-VERSION': '8.0.21', 'X-CPA-BUILD-DATE': 'generated-date',
            'X-Server-Version': 'generated-alias', 'X-Server-Build-Date': 'generated-alias-date',
            'Set-Cookie': 'GENERATED_PRIVATE_COOKIE', 'Authorization': 'GENERATED_PRIVATE_AUTH'},
            body=b'{"error":"not_found"}')

    native.request = absent_config
    preview = prepare(console, request('/config/oauth/model-alias/generated'))
    execute(console, preview)
    _, private = finish(console)
    response = private['result']
    assert response['status'] == 404 and response['body']['value'] == {'error': 'not_found'}
    assert response['revision'] == revision
    assert response['headers']['x-cpa-support-plugin'] == 'true'
    assert response['headers']['x-cpa-version'] == '8.0.21'
    assert response['headers']['x-server-version'] == 'generated-alias'
    assert 'GENERATED_PRIVATE' not in json.dumps(response)


def test_oauth_ownership_cannot_be_bypassed_by_requesting_text(console):
    service, _, native, args = console
    native.responses['GET', PREFIX + '/oauth/auth-url'] = {
        'url': 'https://login.example/?state=text-state', 'state': 'text-state', 'flow': 'device',
        'user_code': 'GENERATED-CODE', 'expires_in': 1800}
    preview = prepare(console, request('/oauth/auth-url', query={'provider': 'kimi'}, response_kind='text'))
    execute(console, preview)
    _, private = finish(console)
    assert private['result']['body']['kind'] == 'text'
    browser = service.dispatch('browser', dict(args, action='copy', value='GENERATED-CODE'))
    assert browser['requires_confirmation'] is False
    service.dispatch('lock', args)
    assert any(method == 'DELETE' and path == PREFIX + '/oauth/session' and
               options['query']['state'] == 'text-state' for method, path, options in native.calls)


@pytest.mark.parametrize('path,method', [
    ('/config', 'GET'), ('/config.yaml', 'GET'), ('/config/access/api-keys', 'GET'),
    ('/config/api-keys/claude', 'GET'), ('/config/oauth/excluded-models', 'GET'),
    ('/config/oauth/model-alias', 'GET'), ('/credentials', 'GET'),
    ('/credentials/models', 'GET'), ('/credentials/download', 'GET'),
    ('/credentials/status', 'PATCH'), ('/credentials/fields', 'PATCH'),
    ('/credentials/refresh', 'POST'), ('/credentials', 'DELETE'),
    ('/routing/cooldown/reset', 'POST'), ('/routing/model-definitions/claude', 'GET'),
    ('/observability/logs', 'GET'), ('/observability/logs', 'DELETE'),
    ('/observability/logs/errors', 'GET'), ('/observability/logs/errors/generated.log', 'GET'),
    ('/observability/logs/requests/generated-id', 'GET'),
    ('/observability/usage/api-keys', 'GET'), ('/observability/usage/queue', 'GET'),
    ('/server/latest-version', 'GET'), ('/plugins', 'GET'), ('/plugins/store', 'GET'),
    ('/plugins/store/generated/install', 'POST'), ('/plugins/generated', 'DELETE'),
    ('/plugins/generated/quota', 'GET'), ('/plugins/generated/quota', 'POST'),
    ('/plugins/generated/quota', 'DELETE'),
])
def test_pinned_native_registry_is_available_after_unlock(console, path, method):
    if path.endswith('/quota'):
        console[2].responses['GET', '/hermes/plugin-routes'] = {
            'registry_revision': '0123456789abcdef-1', 'management': [], 'resources': [],
            'dispatches': [{'plugin_id': 'generated', 'quota': True}]}
    preview = prepare(console, request(path, method, {} if method in ('POST', 'PATCH') else None))
    assert preview['ok'], preview


@pytest.mark.parametrize('path', [
    '/v0/management/generated-sso-auth-url',
    PREFIX + '/oauth/auth-url',
])
def test_plugin_oauth_uses_current_native_dispatch_catalog(console, path):
    service, _, native, args = console
    revision = '0123456789abcdef-1'
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': revision, 'management': [], 'resources': [],
        'dispatches': [{'plugin_id': 'generated-plugin', 'oauth_provider': 'generated-sso', 'quota': False}]}
    native.responses['GET', path] = SimpleNamespace(status=200, headers={
        'X-Hermes-Plugin-Revision': revision, 'X-Hermes-Plugin-Invoked': '1'},
        body=json.dumps({'url': 'https://generated-sso.example/authorize',
                         'state': 'generated-plugin-state'}).encode())
    query = {'provider': 'generated-sso'} if path == PREFIX + '/oauth/auth-url' else {}
    preview = prepare(console, request(path, query=query), 'plugin-oauth')
    assert {'oauth_start', 'plugin_code', 'network_destination'} <= set(preview['effects'])
    assert 'Installed plugin: generated-plugin' in preview['summary']
    execute(console, preview, 'plugin-oauth')
    public, _ = finish(console)
    assert public['ok']
    assert native.calls[-1][2]['headers'] == {'X-Hermes-Plugin-Revision': revision}
    browser = service.dispatch('browser', dict(args, action='open', value='https://generated-sso.example/authorize'))
    assert browser['requires_confirmation'] is False
    denied = prepare(console, request('/v0/management/not-recorded-auth-url'), 'unregistered-oauth')
    assert denied['code'] == 'ROUTE_NOT_ALLOWED'


def test_vertex_import_preserves_native_location_and_file(console):
    _, _, native, _ = console
    raw = b'{\"type\":\"service_account\",\"private_key\":\"GENERATED_KEY\"}'
    wire = request('/oauth/import', 'POST', query={'provider': 'vertex'})
    wire['body'] = {'kind': 'multipart', 'fields': {'location': 'us-central1'}, 'files': [{
        'name': 'file', 'filename': 'generated-vertex.json', 'content_type': 'application/json',
        'base64': base64.b64encode(raw).decode()}]}
    preview = prepare(console, wire)
    assert preview['effects'] == ['credential_import']
    execute(console, preview)
    finish(console)
    method, path, options = native.calls[-1]
    assert (method, path) == ('POST', PREFIX + '/oauth/import')
    assert options['query'] == {'provider': 'vertex'}
    assert raw in options['body'] and b'us-central1' in options['body']


def resource_grant(console, *, entry='index.html', resources=None):
    service, _, native, lease_args = console
    root = '/v0/resource/plugins/generated/'
    names = resources if resources is not None else ('index.html', 'style.css', 'main.js', 'chunk.js', 'image.bin', 'save')
    native.responses['GET', PREFIX + '/plugins'] = {'plugins': [{
        'id': 'generated', 'effective_enabled': True, 'menus': [{'path': root + entry}]}]}
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': '0123456789abcdef-1', 'management': [],
        'resources': [{'method': 'GET', 'path': root + name, 'plugin_id': 'generated'}
                      for name in names]}
    preview = service.dispatch('plugin', dict(lease_args, plugin_id='generated', menu_index=0))
    assert preview['effects'] == ['plugin_code']
    assert 'GET is not read-only' in preview['summary']
    assert 'credentials' in preview['summary'] and 'consume account entitlements' in preview['summary']
    grant = service.dispatch('plugin', dict(lease_args, review_id=preview['review_id'],
                                            approved_effects=preview['effects']))
    assert grant['kind'] == 'native-resource'
    assert set(grant) == {'ok', 'kind', 'grant_id', 'entry_path', 'plugin_id', 'expires_at'}
    assert grant['grant_id'] != lease_args['lease_id']
    args = {'_scope': lease_args['_scope'], 'document_id': lease_args['document_id'], 'grant_id': grant['grant_id']}
    return grant, args


def test_resource_delivery_preserves_native_bytes_routes_queries_and_header_isolation(console):
    service, owner, native, lease_args = console
    grant, args = resource_grant(console)
    root = '/v0/resource/plugins/generated/'
    fixtures = {
        'index.html': (b'<link href="style.css"><script type="module" src="/v0/resource/plugins/generated/main.js"></script>'
                       b'<form action="save"><input name="op" value="save"></form>', 'text/html'),
        'style.css': (b'body { background: url(image.bin); }', 'text/css'),
        'main.js': (b'import "./chunk.js"; import("./chunk.js"); fetch("save?op=save");', 'text/javascript'),
        'chunk.js': (b'export const generated = true;', 'text/javascript'),
        'image.bin': (bytes(range(256)), 'application/octet-stream'),
    }
    before = len(native.calls)
    for name, (body, media) in fixtures.items():
        native.responses['GET', root + name] = SimpleNamespace(status=200, body=body, headers={
            'Content-Type': media, 'Content-Encoding': 'identity', 'Content-Length': '999999',
            'Set-Cookie': 'GENERATED_FORBIDDEN_COOKIE', 'WWW-Authenticate': 'GENERATED_PRIVATE_AUTH',
            'Refresh': '0; url=https://forbidden.example', 'Content-Security-Policy': "default-src *",
            'Access-Control-Allow-Credentials': 'true', 'Connection': 'keep-alive'})
        result = service.dispatch('resource', dict(args, path=root + name, query=[
            ['profile', 'must-stay-native'], ['x', 'one'], ['x', 'two']], accept=media))
        assert result['ok'], result
        assert base64.b64decode(result['body_base64']) == body
        assert result['headers']['content-type'] == media
        assert result['headers']['content-encoding'] == 'identity'
        assert result['headers']['cache-control'] == 'no-store'
        assert result['headers']['referrer-policy'] == 'no-referrer'
        assert result['headers']['x-content-type-options'] == 'nosniff'
        assert not set(result['headers']) & {
            'set-cookie', 'www-authenticate', 'refresh', 'content-length', 'content-security-policy',
            'access-control-allow-credentials', 'connection'}
    resource_calls = [(m, p, options) for m, p, options in native.calls[before:] if p.startswith(root)]
    assert len(resource_calls) == len(fixtures)
    assert all(options['headers'].keys() == {'Accept', 'X-Hermes-Resource-Revision'}
               and options['headers']['X-Hermes-Resource-Revision'] == '0123456789abcdef-1'
               for _, _, options in resource_calls)
    assert all(options['query'] == [('profile', 'must-stay-native'), ('x', 'one'), ('x', 'two')]
               for _, _, options in resource_calls)
    assert all(m == 'GET' for m, _, _ in resource_calls)
    assert str(owner.settings.home) == lease_args['_scope']['home']
    assert not owner.submissions


@pytest.mark.parametrize('path', [
    '/v8/management/config', '/v1/models', '/hermes/health',
    '/v0/resource/plugins/generated/not-declared', '/v0/resource/plugins/other/index.html',
])
def test_resource_grant_never_authorizes_management_inference_or_unregistered_routes(console, path):
    service, _, native, _ = console
    _, args = resource_grant(console)
    before = len(native.calls)
    result = service.dispatch('resource', dict(args, path=path, query=[]))
    assert result['ok'] is False and result['effect_invoked'] is False
    assert not any(p == path for _, p, _ in native.calls[before:])


def test_resource_get_save_is_effectful_and_not_replayed(console):
    service, _, native, _ = console
    grant, args = resource_grant(console)
    path = grant['entry_path'].rsplit('/', 1)[0] + '/save'
    original_request = native.request
    effects = []

    def save(method, target, **options):
        if target == path:
            effects.append(options['query'])
            error = RuntimeError('GENERATED_PRIVATE_HANDLER_ERROR')
            error.code = 'OUTCOME_UNKNOWN'
            raise error
        return original_request(method, target, **options)

    native.request = save
    result = service.dispatch('resource', dict(args, path=path, query=[['op', 'save']]))
    assert result == {'ok': False, 'code': 'OUTCOME_UNKNOWN', 'effect_invoked': True, 'response_withheld': False}
    assert effects == [[('op', 'save')]]
    assert 'GENERATED_PRIVATE_HANDLER_ERROR' not in json.dumps(result)


@pytest.mark.parametrize('location,allowed', [
    ('./main.js?x=one&x=two#fragment', True),
    ('/v0/resource/plugins/generated/chunk.js', True),
    ('https://outside.example/?secret=GENERATED_SECRET', False),
    ('//outside.example/generated', False),
    ('///v0/resource/plugins/generated/main.js', False),
    ('/v8/management/config', False),
    ('/v0/resource/plugins/generated/not-declared', False),
])
def test_resource_redirects_are_returned_only_for_exact_same_grant_routes(console, location, allowed):
    service, _, native, _ = console
    grant, args = resource_grant(console)
    native.responses['GET', grant['entry_path']] = SimpleNamespace(
        status=302, body=b'generated redirect body', headers={'Location': location})
    before = len(native.calls)
    result = service.dispatch('resource', dict(args, path=grant['entry_path'], query=[]))
    assert result['ok'] is allowed
    if allowed:
        assert result['status'] == 302
        assert result['headers']['location'].startswith('/v0/resource/plugins/generated/')
        assert base64.b64decode(result['body_base64']) == b'generated redirect body'
    else:
        assert result['code'] == 'REDIRECT_DENIED'
        assert result['effect_invoked'] and result['response_withheld']
        assert 'GENERATED_SECRET' not in json.dumps(result)
    assert len([p for _, p, _ in native.calls[before:] if p.startswith('/v0/resource/')]) == 1


@pytest.mark.parametrize('change', ['registry', 'generation', 'policy', 'close'])
def test_resource_revalidates_before_revealing_native_bytes(console, change):
    service, owner, native, _ = console
    grant, args = resource_grant(console)
    original_request = native.request
    calls = []

    def revoke_after_handler(method, path, **options):
        if path == grant['entry_path']:
            calls.append(path)
            if change == 'registry':
                native.responses['GET', '/hermes/plugin-routes']['registry_revision'] = '0123456789abcdef-2'
            elif change == 'generation':
                owner.generation += 1
            elif change == 'policy':
                owner.allowed = False
            else:
                assert service.dispatch('resource_close', args)['closed']
            return SimpleNamespace(status=200, body=b'GENERATED_NATIVE_SECRET', headers={
                'Content-Type': 'text/html', 'X-Hermes-Resource-Invoked': '1',
                'X-Hermes-Resource-Revision': '0123456789abcdef-1'})
        return original_request(method, path, **options)

    native.request = revoke_after_handler
    result = service.dispatch('resource', dict(args, path=grant['entry_path'], query=[]))
    assert result['ok'] is False
    assert result['effect_invoked'] is True and result['response_withheld'] is True
    assert result['code'] in ('RESOURCE_REVOKED', 'LEASE_EXPIRED', 'POLICY_LOCKED', 'STALE_GENERATION')
    assert 'body_base64' not in result and 'GENERATED_NATIVE_SECRET' not in json.dumps(result)
    assert calls == [grant['entry_path']]


def test_resource_grants_revoke_with_lock_and_scope_cannot_be_rebound(console):
    service, _, native, lease_args = console
    grant, args = resource_grant(console)
    changed_scope = dict(args['_scope'], selection_epoch=args['_scope']['selection_epoch'] + 1)
    before = len(native.calls)
    result = service.dispatch('resource', dict(args, _scope=changed_scope, path=grant['entry_path'], query=[]))
    assert result['code'] == 'STALE_SCOPE' and not result['effect_invoked']
    assert len(native.calls) == before
    assert service.dispatch('lock', lease_args)['locked']
    result = service.dispatch('resource', dict(args, path=grant['entry_path'], query=[]))
    assert result['code'] == 'RESOURCE_GONE' and not result['effect_invoked']
    assert service.dispatch('resource_close', args) == {'ok': True, 'closed': True}


def test_resource_registry_replacement_rejects_before_handler_invocation(console):
    service, _, native, _ = console
    grant, args = resource_grant(console)
    native.responses['GET', '/hermes/plugin-routes']['registry_revision'] = '0123456789abcdef-2'
    before = len(native.calls)
    result = service.dispatch('resource', dict(args, path=grant['entry_path'], query=[]))
    assert result['code'] == 'RESOURCE_REVOKED' and result['effect_invoked'] is False
    assert not any(path == grant['entry_path'] for _, path, _ in native.calls[before:])


def test_resource_grant_expiry_is_no_later_than_console_lease(console, monkeypatch):
    import hermes_cpa.console_service as service_module
    import time
    service, _, _, lease_args = console
    monkeypatch.setattr(service_module, 'LEASE_SECONDS', 60)
    unlocked = service.dispatch('unlock', dict(lease_args, request_id='short-resource-lease',
                                              expected_generation=10, consent=True))
    short_args = dict(lease_args, lease_id=unlocked['lease_id'])
    short_console = (console[0], console[1], console[2], short_args)
    grant, args = resource_grant(short_console)
    assert 0 < grant['expires_at'] - time.time() <= 60
    assert grant['expires_at'] <= unlocked['expires_at']
    clock = time.monotonic
    monkeypatch.setattr(time, 'monotonic', lambda: clock() + 301)
    result = service.dispatch('resource', dict(args, path=grant['entry_path'], query=[]))
    assert result['code'] == 'RESOURCE_GONE' and not result['effect_invoked']


def test_resource_invocation_conflicts_with_console_config_mutation(console):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    service, owner, native, _ = console
    grant, args = resource_grant(console)
    revision = hashlib.sha256(owner.settings.config_path.read_bytes()).hexdigest()
    preview = prepare(console, request('/config/server/port', 'PUT', 8320, expected_revision=revision), 'configuration')
    entered, release = threading.Event(), threading.Event()
    original_request = native.request

    def blocking_resource(method, path, **options):
        if path == grant['entry_path']:
            entered.set()
            assert release.wait(2)
        return original_request(method, path, **options)

    native.request = blocking_resource
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.dispatch, 'resource', dict(args, path=grant['entry_path'], query=[]))
        try:
            assert entered.wait(2)
            assert execute(console, preview, 'configuration')['code'] == 'RESOURCE_CONFLICT'
        finally:
            release.set()
        assert pending.result(timeout=2)['ok']
    assert not owner.transactions


def test_resource_binary_envelope_preserves_compressed_body_and_explicit_limit(console, monkeypatch):
    import gzip
    import hermes_cpa.console_resources as resource_module
    service, _, native, _ = console
    grant, args = resource_grant(console)
    path = grant['entry_path'].rsplit('/', 1)[0] + '/image.bin'
    body = gzip.compress(bytes(range(256)) * 8192)
    native.responses['GET', path] = SimpleNamespace(status=200, body=body, headers={
        'Content-Type': 'application/octet-stream', 'Content-Encoding': 'gzip',
        'Content-Disposition': 'inline; filename="generated.bin"'})
    result = service.dispatch('resource', dict(args, path=path, query=[]))
    assert result['ok']
    assert base64.b64decode(result['body_base64']) == body
    assert result['headers']['content-encoding'] == 'gzip'
    assert result['headers']['content-disposition'] == 'inline; filename="generated.bin"'
    assert resource_module.RESULT_LIMIT == 32 * 1024 * 1024
    monkeypatch.setattr(resource_module, 'RESULT_LIMIT', len(body) - 1)
    rejected = service.dispatch('resource', dict(args, path=path, query=[]))
    assert rejected['code'] == 'RESPONSE_TOO_LARGE'
    assert rejected['effect_invoked'] and rejected['response_withheld']
    assert 'body_base64' not in rejected


def test_plugin_review_cannot_mint_resource_grant_after_registry_replacement(console):
    service, _, native, lease_args = console
    resource_grant(console)
    review = service.dispatch('plugin', dict(lease_args, plugin_id='generated', menu_index=0))
    native.responses['GET', '/hermes/plugin-routes']['registry_revision'] = '0123456789abcdef-2'
    result = service.dispatch('plugin', dict(lease_args, review_id=review['review_id'],
                                            approved_effects=review['effects']))
    assert result['code'] == 'REVIEW_STALE'


def test_resource_close_still_revokes_after_policy_loss(console):
    service, owner, _, _ = console
    grant, args = resource_grant(console)
    owner.allowed = False
    assert service.dispatch('resource_close', args) == {'ok': True, 'closed': True}
    owner.allowed = True
    result = service.dispatch('resource', dict(args, path=grant['entry_path'], query=[]))
    assert result['code'] == 'RESOURCE_GONE' and not result['effect_invoked']


@pytest.mark.parametrize('name', ['文档.html', 'literal%2Fname.js', 'path//asset.js', 'question?mark#part.js'])
def test_resource_native_path_is_decoded_once_and_transport_is_uri_encoded(console, name):
    from urllib.parse import quote

    service, _, native, _ = console
    grant, args = resource_grant(console, entry=name, resources=[name])
    path = '/v0/resource/plugins/generated/' + name
    target = quote(path, safe='/')
    assert grant['entry_path'] == target
    native.responses['GET', target] = SimpleNamespace(
        status=200, body=b'generated exact asset', headers={'Content-Type': 'application/octet-stream'})
    result = service.dispatch('resource', dict(args, path=path, query=[]))
    assert result['ok'], result
    assert base64.b64decode(result['body_base64']) == b'generated exact asset'
    assert len([p for _, p, _ in native.calls if p == target]) == 1
    if target != path:
        # A second decode/encode interpretation must never borrow this grant.
        rejected = service.dispatch('resource', dict(args, path=target, query=[]))
        assert rejected['code'] == 'RESOURCE_PATH_DENIED' and not rejected['effect_invoked']


def test_resource_redirect_uri_targets_exact_decoded_native_route(console):
    service, _, native, _ = console
    grant, args = resource_grant(console, resources=['index.html', '文档%notes.js'])
    native.responses['GET', grant['entry_path']] = SimpleNamespace(
        status=302, body=b'generated redirect', headers={'Location': './%E6%96%87%E6%A1%A3%25notes.js?x=1&x=2'})
    result = service.dispatch('resource', dict(args, path=grant['entry_path'], query=[]))
    assert result['ok'] and result['status'] == 302
    assert result['headers']['location'] == '/v0/resource/plugins/generated/%E6%96%87%E6%A1%A3%25notes.js?x=1&x=2'
    assert not any(p.endswith('notes.js') for _, p, _ in native.calls)


def test_resource_form_query_preserves_large_multiline_and_empty_values(console):
    service, _, native, _ = console
    grant, args = resource_grant(console)
    path = grant['entry_path'].rsplit('/', 1)[0] + '/save'
    query = [['profile', 'native-only'], ['', 'empty-key'], ['text', 'x' * 9000],
             ['text', 'line one\r\nline two\n&?=#\0'], ['unicode', '文档'], ['blank', '']]
    result = service.dispatch('resource', dict(args, path=path, query=query))
    assert result['ok'], result
    calls = [options for _, target, options in native.calls if target == path]
    assert len(calls) == 1 and calls[0]['query'] == [tuple(pair) for pair in query]
    rejected = service.dispatch('resource', dict(args, path=path, query=[['text', '&' * 22000]]))
    assert rejected['code'] == 'PAYLOAD_TOO_LARGE' and not rejected['effect_invoked']
    assert len([target for _, target, _ in native.calls if target == path]) == 1


def test_resource_inflight_admission_is_bounded_and_released(console):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    service, _, native, _ = console
    grant, args = resource_grant(console)
    path = grant['entry_path']
    entered, release = threading.Barrier(3), threading.Event()
    original_request = native.request

    def blocked(method, target, **options):
        if target == path:
            entered.wait(timeout=3)
            assert release.wait(timeout=3)
        return original_request(method, target, **options)

    native.request = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(service.dispatch, 'resource', dict(args, path=path, query=[])) for _ in range(2)]
        try:
            entered.wait(timeout=3)
            rejected = service.dispatch('resource', dict(args, path=path, query=[]))
            assert rejected['code'] == 'BUSY' and not rejected['effect_invoked']
        finally:
            release.set()
        assert all(future.result(timeout=3)['ok'] for future in pending)
    native.request = original_request
    assert service.dispatch('resource', dict(args, path=path, query=[]))['ok']


def test_resource_oversized_transport_response_is_withheld_without_retry(console):
    service, _, native, _ = console
    grant, args = resource_grant(console)
    path = grant['entry_path']
    original_request, attempted = native.request, []

    def oversized(method, target, **options):
        if target == path:
            attempted.append(target)
            error = RuntimeError('GENERATED_BODY_LIMIT')
            error.code = 'RESPONSE_TOO_LARGE'
            raise error
        return original_request(method, target, **options)

    native.request = oversized
    result = service.dispatch('resource', dict(args, path=path, query=[]))
    assert result == {'ok': False, 'code': 'RESPONSE_TOO_LARGE',
                      'effect_invoked': True, 'response_withheld': True}
    assert attempted == [path]


@pytest.mark.parametrize('status', [404, 412])
def test_atomic_native_resource_rejection_proves_no_handler_and_revokes_grant(console, status):
    service, _, native, _ = console
    grant, args = resource_grant(console)
    path = grant['entry_path']
    revision = '0123456789abcdef-2' if status == 412 else '0123456789abcdef-1'
    body = (json.dumps({'error': 'stale_resource_revision', 'registry_revision': revision}).encode()
            if status == 412 else b'404 page not found\n')
    native.responses['GET', path] = SimpleNamespace(status=status, headers={
        'X-Hermes-Resource-Invoked': '0', 'X-Hermes-Resource-Revision': revision,
    }, body=body)
    result = service.dispatch('resource', dict(args, path=path, query=[]))
    assert result == {'ok': False, 'code': 'RESOURCE_REVOKED',
                      'effect_invoked': False, 'response_withheld': False}
    assert len([target for _, target, _ in native.calls if target == path]) == 1
    second = service.dispatch('resource', dict(args, path=path, query=[]))
    assert second['code'] == 'RESOURCE_GONE' and not second['effect_invoked']
    assert len([target for _, target, _ in native.calls if target == path]) == 1


@pytest.mark.parametrize('status', [404, 412, 502])
def test_plugin_http_error_is_not_mistaken_for_no_effect_resource_rejection(console, status):
    service, _, native, _ = console
    grant, args = resource_grant(console)
    path = grant['entry_path']
    body = b'{"error":"stale_resource_revision","GENERATED_NATIVE_EFFECT":"already-applied"}'
    native.responses['GET', path] = SimpleNamespace(status=status, headers={
        'X-Hermes-Resource-Invoked': '1', 'X-Hermes-Resource-Revision': '0123456789abcdef-1',
        'Content-Type': 'application/json',
    }, body=body)
    result = service.dispatch('resource', dict(args, path=path, query=[]))
    assert result['ok'] and result['status'] == status
    assert base64.b64decode(result['body_base64']) == body
    assert not any(name.startswith('x-hermes-') for name in result['headers'])
    assert len([target for _, target, _ in native.calls if target == path]) == 1


@pytest.mark.parametrize('status', [200, 404, 412, 502])
def test_missing_or_dropped_duplicate_native_marker_is_unknown_not_no_effect(console, status):
    service, _, native, _ = console
    grant, args = resource_grant(console)
    path = grant['entry_path']
    original_request, attempts = native.request, []

    def unproven_response(method, target, **options):
        if target == path:
            attempts.append(target)
            return SimpleNamespace(status=status, headers={}, body=b'GENERATED_UNPROVEN_RESPONSE')
        return original_request(method, target, **options)

    native.request = unproven_response
    result = service.dispatch('resource', dict(args, path=path, query=[]))
    assert result == {'ok': False, 'code': 'OUTCOME_UNKNOWN',
                      'effect_invoked': True, 'response_withheld': True}
    assert attempts == [path] and 'GENERATED_UNPROVEN_RESPONSE' not in json.dumps(result)


@pytest.mark.parametrize('location,expected', [
    ('./path//asset.js', 'path//asset.js'),
    ('./folder/../path//asset.js', 'path//asset.js'),
    ('./folder/%2e%2E/path//asset.js', 'path//asset.js'),
    ('#fragment', 'index.html?op=save&op=two#fragment'),
    ('?mode=one&mode=two#fragment', 'index.html?mode=one&mode=two#fragment'),
    ('?', 'index.html'),
])
def test_resource_relative_redirect_preserves_empty_segments_and_query_semantics(console, location, expected):
    service, _, native, _ = console
    grant, args = resource_grant(console, resources=['index.html', 'path//asset.js'])
    native.responses['GET', grant['entry_path']] = SimpleNamespace(
        status=302, body=b'generated native redirect', headers={'Location': location})
    result = service.dispatch('resource', dict(args, path=grant['entry_path'], query=[['op', 'save'], ['op', 'two']]))
    assert result['ok'] and result['status'] == 302
    assert result['headers']['location'] == '/v0/resource/plugins/generated/' + expected
    assert len([target for _, target, _ in native.calls if target.startswith('/v0/resource/')]) == 1


def test_resource_routes_cannot_enter_management_pipeline_even_with_registry_entry(console):
    from hermes_cpa.console_protocol import ConsoleError, classify, parse_request
    service, owner, native, _ = console
    grant, _ = resource_grant(console)
    path = grant['entry_path']
    before = len(native.calls)
    refused = prepare(console, request(path, query={'op': 'save'}, response_kind='bytes'))
    assert refused == {'ok': False, 'code': 'ROUTE_NOT_ALLOWED'}
    assert not owner.submissions and len(native.calls) == before
    with pytest.raises(ConsoleError, match='ROUTE_NOT_ALLOWED'):
        classify(parse_request(request(path)), {('GET', path): 'generated'})


def declared_management_review(console):
    _, _, native, _ = console
    path = '/v0/management/generated/run'
    revision = '0123456789abcdef-1'
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': revision,
        'management': [{'method': 'POST', 'path': path, 'plugin_id': 'generated'}],
        'resources': []}
    review = prepare(console, request(path, 'POST', {}))
    assert review['ok']
    assert 'Installed plugin: generated' in review['summary']
    assert revision in review['summary']
    return review, path, revision


@pytest.mark.parametrize('replacement', ['revision', 'plugin', 'removed'])
def test_management_review_binds_registry_incarnation_and_plugin_owner(console, replacement):
    _, _, native, _ = console
    review, path, _ = declared_management_review(console)
    assert execute(console, review)['ok']
    registry = native.responses['GET', '/hermes/plugin-routes']
    if replacement == 'revision':
        registry['registry_revision'] = '0123456789abcdef-2'
    elif replacement == 'plugin':
        registry['management'][0]['plugin_id'] = 'replacement'
    else:
        registry['management'] = []
    public, private = finish(console)
    assert public['code'] == 'REVIEW_STALE' and private['state'] == 'failed'
    assert not any(target == path for _, target, _ in native.calls)


def test_management_replacement_after_precheck_uses_atomic_native_assertion(console):
    _, owner, native, _ = console
    review, path, revision = declared_management_review(console)
    original = native.request
    attempts = []

    def replaced(method, target, **options):
        if target != path:
            return original(method, target, **options)
        attempts.append(options)
        assert options['headers'] == {'X-Hermes-Plugin-Revision': revision}
        return SimpleNamespace(status=412, headers={
            'X-Hermes-Plugin-Invoked': '0', 'X-Hermes-Plugin-Revision': '0123456789abcdef-2'},
            body=b'{"error":"stale_plugin_revision"}')

    native.request = replaced
    execute(console, review)
    public, private = finish(console)
    assert public['code'] == 'REVIEW_STALE' and private['state'] == 'failed'
    assert 'result' not in private
    execute(console, review)
    assert len(attempts) == 1 and len(owner.submissions) == 1


@pytest.mark.parametrize('status', [200, 404, 412, 502])
def test_management_plugin_http_errors_are_observed_handler_replies(console, status):
    _, _, native, _ = console
    review, path, revision = declared_management_review(console)
    native.responses['POST', path] = SimpleNamespace(status=status, headers={
        'X-Hermes-Plugin-Invoked': '1', 'X-Hermes-Plugin-Revision': revision,
        'Content-Type': 'application/json'}, body=b'{"error":"generated-after-effect"}')
    execute(console, review)
    public, private = finish(console)
    assert public == {'ok': True, 'code': 'OK', 'result_ready': True}
    assert private['state'] == 'succeeded' and private['result']['status'] == status
    assert private['result']['body']['value']['error'] == 'generated-after-effect'
    assert 'x-hermes-plugin-invoked' not in private['result']['headers']
    assert 'x-hermes-plugin-revision' not in private['result']['headers']


@pytest.mark.parametrize('headers', [
    {}, {'X-Hermes-Plugin-Invoked': '1'},
    {'X-Hermes-Plugin-Invoked': '1', 'X-Hermes-Plugin-Revision': '0123456789abcdef-2'},
    {'X-Hermes-Plugin-Invoked': '0, 1', 'X-Hermes-Plugin-Revision': '0123456789abcdef-1'},
])
def test_management_untrusted_invocation_facts_never_claim_no_effect(console, headers):
    _, _, native, _ = console
    review, path, _ = declared_management_review(console)
    native.responses['POST', path] = SimpleNamespace(
        status=412, headers=headers, body=b'{"error":"GENERATED_PRIVATE_REPLY"}')
    execute(console, review)
    public, private = finish(console)
    assert public['code'] == 'OUTCOME_UNKNOWN' and private['state'] == 'unknown'
    assert 'result' not in private and 'GENERATED_PRIVATE_REPLY' not in json.dumps(private)


@pytest.mark.parametrize('failure', ['transport', 'projection', 'storage', 'recording'])
def test_post_dispatch_failures_never_become_definitive_failed_or_replay(console, monkeypatch, failure):
    import hermes_cpa.console_service as module
    from hermes_cpa.console_protocol import ConsoleError
    from hermes_cpa.owner_transport import TransportError
    service, owner, native, args = console
    review = prepare(console, request('/requests/api-call', 'POST', {
        'method': 'POST', 'url': 'https://generated.example/mutate', 'data': '{}'}))
    execute(console, review)

    def fail(*_args, **_kwargs):
        raise ConsoleError('BUSY')

    if failure == 'transport':
        native.failure = TransportError('RESPONSE_TOO_LARGE')
    elif failure == 'projection':
        monkeypatch.setattr(module, 'management_response', fail)
    elif failure == 'storage':
        monkeypatch.setattr(service, '_space', fail)
    else:
        monkeypatch.setattr(service, '_record_native', fail)
    public, private = finish(console)
    if failure == 'transport':
        assert not public['ok'] and public['code'] == 'OUTCOME_UNKNOWN'
        assert private['state'] == 'unknown'
    else:
        assert public == {'ok': True, 'code': 'RESPONSE_WITHHELD', 'result_ready': True}
        assert private['state'] == 'succeeded' and private['result_withheld']
        assert private['response_status'] == 200
    assert 'result' not in private
    service.job_rejected(owner.submissions[-1], 'POLICY_LOCKED')
    assert service.dispatch('result', dict(args, operation_id='generated-operation')) == private
    execute(console, review)
    owner.receipts.clear()
    assert execute(console, review)['code'] == 'RECONCILE_REQUIRED'
    assert len(native.calls) == 1 and len(owner.submissions) == 1


def test_oversized_encoded_http_error_is_observed_withheld_not_rollback(console, monkeypatch):
    import hermes_cpa.console_protocol as protocol
    _, _, native, _ = console
    review = prepare(console, request('/credentials/refresh', 'POST', {'name': 'generated.json'}))
    execute(console, review)
    native.responses['POST', PREFIX + '/credentials/refresh'] = SimpleNamespace(
        status=502, headers={'content-type': 'text/plain'}, body=('é' * 128).encode())
    monkeypatch.setattr(protocol, 'WIRE_LIMIT', 65536 + 512)
    public, private = finish(console)
    assert public['ok'] and public['code'] == 'RESPONSE_WITHHELD'
    assert private['state'] == 'succeeded' and private['response_status'] == 502
    assert private['result_withheld'] and 'result' not in private
    assert 'rollback' not in json.dumps(private)


@pytest.mark.parametrize('status', [200, 502])
@pytest.mark.parametrize('lock_inflight', [False, True])
def test_oauth_observed_state_is_tracked_before_presentation_validation(console, status, lock_inflight):
    service, _, native, args = console
    path = PREFIX + '/oauth/auth-url'
    native.responses['GET', path] = SimpleNamespace(status=status, headers={}, body=json.dumps({
        'state': 'generated-oversize-state', 'url': 'https://generated.example/' + 'x' * 65536}).encode())
    review = prepare(console, request('/oauth/auth-url', query={'provider': 'codex'}))
    execute(console, review)
    original = native.request

    def started(method, target, **options):
        response = original(method, target, **options)
        if target == path:
            assert service._oauth_starting == 1
            if lock_inflight:
                assert service.dispatch('lock', args)['locked']
        return response

    native.request = started
    public, private = finish(console)
    assert public['ok'] and private['state'] == 'succeeded' and private['result_withheld']
    assert service._oauth_starting == 0
    if not lock_inflight:
        assert service._oauth['generated-oversize-state']['values'] == []
        service.dispatch('lock', args)
    assert 'generated-oversize-state' not in service._oauth
    assert any(method == 'DELETE' and target == PREFIX + '/oauth/session' and
               options['query'] == {'state': 'generated-oversize-state'}
               for method, target, options in native.calls)


def test_oauth_inflight_reservation_counts_toward_capacity_before_native_start(console):
    import time
    service, _, native, args = console
    review = prepare(console, request('/oauth/auth-url', query={'provider': 'codex'}))
    execute(console, review)
    with service._gate:
        service._oauth.update({f'generated-existing-{n}': {
            'lease_id': args['lease_id'], 'provider': 'codex', 'expires': time.monotonic() + 1800, 'values': []}
            for n in range(127)})
        # One earlier worker is inside native start and has not received its state.
        service._oauth_starting = 1
    public, private = finish(console)
    assert public['code'] == 'BUSY' and private['state'] == 'failed'
    assert not native.calls and service._oauth_starting == 1
    service._oauth_starting = 0


def test_oauth_last_slot_is_reserved_until_native_state_is_recorded(console):
    import time
    service, _, native, args = console
    review = prepare(console, request('/oauth/auth-url', query={'provider': 'codex'}))
    execute(console, review)
    with service._gate:
        service._oauth.update({f'generated-existing-{n}': {
            'lease_id': args['lease_id'], 'provider': 'codex', 'expires': time.monotonic() + 1800, 'values': []}
            for n in range(127)})
    original = native.request
    path = PREFIX + '/oauth/auth-url'
    native.responses['GET', path] = {'state': 'generated-last-state', 'url': 'https://generated.example'}

    def reserved(method, target, **options):
        assert len(service._oauth) + service._oauth_starting == 128
        return original(method, target, **options)

    native.request = reserved
    public, private = finish(console)
    assert public['ok'] and private['state'] == 'succeeded'
    assert len(service._oauth) == 128 and 'generated-last-state' in service._oauth
    assert service._oauth_starting == 0


@pytest.mark.parametrize('grant', [None, {}, {'token': 'generated-revoked'}])
def test_execute_revalidates_host_grant_before_private_admission(console, monkeypatch, grant):
    import hermes_cpa.operation_guard as guard
    from hermes_cpa.console_service import execute_intent
    service, owner, native, args = console
    review = prepare(console, request('/credentials/refresh', 'POST', {'name': 'generated.json'}))
    payload = dict(args, request_id='generated-operation', review_id=review['review_id'],
                   approved_effects=review['effects'], _host_grant=grant)
    checked = []

    def denied(value, intent, *, timeout):
        checked.append((value, intent, timeout))
        return False

    monkeypatch.setattr(guard, 'revalidate_grant', denied)
    assert service.dispatch('execute', payload) == {'ok': False, 'code': 'POLICY_LOCKED'}
    assert checked == [(grant, execute_intent(payload, owner.generation), 1.)]
    assert not service._results and not service._active and not owner.submissions and not native.calls
    assert not service._reviews[review['review_id']].get('accepted')


def test_declared_auth_url_shaped_route_stays_on_asserted_management_path(console):
    service, _, native, _ = console
    path, revision = '/v0/management/generated-auth-url', '0123456789abcdef-1'
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': revision,
        'management': [{'method': 'GET', 'path': path, 'plugin_id': 'generated'}], 'resources': []}
    native.responses['GET', path] = SimpleNamespace(status=200, headers={
        'X-Hermes-Plugin-Revision': revision, 'X-Hermes-Plugin-Invoked': '1'},
        body=b'{"declared_handler":true}')
    review = prepare(console, request(path))
    assert review['ok'] and 'plugin_code' in review['effects'] and 'oauth_start' not in review['effects']
    execute(console, review)
    public, private = finish(console)
    assert public['ok'] and private['result']['body']['value'] == {'declared_handler': True}
    assert native.calls[-1][2]['headers'] == {'X-Hermes-Plugin-Revision': revision}
    assert not service._oauth and service._oauth_starting == 0


def test_host_grant_change_between_private_and_ledger_admission_does_not_accept_job(console, monkeypatch):
    import hermes_cpa.operation_guard as guard
    service, owner, native, args = console
    review = prepare(console, request('/credentials/refresh', 'POST', {'name': 'generated.json'}))
    monkeypatch.setattr(guard, 'revalidate_grant', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(owner, 'submit', lambda _: {'ok': False, 'code': 'POLICY_LOCKED'})
    refused = service.dispatch('execute', dict(args, request_id='generated-operation',
        review_id=review['review_id'], approved_effects=review['effects'], _host_grant={'token': 'generated'}))
    assert refused == {'ok': False, 'code': 'POLICY_LOCKED'}
    assert not service._active and not owner.submissions and not native.calls
    private = service.dispatch('result', dict(args, operation_id='generated-operation'))
    assert private['state'] == 'failed' and private['code'] == 'POLICY_LOCKED'


def test_oauth_reservation_is_released_after_ambiguous_native_start(console):
    from hermes_cpa.owner_transport import TransportError
    service, owner, native, _ = console
    review = prepare(console, request('/oauth/auth-url', query={'provider': 'codex'}))
    execute(console, review)
    original = native.request

    def lost_reply(method, path, **options):
        assert service._oauth_starting == 1
        original(method, path, **options)
        raise TransportError('OUTCOME_UNKNOWN')

    native.request = lost_reply
    public, private = finish(console)
    assert public['code'] == 'OUTCOME_UNKNOWN' and private['state'] == 'unknown'
    assert service._oauth_starting == 0 and not service._oauth
    execute(console, review)
    assert len(native.calls) == 1 and len(owner.submissions) == 1


def fixed_plugin_review(console, surface):
    native = console[2]
    revision = '0123456789abcdef-1'
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': revision, 'management': [], 'resources': [],
        'dispatches': [{'plugin_id': 'generated', 'oauth_provider': 'generated-sso', 'quota': True}]}
    if surface.startswith('quota-'):
        method, path = surface.removeprefix('quota-'), PREFIX + '/plugins/generated/quota'
        wire = request(path, method, {'auth_index': 'generated-auth'} if method == 'POST' else None,
                       query={} if method == 'POST' else {'auth_index': 'generated-auth'})
    else:
        method = 'GET'
        path = PREFIX + '/oauth/auth-url' if surface == 'oauth-v8' else '/v0/management/generated-sso-auth-url'
        wire = request(path, query={'provider': 'generated-sso'} if surface == 'oauth-v8' else {})
    review = prepare(console, wire)
    assert review['ok'], review
    assert {'plugin_code', 'network_destination'} <= set(review['effects'])
    assert 'Installed plugin: generated' in review['summary'] and revision in review['summary']
    return review, method, path, revision


FIXED_PLUGIN_SURFACES = ['quota-GET', 'quota-POST', 'quota-DELETE', 'oauth-v8', 'oauth-legacy']


@pytest.mark.parametrize('surface', FIXED_PLUGIN_SURFACES)
@pytest.mark.parametrize('replacement', ['same-id-instance', 'removed', 'owner'])
def test_routeless_plugin_reviews_bind_dispatch_identity(console, surface, replacement):
    _, _, native, _ = console
    review, _, path, _ = fixed_plugin_review(console, surface)
    execute(console, review)
    registry = native.responses['GET', '/hermes/plugin-routes']
    if replacement == 'same-id-instance':
        registry['registry_revision'] = '0123456789abcdef-2'
    elif replacement == 'removed':
        registry['dispatches'] = []
    else:
        registry['dispatches'][0]['plugin_id'] = 'replacement-plugin'
    public, private = finish(console)
    assert public['code'] == 'REVIEW_STALE' and private['state'] == 'failed'
    assert not any(target == path for _, target, _ in native.calls)


@pytest.mark.parametrize('surface', FIXED_PLUGIN_SURFACES)
def test_routeless_plugin_replacement_after_precheck_is_atomically_refused(console, surface):
    _, owner, native, _ = console
    review, method, path, revision = fixed_plugin_review(console, surface)
    original = native.request
    attempts = []

    def replaced(actual_method, target, **options):
        if target != path:
            return original(actual_method, target, **options)
        assert actual_method == method and options['headers'] == {'X-Hermes-Plugin-Revision': revision}
        attempts.append(target)
        return SimpleNamespace(status=412, headers={
            'X-Hermes-Plugin-Invoked': '0', 'X-Hermes-Plugin-Revision': '0123456789abcdef-2'},
            body=b'{"error":"stale_plugin_revision"}')

    native.request = replaced
    execute(console, review)
    public, private = finish(console)
    assert public['code'] == 'REVIEW_STALE' and private['state'] == 'failed'
    execute(console, review)
    assert attempts == [path] and len(owner.submissions) == 1


@pytest.mark.parametrize('surface', FIXED_PLUGIN_SURFACES)
@pytest.mark.parametrize('status', [200, 400, 404, 502])
def test_fixed_plugin_handler_http_errors_remain_observed(console, surface, status):
    service, _, native, _ = console
    review, method, path, revision = fixed_plugin_review(console, surface)
    value = {'state': 'generated-fixed-state', 'url': 'https://generated.example/authorize',
             'native_status': status}
    native.responses[method, path] = SimpleNamespace(status=status, headers={
        'X-Hermes-Plugin-Invoked': '1', 'X-Hermes-Plugin-Revision': revision},
        body=json.dumps(value).encode())
    execute(console, review)
    public, private = finish(console)
    assert public['ok'] and private['state'] == 'succeeded'
    assert private['result']['status'] == status and private['result']['body']['value'] == value
    assert 'rollback' not in json.dumps(private)
    if surface.startswith('oauth'):
        assert service._oauth['generated-fixed-state']['provider'] == 'generated-sso'
    else:
        assert not service._oauth


@pytest.mark.parametrize('surface', FIXED_PLUGIN_SURFACES)
@pytest.mark.parametrize('status', [400, 404])
def test_fixed_plugin_pre_dispatch_http_refusals_are_observed_without_claiming_effects(console, surface, status):
    service, _, native, _ = console
    review, method, path, revision = fixed_plugin_review(console, surface)
    native.responses[method, path] = SimpleNamespace(status=status, headers={
        'X-Hermes-Plugin-Invoked': '0', 'X-Hermes-Plugin-Revision': revision},
        body=b'{"error":"generated-invalid-auth"}')
    execute(console, review)
    public, private = finish(console)
    assert public['ok'] and private['state'] == 'succeeded'
    assert private['result']['status'] == status
    assert not service._oauth and service._oauth_starting == 0


@pytest.mark.parametrize('surface', FIXED_PLUGIN_SURFACES)
def test_fixed_plugin_missing_invocation_proof_is_unknown(console, surface):
    _, _, native, _ = console
    review, method, path, _ = fixed_plugin_review(console, surface)
    native.responses[method, path] = SimpleNamespace(status=404, headers={}, body=b'{"error":"missing"}')
    execute(console, review)
    public, private = finish(console)
    assert public['code'] == 'OUTCOME_UNKNOWN' and private['state'] == 'unknown'
    assert 'result' not in private


@pytest.mark.parametrize('provider', ['claude', 'codex', 'antigravity', 'kimi', 'kimi-ai', 'xai', 'devin', 'meta'])
@pytest.mark.parametrize('version', ['v8', 'v0'])
def test_builtin_oauth_never_requires_plugin_dispatch_identity(console, provider, version):
    _, _, native, _ = console
    if version == 'v8':
        path = PREFIX + '/oauth/auth-url'
        query = {'provider': ' ' + provider.upper() + ' '}
    else:
        path = '/v0/management/' + ('anthropic' if provider == 'claude' else provider) + '-auth-url'
        query = {}
    native.responses['GET', path] = {'state': 'generated-builtin-state', 'url': 'https://generated.example'}
    review = prepare(console, request(path, query=query))
    assert review['ok'] and review['effects'] == ['oauth_start']
    assert not native.calls
    execute(console, review)
    public, private = finish(console)
    assert public['ok'] and private['state'] == 'succeeded'
    assert len(native.calls) == 1 and native.calls[0][2]['headers'] == {}


def test_plugin_oauth_precedes_declared_suffix_handler_in_review(console):
    service, _, native, _ = console
    path, revision = '/v0/management/generated-sso-auth-url', '0123456789abcdef-1'
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': revision, 'resources': [],
        'management': [{'method': 'GET', 'path': path, 'plugin_id': 'declared-handler-plugin'}],
        'dispatches': [{'plugin_id': 'oauth-plugin', 'oauth_provider': 'generated-sso', 'quota': False}]}
    native.responses['GET', path] = SimpleNamespace(status=200, headers={
        'X-Hermes-Plugin-Invoked': '1', 'X-Hermes-Plugin-Revision': revision},
        body=b'{"state":"generated-precedence-state","url":"https://generated.example"}')
    review = prepare(console, request(path))
    assert review['ok'] and 'oauth_start' in review['effects']
    assert 'Installed plugin: oauth-plugin' in review['summary']
    execute(console, review)
    public, _ = finish(console)
    assert public['ok'] and 'generated-precedence-state' in service._oauth


def test_v8_anthropic_is_plugin_lookup_not_builtin_claude_alias(console):
    _, _, native, _ = console
    revision, path = '0123456789abcdef-1', PREFIX + '/oauth/auth-url'
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': revision, 'management': [], 'resources': [],
        'dispatches': [{'plugin_id': 'generated-anthropic-plugin', 'oauth_provider': 'anthropic', 'quota': False}]}
    native.responses['GET', path] = SimpleNamespace(status=200, headers={
        'X-Hermes-Plugin-Invoked': '1', 'X-Hermes-Plugin-Revision': revision},
        body=b'{"state":"generated-anthropic-state","url":"https://generated.example"}')
    review = prepare(console, request(path, query={'provider': ' ANTHROPIC '}))
    assert review['ok'] and 'plugin_code' in review['effects']
    assert 'Installed plugin: generated-anthropic-plugin' in review['summary']
    execute(console, review)
    public, _ = finish(console)
    assert public['ok'] and native.calls[-1][2]['headers'] == {'X-Hermes-Plugin-Revision': revision}


def test_synthetic_oauth_provider_path_is_not_a_native_start_alias(console):
    _, _, native, _ = console
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': '0123456789abcdef-1', 'management': [], 'resources': [],
        'dispatches': [{'plugin_id': 'generated', 'oauth_provider': 'generated-sso', 'quota': False}]}
    result = prepare(console, request('/v0/management/oauth/providers/generated-sso/auth-url'))
    assert result == {'ok': False, 'code': 'ROUTE_NOT_ALLOWED'}
    assert all(path == '/hermes/plugin-routes' for _, path, _ in native.calls)


@pytest.mark.parametrize('dispatches', [[], [{'plugin_id': 'generated', 'quota': False}]])
def test_quota_review_requires_effective_native_quota_capability(console, dispatches):
    _, owner, native, _ = console
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': '0123456789abcdef-1', 'management': [], 'resources': [], 'dispatches': dispatches}
    result = prepare(console, request('/plugins/generated/quota', query={'auth_index': 'generated'}))
    assert result == {'ok': False, 'code': 'ROUTE_NOT_ALLOWED'}
    assert not owner.submissions


def test_duplicate_oauth_catalog_owners_cannot_authorize_ambiguous_plugin_code(console):
    _, owner, native, _ = console
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': '0123456789abcdef-1', 'management': [], 'resources': [],
        'dispatches': [
            {'plugin_id': 'first', 'oauth_provider': 'generated-sso', 'quota': False},
            {'plugin_id': 'second', 'oauth_provider': 'generated-sso', 'quota': False}]}
    result = prepare(console, request('/oauth/auth-url', query={'provider': 'generated-sso'}))
    assert result == {'ok': False, 'code': 'UNAVAILABLE'}
    assert not owner.submissions


def test_native_plugin_review_unsupported_is_an_observed_pre_dispatch_refusal(console):
    _, _, native, _ = console
    review, path, revision = declared_management_review(console)
    native.responses['POST', path] = SimpleNamespace(status=400, headers={
        'X-Hermes-Plugin-Invoked': '0', 'X-Hermes-Plugin-Revision': revision},
        body=b'{"error":"plugin_review_unsupported"}')
    execute(console, review)
    public, private = finish(console)
    assert public['ok'] and private['state'] == 'succeeded'
    assert private['result']['status'] == 400
    assert private['result']['body']['value'] == {'error': 'plugin_review_unsupported'}


# ---- once-per-session approval of fixed built-in quota/profile reads ----
API_CALL = PREFIX + '/requests/api-call'
INVENTORY = PREFIX + '/credentials'
ACCOUNT = '0f3c7a52-1b9e-4c8a-9d21-6a5e4b7c8d90'
SCOPE_EFFECTS = ['network_destination', 'quota_reads_until_lock']
TOKEN = 'Bearer $TOKEN$'
CODEX_AGENT = 'codex-tui/0.149.1 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.149.1)'
CODEX_HEADER = {'Authorization': TOKEN, 'Content-Type': 'application/json',
                'User-Agent': CODEX_AGENT, 'Chatgpt-Account-Id': ACCOUNT}
CODEX_CREDITS_HEADER = dict(CODEX_HEADER, Accept='application/json',
                            **{'OpenAI-Beta': 'codex-1', 'Originator': 'Codex Desktop'})
CLAUDE_HEADER = {'Authorization': TOKEN, 'Content-Type': 'application/json',
                 'User-Agent': 'claude-cli/2.1.280 (external, cli)', 'anthropic-beta': 'oauth-2025-04-20'}
AGY_HEADER = {'Authorization': TOKEN, 'Content-Type': 'application/json',
              'User-Agent': 'antigravity/cli/1.0.13 (aidev_client; os_type=darwin; arch=arm64)'}
CODEX_URL = 'https://chatgpt.com/backend-api'
AGY_HOST = 'https://daily-cloudcode-pa.googleapis.com/v1internal:'
PROJECT = 'generated-project-123'


def credential(provider, index, **extra):
    return {'auth_index': index, 'id': index + '.json', 'name': index + '.json', 'provider': provider,
            'type': provider, 'email': index + '@generated.example', 'created_at': '2026-01-01T00:00:00Z',
            'path': '/generated/auth/' + index + '.json', 'source': 'file', 'modtime': '2026-01-02T00:00:00Z',
            'size': 100, **extra}


def codex_credential(index='codex-index', account=ACCOUNT, **extra):
    return credential('codex', index, id_token={'chatgpt_account_id': account, 'plan_type': 'plus'}, **extra)


def inventory(native, *rows):
    native.responses['GET', INVENTORY] = {'files': list(rows)}


def api_call(url, header, index='codex-index', method='GET', **extra):
    return request('/requests/api-call', 'POST', {
        'authIndex': index, 'method': method, 'url': url, 'header': dict(header), **extra})


def codex_usage(index='codex-index'):
    return api_call(CODEX_URL + '/wham/usage', CODEX_HEADER, index)


def codex_subscription(account=ACCOUNT, index='codex-index'):
    return api_call(CODEX_URL + '/subscriptions?account_id=' + account, CODEX_HEADER, index)


def codex_credits(index='codex-index'):
    return api_call(CODEX_URL + '/wham/rate-limit-reset-credits', CODEX_CREDITS_HEADER, index)


def claude_call(path, index='claude-index'):
    return api_call('https://api.anthropic.com' + path, CLAUDE_HEADER, index)


def agy_call(endpoint, data, index='agy-index', host=AGY_HOST, header=AGY_HEADER):
    return api_call(host + endpoint, header, index, 'POST', data=data)


def agy_quota(project=PROJECT, index='agy-index'):
    return agy_call('retrieveUserQuotaSummary', json.dumps({'project': project}), index)


def agy_subscription(index='agy-index'):
    return agy_call('loadCodeAssist', json.dumps({'metadata': {'ideType': 'ANTIGRAVITY'}}), index)


def egress_policy_of(call):
    header = call[2]['headers']['X-Hermes-Egress-Policy']
    return json.loads(base64.urlsafe_b64decode(header + '=' * (-len(header) % 4)))


def api_calls(native):
    return [call for call in native.calls if call[1] == API_CALL]


def run(console, wire, request_id):
    review = prepare(console, wire, request_id)
    assert review['ok'], review
    assert execute(console, review, request_id)['ok']
    public, _ = finish(console)
    assert public['ok']
    return review


def reopen(console, suffix, *, quota_reads=False):
    service, owner, _, args = console
    scope = dict(args['_scope'], document_id=str(uuid.uuid4()))
    unlocked = service.dispatch('unlock', {'_scope': scope, 'document_id': scope['document_id'],
        'request_id': 'unlock-' + suffix, 'expected_generation': owner.generation, 'consent': True,
        'quota_reads': quota_reads})
    assert unlocked['ok']
    return console[:3] + (dict(args, _scope=scope, document_id=scope['document_id'], lease_id=unlocked['lease_id']),)


def test_first_codex_read_prompts_and_one_acceptance_covers_parallel_and_followup_reads(console):
    service, owner, native, _ = console
    inventory(native, codex_credential())
    usage = prepare(console, codex_usage(), 'usage')
    subscription = prepare(console, codex_subscription(), 'subscription')
    credits_early = prepare(console, codex_credits(), 'credits-early')
    for review in (usage, subscription, credits_early):
        assert review['requires_confirmation'] is True, 'prepare alone never installs a remembered grant'
        assert review['effects'] == SCOPE_EFFECTS
        assert review['approval_scope'] == {'kind': 'quota_reads', 'provider': 'codex', 'granted': False}
        assert 'until you Lock the console or this unlocked session expires' in review['summary']
        assert 'not other network calls' in review['summary'] and 'credit consumption' in review['summary']
        assert 'generated.example' not in review['summary'] and '$TOKEN$' not in review['summary']
    assert execute(console, usage, 'usage', approve=False)['code'] == 'CONSENT_REQUIRED'
    assert prepare(console, codex_credits(), 'credits-after-decline')['requires_confirmation'] is True
    # Both reviews were prepared before either ran: neither pins a resource version the other can invalidate.
    assert execute(console, usage, 'usage')['ok'] and execute(console, subscription, 'subscription')['ok']
    for job in list(owner.submissions):
        assert service.execute_job(job)['ok']
    policies = sorted((egress_policy_of(call) for call in api_calls(native)),
                      key=lambda policy: policy['destinations'][0]['path_prefix'])
    assert [p['destinations'][0]['path_prefix'] for p in policies] == [
        '/backend-api/subscriptions', '/backend-api/wham/usage']
    for policy in policies:
        assert policy == {'version': 1, 'destinations': [{
            'scheme': 'https', 'host': 'chatgpt.com', 'port': 443,
            'path_prefix': policy['destinations'][0]['path_prefix']}], 'methods': ['GET'],
            'redirects': 'deny', 'allow_token': True, 'proxy': {'mode': 'configured'},
            'auth_index': 'codex-index'}
    # The follow-up read needs no new dialog, yet keeps its own owner review and exact effect consent.
    followup = prepare(console, codex_credits(), 'credits')
    assert followup['requires_confirmation'] is False and followup['effects'] == SCOPE_EFFECTS
    assert followup['approval_scope']['granted'] is True
    assert prepare(console, codex_credits(), 'credits-early')['requires_confirmation'] is False
    assert execute(console, followup, 'credits', approve=False)['code'] == 'CONSENT_REQUIRED'
    assert execute(console, followup, 'credits')['ok']
    public, _ = finish(console)
    assert public['ok']
    assert egress_policy_of(api_calls(native)[-1])['destinations'][0]['path_prefix'] == (
        '/backend-api/wham/rate-limit-reset-credits')
    assert len(owner.submissions) == 3


def test_grant_is_per_credential_and_provider(console):
    _, _, native, _ = console
    inventory(native, codex_credential(), codex_credential('codex-other', account='other-account'),
              credential('claude', 'claude-index'))
    run(console, codex_usage(), 'usage')
    assert prepare(console, codex_credits(), 'credits')['requires_confirmation'] is False
    other = prepare(console, api_call(CODEX_URL + '/wham/usage', dict(CODEX_HEADER, **{
        'Chatgpt-Account-Id': 'other-account'}), 'codex-other'), 'other-usage')
    assert other['requires_confirmation'] is True and other['approval_scope']['granted'] is False
    claude = prepare(console, claude_call('/api/oauth/usage'), 'claude-usage')
    assert claude['requires_confirmation'] is True and claude['approval_scope']['provider'] == 'claude'
    run(console, claude_call('/api/oauth/usage'), 'claude-run')
    assert prepare(console, claude_call('/api/oauth/profile'), 'claude-profile')['requires_confirmation'] is False
    assert prepare(console, claude_call('/api/oauth/usage?cedar_ember=1&skip_spend=1'),
                   'claude-grants')['requires_confirmation'] is False
    assert prepare(console, codex_usage(), 'usage-again')['requires_confirmation'] is False
    assert prepare(console, codex_credits('codex-other'), 'unlisted')['requires_confirmation'] is True


def test_antigravity_exact_post_reads_are_scoped_and_keep_read_effects(console):
    _, _, native, _ = console
    inventory(native, credential('antigravity', 'agy-index', project_id=PROJECT))
    first = prepare(console, agy_subscription(), 'agy-subscription')
    assert first['effects'] == SCOPE_EFFECTS and first['requires_confirmation'] is True
    assert 'external_mutation' not in first['effects']
    run(console, agy_subscription(), 'agy-subscription')
    sandbox = agy_call('retrieveUserQuotaSummary', json.dumps({'project': PROJECT}),
                       host='https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:')
    assert prepare(console, agy_quota(), 'agy-quota')['requires_confirmation'] is False
    assert prepare(console, sandbox, 'agy-sandbox')['requires_confirmation'] is False
    policy = egress_policy_of(api_calls(native)[-1])
    assert policy['methods'] == ['POST'] and policy['redirects'] == 'deny'
    assert policy['destinations'][0]['path_prefix'] == '/v1internal:loadCodeAssist'
    for index, wire in enumerate([
            agy_quota('other-project-999'),
            agy_call('retrieveUserQuotaSummary', json.dumps({'project': PROJECT, 'model': 'generated'})),
            agy_call('retrieveUserQuotaSummary', '{"project":"%s","project":"other-project-999"}' % PROJECT),
            agy_call('loadCodeAssist', json.dumps({'metadata': {'ideType': 'OTHER'}})),
            agy_call('generateContent', '{}'),
            agy_call('retrieveUserQuotaSummary', json.dumps({'project': PROJECT}),
                     host='https://cloudcode-pa.googleapis.com/v1internal:', header=dict(AGY_HEADER, Host='x'))]):
        review = prepare(console, wire, f'agy-negative-{index}')
        assert review.get('requires_confirmation', True) is True and 'approval_scope' not in review
        assert 'quota_reads_until_lock' not in review.get('effects', [])
    # A project the trusted inventory does not prove keeps the ordinary review.
    inventory(native, credential('antigravity', 'agy-index'))
    unproven = prepare(console, agy_quota(), 'agy-no-trusted-project')
    assert unproven['requires_confirmation'] is True and 'approval_scope' not in unproven
    assert 'external_mutation' in unproven['effects']


NEGATIVE_SHAPES = {
    'extra-header': lambda v: v['header'].update({'X-Debug': '1'}),
    'host-header': lambda v: v['header'].update({'Host': 'chatgpt.com'}),
    'duplicate-case-header': lambda v: v['header'].update(authorization=TOKEN),
    'literal-token': lambda v: v['header'].update(Authorization='Bearer literal'),
    'wrong-account-header': lambda v: v['header'].update({'Chatgpt-Account-Id': 'other-account'}),
    'proxy': lambda v: v.update(proxy_url='socks5://proxy.example:1080'),
    'direct-proxy': lambda v: v.update(proxy_url='direct'),
    'query': lambda v: v.update(url=v['url'] + '?debug=1'),
    'other-path': lambda v: v.update(url=v['url'] + '/extra'),
    'other-host': lambda v: v.update(url=v['url'].replace('chatgpt.com', 'chatgpt.com.example')),
    'userinfo': lambda v: v.update(url=v['url'].replace('chatgpt.com', 'user@chatgpt.com')),
    'plain-http': lambda v: v.update(url=v['url'].replace('https', 'http')),
    'user-agent-suffix': lambda v: v['header'].update({'User-Agent': CODEX_AGENT + ' extra'}),
    'explicit-port': lambda v: v.update(url=v['url'].replace('chatgpt.com', 'chatgpt.com:443')),
    'method': lambda v: v.update(method='POST'),
    'body': lambda v: v.update(data='{}'),
    'ambiguous-index': lambda v: v.update(auth_index='codex-index'),
    'unknown-field': lambda v: v.update(extra='x'),
}


@pytest.mark.parametrize('shape', sorted(NEGATIVE_SHAPES))
def test_changed_quota_request_shapes_stay_on_ordinary_review_after_a_grant(console, shape):
    _, _, native, _ = console
    inventory(native, codex_credential())
    run(console, codex_usage(), 'usage')
    assert prepare(console, codex_credits(), 'covered')['requires_confirmation'] is False
    wire = codex_usage()
    NEGATIVE_SHAPES[shape](wire['body']['value'])
    review = prepare(console, wire, 'changed')
    assert review.get('requires_confirmation', True) is True and 'approval_scope' not in review
    assert 'quota_reads_until_lock' not in review.get('effects', [])


def test_destinations_overrides_and_non_read_quota_actions_are_never_grouped(console):
    service, owner, native, _ = console
    inventory(native, codex_credential(), credential('claude', 'claude-index'),
              credential('antigravity', 'agy-index', project_id=PROJECT))
    run(console, codex_usage(), 'usage')
    redirect = prepare(console, codex_usage(), 'redirect',
                       egress_destinations=[{'scheme': 'https', 'host': 'redirect.example'}])
    assert 'cross_origin_redirect' in redirect['effects'] and redirect['requires_confirmation'] is True
    assert 'approval_scope' not in redirect
    consume = prepare(console, api_call(CODEX_URL + '/wham/rate-limit-reset-credits/consume', CODEX_HEADER,
                                        method='POST', data='{"redeem_request_id":"generated"}'), 'consume')
    claim = prepare(console, api_call('https://api.anthropic.com/api/organizations/generated/reset_rate_limits',
                                      CLAUDE_HEADER, 'claude-index', 'POST', data='{}'), 'claim')
    inference = prepare(console, agy_call('generateContent', '{}'), 'inference')
    for review, effect in ((consume, 'credit_consume'), (claim, 'entitlement_claim'), (inference, 'paid_inference')):
        assert effect in review['effects'] and review['requires_confirmation'] is True
        assert 'approval_scope' not in review and 'quota_reads_until_lock' not in review['effects']
    native.responses['GET', '/hermes/plugin-routes'] = {
        'registry_revision': '0123456789abcdef-1', 'management': [], 'resources': [],
        'dispatches': [{'plugin_id': 'demo', 'quota': True}]}
    plugin = prepare(console, request('/plugins/demo/quota', query={'auth_index': 'codex-index'}), 'plugin')
    assert 'plugin_code' in plugin['effects'] and plugin['requires_confirmation'] is True
    assert 'approval_scope' not in plugin
    assert prepare(console, codex_credits(), 'still-covered')['requires_confirmation'] is False


def test_lock_expiry_generation_and_reopen_clear_the_grant(console, monkeypatch):
    import time
    import hermes_cpa.console_service as module
    service, owner, native, args = console
    inventory(native, codex_credential())
    run(console, codex_usage(), 'usage')
    assert prepare(console, codex_credits(), 'covered')['requires_confirmation'] is False
    assert service.dispatch('lock', args)['locked']
    assert prepare(console, codex_credits(), 'after-lock')['code'] in ('LEASE_EXPIRED', 'STALE_SCOPE')
    reopened = reopen(console, 'after-lock')
    assert prepare(reopened, codex_credits(), 'reopened')['requires_confirmation'] is True
    run(reopened, codex_usage(), 'usage-reopened')
    assert prepare(reopened, codex_credits(), 'covered-again')['requires_confirmation'] is False
    clock = time.monotonic
    monkeypatch.setattr(time, 'monotonic', lambda: clock() + module.LEASE_SECONDS + 1)
    assert prepare(reopened, codex_credits(), 'after-expiry')['code'] in ('LEASE_EXPIRED', 'STALE_SCOPE')
    again = reopen(console, 'after-expiry')
    assert prepare(again, codex_credits(), 'reopened-after-expiry')['requires_confirmation'] is True
    monkeypatch.undo()
    owner.generation += 1
    assert prepare(again, codex_credits(), 'after-generation')['code'] in (
        'STALE_GENERATION', 'LEASE_EXPIRED', 'STALE_SCOPE')
    fresh = reopen(console, 'after-generation')
    assert prepare(fresh, codex_credits(), 'fresh-generation')['requires_confirmation'] is True


def test_changed_credential_identity_invalidates_prepared_and_remembered_grants(console):
    service, owner, native, _ = console
    inventory(native, codex_credential())
    run(console, codex_usage(), 'usage')
    covered = prepare(console, codex_credits(), 'covered')
    assert covered['requires_confirmation'] is False
    inventory(native, codex_credential(email='replacement@generated.example'))
    assert prepare(console, codex_credits(), 'covered')['code'] == 'REQUEST_CONFLICT', (
        'a reused request ID cannot carry a review across a replaced credential')
    replaced = prepare(console, codex_credits(), 'replaced')
    assert replaced['requires_confirmation'] is True and replaced['approval_scope']['granted'] is False
    # The accepted review is also re-verified immediately before anything is sent.
    assert execute(console, replaced, 'replaced')['ok']
    inventory(native, codex_credential(email='another@generated.example'))
    sent = len(api_calls(native))
    public = service.execute_job(owner.submissions[-1])
    assert public['ok'] is False and public['code'] == 'REVIEW_STALE'
    assert len(api_calls(native)) == sent, 'nothing is sent for a replaced credential'
    private = service.dispatch('result', dict(console[3], operation_id='replaced'))
    assert private['state'] == 'failed' and private['code'] == 'REVIEW_STALE'
    inventory(native, codex_credential(email='replacement@generated.example'))
    run(console, codex_credits(), 'replaced-again')
    assert prepare(console, codex_usage(), 'covered-by-replacement')['requires_confirmation'] is False
    inventory(native, credential('claude', 'codex-index'))
    assert prepare(console, codex_usage(), 'provider-changed')['requires_confirmation'] is True
    inventory(native)
    assert prepare(console, codex_usage(), 'removed')['requires_confirmation'] is True
    native.responses['GET', INVENTORY] = SimpleNamespace(status=503, headers={}, body=b'{}')
    unavailable = prepare(console, codex_usage(), 'inventory-down')
    assert unavailable['requires_confirmation'] is True and 'approval_scope' not in unavailable


def test_credential_replacement_voids_the_grant_but_a_token_refresh_does_not(console):
    _, _, native, _ = console
    inventory(native, codex_credential())
    run(console, codex_usage(), 'usage')
    inventory(native, codex_credential(modtime='2026-02-01T00:00:00Z', size=4096, updated_at='2026-02-01T00:00:00Z'))
    assert prepare(console, codex_credits(), 'after-refresh')['requires_confirmation'] is False
    inventory(native, codex_credential(created_at='2026-03-01T00:00:00Z'))
    assert prepare(console, codex_credits(), 'after-replacement')['requires_confirmation'] is True


def test_credential_operations_revoke_remembered_quota_grants(console):
    _, _, native, _ = console
    inventory(native, codex_credential())
    run(console, codex_usage(), 'usage')
    assert prepare(console, codex_credits(), 'covered')['requires_confirmation'] is False
    run(console, request('/credentials/refresh', 'POST', {'name': 'codex-index.json'}), 'refresh')
    assert prepare(console, codex_credits(), 'after-refresh')['requires_confirmation'] is True


def test_refused_execute_never_records_a_grant_and_host_grants_stay_per_operation(console, monkeypatch):
    import hermes_cpa.operation_guard as guard
    service, owner, native, args = console
    inventory(native, codex_credential())
    accept = owner.submit
    monkeypatch.setattr(owner, 'submit', lambda _: {'ok': False, 'code': 'POLICY_LOCKED'})
    first = prepare(console, codex_usage(), 'refused')
    assert execute(console, first, 'refused') == {'ok': False, 'code': 'POLICY_LOCKED'}
    assert prepare(console, codex_credits(), 'after-refusal')['requires_confirmation'] is True
    monkeypatch.setattr(owner, 'submit', accept)
    run(console, codex_usage(), 'usage')
    covered = prepare(console, codex_credits(), 'covered')
    assert covered['requires_confirmation'] is False
    monkeypatch.setattr(guard, 'revalidate_grant', lambda *_args, **_kwargs: False)
    submitted = len(owner.submissions)
    denied = service.dispatch('execute', dict(args, request_id='covered', review_id=covered['review_id'],
        approved_effects=covered['effects'], _host_grant={'token': 'generated-revoked'}))
    assert denied == {'ok': False, 'code': 'POLICY_LOCKED'}
    assert len(owner.submissions) == submitted, 'a remembered approval never replaces the per-operation host grant'


# ---- quota reads depend on, but never reserve or bump, the credentials version ----
def bump_credentials(service):
    """A managed credential write completing, as _release records it; the inventory row keeps its CreatedAt."""
    service._versions['credentials'] = service._versions.get('credentials', 0) + 1


def test_old_covered_review_is_refused_after_a_credential_replacement_completes(console):
    service, owner, native, _ = console
    inventory(native, codex_credential())
    run(console, codex_usage(), 'usage')
    old = prepare(console, codex_credits(), 'old-covered')
    assert old['requires_confirmation'] is False
    run(console, request('/credentials/refresh', 'POST', {'name': 'codex-index.json'}), 'replace')
    sent = len(api_calls(native))
    submitted = len(owner.submissions)
    assert execute(console, old, 'old-covered')['code'] == 'REVIEW_STALE'
    assert len(owner.submissions) == submitted and len(api_calls(native)) == sent
    fresh = prepare(console, codex_credits(), 'fresh')
    assert fresh['requires_confirmation'] is True and fresh['approval_scope']['granted'] is False
    run(console, codex_usage(), 'usage-after')
    assert prepare(console, codex_credits(), 'covered-again')['requires_confirmation'] is False


def test_active_credential_writer_blocks_quota_acceptance_and_dispatch(console):
    service, owner, native, _ = console
    inventory(native, codex_credential())
    run(console, codex_usage(), 'usage')
    old = prepare(console, codex_credits(), 'old-covered')
    accepted_read = prepare(console, codex_usage(), 'accepted-read')
    assert execute(console, accepted_read, 'accepted-read')['ok']
    upload = prepare(console, request('/credentials/refresh', 'POST', {'name': 'codex-index.json'}), 'upload')
    assert execute(console, upload, 'upload')['ok']
    assert execute(console, old, 'old-covered')['code'] == 'REVIEW_STALE', 'no read is accepted beside a credential writer'
    sent = len(api_calls(native))
    read_job = next(job for job in owner.submissions if job['request_id'] == 'accepted-read')
    assert service.execute_job(read_job)['code'] == 'REVIEW_STALE'
    assert len(api_calls(native)) == sent, 'an accepted read is not dispatched beside a credential writer'


def test_version_change_between_inventory_read_and_review_storage_stores_nothing(console, monkeypatch):
    service, owner, native, _ = console
    inventory(native, codex_credential())
    run(console, codex_usage(), 'usage')
    read = service._quota_read

    def replaced_after_inventory(wire):
        proven = read(wire)
        bump_credentials(service)
        return proven

    monkeypatch.setattr(service, '_quota_read', replaced_after_inventory)
    assert prepare(console, codex_credits(), 'racing')['code'] == 'REVIEW_STALE'
    assert not any(review.get('request_id') == 'racing' for review in service._reviews.values())
    monkeypatch.setattr(service, '_quota_read', read)
    assert prepare(console, codex_credits(), 'after-race')['requires_confirmation'] is True


@pytest.mark.parametrize('moment', ['before_dispatch', 'during_dispatch_inventory'])
def test_version_change_between_acceptance_and_dispatch_sends_nothing_and_leaves_no_grant(console, monkeypatch, moment):
    service, owner, native, _ = console
    inventory(native, codex_credential())
    first = prepare(console, codex_usage(), 'first')
    assert execute(console, first, 'first')['ok']
    job = owner.submissions[-1]
    read = service._quota_read
    if moment == 'before_dispatch':
        bump_credentials(service)
    else:
        def replaced_during_inventory(wire):
            proven = read(wire)
            bump_credentials(service)
            return proven
        monkeypatch.setattr(service, '_quota_read', replaced_during_inventory)
    sent = len(api_calls(native))
    assert service.execute_job(job)['code'] == 'REVIEW_STALE'
    assert len(api_calls(native)) == sent
    monkeypatch.setattr(service, '_quota_read', read)
    assert prepare(console, codex_credits(), 'after')['requires_confirmation'] is True


def test_acceptance_never_installs_a_grant_under_a_newer_credentials_version(console, monkeypatch):
    service, owner, native, _ = console
    inventory(native, codex_credential())
    first = prepare(console, codex_usage(), 'first')
    submit = owner.submit

    def accepted_then_replaced(job):
        receipt = submit(job)
        bump_credentials(service)
        return receipt

    monkeypatch.setattr(owner, 'submit', accepted_then_replaced)
    assert execute(console, first, 'first')['ok']
    monkeypatch.setattr(owner, 'submit', submit)
    assert prepare(console, codex_credits(), 'after')['requires_confirmation'] is True
    run(console, codex_usage(), 'second')
    assert prepare(console, codex_credits(), 'covered')['requires_confirmation'] is False


def test_unlock_quota_consent_covers_only_existing_verified_accounts(console):
    _, _, native, _ = console
    inventory(native, codex_credential(), credential('claude', 'claude-index'),
              credential('antigravity', 'agy-index', project_id=PROJECT))
    scoped = reopen(console, 'quota-consent', quota_reads=True)
    for number, wire in enumerate((codex_usage(), codex_subscription(), codex_credits(),
                                   claude_call('/api/oauth/usage'), agy_subscription(), agy_quota())):
        assert run(scoped, wire, 'covered-' + str(number))['requires_confirmation'] is False
    consume = api_call(CODEX_URL + '/wham/rate-limit-reset-credits/consume', CODEX_HEADER, method='POST')
    assert prepare(scoped, consume, 'consume')['requires_confirmation'] is True
    assert prepare(scoped, api_call('https://unapproved.example/usage', CODEX_HEADER),
                   'other-destination')['requires_confirmation'] is True


@pytest.mark.parametrize('change', ['added', 'replaced'])
def test_unlock_quota_consent_does_not_authorize_new_or_replaced_accounts(console, change):
    _, _, native, _ = console
    inventory(native, codex_credential())
    scoped = reopen(console, 'current-accounts', quota_reads=True)
    if change == 'added':
        inventory(native, codex_credential(), codex_credential('new-index'))
        wire = codex_usage('new-index')
    else:
        inventory(native, codex_credential(email='replacement@generated.example'))
        wire = codex_usage()
    assert prepare(scoped, wire, 'changed-account')['requires_confirmation'] is True


def test_unlock_quota_permission_is_bound_to_the_host_grant(console):
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from hermes_cpa.console_service import unlock_intent
    from hermes_cpa.operation_guard import OperationGuardServer

    service, owner, native, args = console
    payload = {'_scope': args['_scope'], 'document_id': args['document_id'],
               'request_id': 'quota-authority', 'expected_generation': owner.generation,
               'consent': True, 'quota_reads': False}
    with TemporaryDirectory(prefix='cpa-quota-') as directory:
        server = OperationGuardServer(Path(directory))
        try:
            grant = server.grant(unlock_intent(payload), lambda **kwargs: True)
            result = service.dispatch('unlock', dict(payload, quota_reads=True, _host_grant=grant))
            assert result['code'] == 'STALE_SCOPE'
            assert not any(call[1] == INVENTORY for call in native.calls)
        finally:
            server.close()


@pytest.mark.parametrize('change', ['credentials', 'generation'])
def test_unlock_rejects_inventory_changed_during_quota_consent(console, monkeypatch, change):
    service, owner, native, args = console
    inventory(native, codex_credential())
    original = native.request

    def changed(method, path, **kwargs):
        response = original(method, path, **kwargs)
        if path == INVENTORY:
            if change == 'credentials':
                bump_credentials(service)
            else:
                owner.generation += 1
        return response

    monkeypatch.setattr(native, 'request', changed)
    count = len(service._leases)
    result = service.dispatch('unlock', dict(args, request_id='raced-unlock',
        expected_generation=owner.generation, consent=True, quota_reads=True))
    assert result['code'] == ('REVIEW_STALE' if change == 'credentials' else 'STALE_GENERATION')
    assert len(service._leases) == count


def test_unlock_quota_consent_fails_closed_when_inventory_is_unavailable(console):
    service, owner, native, args = console
    native.responses['GET', INVENTORY] = SimpleNamespace(status=503, headers={}, body=b'{}')
    result = service.dispatch('unlock', dict(args, request_id='unavailable-inventory',
        expected_generation=owner.generation, consent=True, quota_reads=True))
    assert result['code'] == 'UNAVAILABLE'
