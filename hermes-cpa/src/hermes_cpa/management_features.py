"""Fixed passive reads and closed projections for the read-oriented CPAMC slice.

A syntactically valid model identifier can still be a secret. Only finite pinned
catalog literals may cross the boundary. No arbitrary upstream text is returned.
"""
import re

PROVIDERS = frozenset(('gemini', 'interactions', 'vertex', 'codex', 'claude',
                       'xai', 'meta', 'openai-compatibility'))
# CLIProxyAPI 54946fa3, internal/registry/models/models.json, codex-pro.
MODELS = frozenset(('gpt-5.5', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6.1-sol',
                    'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra',
                    'gpt-5.6-luna', 'codex-auto-review'))
FEATURE_PATHS = {
    'providers': '/v8/management/config',
    'auth': None,
    'quota': None,
    'models': '/v8/management/routing/model-definitions/codex',
    'logs': '/v8/management/observability/logs?limit=100',
    'plugins': None,
}
UNSUPPORTED = frozenset(('auth', 'quota', 'plugins'))
# CLIProxyAPI 54946fa3dfa29c6ca7312ac141a92cdd5e413771:
# internal/logging/global_logger.go:100-139, requestid.go:40-47.
# Timestamp, short request ID (or --------), then %-5s level; warning -> warn.
# Only printable ASCII IDs without brackets are structurally unambiguous.
# Other IDs fail closed; never scan the message.
LOG_LEVEL = re.compile(r'\A\[[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01]) '
                       r'(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\] '
                       r'\[[\x20-\x5a\x5c\x5e-\x7e]{0,8}\] '
                       r'\[(trace|debug|info |warn |error|fatal|panic)\] ')
# Keep the existing backend/desktop severity enum; do not export upstream text.
LOG_SEVERITIES = {'trace': 'trace', 'debug': 'debug', 'info ': 'info',
                  'warn ': 'warning', 'error': 'error', 'fatal': 'fatal', 'panic': 'panic'}


def integer(value, maximum=10000000, minimum=0):
    return value if type(value) is int and minimum <= value <= maximum else None


def project_feature(feature, data):
    if feature in UNSUPPORTED:
        return {'available': False, 'reason': 'PLUGIN_CALLBACK_UNSAFE'}
    if feature not in FEATURE_PATHS or not isinstance(data, dict):
        return {'available': False}
    if feature == 'providers':
        if type(data.get('config-version')) is not int or data['config-version'] != 8:
            return {'available': False}
        families = data.get('api-keys', {})
        if not isinstance(families, dict):
            return {'available': False}
        items = []
        for provider in sorted(PROVIDERS):
            groups = families.get(provider, [])
            if groups is None:
                groups = []
            if not isinstance(groups, list) or len(groups) > 1000:
                return {'available': False}
            for group in groups:
                if not isinstance(group, dict):
                    return {'available': False}
                keys = group.get('keys', [])
                if not isinstance(keys, list) or len(keys) > 1000 or any(not isinstance(k, dict) for k in keys):
                    return {'available': False}
                items.append({'ordinal': len(items) + 1, 'provider': provider,
                              'priority': integer(group.get('priority'), minimum=-10000000),
                              'credentials': len(keys),
                              'disabled_credentials': sum(k.get('disabled') is True for k in keys)})
                if len(items) > 1000:
                    return {'available': False}
        return {'available': True, 'items': items}
    rows = data.get('models' if feature == 'models' else 'lines')
    if not isinstance(rows, list) or len(rows) > 1000:
        return {'available': False}
    if feature == 'logs':
        if any(not isinstance(row, str) for row in rows):
            return {'available': False}
        items = []
        for i, row in enumerate(rows):
            match = LOG_LEVEL.match(row)
            items.append({'ordinal': i + 1,
                          'severity': LOG_SEVERITIES[match[1]] if match else 'unknown'})
        return {'available': True, 'items': items, 'entries': len(rows), 'content': 'withheld'}
    if any(not isinstance(row, dict) for row in rows):
        return {'available': False}
    items = []
    for row in rows:
        model = row.get('id')
        if isinstance(model, str) and model in MODELS:
            items.append({'ordinal': len(items) + 1, 'model': model,
                          'context_length': integer(row.get('context_length')),
                          'max_completion_tokens': integer(row.get('max_completion_tokens'))})
    return {'available': True, 'items': items, 'entries': len(rows),
            'withheld': len(rows) - len(items), 'channel': 'codex', 'source': 'static_catalog'}
