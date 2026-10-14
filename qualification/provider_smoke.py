#!/usr/bin/env python3
"""Real registered-provider qualification using generated loopback data only.

Run as the qualification image's nonroot user, in its network_mode: none
namespace, with the patched host/provenance and pinned archive already built.
Use a NEW root, not the compose backend's /work/q or a previously used root:

  python /opt/q/scripts/qualification.py prepare --root /work/provider-check
  python /opt/q/scripts/qualification.py permit --root /work/provider-check --hours 1
  python /opt/q/qualification/provider_smoke.py --root /work/provider-check --consent

Use docker compose exec -T keepdev for these commands, not docker compose run.
The caller issues the permit explicitly; this script never issues or renews it.
It refuses any state beyond prepare + permit, including previous failed runs.
--host defaults to /opt/q/host in the image. The invoking Python must be the
image's Hermes venv interpreter. No download, account, OAuth or paid inference.

stdout is one JSON report with fixed checks, counts and generated model IDs.
Child output, exception messages, headers, keys, config and tokens are never
reported. Native setup creates disposable credentials inside the owned root.
All inference goes through registered custom:cpa, its real key_cmd, the broker,
pinned CPA and the generated HTTP upstream. The generated fallback is resolved
but never invoked. Unrelated metadata catalogs are disabled/unavailable locally.
The public one-second cache TTL is used only for this proof; production TTL and
enrollment snapshots are not edited. CPA provider inventory changes use its
native management API. Retirement is proved independently from live grants for
disable and remove, restoring only the generated config between those actions.
Finally, bounded child process-group cleanup precedes owned-root quiescence.
The root is retained for diagnosis; inspect it as secret-bearing private state.
"""
import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import http.server
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import qualification as q
from console_client import ConsoleClient, ConsoleFailure


class SmokeFailure(Exception):
    """Only fixed, locally authored codes may be supplied."""


def require(condition, code):
    if not condition:
        raise SmokeFailure(code)


def loopback_namespace():
    # Fail closed outside a network-none namespace, including ordinary host WSL.
    # This also contains network activity from the native Go executable.
    require(sys.platform == 'linux' and
            {entry.name for entry in Path('/sys/class/net').iterdir()} == {'lo'},
            'network_none_namespace_required')


def fresh_root(root):
    expected = {q.MARKER, q.TOKEN, q.PERMIT, q.ARCHIVE_NAME, '.qualification.lock', '.hermes'}
    require({p.name for p in root.iterdir()} == expected, 'fresh_prepared_root_required')
    home = root / '.hermes'
    require({p.name for p in home.iterdir()} == {'config.yaml', 'plugins'},
            'existing_profile_state_refused')
    require({p.name for p in (home / 'plugins').iterdir()} == {'hermes-cpa'},
            'existing_plugins_refused')
    from ruamel.yaml import YAML
    config = YAML(typ='safe').load(q.read_file(home / 'config.yaml', private=True, limit=65536))
    require(config == {'plugins': {'isolation': 'host', 'enabled': ['hermes-cpa'],
                                  'entries': {'hermes-cpa': {'settings': {'mode': 'external'}}}}},
            'existing_provider_or_settings_refused')
    q.read_file(root / q.PERMIT, private=True, limit=16384)
    require(q.digest(q.read_file(root / q.ARCHIVE_NAME)) ==
            q.runtime_artifact(q.package_path(root)).archive_sha256, 'archive_mismatch')


