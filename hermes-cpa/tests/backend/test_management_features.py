"""Generated secret canaries and real loopback transport; never live CPA."""
import json
import pytest
from hermes_cpa.management_features import project_feature
from test_adapter import server
from test_api import setup, headers

SECRET = 'generated-secret-echo-do-not-render'

@pytest.mark.parametrize('feature', ['providers', 'models', 'logs'])
def test_unavailable_not_empty(feature):
    assert project_feature(feature, None) == {'available': False}

@pytest.mark.parametrize('feature,body', [
    ('providers', {'config-version': 8, 'api-keys': {'codex': [{'name': SECRET, 'keys': []}]}}),
    ('models', {'models': [{'id': SECRET}]}),
    ('logs', {'lines': [SECRET]}),
])
def test_feature_real_http_scoped_management_transport(server, tmp_path, feature, body):
    from hermes_cpa.controller import CpaController, DeadlineAdapter
    from hermes_cpa.settings import CpaSettings, RequestScope
    origin, state = server
    state.body = body
    scope = RequestScope('A', 'connection', 1, tmp_path)
    class Resolver:
        def resolve_management(self, requested):
            assert requested == scope
            return state.key
    adapter = DeadlineAdapter.from_scope(origin, scope, Resolver())
    controller = CpaController(CpaSettings(tmp_path), scope=scope, adapter=adapter)
    result = controller.get_feature(feature)
    assert result['available'] is True
    assert state.authorized and state.hits == 1
    assert SECRET not in json.dumps(result)
    state.version = 'v9.0.0'
    assert controller.get_feature(feature) == {'available': False}

@pytest.mark.parametrize('feature', ['providers', 'auth', 'quota', 'models', 'logs', 'plugins'])
def test_feature_rest_authentication_scope_and_aba(setup, feature):
    client, host = setup
    path = '/api/plugins/hermes-cpa/features/' + feature
    assert client.get(path).status_code == 401
    host.controller.get_feature = lambda name: {'available': True, 'items': []}
    original = headers(host)
    result = client.get(path, headers=original)
    assert result.status_code == 200
    assert result.json()['scope_id'] == 'A'
    assert result.json()['data']['available'] is True
    assert result.headers['cache-control'] == 'no-store'
    host.select('B')
    assert client.get(path, headers=original).status_code == 409
    host.select('A')
    assert client.get(path, headers=original).status_code == 409

def test_unknown_feature_rejected_without_transport(setup):
    client, host = setup
    assert client.get('/api/plugins/hermes-cpa/features/fetch', headers=headers(host)).status_code == 422
