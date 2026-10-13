import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from hermes_cpa.cpa_adapter import CpaAdapter
from hermes_cpa.settings import RequestScope
from hermes_cpa.telemetry import TelemetryCollector


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
    state.body = {'PROVIDER_CANARY': {'https://secret|API_KEY_CANARY': {'success': 0, 'failed': 0}}}
    ok, usage = CpaAdapter(origin, state.key).probe_usage()
    assert ok and usage['total_success'] == 0
    assert 'CANARY' not in json.dumps(usage)
    assert TelemetryCollector.normalize_usage(None)['total_success'] is None
    assert TelemetryCollector.normalize_usage({})['total_success'] == 0
    assert not TelemetryCollector.normalize_usage({'p': {'k': {'success': True, 'failed': 0}}})['available']
