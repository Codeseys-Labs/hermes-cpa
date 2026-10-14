"""Private control request targets: shared path/query bounds, exact query round trip."""
from urllib.parse import parse_qsl, quote, urlsplit

import pytest
from hermes_cpa.owner_transport import (PATH_LIMIT, QUERY_LIMIT, ControlChannel, TransportError,
                                        config_route, owner_route, request_target)

REVISION = '9f86d081884c7d65-42'  # Opaque native registry revision: nonce-counter.


def refusal(channel, method, path, headers):
    with pytest.raises(TransportError) as refused:
        channel.request(method, path, headers=headers, timeout=.1)
    return refused.value.code


def test_resource_revision_header_only_on_native_resource_get(tmp_path):
    # No live owned child: an accepted request fails UNAVAILABLE before any byte is sent.
    channel = ControlChannel(tmp_path, lambda: None)
    pin = {'X-Hermes-Resource-Revision': REVISION}
    for value in (REVISION, 'none-0'):
        assert refusal(channel, 'GET', '/v0/resource/plugins/generated/a//b',
                       {'X-Hermes-Resource-Revision': value}) == 'UNAVAILABLE'
    for method, path, headers in [
            ('GET', '/v0/resource/plugins/generated/a', None),
            ('GET', '/v0/resource/plugins/generated/a', {'Accept': 'text/html'}),
            ('GET', '/v0//resource/plugins/generated/a', {}),
            ('GET', '/v0/%72esource/plugins/generated/a', {}),
            ('POST', '/v0/resource/plugins/generated/a', pin),
            ('GET', '/v8/management/plugins', pin),
            ('GET', '/v0/resource/plugins-other/a', pin),
            ('GET', '/v0/resource/plugins/generated/a', {'X-Hermes-Resource-Revision': ''}),
            ('GET', '/v0/resource/plugins/generated/a', {'X-Hermes-Resource-Revision': 'a b'}),
            ('GET', '/v0/resource/plugins/generated/a', {'X-Hermes-Resource-Revision': 'a\r\nX: y'}),
            ('GET', '/v0/resource/plugins/generated/a', {'X-Hermes-Resource-Revision': 'r' * 257}),
            ('GET', '/v0/resource/plugins/generated/a', {'X-Hermes-Resource-Revision': 'a' * 64}),
            ('GET', '/v0/resource/plugins/generated/a',
             {'X-Hermes-Resource-Revision': '0123456789abcdef-0'}),
            ('GET', '/v0/resource/plugins/generated/a',
             {'X-Hermes-Resource-Revision': REVISION, 'x-hermes-resource-revision': REVISION}),
            ('GET', '/v0/resource/plugins/generated/a', {'Authorization': 'Bearer generated'})]:
        assert refusal(channel, method, path, headers) == 'INVALID_REQUEST', (method, path, headers)


def test_native_resource_paths_are_not_narrowed():
    # Native resource routes allow Unicode, literal '%' and inner duplicate slashes.
    exact = '/v0/resource/plugins/generated/a//b/día/100%/file.json'
    target = quote(exact, safe='/')
    assert request_target(target) == target
    for unsafe in ('//host/x', '/a/../b', '/a/./b', '/a/%2e%2e/b', '/a/%2Fb', '/a b', '/a?x', '/a#x'):
        with pytest.raises(TransportError) as refused:
            request_target(unsafe)
        assert refused.value.code == 'INVALID_REQUEST'


def test_extra_slashes_never_hide_gated_owner_or_config_routes():
    assert config_route('/v8/management//config/routing/strategy')
    assert config_route('/v8//management/config.yaml')
    assert owner_route('/hermes//config/commit')
    assert not config_route('/v0/resource/plugins/generated//config')


def test_multiline_credential_query_round_trips_with_duplicates():
    value = '{\n  "token": "' + 'x' * 20000 + '",\n  "nul": "\x00"\n}'
    pairs = [('op', 'save'), ('field', value), ('field', 'second & = ?#')]
    target = request_target('/v8/management/plugins/generated/resource', pairs)
    parts = urlsplit(target)
    assert parts.path == '/v8/management/plugins/generated/resource'
    assert '\n' not in target and '\x00' not in target and '#' not in target
    assert parse_qsl(parts.query, keep_blank_values=True) == pairs


