import copy
import json

import pytest

from hermes_cpa.telemetry import TelemetryCollector


MAX_SAFE_INTEGER = 2 ** 53 - 1
PROVIDERS = ('gemini', 'gemini-interactions', 'vertex', 'codex', 'claude', 'xai', 'meta', 'other')


def buckets(success=0, failed=0):
    return [{'success': success, 'failed': failed} for _ in range(20)]


def entry(success=0, failed=0):
    return {'success': success, 'failed': failed, 'recent_requests': buckets()}


def projection(raw):
    result = TelemetryCollector.normalize_usage(raw)
    assert isinstance(result.pop('timestamp'), float)
    assert result.pop('unit') == 'requests'
    return result


def assert_unavailable(raw):
    assert projection(raw) == {
        'available': False, 'scope': 'api_key_credentials', 'credential_entries': None,
        'total_success': None, 'total_failed': None, 'providers': [],
        'window': {'available': False, 'bucket_seconds': 600, 'buckets': []},
    }


def test_telemetry_normalization_aligns_all_entries_and_collapses_custom_providers():
    first = entry(200, 20)
    first['recent_requests'] = [{'success': i, 'failed': 1, 'time': '12:00-12:10'}
                                for i in range(20)]
    second = entry(400, 40)
    second['recent_requests'] = [{'success': 2 * i, 'failed': 2} for i in range(20)]
    raw = {
        'my-private-proxy': {'https://private.example|sk-live-123': entry(80, 20)},
        'claude': {'key1': first, 'key2': second},
        'gemini': {'key3': entry(0, 0)},
        'another-private-proxy': {'key4': entry(10, 5)},
        'codex': {},
    }
    expected = [{'success': 3 * i, 'failed': 3} for i in range(20)]
    assert projection(raw) == {
        'available': True, 'scope': 'api_key_credentials', 'credential_entries': 5,
        'total_success': 690, 'total_failed': 85,
        'providers': [
            {'provider': 'gemini', 'credential_entries': 1, 'success': 0, 'failed': 0,
             'buckets': buckets()},
            {'provider': 'claude', 'credential_entries': 2, 'success': 600, 'failed': 60,
             'buckets': expected},
            {'provider': 'other', 'credential_entries': 2, 'success': 90, 'failed': 25,
             'buckets': buckets()},
        ],
        'window': {'available': True, 'bucket_seconds': 600, 'buckets': expected},
    }


def test_provider_enum_and_order_are_closed():
    raw = {name: {'key': entry(1)} for name in reversed(PROVIDERS)}
    raw.update({name: {'key': entry(1)} for name in ('anthropic', 'openai', 'qwen', 'Codex', '')})
    result = projection(raw)
    assert [group['provider'] for group in result['providers']] == list(PROVIDERS)
    assert result['credential_entries'] == 13
    assert result['providers'][-1] == {
        'provider': 'other', 'credential_entries': 6, 'success': 6, 'failed': 0,
        'buckets': buckets(),
    }


@pytest.mark.parametrize('raw', [{}, {'codex': {}, 'custom': {}}])
def test_empty_credentials_have_lifetime_zero_but_no_recent_window(raw):
    assert projection(raw) == {
        'available': True, 'scope': 'api_key_credentials', 'credential_entries': 0,
        'total_success': 0, 'total_failed': 0, 'providers': [],
        'window': {'available': False, 'bucket_seconds': 600, 'buckets': []},
    }


def test_observed_zero_entry_has_a_genuine_quiet_window():
    result = projection({'codex': {'key': entry()}})
    assert result['credential_entries'] == 1
    assert result['total_success'] == result['total_failed'] == 0
    assert result['window'] == {'available': True, 'bucket_seconds': 600, 'buckets': buckets()}
    assert result['providers'][0]['buckets'] == buckets()


@pytest.mark.parametrize('raw', [None, [], '', 0, True, {'codex': []},
                                 {'codex': {'key': None}}, {'codex': {'key': []}},
                                 {1: {}}, {'codex': {1: entry()}}])
def test_malformed_lifetime_structure_invalidates_whole_projection(raw):
    assert_unavailable(raw)


@pytest.mark.parametrize('field', ['success', 'failed'])
@pytest.mark.parametrize('value', [None, True, False, -1, 1.0, '1', MAX_SAFE_INTEGER + 1,
                                  float('inf'), float('nan')])
