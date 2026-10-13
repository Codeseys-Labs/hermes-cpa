"""Pinned CPA formatter behavior at the content-withholding projection boundary.

Source: CLIProxyAPI 54946fa3dfa29c6ca7312ac141a92cdd5e413771,
internal/logging/global_logger.go:100-139 (implementation, not the stale comment).
"""
import json

import pytest

from hermes_cpa.management_features import project_feature


SECRET = 'sk-generated-secret-canary-never-render'
STAMP = '[2026-10-12 17:00:00]'
VALID = STAMP + ' [--------] [info ] [fixture.go:1] withheld'


def test_actual_disposable_pinned_formatter_output():
    from pathlib import Path

    fixture = json.loads((Path(__file__).parent / 'fixtures/pinned_formatter_logs.json').read_text())
    assert fixture['upstream_commit'] == '54946fa3dfa29c6ca7312ac141a92cdd5e413771'
    rows = fixture['rows']
    # CPA's management log reader strips line terminators; it does not reorder fields.
    result = project_feature('logs', {'lines': [row['line'].rstrip('\r\n') for row in rows]})
    assert result == {
        'available': True,
        'items': [{'ordinal': n + 1, 'severity': row['severity']} for n, row in enumerate(rows)],
        'entries': len(rows), 'content': 'withheld',
    }
    assert 'generated-canary' not in json.dumps(result)


def test_pinned_formatter_info_with_request_placeholder_and_padding():
    result = project_feature('logs', {'lines': [VALID]})
    assert result == {
        'available': True,
        'items': [{'ordinal': 1, 'severity': 'info'}],
        'entries': 1,
        'content': 'withheld',
    }


@pytest.mark.parametrize(('field', 'severity'), [
    ('trace', 'trace'), ('debug', 'debug'), ('info ', 'info'),
    ('warn ', 'warning'), ('error', 'error'), ('fatal', 'fatal'), ('panic', 'panic'),
])
@pytest.mark.parametrize('request_id', ['--------', '5678abcd', '00000042', 'req', '', 'id!_A-9?'])
@pytest.mark.parametrize('caller', ['', '[private-secret-path.go:42] '])
def test_exact_pinned_levels_ids_and_optional_caller_withhold_content(field, severity, request_id, caller):
    # ShortRequestID trims and truncates to eight bytes; it is not a hex validator.
    # An all-whitespace nonempty input becomes an empty formatted request ID.
    line = f'{STAMP} [{request_id}] [{field}] {caller}{SECRET} [error]'
    result = project_feature('logs', {'lines': [line], 'next-cursor': SECRET})
    assert result == {
        'available': True, 'items': [{'ordinal': 1, 'severity': severity}],
        'entries': 1, 'content': 'withheld',
    }
    assert SECRET not in json.dumps(result)
    assert 'private-secret-path' not in json.dumps(result)


@pytest.mark.parametrize('line', [
    '', SECRET, '[info] ' + SECRET,
    STAMP + ' [info] ' + SECRET,  # Legacy/non-pinned field order.
    STAMP + ' [error] [server.go:12] ' + SECRET,
    STAMP + ' [--------] [info] ' + SECRET,  # Required width-five padding.
    STAMP + ' [--------] [info  ] ' + SECRET,
    STAMP + ' [--------] [ info] ' + SECRET,
    STAMP + ' [--------] [info\t] ' + SECRET,
    STAMP + ' [--------] [warn] ' + SECRET,
    STAMP + ' [--------] [warning] ' + SECRET,
    STAMP + ' [--------] [ERROR] ' + SECRET,
    STAMP + ' [--------] [notice] ' + SECRET,
    STAMP + ' [--------] [error-secret] ' + SECRET,
    STAMP + ' [---------] [error] ' + SECRET,
    STAMP + ' [a[b] [error] ' + SECRET,
    STAMP + ' [a]b] [error] ' + SECRET,
    STAMP + ' [id\nid] [error] ' + SECRET,
    STAMP + ' [id\rid] [error] ' + SECRET,
    STAMP + ' [id\tid] [error] ' + SECRET,
    STAMP + ' [id\x00id] [error] ' + SECRET,
    STAMP + ' [id\x7fid] [error] ' + SECRET,
    STAMP + ' [id\u2028id] [error] ' + SECRET,
    STAMP + ' [--------] [error]',
    STAMP + ' [--------] [error]\t' + SECRET,
    STAMP + '  [--------] [error] ' + SECRET,
    STAMP + '\t[--------] [error] ' + SECRET,
    STAMP + ' [--------]  [error] ' + SECRET,
    '[2026-1-12 17:00:00] [--------] [error] ' + SECRET,
    '[2026-10-12T17:00:00] [--------] [error] ' + SECRET,
    '[2026-10-12 17:00:00.001] [--------] [error] ' + SECRET,
    '[2026-00-12 17:00:00] [--------] [error] ' + SECRET,
    '[2026-13-12 17:00:00] [--------] [error] ' + SECRET,
    '[2026-10-00 17:00:00] [--------] [error] ' + SECRET,
    '[2026-10-32 17:00:00] [--------] [error] ' + SECRET,
    '[2026-10-12 24:00:00] [--------] [error] ' + SECRET,
    '[2026-10-12 17:60:00] [--------] [error] ' + SECRET,
    '[2026-10-12 17:00:60] [--------] [error] ' + SECRET,
    '[２０２６-10-12 17:00:00] [--------] [error] ' + SECRET,
    ' ' + VALID, '\n' + VALID, '\x1b[31m' + VALID,
    SECRET + ' ' + VALID, SECRET + '\n' + VALID,
    STAMP + ' [--------] [notice] ' + VALID,
])
def test_unknown_or_malicious_prefix_fails_closed_without_scanning_message(line):
    assert project_feature('logs', {'lines': [line]}) == {
        'available': True, 'items': [{'ordinal': 1, 'severity': 'unknown'}],
        'entries': 1, 'content': 'withheld',
    }


def test_message_cannot_override_structural_severity_and_all_metadata_is_withheld():
    line = STAMP + ' [canary42] [warn ] [secret-path.go:1] ' + SECRET + '\n' + VALID
    result = project_feature('logs', {'lines': [line, SECRET, VALID],
                                      'next-cursor': SECRET, 'latest-timestamp': SECRET})
    assert result == {
        'available': True,
        'items': [{'ordinal': 1, 'severity': 'warning'},
                  {'ordinal': 2, 'severity': 'unknown'},
                  {'ordinal': 3, 'severity': 'info'}],
        'entries': 3, 'content': 'withheld',
    }


@pytest.mark.parametrize('size', [0, 1000])
def test_existing_row_bound_and_order_preserved(size):
    result = project_feature('logs', {'lines': [VALID] * size})
    assert result == {
        'available': True,
        'items': [{'ordinal': n + 1, 'severity': 'info'} for n in range(size)],
        'entries': size, 'content': 'withheld',
    }


@pytest.mark.parametrize('data', [None, {}, {'lines': None}, {'lines': VALID},
                                  {'lines': [VALID] * 1001}, {'lines': [VALID, None]},
                                  {'lines': [1]}, {'lines': [{}]}, {'lines': [True]}])
def test_existing_invalid_response_fails_unavailable(data):
    assert project_feature('logs', data) == {'available': False}
