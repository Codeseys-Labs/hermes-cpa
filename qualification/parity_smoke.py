#!/usr/bin/env python3
"""Generated full-console qualification; run only in Dockerfile.oauth with --consent.

Requires a fresh prepared root and an explicitly issued permit. The provider fixture
never forwards traffic. No real accounts, API keys, entitlements or paid requests.
"""
import base64
import http.client
import json
import secrets
import time
from urllib.parse import urlsplit
import uuid

from console_client import ConsoleClient, q
from oauth_fixture import OAuthFixture
import provider_smoke as runner

require = runner.require
PREFIX = '/v8/management'


def request_wire(method, path, value=None, *, query=None, revision=None):
    result = {'method': method, 'path': PREFIX + path, 'response_kind': 'json',
              'body': {'kind': 'empty'} if value is None else {'kind': 'json', 'value': value}}
    if query is not None:
        result['query'] = query
    if revision is not None:
        result['expected_revision'] = revision
    return result


def refused(console, request, expected):
    status, response = console.http('/console/prepare', console.scoped(
        request_id=str(uuid.uuid4()), request=request))
    require(status == 409 and response.get('error', {}).get('code') == expected,
            'expected_' + expected.lower())


def exercise(root, host, report):
    cpa_port, broker_port = runner.free_ports()
    report['stage'] = 'native_setup'
    runner.native(host, 'setup', '--archive', str(root / q.ARCHIVE_NAME), '--consent',
        '--isolated-authorization', str(root / q.PERMIT), '--no-autostart', '--no-enroll',
        '--port', str(cpa_port), '--broker-port', str(broker_port))
    token_before = q.digest(q.read_file(root / q.TOKEN, private=True, limit=128))
    permit_before = q.read_file(root / q.PERMIT, private=True, limit=16384)
    with OAuthFixture() as provider, ConsoleClient(root, host) as console:
        def native(method, path, value=None, **kwargs):
            return console.json_result(console.request(method, PREFIX + path, value, **kwargs))

        def credentials():
            result = native('GET', '/credentials')
            require(isinstance(result.get('files'), list), 'credential_list_invalid')
            return result['files']

        report['stage'] = 'native_configuration'
        initial = console.request('GET', PREFIX + '/config')
        require(console.json_result(initial).get('config-version') == 8, 'config_v8_required')
        console.config('requests/proxy-url', provider.url)
        refused(console, request_wire('PUT', '/config/requests/proxy-url', 'direct',
                                      revision=initial['revision']), 'STALE_REVISION')
        require(native('GET', '/config/requests/proxy-url') == provider.url,
                'stale_write_changed_proxy')
        public_key = 'generated-public-' + secrets.token_urlsafe(32)
        console.config('access/api-keys', [public_key])
        require(native('GET', '/config/access/api-keys') == [public_key], 'public_key_roundtrip_failed')
        console.config('management/secret-key', '')
        # A non-127.0.0.1 public bind proves neither management nor browser callback
        # completion secretly depends on the editable public CPA listener.
        console.config('server/host', '127.0.0.2')
        require(native('GET', '/config/server/host') == '127.0.0.2', 'public_bind_roundtrip_failed')
        require(native('GET', '/config/management/secret-key') == '', 'public_management_not_disabled')
        report.update(native_snapshot_cas=True, public_key_editable=True,
                      private_management_independent=True)

        report['stage'] = 'oauth_callback_ownership'
        first = native('GET', '/oauth/auth-url', query={'provider': 'claude', 'is_webui': 'true'})
        callback = provider.authorize(first['url'])
        state = first['state']
        require(native('GET', '/oauth/status', query={'state': state})['status'] == 'wait',
                'oauth_pending_state_missing')
        refused(console, request_wire('POST', '/oauth/callback', {
            'provider': 'claude', 'redirect_url': callback.replace(state, secrets.token_urlsafe(24))}),
            'OAUTH_STATE_MISMATCH')
        refused(console, request_wire('POST', '/oauth/callback', {
            'provider': 'codex', 'redirect_url': callback}), 'OAUTH_PROVIDER_MISMATCH')
        require(provider.exchanges == 0, 'invalid_callback_exchanged_credentials')
        parsed = urlsplit(callback)
        connection = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=10)
        try:
            connection.request('GET', parsed.path + '?' + parsed.query)
            response = connection.getresponse()
            response.read(65536)
            require(200 <= response.status < 300, 'callback_depends_on_public_listener')
        finally:
            connection.close()
        deadline = time.monotonic() + 30
        while True:
            status = native('GET', '/oauth/status', query={'state': state})['status']
            if status != 'wait':
                break
            require(time.monotonic() < deadline, 'oauth_completion_timed_out')
            time.sleep(.1)
        require(status == 'ok' and provider.exchanges == 1, 'oauth_exchange_failed')
        refused(console, request_wire('POST', '/oauth/callback', {
            'provider': 'claude', 'redirect_url': callback}), 'OAUTH_STATE_MISMATCH')
        matches = [entry for entry in credentials() if entry.get('email') == provider.email]
        require(len(matches) == 1 and isinstance(matches[0].get('name'), str), 'oauth_credential_missing')
        name = matches[0]['name']
        report.update(real_pkce_exchange=True, wrong_state_refused=True,
                      wrong_provider_refused=True, consumed_state_refused=True,
                      fixed_loopback_callback_independent=True)

        report['stage'] = 'credential_lifecycle'
        native('PATCH', '/credentials/status', {'name': name, 'disabled': True})
        require(next(entry for entry in credentials() if entry.get('name') == name)['disabled'] is True,
                'credential_disable_failed')
        native('PATCH', '/credentials/status', {'name': name, 'disabled': False})
        native('PATCH', '/credentials/fields', {'name': name, 'prefix': 'qualification'})
        edited = native('GET', '/credentials/download', query={'name': name})
        require(edited.get('prefix') == 'qualification', 'credential_fields_failed')
        before_refresh = provider.refreshes
        native('POST', '/credentials/refresh', {'name': name})
        report['refresh_observation'] = {'before': before_refresh, 'after': provider.refreshes}
        require(provider.refreshes == 1, 'native_refresh_not_exercised')
        downloaded = console.request('GET', PREFIX + '/credentials/download',
                                     query={'name': name}, response_kind='bytes')
        require(downloaded['status'] == 200 and downloaded['body']['kind'] == 'bytes',
                'credential_export_failed')
        raw = base64.b64decode(downloaded['body']['base64'], validate=True)
        credential = json.loads(raw)
        require(credential.get('access_token') in provider.access and
                credential.get('refresh_token') in provider.refresh, 'exported_tokens_not_generated')
        native('DELETE', '/credentials', {'names': [name]})
        require(not any(entry.get('name') == name for entry in credentials()), 'credential_delete_failed')
        uploaded = console.request('POST', PREFIX + '/credentials', body={'kind': 'multipart',
            'fields': {}, 'files': [{'name': 'files', 'filename': name,
                'content_type': 'application/json', 'base64': base64.b64encode(raw).decode()}]})
        require(200 <= uploaded['status'] < 300 and any(entry.get('name') == name for entry in credentials()),
                'credential_import_failed')
        report.update(credential_disable_enable=True, credential_fields=True,
                      native_refresh=True, credential_export_import_delete=True)

        report['stage'] = 'oauth_cancel_and_pasted_callback'
        pending = native('GET', '/oauth/auth-url', query={'provider': 'claude', 'is_webui': 'true'})
        pending_callback = provider.authorize(pending['url'])
        cancelled = native('DELETE', '/oauth/session', query={'state': pending['state']})
        require(cancelled.get('cancelled') is True, 'oauth_cancel_failed')
        refused(console, request_wire('POST', '/oauth/callback', {
            'provider': 'claude', 'redirect_url': pending_callback}), 'OAUTH_STATE_MISMATCH')
        second = native('GET', '/oauth/auth-url', query={'provider': 'claude', 'is_webui': 'true'})
        pasted = provider.authorize(second['url'])
        native('POST', '/oauth/callback', {'provider': 'claude', 'redirect_url': pasted})
        deadline = time.monotonic() + 30
        while True:
            status = native('GET', '/oauth/status', query={'state': second['state']})['status']
            if status != 'wait':
                break
            require(time.monotonic() < deadline, 'pasted_callback_timed_out')
            time.sleep(.1)
        require(status == 'ok' and provider.exchanges == 2, 'pasted_callback_failed')
        report.update(oauth_cancel=True, cancelled_callback_refused=True, pasted_callback=True)

        report['stage'] = 'large_credential_upload'
        large = dict(credential, email='large-' + secrets.token_hex(8) + '@example.invalid', qualification_padding='')
        serialized = json.dumps(large, separators=(',', ':')).encode()
        large['qualification_padding'] = 'x' * (10 * 1024 * 1024 - len(serialized))
        serialized = json.dumps(large, separators=(',', ':')).encode()
        require(len(serialized) == 10 * 1024 * 1024, 'large_fixture_size_invalid')
        large_name = 'qualification-large.json'
        response = console.request('POST', PREFIX + '/credentials', body={'kind': 'multipart',
            'fields': {}, 'files': [{'name': 'files', 'filename': large_name,
                'content_type': 'application/json', 'base64': base64.b64encode(serialized).decode()}]})
        require(200 <= response['status'] < 300 and any(entry.get('name') == large_name for entry in credentials()),
                'large_native_upload_failed')
        native('DELETE', '/credentials', {'names': [large_name]})
        require(not any(entry.get('name') == large_name for entry in credentials()), 'large_cleanup_failed')
        report['native_ten_mib_upload'] = True

    require(q.digest(q.read_file(root / q.TOKEN, private=True, limit=128)) == token_before,
            'session_token_changed')
    require(q.read_file(root / q.PERMIT, private=True, limit=16384) == permit_before,
            'qualification_permit_changed')
    report.update(session_token_preserved=True, permit_not_renewed=True, fixture_stopped=True)


def child_main(root, host):
    runner.child_main(root, host, exercise_fn=exercise)


if __name__ == '__main__':
    raise SystemExit(runner.main(child_module='parity_smoke', marker='.parity-smoke-used', child_timeout=600))