def test_lifetime_counts_are_exact_safe_nonnegative_integers(field, value):
    invalid = entry()
    invalid[field] = value
    assert_unavailable({'gemini': {'valid': entry(1)}, 'codex': {'invalid': invalid}})


@pytest.mark.parametrize('field', ['success', 'failed'])
def test_missing_lifetime_counts_do_not_default_to_zero(field):
    invalid = entry()
    del invalid[field]
    assert_unavailable({'codex': {'key': invalid}})


@pytest.mark.parametrize('raw', [
    {'codex': {'key': entry(MAX_SAFE_INTEGER, 1)}},
    {'codex': {'first': entry(MAX_SAFE_INTEGER), 'second': entry(1)}},
    {'codex': {'first': entry(MAX_SAFE_INTEGER)}, 'claude': {'second': entry(0, 1)}},
    {'custom-a': {'first': entry(MAX_SAFE_INTEGER)}, 'custom-b': {'second': entry(1)}},
])
def test_lifetime_pair_provider_and_global_overflow_fail_closed(raw):
    assert_unavailable(raw)


@pytest.mark.parametrize('field', ['success', 'failed'])
def test_lifetime_safe_integer_boundary_is_exact(field):
    value = entry()
    value[field] = MAX_SAFE_INTEGER
    result = projection({'codex': {'key': value}})
    assert result['available'] is True
    assert result['total_' + field] == MAX_SAFE_INTEGER
    assert result['providers'][0][field] == MAX_SAFE_INTEGER


@pytest.mark.parametrize('recent', [None, {}, [], buckets()[:-1], buckets() + buckets(),
                                    [None] + buckets()[1:], [[]] + buckets()[1:],
                                    [{}] + buckets()[1:]])
def test_malformed_recent_data_clears_every_provider_window_only(recent):
    bad = entry(3, 1)
    bad['recent_requests'] = recent
    result = projection({'gemini': {'first': entry(4, 2)}, 'codex': {'bad': bad},
                         'claude': {'last': entry(5, 3)}})
    assert result['available'] is True
    assert (result['credential_entries'], result['total_success'], result['total_failed']) == (3, 12, 6)
    assert [(p['provider'], p['success'], p['failed'], p['buckets']) for p in result['providers']] == [
        ('gemini', 4, 2, []), ('codex', 3, 1, []), ('claude', 5, 3, []),
    ]
    assert result['window'] == {'available': False, 'bucket_seconds': 600, 'buckets': []}


def test_missing_recent_data_and_later_invalid_lifetime_are_not_hidden():
    missing = {'success': 4, 'failed': 2}
    result = projection({'codex': {'key': missing}})
    assert result['available'] is True
    assert result['total_success'] == 4
    assert result['providers'][0]['buckets'] == []
    assert result['window']['available'] is False
    assert_unavailable({'codex': {'missing': missing, 'invalid': entry(True)}})


@pytest.mark.parametrize('field', ['success', 'failed'])
@pytest.mark.parametrize('value', [None, True, False, -1, 1.0, '1', MAX_SAFE_INTEGER + 1])
def test_invalid_recent_counts_preserve_lifetime(field, value):
    raw = entry(7, 2)
    raw['recent_requests'][19][field] = value
    result = projection({'codex': {'key': raw}})
    assert result['available'] is True
    assert (result['total_success'], result['total_failed']) == (7, 2)
    assert result['providers'][0]['buckets'] == []
    assert result['window'] == {'available': False, 'bucket_seconds': 600, 'buckets': []}


@pytest.mark.parametrize('case', ['pair', 'same-group', 'other-group', 'collapsed-group', 'across-buckets'])
def test_recent_overflow_invalidates_all_windows_not_lifetime(case):
    first, second = entry(7, 2), entry(3, 1)
    first['recent_requests'][0]['success'] = MAX_SAFE_INTEGER
    raw = {'codex': {'first': first}, 'gemini': {'quiet': entry()}}
    if case == 'pair':
        first['recent_requests'][0]['failed'] = 1
    elif case == 'across-buckets':
        first['recent_requests'][19]['failed'] = 1
    else:
        second['recent_requests'][0]['failed'] = 1
        if case == 'same-group':
            raw['codex']['second'] = second
        elif case == 'other-group':
            raw['claude'] = {'second': second}
        else:
            raw['custom-a'] = raw.pop('codex')
            raw['custom-b'] = {'second': second}
    result = projection(raw)
    assert result['available'] is True
    assert result['total_success'] == (7 if case in ('pair', 'across-buckets') else 10)
    assert all(group['buckets'] == [] for group in result['providers'])
    assert result['window'] == {'available': False, 'bucket_seconds': 600, 'buckets': []}