class Upstream:
    """Generated HTTP server. It never forwards a request or logs credentials."""

    def __init__(self):
        self.key = secrets.token_urlsafe(32)
        self.fallback_key = secrets.token_urlsafe(32)
        self.name = 'qualification-' + secrets.token_hex(8)
        self.models = tuple('q-' + secrets.token_hex(12) for _ in range(4))
        self.active = set(self.models[:2])
        self.lock = threading.Lock()
        self.requests = 0
        self.fallback_requests = 0
        self.accepted = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, body, content_type='application/json'):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                # No substitute /models catalog: Hermes discovers through CPA.
                self.reply(404, {'error': 'not_available'})

            def do_POST(self):
                self.connection.settimeout(5)
                with fixture.lock:
                    fixture.requests += 1
                    if self.path.startswith('/fallback/'):
                        fixture.fallback_requests += 1
                if self.path != '/v1/chat/completions':
                    return self.reply(404, {'error': 'unexpected_path'})
                if self.headers.get('Authorization') != 'Bearer ' + fixture.key:
                    return self.reply(401, {'error': 'unauthorized'})
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 65536:
                        return self.reply(400, {'error': 'invalid_length'})
                    body = json.loads(self.rfile.read(length))
                except (ValueError, OSError):
                    return self.reply(400, {'error': 'invalid_body'})
                model = body.get('model')
                with fixture.lock:
                    active = model in fixture.active
                if not active:
                    return self.reply(404, {'error': 'unknown_model'})
                messages = body.get('messages') or []
                if not any(m.get('role') == 'user' and m.get('content') == 'qualification'
                           for m in messages if isinstance(m, dict)):
                    return self.reply(400, {'error': 'unexpected_prompt'})
                with fixture.lock:
                    fixture.accepted.append((model, bool(body.get('stream'))))
                common = {'id': 'chatcmpl-' + secrets.token_hex(8), 'created': int(time.time()),
                          'model': model}
                text = 'qualified:' + model
                if not body.get('stream'):
                    return self.reply(200, {**common, 'object': 'chat.completion', 'choices': [
                        {'index': 0, 'message': {'role': 'assistant', 'content': text},
                         'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
                chunks = []
                for delta, finish in (({'role': 'assistant', 'content': 'qualified:'}, None),
                                      ({'content': model}, None), ({}, 'stop')):
                    chunk = {**common, 'object': 'chat.completion.chunk', 'choices': [
                        {'index': 0, 'delta': delta, 'finish_reason': finish}]}
                    chunks.append(('data: ' + json.dumps(chunk) + '\n\n').encode())
                chunks.append(b'data: [DONE]\n\n')
                self.reply(200, b''.join(chunks), 'text/event-stream')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def count(self):
        with self.lock:
            return self.requests


def native(host, *arguments):
    result = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'cpa', *arguments],
                            cwd=host, env=dict(os.environ), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    require(result.returncode == 0, 'native_command_failed')


def free_ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(('127.0.0.1', 0))
        second.bind(('127.0.0.1', 0))
        return first.getsockname()[1], second.getsockname()[1]


def eventually(check, code):
    deadline = time.monotonic() + 15
    while True:
        if check():
            return
        require(time.monotonic() < deadline, code)
        time.sleep(.1)


def exercise(root, host, report):
    import httpx
    from openai import OpenAI, AuthenticationError, PermissionDeniedError
    from agent.command_token_source import CommandTokenError
    from hermes_cli.auth import AuthError
    from hermes_cli.config import atomic_config_replace, read_user_config_raw
    from hermes_cli.model_switch import list_authenticated_providers
    from hermes_cli.models import cached_fetch_api_models, probe_api_models
    from hermes_cli.runtime_provider import resolve_runtime_provider, resolve_runtime_with_fallback

    home = root / '.hermes'
    permit_before = q.read_file(root / q.PERMIT, private=True, limit=16384)
    permit_expiry = json.loads(permit_before)['expires_at']
    cpa_port, broker_port = free_ports()
    broker_url = f'http://127.0.0.1:{broker_port}/v1'
    with ExitStack() as stack, Upstream() as upstream, httpx.Client(
            trust_env=False, follow_redirects=False, timeout=5) as http:
        report['stage'] = 'native_setup'
        native(host, 'setup', '--archive', str(root / q.ARCHIVE_NAME), '--consent',
               '--isolated-authorization', str(root / q.PERMIT), '--no-autostart', '--no-enroll',
               '--port', str(cpa_port), '--broker-port', str(broker_port))
        report['stage'] = 'hosted_console'
        console = stack.enter_context(ConsoleClient(root, host))
        report['stage'] = 'native_configuration'
        response = console.request('GET', '/v8/management/config')
        require(response['status'] == 200 and response['headers'].get('x-cpa-version') in
                ('8.0.21', 'v8.0.21') and console.json_result(response).get('config-version') == 8,
                'pinned_cpa_not_ready')

        def inventory(ids):
            payload = [{'name': upstream.name, 'base-url': upstream.url + '/v1',
                        'keys': [{'api-key': upstream.key}],
                        'models': [{'name': model, 'alias': model} for model in ids]}]
            with upstream.lock:
                upstream.active = set(ids)
            console.config('api-keys/openai-compatibility', payload)

        first, removed, explicit_new, automatic_new = upstream.models
        initial = {first, removed}
        report['stage'] = 'initial_inventory'
        inventory(sorted(initial))
        # CPA applies management changes asynchronously. Before enrollment only,
        # wait through the same real broker grant API that native enroll uses.
        # Never probe CPA's inference endpoint or replace registered discovery.
        sys.path.insert(0, str(q.package_path(root) / 'src'))
        from hermes_cpa.subscription_client import provider_runtime
        bootstrap = provider_runtime(home / 'cpa/run/subscription/supervisor.sock', home, broker_url)
        eventually(lambda: set(probe_api_models(bootstrap['api_key'], broker_url).get('models') or [])
                   == initial, 'initial_inventory_not_ready')
        # Native provider enrollment takes its initial snapshot from the broker.
        report['stage'] = 'native_enrollment'
        native(host, 'provider-enroll', '--base-url', broker_url, '--control',
               str(home / 'cpa/run/subscription/supervisor.sock'), '--consent')
        config = read_user_config_raw()
        require(set(config['providers']['cpa']['models']) == initial, 'enrollment_inventory_mismatch')
        # No global catalog is needed. A local unavailable metadata URL keeps
        # optional metadata off the Internet without replacing provider discovery.
        config['model_catalog'] = {'enabled': False}
        config['models_dev'] = {'url': upstream.url + '/metadata-unavailable'}
        fallback_name = 'fallback-' + secrets.token_hex(8)
        config['providers'][fallback_name] = {
            'base_url': upstream.url + '/fallback/v1', 'api_key': upstream.fallback_key,
            'models': [automatic_new], 'discover_models': False}
        config['fallback_providers'] = [{'provider': 'custom:' + fallback_name, 'model': automatic_new}]
        atomic_config_replace(home / 'config.yaml', config)
        # These Hermes config writes retire the earlier console authority.
        console.unlock()
        marker = home / 'cpa/provider-enrollment.json'
        enrollment_before = marker.read_bytes()
        config_before = (home / 'config.yaml').read_bytes()

        def unchanged():
            require(marker.read_bytes() == enrollment_before and
                    (home / 'config.yaml').read_bytes() == config_before,
                    'discovery_changed_enrollment')

        runtime = resolve_runtime_provider(requested='custom:cpa', target_model=first)
        require(runtime['base_url'] == broker_url and runtime['api_mode'] == 'chat_completions'
                and callable(runtime['api_key']), 'registered_runtime_required')
        key = runtime['api_key']
        entry = config['providers']['cpa']
        mode = entry.get('api_mode')
        headers = entry.get('extra_headers') or None
        # This is the normal picker's fingerprint, not runtime's inferred mode.
        cache_args = dict(api_mode=mode, headers=headers)

        def live_ids():
            return probe_api_models(key, broker_url, api_mode=mode,
                                    request_headers=headers).get('models')

        def picker(refresh=False):
            rows = list_authenticated_providers(
                current_provider='custom:cpa', current_base_url=broker_url,
                user_providers=read_user_config_raw().get('providers', {}),
                custom_providers=[], refresh=refresh, for_picker=True,
                fast_custom_probe=False)
            matches = [row for row in rows if row.get('slug') in ('cpa', 'custom:cpa')]
            require(len(matches) == 1 and matches[0].get('api_url') == broker_url,
                    'registered_picker_row_missing')
            return set(matches[0]['models'])

        report['stage'] = 'ordinary_and_sse'
        observed_grants = []

        def observe_request(request):
            require(str(request.url) == broker_url + '/chat/completions', 'sdk_destination_changed')
            auth = request.headers.get('Authorization', '')
            require(auth.startswith('Bearer ') and len(auth) > 7, 'sdk_grant_missing')
            observed_grants.append(auth[7:])

        with httpx.Client(trust_env=False, follow_redirects=False, timeout=10,
                          event_hooks={'request': [observe_request]}) as wire:
            with OpenAI(api_key=key, base_url=runtime['base_url'], max_retries=0,
                        http_client=wire) as client:
                def infer(model, stream=False):
                    result = client.chat.completions.create(model=model,
                        messages=[{'role': 'user', 'content': 'qualification'}], stream=stream)
                    if not stream:
                        require(result.choices[0].finish_reason == 'stop', 'ordinary_incomplete')
                        return result.choices[0].message.content
                    text, complete = '', False
                    with result:
                        for chunk in result:
                            for choice in chunk.choices:
                                text += choice.delta.content or ''
                                complete = complete or choice.finish_reason == 'stop'
                    require(complete, 'sse_incomplete')
                    return text

                require(infer(first) == 'qualified:' + first, 'ordinary_output_mismatch')
                require(infer(first, stream=True) == 'qualified:' + first, 'sse_output_mismatch')
                require(len(observed_grants) == 2 and observed_grants[0] != observed_grants[1],
                        'fresh_request_grants_required')
                require(upstream.accepted == [(first, False), (first, True)], 'upstream_inference_mismatch')
                report.update(ordinary=True, sse=True, distinct_request_grants=True)

                report['stage'] = 'explicit_refresh'
                require(set(live_ids() or []) == initial, 'callable_discovery_failed')
                require(picker() == initial, 'initial_picker_mismatch')
                explicit = {first, explicit_new}
                inventory(sorted(explicit))
                eventually(lambda: set(live_ids() or []) == explicit, 'explicit_inventory_not_ready')
                require(picker(refresh=True) == explicit, 'explicit_refresh_mismatch')
                unchanged()
                report['explicit_refresh'] = True

                report['stage'] = 'public_ttl_swr'
                # Force a synchronous prime so an aged picker entry cannot launch
                # SWR before the inventory changes. Use the same picker identity.
                require(set(cached_fetch_api_models(key, broker_url, ttl_seconds=1,
                            force_refresh=True, **cache_args) or []) == explicit,
                        'shared_cache_fingerprint_mismatch')
                automatic = {first, automatic_new}
                inventory(sorted(automatic))
                eventually(lambda: set(live_ids() or []) == automatic, 'automatic_inventory_not_ready')
                time.sleep(1.1)  # Real elapsed TTL; no clock or cache-file edits.
                stale = cached_fetch_api_models(key, broker_url, ttl_seconds=1, **cache_args)
                require(set(stale or []) == explicit, 'swr_did_not_serve_stale')
                # Ordinary picker uses its unchanged production TTL and sees the
                # same cache refreshed by the public one-second SWR request.
                eventually(lambda: picker() == automatic, 'swr_picker_not_refreshed')
                unchanged()
                require(infer(automatic_new) == 'qualified:' + automatic_new, 'new_model_inference_failed')
                report.update(public_ttl_swr=True, enrollment_unchanged=True, new_model_inference=True)

                report['stage'] = 'retirement'
                fallback = resolve_runtime_provider(requested='custom:' + fallback_name,
                                                    target_model=automatic_new)
                require(fallback['base_url'] == upstream.url + '/fallback/v1' and
                        fallback['api_key'] == upstream.fallback_key, 'generated_fallback_not_resolvable')
                retirement_config = read_user_config_raw()
                for action in ('provider-disable', 'provider-remove'):
                    report['stage'] = action
                    if action == 'provider-remove':
                        # Re-enable only this generated fixture, not by enrollment
                        # or a production provider-enable command.
                        atomic_config_replace(home / 'config.yaml', retirement_config)
                        require(read_user_config_raw() == retirement_config and
                                marker.read_bytes() == enrollment_before,
                                'generated_config_restore_failed')
                        require(infer(automatic_new) == 'qualified:' + automatic_new,
                                'held_sdk_not_live_after_restore')
                    require(q.read_file(root / q.PERMIT, private=True, limit=16384) == permit_before and
                            permit_expiry - time.time() > 30, 'retirement_requires_unexpired_lease')
                    # Each action starts with newly minted, demonstrably live
                    # grants and its own interval, never disable's revoked tokens.
                    captured_at = time.monotonic()
                    captured = [key(), key()]
                    require(captured[0] != captured[1], 'captured_grants_not_distinct')
                    for token in captured:
                        require(http.get(broker_url + '/models', headers={
                            'Authorization': 'Bearer ' + token}).status_code == 200,
                            'captured_grant_not_live')
                    if action == 'provider-remove':
                        report['remove_started_live'] = True
                    before = upstream.count()
                    native(host, action, '--consent')
                    try:
                        infer(automatic_new)
                    except (CommandTokenError, AuthenticationError, PermissionDeniedError):
                        pass
                    else:
                        raise SmokeFailure('held_sdk_survived_retirement')
                    for token in captured:
                        auth = {'Authorization': 'Bearer ' + token}
                        require(http.get(broker_url + '/models', headers=auth).status_code == 401,
                                'captured_discovery_grant_survived_retirement')
                        denied = http.post(broker_url + '/chat/completions', headers=auth, json={
                            'model': automatic_new,
                            'messages': [{'role': 'user', 'content': 'qualification'}]})
                        require(denied.status_code == 401, 'captured_inference_grant_survived_retirement')
                    for resolve in (
                        lambda: resolve_runtime_provider(requested='custom:cpa', target_model=automatic_new),
                        lambda: resolve_runtime_with_fallback(read_user_config_raw(), requested='custom:cpa',
                                                               target_model=automatic_new)):
                        try:
                            resolve()
                        except (AuthError, ValueError):
                            pass
                        else:
                            raise SmokeFailure('retired_provider_resolved_or_rerouted')
                    require(upstream.count() == before and upstream.fallback_requests == 0,
                            'retirement_reached_upstream')
                    # This action's denials must complete before grant or lease
                    # expiry, which must not masquerade as successful retirement.
                    require(time.time() < permit_expiry, 'lease_expired_during_retirement')
                    require(time.monotonic() - captured_at < 25, 'retirement_probe_too_slow')
                    report[action.replace('-', '_')] = True
                require(marker.read_bytes() == enrollment_before, 'retirement_tombstone_changed')
                report.update(held_sdk_denied=True, captured_grants_denied=True,
                              fresh_resolution_denied=True, fallback_not_selected=True,
                              upstream_requests=upstream.count(), fallback_requests=0,
                              model_ids=list(upstream.models))
    report['fixture_stopped'] = True


def interrupt_child(_signum, _frame):
    raise SmokeFailure('interrupted')


def child_main(root, host, *, exercise_fn=exercise):
    report = {'ok': False, 'stage': 'imports'}
    signal.signal(signal.SIGTERM, interrupt_child)
    # Libraries and native children must not leak diagnostic bodies or secrets.
    with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            exercise_fn(Path(root), Path(host), report)
            report.update(ok=True, stage='complete')
        except SmokeFailure as exc:
            report['failure'] = str(exc)
        except ConsoleFailure as exc:
            report.update(failure=str(exc), http_status=exc.http_status, code=exc.code)
        except BaseException as exc:
            report['failure'] = 'unexpected_failure'
            report['failure_type'] = type(exc).__name__
            trace = exc.__traceback__
            frames = []
            while trace is not None:
                frames.append({'file': Path(trace.tb_frame.f_code.co_filename).name,
                               'line': trace.tb_lineno})
                trace = trace.tb_next
            report['failure_frames'] = frames[-6:]
    print(json.dumps(report, sort_keys=True))


def main(argv=None, *, child_module='provider_smoke', marker='.provider-smoke-used', child_timeout=240):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--host', type=Path, default=q.ROOT / 'host')
    parser.add_argument('--consent', action='store_true')
    args = parser.parse_args(argv)
    report = {'ok': False, 'stage': 'guards'}
    owned = False
    root = None
    old_handlers = {}
    child = None
    interrupted = False

    def interrupt(_signum, _frame):
        nonlocal interrupted
        interrupted = True
        # Defer during Popen construction until its handle is assigned inside
        # the cleanup try. Otherwise a signal can orphan the new process group.
        if child is not None:
            raise SmokeFailure('interrupted')

    try:
        require(args.consent, 'consent_required')
        loopback_namespace()
        root, _ = q.load_root(args.root)
        host = q.checked_path(args.host)
        env = q.runtime_environment(root, host)
        with q.root_lock(root):
            fresh_root(root)
            # A failed run is never adopted or retried against generated credentials.
            q.atomic_file(root / marker, b'generated-qualification\n')
            owned = True
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, interrupt)
            code = ('import importlib,sys; sys.path.insert(0, sys.argv[1]); '
                    'importlib.import_module(sys.argv[4]).child_main(sys.argv[2], sys.argv[3])')
            # Own a separate process group so cancellation also stops a native
            # CLI still performing setup before the credentialled owner teardown.
            try:
                child = subprocess.Popen(
                    [sys.executable, '-c', code, str(Path(__file__).resolve().parent), str(root), str(host), child_module],
                    cwd=host, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                require(not interrupted, 'interrupted')
                stdout, _ = child.communicate(timeout=child_timeout)
                require(child.returncode == 0, 'smoke_child_failed')
            finally:
                for sig in old_handlers:
                    signal.signal(sig, signal.SIG_IGN)
                if child is not None:
                    try:
                        try:
                            os.killpg(child.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            child.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            child.communicate(timeout=10)
                    finally:
                        child.stdout.close()
            report = json.loads(stdout)
    except SmokeFailure as exc:
        report['failure'] = str(exc)
    except q.Refused as exc:
        report['failure'] = str(exc)
    except BaseException:
        report['failure'] = 'qualification_failed'
    finally:
        if owned:
            # Reuse the credentialled, root-guarded teardown. Never signal a
            # guessed PID, stop another root, renew a permit or delete state.
            for sig in old_handlers:
                signal.signal(sig, signal.SIG_IGN)
            try:
                q.quiesce(argparse.Namespace(root=root, host=args.host, consent=True))
                report['owner_stopped'] = True
            except BaseException:
                report.update(ok=False, owner_stopped=False, failure='owned_root_quiesce_failed')
        for sig, previous in old_handlers.items():
            signal.signal(sig, previous)
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