def test_bounds_are_total_encoded_query_and_shared_path_limit():
    fits = 'v' * (QUERY_LIMIT - len('k='))
    assert len(urlsplit(request_target('/r', [('k', fits)])).query) == QUERY_LIMIT
    for oversized in ([('k', fits + 'v')], [('k', '\n' * (QUERY_LIMIT // 3))],
                      [('k', '\ud800')]):
        with pytest.raises(TransportError) as refused:
            request_target('/r', oversized)
        assert refused.value.code == 'INVALID_REQUEST'
    long_path = '/' + 'p' * (PATH_LIMIT - 1)
    assert request_target(long_path) == long_path
    with pytest.raises(TransportError):
        request_target(long_path + 'p')


def test_plugin_revision_header_is_separate_from_resource_authority(tmp_path):
    channel = ControlChannel(tmp_path, lambda: None)
    path = '/v0/management/generated/run'
    for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
        for revision in (REVISION, 'none-0'):
            assert refusal(channel, method, path,
                           {'X-Hermes-Plugin-Revision': revision}) == 'UNAVAILABLE'
    for target, headers in [
        ('/v8/management/plugins', {'X-Hermes-Plugin-Revision': REVISION}),
        ('/v0/resource/plugins/generated/a', {'X-Hermes-Plugin-Revision': REVISION}),
        (path, {'X-Hermes-Resource-Revision': REVISION}),
        (path, {'X-Hermes-Plugin-Revision': ''}),
        (path, {'X-Hermes-Plugin-Revision': '0123456789abcdef-0'}),
        (path, {'X-Hermes-Plugin-Revision': 'a' * 64}),
        (path, {'X-Hermes-Plugin-Revision': REVISION, 'x-hermes-plugin-revision': REVISION}),
        (path, {'X-Hermes-Plugin-Revision': REVISION, 'X-Hermes-Plugin-Invoked': '0'}),
    ]:
        assert refusal(channel, 'GET', target, headers) == 'INVALID_REQUEST'
    # The non-host/native control path does not require a plugin review header.
    assert refusal(channel, 'GET', '/v0/management/config', None) == 'UNAVAILABLE'


@pytest.mark.parametrize('status', [200, 404, 412, 502])
def test_duplicate_private_marker_headers_are_dropped_and_http_status_is_preserved(
        tmp_path, monkeypatch, status):
    import io
    from hermes_cpa import owner_transport as module

    class Socket:
        def __init__(self, reply):
            self.reply = reply

        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            pass

        def makefile(self, *args):
            return io.BytesIO(self.reply)

        def close(self):
            pass

    markers = ('X-Hermes-Plugin-Revision', 'X-Hermes-Plugin-Invoked',
               'X-Hermes-Resource-Revision', 'X-Hermes-Resource-Invoked',
               'X-Hermes-Config-Revision')
    fields = ''.join(f'{name}: first\r\n{name.lower()}: second\r\n' for name in markers)
    reply = (f'HTTP/1.1 {status} Generated\r\nContent-Length: 2\r\n'
             f'Connection: close\r\n{fields}\r\n{{}}').encode()
    monkeypatch.setattr(module, 'connect_private', lambda *args, **kwargs: Socket(reply))
    channel = ControlChannel(tmp_path, lambda: None)
    response = channel.request('POST', '/v0/management/generated/run',
                               headers={'X-Hermes-Plugin-Revision': REVISION})
    assert response.status == status and response.body == b'{}'
    assert all(name.lower() not in response.headers for name in markers)


def test_repeated_content_encoding_lines_join_in_wire_order_other_repeats_stay_last(
        tmp_path, monkeypatch):
    import io
    from hermes_cpa import owner_transport as module

    class Socket:
        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            pass

        def makefile(self, *args):
            return io.BytesIO(reply)

        def close(self):
            pass

    # Real http.client parsing of stacked codings emitted as separate header lines.
    reply = (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n'
             b'Content-Encoding: gzip\r\ncontent-encoding: br\r\n'
             b'X-Other: first\r\nx-other: second\r\n\r\n{}')
    monkeypatch.setattr(module, 'connect_private', lambda *args, **kwargs: Socket())
    channel = ControlChannel(tmp_path, lambda: None)
    response = channel.request('GET', '/v0/management/config')
    assert response.headers['content-encoding'] == 'gzip, br'
    assert response.headers['x-other'] == 'second'


@pytest.mark.parametrize('method,path', [
    ('GET', '/v8/management/plugins/generated/quota'),
    ('POST', '/v8/management/plugins/generated/quota'),
    ('DELETE', '/v8/management/plugins/generated/quota'),
    ('GET', '/v8/management/oauth/auth-url'),
])
def test_fixed_plugin_dispatch_surfaces_accept_private_revision(tmp_path, method, path):
    channel = ControlChannel(tmp_path, lambda: None)
    assert refusal(channel, method, path, {'X-Hermes-Plugin-Revision': REVISION}) == 'UNAVAILABLE'


@pytest.mark.parametrize('method,path', [
    ('PATCH', '/v8/management/plugins/generated/quota'),
    ('PUT', '/v8/management/plugins/generated/quota'),
    ('POST', '/v8/management/plugins/generated/quota/reset'),
    ('GET', '/v8/management/plugins/generated'),
    ('GET', '/v8/management/plugins/generated/quota/extra'),
    ('POST', '/v8/management/oauth/auth-url'),
    ('GET', '/v8/management/oauth/status'),
    ('POST', '/v8/management/oauth/callback'),
])
def test_plugin_revision_extension_does_not_authorize_other_v8_routes(tmp_path, method, path):
    channel = ControlChannel(tmp_path, lambda: None)
    assert refusal(channel, method, path, {'X-Hermes-Plugin-Revision': REVISION}) == 'INVALID_REQUEST'
