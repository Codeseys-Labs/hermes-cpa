"""Generated secret canaries and real loopback transport; never live CPA."""
import json
import pytest
from hermes_cpa.management_features import project_feature
from test_adapter import server

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