def test_recent_safe_boundary_is_exact_without_inventing_lifetime_constraints():
    raw = entry()
    raw['recent_requests'][19]['success'] = MAX_SAFE_INTEGER
    result = projection({'codex': {'key': raw}})
    assert result['total_success'] == 0
    assert result['window']['available'] is True
    assert result['window']['buckets'][19] == {'success': MAX_SAFE_INTEGER, 'failed': 0}


def test_source_provider_group_bound_includes_empty_groups():
    raw = {f'custom-{i}': {} for i in range(1000)}
    assert projection(raw)['available'] is True
    raw['one-too-many'] = {}
    assert_unavailable(raw)


def test_source_entry_bound_is_global_not_per_provider():
    raw = {'codex': {str(i): entry() for i in range(500)},
           'custom': {str(i): entry() for i in range(500)}}
    result = projection(raw)
    assert result['available'] is True
    assert result['credential_entries'] == 1000
    assert result['window']['available'] is True
    raw['custom']['extra'] = entry()
    assert_unavailable(raw)


def test_only_allowlisted_fields_escape_without_mutating_or_aliasing_source():
    value = entry(2, 1)
    value.update(model='private-finetune-identifier', token='sk-live-not-for-display',
                 error={'message': 'PRIVATE_CANARY'}, buckets='IGNORE_CANARY')
    value['recent_requests'][0].update(time='09:10-09:20', credential='BUCKET_CANARY',
                                       success_detail={'api_key': 'SECRET_CANARY'})
    raw = {'internal-customer-route': {'https://proxy.internal|sk-prod-secret': value}}
    before = copy.deepcopy(raw)
    result = projection(raw)
    assert raw == before
    encoded = json.dumps(result)
    for private in ('private-finetune-identifier', 'sk-live-not-for-display', 'CANARY',
                    '09:10-09:20', 'internal-customer-route', 'proxy.internal', 'sk-prod-secret'):
        assert private not in encoded
    assert result['providers'][0]['provider'] == 'other'
    assert set(result['providers'][0]) == {'provider', 'credential_entries', 'success', 'failed', 'buckets'}
    assert all(set(bucket) == {'success', 'failed'} for bucket in result['window']['buckets'])
    result['providers'][0]['buckets'][0]['success'] = 42
    assert result['window']['buckets'][0]['success'] == 0
    assert raw == before


def test_custom_provider_windows_merge_by_index_into_one_other_group():
    first, second = entry(10, 1), entry(20, 2)
    first['recent_requests'][0] = {'success': 2, 'failed': 1}
    second['recent_requests'][0] = {'success': 3, 'failed': 2}
    second['recent_requests'][19] = {'success': 7, 'failed': 0}
    result = projection({'private-route-a': {'same-key': first},
                         'private-route-b': {'same-key': second}})
    expected = buckets()
    expected[0] = {'success': 5, 'failed': 3}
    expected[19] = {'success': 7, 'failed': 0}
    assert result['providers'] == [
        {'provider': 'other', 'credential_entries': 2, 'success': 30, 'failed': 3,
         'buckets': expected},
    ]
    assert result['credential_entries'] == 2
    assert result['window'] == {'available': True, 'bucket_seconds': 600, 'buckets': expected}


def test_combined_safe_boundary_across_providers_and_window_indices_is_accepted():
    first, second = entry(MAX_SAFE_INTEGER - 1), entry(0, 1)
    first['recent_requests'][0]['success'] = MAX_SAFE_INTEGER - 1
    second['recent_requests'][19]['failed'] = 1
    result = projection({'codex': {'key': first}, 'claude': {'key': second}})
    assert result['available'] is True
    assert (result['total_success'], result['total_failed']) == (MAX_SAFE_INTEGER - 1, 1)
    assert result['window']['available'] is True
    assert result['window']['buckets'][0] == {'success': MAX_SAFE_INTEGER - 1, 'failed': 0}
    assert result['window']['buckets'][19] == {'success': 0, 'failed': 1}
