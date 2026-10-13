"""Read-oriented slice: no plugin callbacks, secret text or unsafe mutation transport."""
import json
import pytest
from hermes_cpa.management_features import project_feature
from test_api import setup, headers

SECRET = 'sk-generated-canary-never-render'


def test_provider_configuration_is_useful_without_credentials_rpc():
    result = project_feature('providers', {'config-version': 8, 'api-keys': {'codex': [
        {'name': SECRET, 'base-url': SECRET, 'priority': 7, 'keys': [
            {'api-key': SECRET, 'disabled': True}, {'api-key': SECRET}]}]}})
    assert result['items'][0] == {'ordinal': 1, 'provider': 'codex', 'priority': 7,
                                   'credentials': 2, 'disabled_credentials': 1}
    assert SECRET not in json.dumps(result)


def test_models_finite_catalog_allowlist_not_identifier_regex():
    result = project_feature('models', {'models': [
        {'id': 'gpt-5.5', 'context_length': 400000, 'max_completion_tokens': 128000},
        {'id': 'gpt-5.5-' + SECRET, 'context_length': True}]})
    assert result['items'] == [{'ordinal': 1, 'model': 'gpt-5.5',
                               'context_length': 400000, 'max_completion_tokens': 128000}]
    assert result['withheld'] == 1
    assert SECRET not in json.dumps(result)


def test_logs_expose_only_severity_not_messages():
    result = project_feature('logs', {'lines': [
        '[2026-10-12 10:00:00] [--------] [error] [server.go:12] ' + SECRET,
        SECRET]})
    assert result['items'] == [{'ordinal': 1, 'severity': 'error'}, {'ordinal': 2, 'severity': 'unknown'}]
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize('feature', ['auth', 'quota', 'plugins'])
def test_callback_capable_reads_are_unsupported_without_transport(tmp_path, feature):
    from hermes_cpa.controller import CpaController
    from hermes_cpa.settings import CpaSettings
    class Adapter:
        def _request(self, *args):
            pytest.fail('Unsupported view must never contact CPA')
    controller = CpaController(CpaSettings(tmp_path), adapter=Adapter())
    assert controller.get_feature(feature) == {'available': False, 'reason': 'PLUGIN_CALLBACK_UNSAFE'}


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('feature', ['providers', 'auth', 'oauth', 'models', 'quota', 'logs'])
def test_disabled_mutations_rejected_before_controller(setup, method, feature):
    client, host = setup
    host.controller.get_feature = lambda *_: pytest.fail('Mutation reached controller')
    result = client.request(method, '/api/plugins/hermes-cpa/features/' + feature,
                            headers=headers(host), json={'name': SECRET})
    assert result.status_code in (404, 405)
    assert SECRET not in result.text
