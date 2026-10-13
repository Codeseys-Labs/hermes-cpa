import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from hermes_cpa.cpa_adapter import CpaAdapter
from hermes_cpa.settings import RequestScope


@pytest.fixture
def server():
    state = SimpleNamespace(key='generated-management', status=200, version='v8.0.21',
                            body={'config-version': 8, 'access': {'api-keys': ['INFERENCE_CANARY']}},
                            hits=0, authorized=False)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            state.hits += 1
            state.authorized = self.headers.get('Authorization') == 'Bearer ' + state.key
            self.send_response(state.status if state.authorized else 401)
            self.send_header('X-CPA-VERSION', state.version)
            self.send_header('Location', '/redirected')
            self.end_headers()
            self.wfile.write(json.dumps(state.body).encode())
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}', state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.parametrize('origin', ['http://localhost:1', 'http://example.org:80',
                                   'http://127.0.0.1:1/path', 'http://user@127.0.0.1:1',
                                   'http://127.0.0.1:1?x=y', 'https://127.0.0.1:1',
                                   'http://2130706433:1'])
def test_reject_nonliteral_or_decorated_origin(origin):
    with pytest.raises(ValueError):
        CpaAdapter(origin)


@pytest.mark.parametrize('key', ['', 'wrong', 'INFERENCE_CANARY'])
def test_missing_wrong_and_inference_key_not_ready(server, key):
    origin, state = server
    assert not CpaAdapter(origin, key).probe().management_ready
    if not key:
        assert state.hits == 0


def test_scoped_resolution_each_request_and_locked(server, tmp_path):
    origin, state = server
    a = RequestScope('A', 'one', 1, tmp_path / 'a')
    b = RequestScope('B', 'two', 1, tmp_path / 'b')
    class Resolver:
        locked = False
        def resolve_management(self, scope):
            if self.locked:
                raise RuntimeError('LOCKED_CANARY')
            return state.key if scope == a else 'wrong'
    resolver = Resolver()
    adapter = CpaAdapter.from_scope(origin, a, resolver)
    result = adapter.probe()
    assert result.management_ready and result.inference_configured is None
    assert 'INFERENCE_CANARY' not in repr(result)
    assert not CpaAdapter.from_scope(origin, b, resolver).probe().management_ready
    resolver.locked = True
    assert not adapter.probe().management_ready
    assert 'LOCKED_CANARY' not in repr(adapter.probe())


@pytest.mark.parametrize('status,version,body', [(302, 'v8.0.21', {'config-version': 8}),
    (200, 'v8.0.21', {'hello': 'world'}), (200, 'v9.0.0', {'config-version': 8}),
    (200, 'v8.0.21', {'config-version': True})])
def test_wrong_service_redirect_version(server, status, version, body):
    origin, state = server
    state.status, state.version, state.body = status, version, body
    assert not CpaAdapter(origin, state.key).probe().management_ready
    assert state.hits == 1


def test_usage_projection_discards_all_key_material(server):
    origin, state = server
    state.body = {
        'team-production-proxy': {
            'https://private.example/v1|sk-live-generated': {
                'success': 5, 'failed': 2, 'model': 'MODEL_CANARY',
                'recent_requests': [
                    {'success': 1, 'failed': 0, 'time': '12:00-12:10', 'key': 'BUCKET_CANARY'}
                    for _ in range(20)],
            },
        },
    }
    ok, usage = CpaAdapter(origin, state.key).probe_usage()
    assert ok and state.authorized and state.hits == 1
    assert set(usage) == {'available', 'timestamp', 'unit', 'scope', 'credential_entries',
                          'total_success', 'total_failed', 'providers', 'window'}
    assert usage['scope'] == 'api_key_credentials'
    assert (usage['credential_entries'], usage['total_success'], usage['total_failed']) == (1, 5, 2)
    expected = [{'success': 1, 'failed': 0} for _ in range(20)]
    assert usage['providers'] == [
        {'provider': 'other', 'credential_entries': 1, 'success': 5, 'failed': 2, 'buckets': expected},
    ]
    assert usage['window'] == {'available': True, 'bucket_seconds': 600, 'buckets': expected}
    encoded = json.dumps(usage)
    for private in ('CANARY', 'team-production-proxy', 'private.example', 'sk-live-generated', '12:00-12:10'):
        assert private not in encoded


def test_usage_empty_map_is_observed_no_entries_not_a_quiet_window(server):
    origin, state = server
    state.body = {}
    ok, usage = CpaAdapter(origin, state.key).probe_usage()
    assert ok and usage['available']
    assert usage['credential_entries'] == 0
    assert usage['total_success'] == usage['total_failed'] == 0
    assert usage['providers'] == []
    assert usage['window'] == {'available': False, 'bucket_seconds': 600, 'buckets': []}


@pytest.mark.parametrize('recent', [None, [], [{'success': 0, 'failed': 0}] * 21])
def test_usage_bad_recent_data_keeps_lifetime_observation(server, recent):
    origin, state = server
    state.body = {'codex': {'private-key': {'success': 4, 'failed': 1, 'recent_requests': recent}}}
    ok, usage = CpaAdapter(origin, state.key).probe_usage()
    assert ok and usage['available']
    assert (usage['credential_entries'], usage['total_success'], usage['total_failed']) == (1, 4, 1)
    assert usage['providers'] == [
        {'provider': 'codex', 'credential_entries': 1, 'success': 4, 'failed': 1, 'buckets': []},
    ]
    assert usage['window'] == {'available': False, 'bucket_seconds': 600, 'buckets': []}


@pytest.mark.parametrize('status,version,body', [
    (503, 'v8.0.21', {'error': 'SERVICE_CANARY'}),
    (302, 'v8.0.21', {}),
    (200, 'v9.0.0', {}),
    (200, 'v8.0.21', []),
    (200, 'v8.0.21', {'codex': {'key': {'success': True, 'failed': 0}}}),
    (200, 'v8.0.21', {'codex': {'key': {'success': 2 ** 53, 'failed': 0}}}),
    (200, 'v8.0.21', {'codex': {'key': {'success': 2 ** 53 - 1, 'failed': 1}}}),
])
def test_unavailable_usage_never_becomes_zero_or_echoes_upstream(server, status, version, body):
    origin, state = server
    state.status, state.version, state.body = status, version, body
    ok, usage = CpaAdapter(origin, state.key).probe_usage()
    assert not ok and not usage['available']
    assert usage['credential_entries'] is usage['total_success'] is usage['total_failed'] is None
    assert usage['providers'] == []
    assert usage['window'] == {'available': False, 'bucket_seconds': 600, 'buckets': []}
    assert 'CANARY' not in json.dumps(usage)
    assert state.hits == 1


def test_usage_source_response_size_is_bounded_before_projection(server):
    origin, state = server
    state.body = {'codex': {'private-key': {
        'success': 1, 'failed': 0, 'ignored': 'x' * (1024 * 1024),
    }}}
    ok, usage = CpaAdapter(origin, state.key).probe_usage()
    assert not ok
    assert usage['credential_entries'] is usage['total_success'] is usage['total_failed'] is None
    assert usage['providers'] == []
    assert usage['window'] == {'available': False, 'bucket_seconds': 600, 'buckets': []}
