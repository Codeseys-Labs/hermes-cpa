"""Pinned native management request grammar and effect classification.

No management credentials or arbitrary caller headers cross this seam. Config
semantics belong to ConfigV8 in the owned CPA process, not this registry.
"""
import base64
import binascii
import json
import re
import secrets
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

FILE_LIMIT = 10 * 1024 * 1024
WIRE_LIMIT = 48 * 1024 * 1024
RESULT_LIMIT = 32 * 1024 * 1024
PREFIX = '/v8/management'
METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}


class ConsoleError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code='INVALID_INPUT'):
    if not condition:
        raise ConsoleError(code)


def identifier(value):
    require(isinstance(value, str) and 1 <= len(value) <= 128 and
            re.fullmatch(r'[A-Za-z0-9_.:-]+', value) is not None)
    return value


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False,
                          separators=(',', ':'), sort_keys=True).encode()
    except (ValueError, TypeError, RecursionError):
        raise ConsoleError('INVALID_INPUT') from None


def string_map(value):
    require(isinstance(value, dict) and len(value) <= 256)
    require(all(isinstance(k, str) and 0 < len(k) <= 256 and
                isinstance(v, str) and len(v) <= 65536 and
                not any(c in k for c in '\r\n\0') for k, v in value.items()))
    return value


def path_value(value):
    require(isinstance(value, str) and 0 < len(value) <= 4096 and value.startswith('/')
            and not value.startswith('//') and not any(c in value for c in '?\x00#\\\r\n'))
    decoded = unquote(value)
    require(not any(p in ('.', '..') for p in decoded.split('/')) and
            not any(c in decoded for c in '\x00\\\r\n') and '%2f' not in value.lower())
    return value


def decode_bytes(value, limit):
    require(isinstance(value, str) and len(value) <= ((limit + 2) // 3) * 4,
            'PAYLOAD_TOO_LARGE')
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ConsoleError('INVALID_INPUT') from None
    require(len(result) <= limit, 'PAYLOAD_TOO_LARGE')
    return result


def content_type(value):
    require(isinstance(value, str) and 0 < len(value) <= 256 and
            not any(c in value for c in '\r\n\0'))
    return value


def part_name(value):
    require(isinstance(value, str) and 0 < len(value) <= 512 and
            not any(c in value for c in '\r\n\0"\\'))
    return value


@dataclass(frozen=True)
class NativeRequest:
    wire: dict
    body: bytes
    content_type: str | None

    @property
    def method(self):
        return self.wire['method']

    @property
    def path(self):
        return self.wire['path']

    @property
    def config_write(self):
        return self.method != 'GET' and (self.path in (PREFIX + '/config', PREFIX + '/config.yaml')
                                        or self.path.startswith(PREFIX + '/config/'))

    def config_wire(self):
        result = {'method': self.method, 'path': self.path}
        if self.content_type:
            result['content_type'] = self.content_type
        if self.body:
            result['body_base64'] = base64.b64encode(self.body).decode()
        return result


def parse_request(value):
    require(isinstance(value, dict) and not set(value) - {
        'method', 'path', 'query', 'body', 'response_kind', 'expected_revision'})
    require(value.get('method') in METHODS and value.get('response_kind') in ('json', 'text', 'bytes'))
    path_value(value.get('path'))
    query = string_map(value.get('query', {}))
    require(sum(len(k) + len(v) for k, v in query.items()) <= 65536)
    revision = value.get('expected_revision')
    require(revision is None or isinstance(revision, str) and re.fullmatch('[a-f0-9]{64}', revision))
    body = value.get('body')
    require(isinstance(body, dict))
    kind = body.get('kind')
    raw, media = b'', None
    if kind == 'empty':
        require(set(body) == {'kind'})
    elif kind == 'json':
        require(set(body) == {'kind', 'value'})
        raw, media = encoded(body['value']), 'application/json'
    elif kind == 'text':
        require(set(body) == {'kind', 'value', 'content_type'} and isinstance(body['value'], str))
        raw, media = body['value'].encode(), content_type(body['content_type'])
    elif kind == 'bytes':
        require(set(body) == {'kind', 'base64', 'content_type'})
        raw, media = decode_bytes(body['base64'], FILE_LIMIT), content_type(body['content_type'])
    elif kind == 'multipart':
        require(set(body) == {'kind', 'fields', 'files'})
        fields = string_map(body['fields'])
        files = body['files']
        require(isinstance(files, list) and len(files) >= 1)
        require(len(files) == 1, 'BATCH_SPLIT_REQUIRED')
        boundary = 'hermes-' + secrets.token_hex(24)
        parts = []
        for name, text in fields.items():
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{part_name(name)}"'
                          f'\r\n\r\n{text}\r\n').encode())
        for file in files:
            require(isinstance(file, dict) and set(file) == {'name', 'filename', 'content_type', 'base64'})
            name, filename = part_name(file['name']), part_name(file['filename'])
            require('/' not in filename)
            media = content_type(file['content_type'])
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                          f'filename="{filename}"\r\nContent-Type: {media}\r\n\r\n').encode())
            parts.extend((decode_bytes(file['base64'], FILE_LIMIT), b'\r\n'))
        parts.append(f'--{boundary}--\r\n'.encode())
        raw, media = b''.join(parts), f'multipart/form-data; boundary={boundary}'
    else:
        raise ConsoleError('INVALID_INPUT')
    require(len(raw) <= RESULT_LIMIT, 'PAYLOAD_TOO_LARGE')
    require(value['method'] != 'GET' or not raw)
    return NativeRequest(value, raw, media)


