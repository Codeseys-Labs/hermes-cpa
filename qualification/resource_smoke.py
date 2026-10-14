#!/usr/bin/env python3
"""Real native plugin-resource qualification using a generated Go plugin.

Run only in the Dockerfile.smoke image (qualification/native-resource-fixture/),
inside its network_mode: none namespace, as the nonroot qualification user, with
--consent, against a NEW prepared root plus an explicit permit:

  python /opt/q/scripts/qualification.py prepare --root /work/resource-check
  python /opt/q/scripts/qualification.py permit --root /work/resource-check --hours 1
  python /opt/q/qualification/resource_smoke.py --root /work/resource-check --consent

The caller issues the permit; this script never issues or renews it, refuses any
root beyond prepare + permit (including earlier failed runs) and never touches
another root. Nothing here mocks the native runtime, the owner console service or
its grants:

* the pinned patched CPA owner build installs a generated, reproducibly built
  native Go plugin through its own plugin-store API, from a loopback registry;
* every management request, config transaction, review, approval, resource grant
  and resource fetch goes through the real console routes and the real owner;
* effects are observed out of band: the plugin appends each counted GET effect to
  a JSONL file inside the owned root, so what the owner reports is compared with
  what the native handler did.

stdout is one JSON report of fixed check names and booleans. Child output,
exception text, headers, tokens, grant ids and paths are never reported. All
generated data only: no account, real credential, public network or paid request.
Cleanup is the runner's bounded child process-group termination followed by the
credentialled, root-guarded owner teardown (q.quiesce); nothing else is signalled.
"""
import base64
import gzip
import json
from pathlib import Path
import struct
import sys
import time
from types import SimpleNamespace

from console_client import ConsoleClient, q
import provider_smoke as runner

sys.path.insert(0, str(Path(__file__).resolve().parent / 'native-resource-fixture'))
import fixture_common as fx  # noqa: E402
import fixture_install as native_fixture  # noqa: E402

require = runner.require
BASE = fx.BASE
RESULT_LIMIT = 32 * 1024 * 1024
HOSTILE = ('set-cookie', 'refresh', 'www-authenticate', 'content-security-policy', 'x-frame-options',
           'access-control-allow-origin', 'access-control-allow-credentials',
           'cross-origin-resource-policy')


class Resources:
    """The console resource routes exactly as a host presents them."""

    def __init__(self, console, hide):
        self.console = console
        self.hide = hide

    def review(self):
        review = self.console.call('plugin', self.console.scoped(plugin_id=fx.PLUGIN_ID, menu_index=0))
        summary = review.get('summary', '')
        require(review.get('effects') == ['plugin_code'] and review.get('requires_confirmation') is True,
                'resource_review_effects_unexpected')
        require('GET is not read-only' in summary and 'access or change credentials' in summary and
                'No console, management or inference authority' in summary, 'resource_review_warning_missing')
        return review

    def approve(self, review):
        grant = self.console.call('plugin', self.console.scoped(
            review_id=review['review_id'], approved_effects=review['effects']))
        remaining = grant.get('expires_at', 0) - time.time()
        require(grant.get('kind') == 'native-resource' and isinstance(grant.get('grant_id'), str) and
                grant.get('plugin_id') == fx.PLUGIN_ID and grant.get('entry_path') == fx.ENTRY and
                'lease_id' not in grant and grant['grant_id'] != self.console.lease and
                0 < remaining <= 301, 'resource_grant_invalid')
        return SimpleNamespace(id=grant['grant_id'], document=self.console.document)

    def open(self):
        return self.approve(self.review())

    def fetch(self, grant, path, query=(), *, document=None, accept=None, extra=None):
        payload = {'document_id': document or grant.document, 'grant_id': grant.id, 'path': path,
                   'query': [list(pair) for pair in query]}
        if accept is not None:
            payload['accept'] = accept
        payload.update(extra or {})
        status, envelope = self.console.http('/console/resource', payload)
        if status == 200:
            data = envelope.get('data') if isinstance(envelope, dict) else None
            require(isinstance(data, dict) and data.get('ok') is True and type(data.get('status')) is int and
                    isinstance(data.get('headers'), dict), 'resource_response_invalid')
            text = json.dumps(envelope)
            require(not any(secret in text for secret in self.hide + [self.console.lease or '\0']),
                    'resource_response_leaked_authority')
            return SimpleNamespace(ok=True, status=data['status'], headers=data['headers'],
                                   body=base64.b64decode(data['body_base64'], validate=True))
        error = envelope.get('error') if isinstance(envelope, dict) else None
        require(status in (409, 422) and isinstance(error, dict) and isinstance(error.get('code'), str),
                'resource_error_invalid')
        return SimpleNamespace(ok=False, code=error['code'], invoked=error.get('effect_invoked'),
                               withheld=error.get('response_withheld'))

    def json(self, grant, path, query=(), **kwargs):
        response = self.fetch(grant, path, query, **kwargs)
        require(response.ok and response.status == 200, 'resource_fetch_failed')
        return json.loads(response.body)

    def state(self, grant):
        return self.json(grant, BASE + '/api/state')

    def close(self, grant):
        status, envelope = self.console.http('/console/resource/close', {
            'document_id': grant.document, 'grant_id': grant.id})
        require(status == 200 and envelope['data'].get('closed') is True, 'resource_close_failed')