# Regexes are anchored and cover the native v8.0.21 registration, not arbitrary HTTP.
ROUTES = (
    (r'/config', {'GET', 'PUT', 'PATCH'}, 'config'),
    (r'/config\.yaml', {'GET', 'PUT'}, 'config'),
    (r'/config/.+', {'GET', 'PUT', 'PATCH', 'DELETE'}, 'config'),
    (r'/credentials', {'GET', 'POST', 'DELETE'}, 'credentials'),
    (r'/credentials/(models|download)', {'GET'}, 'credentials'),
    (r'/credentials/(status|fields)', {'PATCH'}, 'credentials'),
    (r'/credentials/refresh', {'POST'}, 'credentials'),
    (r'/oauth/auth-url', {'GET'}, 'oauth'),
    (r'/oauth/status', {'GET'}, 'oauth'),
    (r'/oauth/session', {'DELETE'}, 'oauth'),
    (r'/oauth/callback', {'GET', 'POST'}, 'oauth'),
    (r'/oauth/import', {'POST'}, 'credentials'),
    (r'/requests/api-call', {'POST'}, 'egress'),
    (r'/routing/cooldown/reset', {'POST'}, 'credentials'),
    (r'/routing/model-definitions/[^/]+', {'GET'}, 'models'),
    (r'/observability/logs', {'GET', 'DELETE'}, 'logs'),
    (r'/observability/logs/errors', {'GET'}, 'logs'),
    (r'/observability/logs/(errors|requests)/[^/]+', {'GET'}, 'logs'),
    (r'/observability/usage/(api-keys|queue)', {'GET'}, 'usage'),
    (r'/server/latest-version', {'GET'}, 'server'),
    (r'/plugins', {'GET'}, 'plugins'),
    (r'/plugins/store', {'GET'}, 'plugins'),
    (r'/plugins/store/[^/]+/install', {'POST'}, 'plugins'),
    (r'/plugins/[^/]+', {'DELETE'}, 'plugins'),
    (r'/plugins/[^/]+/quota', {'GET', 'POST', 'DELETE'}, 'plugins'),
)


def classify(request, plugin_routes=()):
    method, path = request.method, request.path
    require(not unquote(path).startswith('/v0/resource/'), 'ROUTE_NOT_ALLOWED')
    if path.startswith('/v0/management/') and (method, path) in plugin_routes:
        return 'plugins', {'plugin_code', 'network_destination'}, None
    if method == 'GET' and path in ('/v1/models', '/v0/management/config'):
        return ('models' if path == '/v1/models' else 'config'), set(), None
    require(path.startswith(PREFIX + '/'), 'ROUTE_NOT_ALLOWED')
    relative = path[len(PREFIX):]
    group = next((group for pattern, methods, group in ROUTES
                  if method in methods and re.fullmatch(pattern, relative)), None)
    require(group is not None, 'ROUTE_NOT_ALLOWED')
    effects = set()
    if method == 'DELETE':
        effects.add('destructive_delete')
    if request.config_write:
        require(request.wire.get('expected_revision'), 'STALE_REVISION')
        effects.add('config_write')
    if relative == '/credentials/download':
        effects.add('credential_export')
    if relative == '/config.yaml' and method == 'GET':
        effects.add('config_export')
    if relative.startswith(('/observability/logs/errors/', '/observability/logs/requests/')):
        effects.add('log_export')
    if relative == '/oauth/auth-url':
        effects.add('oauth_start')
    if relative == '/oauth/import' or relative == '/credentials' and method == 'POST':
        effects.add('credential_import')
    if relative == '/credentials/refresh':
        effects.add('credential_refresh')
    if relative == '/credentials/fields':
        fields = request.wire.get('body', {}).get('value', {})
        if isinstance(fields, dict) and any('proxy' in str(k) or 'url' in str(k) or k == 'headers' for k in fields):
            effects.add('network_destination')
    if group == 'plugins' and (method != 'GET' or relative.endswith('/quota')):
        effects.add('plugin_code')
    if relative == '/plugins/store' or relative == '/server/latest-version':
        effects.add('network_destination')
    if group == 'egress':
        policy, extra = egress_policy(request)
        return group, effects | extra, policy
    return group, effects, None