def never_invoked(response, code):
    require(not response.ok and response.code == code and response.invoked in (False, None) and
            not response.withheld, 'expected_' + code.lower())


def entry_html_ok(body):
    html = body.decode()
    for needle in ('<link rel="stylesheet" href="style.css">', '<script src="app.js" defer>',
                   '<script src="gzip-asset.js" defer>', '<script type="module" src="' + BASE + '/module.js">',
                   '<img id="probe-img" src="logo.png"', 'data-plugin-id="' + fx.PLUGIN_ID + '"',
                   'data-base="' + BASE + '"', 'id="effect-button"', 'id="outside-form"'):
        require(needle in html, 'entry_page_markup_missing')
    require('__BASE__' not in html and '__GENERATION__' not in html, 'entry_page_untemplated')


def poll_revoked(res, grant, code='RESOURCE_REVOKED'):
    """Wait for the native registry reload to invalidate GRANT without a counted effect."""
    deadline = time.monotonic() + 60
    while True:
        response = res.fetch(grant, BASE + '/api/state')
        if not response.ok:
            never_invoked(response, code)
            return
        require(time.monotonic() < deadline, 'registry_replacement_not_observed')
        time.sleep(.2)


def scenarios(root, console, registry, fixture, report):
    instances = root / '.hermes/cpa/instances/default'
    log_path = native_fixture.effect_log_path(root)
    token = q.read_file(root / q.TOKEN, private=True, limit=128).decode().strip()
    res = Resources(console, [token])
    one, two = fx.VARIANTS
    expected = 0

    def settle(grant, total, generation='1'):
        state = res.state(grant)
        require(state['effects'] == total and
                len(native_fixture.effects(log_path, generation)) == total, 'effect_count_mismatch')
        return state

    report['stage'] = 'native_readiness'
    initial = console.request('GET', '/v8/management/config')
    require(initial['status'] == 200 and console.json_result(initial).get('config-version') == 8,
            'pinned_cpa_not_ready')

    report['stage'] = 'native_plugin_install'
    entry = native_fixture.install_fixture(console, registry, log_path, one[0])
    require(registry.registry_requests >= 1 and registry.artifact_requests == 1, 'plugin_store_not_exercised')
    installed = Path(entry['path'])
    installed = installed if installed.is_absolute() else instances / installed
    require(installed.resolve().is_relative_to(root.resolve()) and
            fx.sha256(installed.read_bytes()) == fixture['variants'][one[0]]['sha256'],
            'installed_plugin_mismatch')
    report.update(native_store_install=True, installed_bytes_match=True, native_menu_registered=True)

    report['stage'] = 'resource_review'
    review = res.review()
    for approved in ([], ['plugin_code', 'credential_export']):
        status, envelope = console.http('/console/plugin', console.scoped(
            review_id=review['review_id'], approved_effects=approved))
        require(status == 409 and envelope['error']['code'] == 'CONSENT_REQUIRED', 'exact_consent_not_enforced')
    grant = res.approve(review)
    status, envelope = console.http('/console/plugin', console.scoped(
        review_id=review['review_id'], approved_effects=review['effects']))
    require(status == 409, 'review_replay_not_refused')
    report.update(review_discloses_native_effects=True, exact_consent_required=True,
                  review_single_use=True, grant_has_no_lease=True)

    report['stage'] = 'entry_and_assets'
    state = res.state(grant)
    require(state['effects'] == 0 and state['generation'] == one[1] and state['version'] == one[0] and
            state['registrations'] >= 1 and state['log_path'] == str(log_path) and
            native_fixture.effects(log_path) == [], 'fixture_initial_state_invalid')
    panel = res.fetch(grant, fx.ENTRY)
    require(panel.ok and panel.status == 200 and panel.headers.get('content-type', '').startswith('text/html'),
            'entry_page_unavailable')
    require(not any(name in panel.headers for name in HOSTILE) and
            not any(name.startswith('x-hermes-') for name in panel.headers) and
            panel.headers.get('cache-control') == 'no-store', 'entry_headers_not_sanitized')
    entry_html_ok(panel.body)
    assets = {'/app.js': 'application/javascript', '/module.js': 'application/javascript',
              '/static-dep.js': 'application/javascript', '/dynamic.js': 'application/javascript',
              '/style.css': 'text/css', '/sw.js': 'application/javascript'}
    for path, media in assets.items():
        response = res.fetch(grant, BASE + path)
        require(response.ok and response.status == 200 and
                response.headers.get('content-type', '').startswith(media) and
                b'__BASE__' not in response.body and b'__GENERATION__' not in response.body, 'asset_delivery_failed')
    require(b"import('" + BASE.encode() + b"/dynamic.js')" in res.fetch(grant, BASE + '/module.js').body,
            'module_root_relative_import_missing')
    for path, marker in (('/gzip-asset.js', b'gzip-script-ok-g1'), ('/gzip-module.js', b'gzip-module-ok-g1'),
                         ('/api/gzip', b'"gzip":true')):
        response = res.fetch(grant, BASE + path)
        require(response.ok and response.headers.get('content-encoding') == 'gzip' and
                response.body[:2] == b'\x1f\x8b' and marker in gzip.decompress(response.body),
                'gzip_resource_not_genuine')
    logo = res.fetch(grant, BASE + '/logo.png')
    require(logo.ok and logo.headers.get('content-type') == 'image/png' and
            logo.body[:8] == b'\x89PNG\r\n\x1a\n' and struct.unpack('>II', logo.body[16:24]) == (16, 16),
            'image_delivery_failed')
    binary = res.fetch(grant, BASE + '/api/binary')
    require(binary.ok and binary.body == bytes(range(256)) * 4 and
            'attachment' in binary.headers.get('content-disposition', '') and
            binary.headers.get('etag') == '"fixture-binary"' and binary.headers.get('content-language') == 'en',
            'binary_delivery_failed')
    state = settle(grant, 0)
    fetched = ['/panel', '/app.js', '/module.js', '/static-dep.js', '/dynamic.js', '/style.css', '/sw.js',
               '/gzip-asset.js', '/gzip-module.js', '/api/gzip', '/logo.png', '/api/binary']
    expected_hits = {path: 1 for path in fetched}
    expected_hits['/module.js'] = 2  # fetched twice above
    require({path: state['hits'].get(path) for path in fetched} == expected_hits, 'native_hit_count_mismatch')
    report.update(entry_page=True, relative_assets=True, module_assets=True, gzip_native_bytes=True,
                  binary_exact=True, native_each_get_executed_once=True, response_headers_sanitized=True)

    report['stage'] = 'counter_once'
    response = res.json(grant, BASE + '/api/effect', [('source', 'smoke')])
    expected += 1
    require(response['ok'] is True and response['effect_count'] == 1 and response['generation'] == one[1],
            'counter_response_mismatch')
    state = settle(grant, expected)
    events = native_fixture.effects(log_path)
    require(len(events) == 1 and events[0]['seq'] == 1 and events[0]['route'] == '/api/effect' and
            state['hits']['/api/effect'] == 1, 'counter_not_exactly_once')
    report['counter_executes_once'] = True

    report['stage'] = 'queries_and_headers'
    pairs = [('a', '1'), ('a', '2'), ('b', '%'), ('c', 'x y'), ('d', '\u00e9'), ('e', 'a&b=c'), ('f', '+'),
             ('g', ';'), ('h', '\n'), ('', 'empty-key'), ('dup', 'z'), ('dup', 'y'), ('dup', '')]
    grouped = {}
    for name, value in pairs:
        grouped.setdefault(name, []).append(value)
    require(res.json(grant, BASE + '/api/query', pairs)['query'] == grouped, 'duplicate_encoded_query_mismatch')
    headers = res.json(grant, BASE + '/api/headers', accept='application/json')
    require(headers['forbidden_present'] == [] and headers['accept'] == 'application/json' and
            'accept' in headers['names'] and 'authorization' not in headers['names'] and
            not any(name.startswith('x-hermes-') for name in headers['names']), 'plugin_saw_forbidden_headers')
    forged = res.fetch(grant, BASE + '/api/forge')
    expected += 1
    require(forged.ok and forged.status == 200 and
            not any(name in forged.headers for name in HOSTILE) and
            not any(name.startswith('x-hermes-') for name in forged.headers) and
            forged.headers.get('content-disposition') == 'inline; filename="forged.json"' and
            forged.headers.get('etag') == '"fixture-forge"', 'forged_response_headers_not_dropped')
    # The plugin forged X-Hermes-Resource-Invoked: 0; a host that honoured it
    # would have reported the handler as not invoked and revoked the grant.
    settle(grant, expected)
    report.update(duplicate_encoded_queries=True, request_headers_sanitized=True,
                  forged_response_headers_dropped=True, forged_invoked_marker_ignored=True)

    report['stage'] = 'redirects'
    for path, hop in (('/redirect/start', '1'), ('/redirect/absolute', '2'), ('/redirect/dotdot', '3')):
        redirect = res.fetch(grant, BASE + path)
        require(redirect.ok and redirect.status == 302 and
                redirect.headers.get('location') == BASE + '/redirect/target?hop=' + hop +
                ('&dup=a&dup=b' if hop == '1' else ''), 'exact_route_redirect_mismatch')
    target = res.json(grant, BASE + '/redirect/target', [('hop', '1'), ('dup', 'a'), ('dup', 'b')])
    require(target['target'] is True and target['query'] == {'hop': ['1'], 'dup': ['a', 'b']}, 'redirect_target_mismatch')
    for path in ('/redirect/outside', '/redirect/protocol-relative', '/redirect/unregistered'):
        denied = res.fetch(grant, BASE + path)
        require(not denied.ok and denied.code == 'REDIRECT_DENIED' and denied.invoked is True and
                denied.withheld is True, 'redirect_denial_untruthful')
    effect_redirect = res.fetch(grant, BASE + '/redirect/effect-start')
    require(effect_redirect.ok and effect_redirect.status == 302 and
            effect_redirect.headers.get('location') == BASE + '/redirect/target?hop=effect', 'redirect_effect_failed')
    expected += 1
    state = settle(grant, expected)
    require(all(state['hits'].get(path) == 1 for path in
                ('/redirect/outside', '/redirect/protocol-relative', '/redirect/unregistered')),
            'redirect_denied_handler_replayed')
    report.update(redirects_between_exact_routes=True, redirect_denials_report_effect=True)

    report['stage'] = 'native_failures'
    for path, status in (('/api/fail', 500), ('/api/missing', 404)):
        failure = res.fetch(grant, BASE + path)
        expected += 1
        require(failure.ok and failure.status == status, 'native_status_not_reported')
        settle(grant, expected)
    plugin_error = res.fetch(grant, BASE + '/api/error')
    expected += 1
    require(plugin_error.ok and plugin_error.status == 502, 'native_plugin_error_not_reported')
    settle(grant, expected)
    # Success with a megabyte of binary data, then the owner's 32 MiB cap.
    big = res.fetch(grant, BASE + '/api/large', [('bytes', str(1024 * 1024))])
    expected += 1
    require(big.ok and big.body == bytes(index % 251 for index in range(1024 * 1024)), 'large_binary_mismatch')
    settle(grant, expected)
    before = len(native_fixture.effects(log_path))
    slow = res.fetch(grant, BASE + '/api/slow', [('ms', '3500')])
    expected += 1
    report['slow_observation'] = {key: getattr(slow, key, None)
                                  for key in ('ok', 'status', 'code', 'invoked', 'withheld')}
    require(not slow.ok and slow.code == 'OUTCOME_UNKNOWN' and slow.invoked is True and slow.withheld is False,
            'slow_outcome_untruthful')
    require(len(native_fixture.effects(log_path)) == before + 1, 'slow_effect_not_counted_once')
    deadline = time.monotonic() + 20
    while not any(event.get('event') == 'finished' and event.get('route') == '/api/slow'
                  for event in native_fixture.read_log(log_path)):
        require(time.monotonic() < deadline, 'slow_handler_never_finished')
        time.sleep(.1)
    settle(grant, expected)
    oversize = res.fetch(grant, BASE + '/api/large', [('bytes', str(RESULT_LIMIT + 1))])
    expected += 1
    require(not oversize.ok and oversize.code == 'RESPONSE_TOO_LARGE' and oversize.invoked is True and
            oversize.withheld is True, 'oversize_outcome_untruthful')
    settle(grant, expected)
    report.update(native_http_failures_reported=True, plugin_error_reported=True,
                  uncertain_outcome_reports_effect=True, withheld_response_reports_effect=True,
                  no_automatic_replay=True)

    report['stage'] = 'denied_requests'
    before = len(native_fixture.read_log(log_path))
    for path in ('/v0/resource/plugins/other-plugin/panel', BASE + '/redirect/../api/effect',
                 BASE + '/not-registered', '/v8/management/config', '/hermes/health'):
        never_invoked(res.fetch(grant, path), 'RESOURCE_PATH_DENIED')
    # The body-only grant refuses any caller supplied core authority.
    never_invoked(res.fetch(grant, BASE + '/api/effect', extra={'lease_id': console.lease}), 'INVALID_INPUT')
    never_invoked(res.fetch(grant, BASE + '/api/effect', [('k', 'v')] * 257), 'INVALID_INPUT')
    require(len(native_fixture.read_log(log_path)) == before, 'denied_request_invoked_native_code')
    settle(grant, expected)
    report.update(exact_routes_only=True, caller_authority_refused=True)

    report['stage'] = 'revocation_close'
    closing = res.open()
    require(res.state(closing)['effects'] == expected, 'closing_grant_unusable')
    res.close(closing)
    res.close(closing)  # idempotent
    log_before = len(native_fixture.read_log(log_path))
    never_invoked(res.fetch(closing, BASE + '/api/effect'), 'RESOURCE_GONE')
    require(len(native_fixture.read_log(log_path)) == log_before, 'closed_grant_invoked_native_code')
    settle(grant, expected)
    report['close_revokes_grant'] = True

    report['stage'] = 'registry_replacement_reconfigure'
    lease, document = console.lease, console.document
    before_state = res.state(grant)
    console.config('plugins/configs/' + fx.PLUGIN_ID + '/note', 'replacement-reconfigure')
    require((console.lease, console.document) == (lease, document), 'reconfigure_changed_console_authority')
    poll_revoked(res, grant)
    never_invoked(res.fetch(grant, BASE + '/api/effect'), 'RESOURCE_GONE')
    grant = res.open()
    after_state = res.state(grant)
    require(after_state['instance_nonce'] == before_state['instance_nonce'] and
            after_state['registrations'] > before_state['registrations'] and
            after_state['config_sha256'] != before_state['config_sha256'] and
            after_state['effects'] == expected == len(native_fixture.effects(log_path)),
            'reconfigure_state_mismatch')
    report['registry_incarnation_revokes_grant'] = True

    report['stage'] = 'revocation_lock'
    first, second = res.open(), res.open()
    res.state(first)
    console.call('lock', console.scoped())
    log_before = len(native_fixture.read_log(log_path))
    for revoked in (first, second, grant):
        never_invoked(res.fetch(revoked, BASE + '/api/effect'), 'RESOURCE_GONE')
    require(len(native_fixture.read_log(log_path)) == log_before, 'locked_grant_invoked_native_code')
    console.unlock()
    grant = res.open()
    settle(grant, expected)
    report['lock_revokes_grants'] = True

    report['stage'] = 'revocation_new_document'
    stale, old_document = res.open(), console.document
    res.state(stale)
    console.unlock()
    log_before = len(native_fixture.read_log(log_path))
    report['document_denials'] = []
    for document in (console.document, old_document):
        denied = res.fetch(stale, BASE + '/api/effect', document=document)
        report['document_denials'].append({
            'code': getattr(denied, 'code', None), 'invoked': getattr(denied, 'invoked', None),
            'withheld': getattr(denied, 'withheld', None)})
        require(not denied.ok and denied.code in
                ('RESOURCE_GONE', 'RESOURCE_REVOKED', 'STALE_SCOPE', 'LEASE_EXPIRED') and
                denied.invoked is False and denied.withheld is False, 'new_document_retirement_failed')
    require(len(native_fixture.read_log(log_path)) == log_before, 'stale_document_invoked_native_code')
    grant = res.open()
    settle(grant, expected)
    report['new_document_revokes_grants'] = True

    report['stage'] = 'registry_replacement_native_code'
    lease, document, old_nonce = console.lease, console.document, res.state(grant)['instance_nonce']
    native_fixture.install_version(console, registry, two[0])
    require(registry.artifact_requests == 2 and (console.lease, console.document) == (lease, document),
            'second_install_not_exercised')
    poll_revoked(res, grant)
    entry = native_fixture.wait_registered(console, two[0])
    replaced = Path(entry['path'])
    replaced = replaced if replaced.is_absolute() else instances / replaced
    require(replaced.resolve().is_relative_to(root.resolve()) and
            fx.sha256(replaced.read_bytes()) == fixture['variants'][two[0]]['sha256'], 'replacement_plugin_mismatch')
    grant = res.open()
    state = res.state(grant)
    require(state['generation'] == two[1] and state['version'] == two[0] and state['effects'] == 0 and
            state['instance_nonce'] != old_nonce and state['log_path'] == str(log_path),
            'replacement_code_not_running')
    response = res.json(grant, BASE + '/api/effect', [('source', 'replacement')])
    require(response['effect_count'] == 1 and response['generation'] == two[1], 'replacement_counter_mismatch')
    require(len(native_fixture.effects(log_path, two[1])) == 1 and
            len(native_fixture.effects(log_path, one[1])) == expected, 'replacement_effect_log_mismatch')
    report['native_code_replacement_revokes_grant'] = True
    res.close(grant)