def egress_policy(request):
    value = request.wire['body'].get('value')
    require(request.wire['body']['kind'] == 'json' and isinstance(value, dict))
    require(not set(value) - {'auth_index', 'authIndex', 'AuthIndex', 'proxy_url', 'method', 'url', 'header', 'data'})
    require(isinstance(value.get('method'), str))
    method = value['method'].upper()
    require(method in METHODS or method in ('HEAD', 'OPTIONS'))
    url = value.get('url')
    require(isinstance(url, str) and len(url) <= 8192)
    try:
        target = urlsplit(url)
        port = target.port
    except ValueError:
        raise ConsoleError('INVALID_INPUT') from None
    require(target.scheme in ('http', 'https') and target.hostname and not target.username
            and not target.password and not target.fragment)
    headers = string_map(value.get('header', {}))
    require(all(not any(c in v for c in '\r\n\0') for v in headers.values()))
    require(isinstance(value.get('data', ''), str))
    indices = [value[k] for k in ('auth_index', 'authIndex', 'AuthIndex') if k in value]
    require(all(isinstance(i, str) for i in indices) and len(set(indices)) <= 1)
    proxy = value.get('proxy_url', '')
    require(isinstance(proxy, str))
    policy = {'version': 1, 'destinations': [{'scheme': target.scheme, 'host': target.hostname,
              'port': port or (443 if target.scheme == 'https' else 80), 'path_prefix': '/'}],
              'methods': list(dict.fromkeys([method, 'GET'])), 'redirects': 'allowed', 'max_redirects': 5,
              'allow_token': True,
              'proxy': {'mode': 'configured'}}
    if indices:
        policy['auth_index'] = indices[0]
    if proxy.lower() in ('direct', 'none'):
        policy['proxy'] = {'mode': 'direct'}
    elif proxy:
        parsed_proxy = urlsplit(proxy)
        require(parsed_proxy.scheme in ('http', 'https', 'socks5', 'socks5h') and parsed_proxy.hostname)
        policy['proxy'] = {'mode': 'explicit', 'url': proxy}
    host_values = [v for k, v in headers.items() if k.lower() == 'host']
    if host_values:
        require(len(host_values) == 1)
        policy['host_header'] = host_values
    # Every external request is an exact approval. There is no host-based exemption
    # that can quietly turn a quota refresh into chargeable inference.
    effects = {'network_destination'}
    if proxy or host_values:
        effects.add('proxy_or_host_override')
    path = target.path.lower()
    if method != 'GET' and any(p in path for p in ('/chat/completions', '/responses', ':generatecontent', '/messages')):
        effects.add('paid_inference')
    if path.endswith('/rate-limit-reset-credits/consume'):
        effects.add('credit_consume')
    if path.endswith('/reset_rate_limits'):
        effects.add('entitlement_claim')
    if method not in ('GET', 'HEAD', 'OPTIONS'):
        effects.add('external_mutation')
    return policy, effects


def management_response(response, response_kind):
    headers = {k.lower(): str(v) for k, v in response.headers.items() if k.lower() in {
        'content-type', 'content-disposition', 'x-hermes-config-revision',
        'x-cpa-version', 'x-cpa-build-date', 'x-cpa-commit', 'x-cpa-support-plugin',
        'x-server-version', 'x-server-build-date', 'etag'}}
    if response_kind == 'bytes':
        body = {'kind': 'bytes', 'base64': base64.b64encode(response.body).decode(),
                'content_type': headers.get('content-type', 'application/octet-stream')}
    elif response_kind == 'json':
        try:
            value = json.loads(response.body)
        except (ValueError, UnicodeError):
            # Native non-JSON failures still retain their real HTTP status/body.
            body = {'kind': 'text', 'value': response.body.decode('utf-8', errors='replace')}
        else:
            body = {'kind': 'json', 'value': value}
    else:
        body = {'kind': 'text', 'value': response.body.decode('utf-8', errors='replace')}
    result = {'status': response.status, 'headers': headers, 'body': body}
    if 'x-hermes-config-revision' in headers:
        result['revision'] = headers['x-hermes-config-revision']
    require(len(encoded(result)) <= WIRE_LIMIT - 65536, 'RESPONSE_TOO_LARGE')
    return result