def exercise(root, host, report):
    report['stage'] = 'fixture_artifacts'
    try:
        fixture = fx.load_manifest()
    except fx.FixtureError as exc:
        raise runner.SmokeFailure(str(exc)) from None
    cpa_port, broker_port = runner.free_ports()
    report['stage'] = 'native_setup'
    runner.native(host, 'setup', '--archive', str(root / q.ARCHIVE_NAME), '--consent',
                  '--isolated-authorization', str(root / q.PERMIT), '--no-autostart', '--no-enroll',
                  '--port', str(cpa_port), '--broker-port', str(broker_port))
    token_before = q.digest(q.read_file(root / q.TOKEN, private=True, limit=128))
    permit_before = q.read_file(root / q.PERMIT, private=True, limit=16384)
    try:
        with native_fixture.PluginRegistry(fixture) as registry, ConsoleClient(root, host) as console:
            scenarios(root, console, registry, fixture, report)
    except (fx.FixtureError, q.Refused) as exc:
        raise runner.SmokeFailure(str(exc)) from None
    require(q.digest(q.read_file(root / q.TOKEN, private=True, limit=128)) == token_before,
            'session_token_changed')
    require(q.read_file(root / q.PERMIT, private=True, limit=16384) == permit_before,
            'qualification_permit_changed')
    report.update(session_token_preserved=True, permit_not_renewed=True, fixture_stopped=True)


def child_main(root, host):
    runner.child_main(root, host, exercise_fn=exercise)


if __name__ == '__main__':
    raise SystemExit(runner.main(child_module='resource_smoke', marker='.resource-smoke-used',
                                 child_timeout=900))
