#!/usr/bin/env python3
"""Generated Hermes Dashboard backend (plus optional TLS ingress) for the real browser proof.

Run only inside the network-none UI qualification image, as its nonroot user, against a
root that qualification/desktop_prepare.py prepared (fresh, permitted, native resource
fixture installed). One run per root: the root is burned by a single-use marker.

  python /opt/q/qualification/dashboard_scenario.py --root /work/<root> --mode http       --consent
  python /opt/q/qualification/dashboard_scenario.py --root /work/<root> --mode http-remap --consent
  python /opt/q/qualification/dashboard_scenario.py --root /work/<root> --mode https      --consent
  python /opt/q/qualification/dashboard_scenario.py --root /work/<root> --mode https-bare --consent

Every mode starts the real, non-headless Dashboard (SPA, plugin routes, resource
presentation middleware) exactly as `hermes dashboard` would: plugin discovery, then
web_server.start_server(headless=False). Nothing here mocks the host, the plugin host,
the owner or the native plugin.

  http        no configuration change. The Dashboard is opened on http://127.0.0.1:<port>;
              resource origins are the reserved loopback form http://p-<handle>.localhost:<port>.
  http-remap  like http, but the browser reaches the backend through a loopback TCP relay on
              another port (an `ssh -L` style remap): origins must use the port the browser used.
  https       a generated self-signed certificate (dashboard.fixture.invalid and
              *.resource.fixture.invalid), a loopback TLS reverse proxy (tls_ingress.py) that
              preserves Host and sets X-Forwarded-Proto, dashboard.public_url plus
              dashboard.resource_origin_template written through Hermes' own config writer, and a
              generated password for the bundled `basic` dashboard auth provider (a remote
              public_url engages the auth gate; the provider is configured through the child
              environment only, never a file). The browser pins the certificate by SPKI hash.
  https-bare  the same, but the proxy only terminates TLS and remaps the port: no
              X-Forwarded-Proto or other forwarding header is added.

Protocol: one JSON line on stdout when ready ({"ready": true, ...}), then
{"command": "stop"} on stdin answers {"stopped": true, "log_clean": bool, ...}. Failures
print {"error": "dashboard_ui_scenario_failed", "stage": <fixed name>}. No exception text,
token, grant id or path other than the generated fixture values the browser needs is printed.
Generated data only; the backend and ingress bind 127.0.0.1 and nothing leaves the namespace.
stderr carries only `stage <fixed name>` breadcrumbs (the name of the step in progress, e.g.
command_wait while idle between commands) so a caller that stops hearing from the helper can say
which step it stalled in. After {"command": "stop"} (or stdin EOF, or SIGTERM) the backend, the
ingress and the root locks are always released before the helper exits.
"""
import argparse
from contextlib import ExitStack
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

from console_client import ConsoleClient, q
from oauth_fixture import OAuthFixture
from provider_smoke import free_ports, loopback_namespace, require

HERE = Path(__file__).resolve().parent
INGRESS = HERE / 'native-resource-fixture' / 'tls_ingress.py'
DASHBOARD_HOST = 'dashboard.fixture.invalid'
RESOURCE_SUFFIX = 'resource.fixture.invalid'
USED = '.dashboard-ui-used'
EFFECT_LOG = '.hermes/cpa/instances/default/hermes-resource-fixture-effects.jsonl'
LOG_LIMIT = 8 * 1024 * 1024

# The real dashboard start sequence (hermes_cli.main._dashboard_prepare_runtime then
# start_server), with the qualification import guard of scripts/qualification.py serve.
BACKEND = (
    'from pathlib import Path; import sys; '
    'from hermes_cli.plugins import discover_plugins; discover_plugins(); '
    'from hermes_cli import web_server; '
    'expected=Path(sys.argv[1])/"hermes_cli/web_server.py"; '
    'actual=Path(web_server.__file__).resolve(); '
    'assert actual == expected, "ambiguous_host_import"; '
    'web_server.start_server(host="127.0.0.1", port=int(sys.argv[2]), '
    'open_browser=False, headless=False, isolated=True)')

# Hermes' own config writer (records the plugin policy epoch like every other writer).
CONFIG = (
    'import json, sys; from pathlib import Path; '
    'from hermes_cli.config import atomic_config_replace, read_user_config_raw; '
    'values = json.loads(sys.argv[1]); config = read_user_config_raw(); '
    'section = dict(config.get("dashboard") or {}); section.update(values); '
    'config["dashboard"] = section; '
    'atomic_config_replace(Path(sys.argv[2]) / ".hermes/config.yaml", config)')


class Relay:
    """Loopback TCP relay on another port, like `ssh -L`: Hermes sees the browser's own Host."""

    def __init__(self, upstream_port):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    upstream = socket.create_connection(('127.0.0.1', upstream_port), timeout=10)
                except OSError:
                    return
                upstream.settimeout(None)

                def pump(source, target):
                    try:
                        while chunk := source.recv(65536):
                            target.sendall(chunk)
                    except OSError:
                        pass
                    finally:
                        try:
                            target.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass

                upward = threading.Thread(target=pump, args=(self.request, upstream), daemon=True)
                upward.start()
                pump(upstream, self.request)
                upward.join(timeout=30)
                upstream.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def emit(value):
    print(json.dumps(value, separators=(',', ':')), flush=True)


class Scenario:
    def __init__(self, root, host, mode):
        self.root, self.host, self.mode = root, host, mode
        # Computed under the caller's ordinary HOME; the generated HOME/HERMES_HOME
        # reach only the children (q.load_root refuses to run inside them).
        self.environment = q.runtime_environment(root, host)
        self.scratch = Path(tempfile.mkdtemp(prefix='hermes-ui-dashboard-'))
        self.log_path = self.scratch / 'backend.log'
        self.processes = []
        self.backend = None
        self.lock_fd = None
        self.relay = None
        self.password = None
        self.token = None
        self.stage = 'init'
        self.finished = False
        self.cleanup = ExitStack()
        self.oauth = None

    @property
    def stage(self):
        return self._stage

    @stage.setter
    def stage(self, name):
        """Fixed stage names only. The browser spec reads these stderr lines to say where a wait stalled."""
        self._stage = name
        try:
            print('stage ' + name, file=sys.stderr, flush=True)
        except OSError:
            pass

    def tool_env(self):
        return {'PATH': self.environment['PATH'], 'LANG': 'C.UTF-8', 'HOME': str(self.scratch)}

    def start(self):
        import fcntl
        self.stage = 'single_use_marker'
        q.atomic_file(self.root / USED, b'generated-dashboard-ui-qualification\n')
        port, _ = free_ports()
        environment = dict(self.environment)
        # runtime_environment() is the headless `serve` environment; the Dashboard must mount its SPA.
        environment.pop('HERMES_SERVE_HEADLESS', None)
        self.token = q.read_file(self.root / q.TOKEN, private=True, limit=128).decode().strip()
        environment['HERMES_DASHBOARD_SESSION_TOKEN'] = self.token
        info = {'ready': True, 'mode': self.mode, 'backend_port': port,
                'effect_log': str(self.root / EFFECT_LOG)}
        if self.mode == 'http':
            self.stage = 'generated_oauth_authority'
            self.oauth = self.cleanup.enter_context(OAuthFixture())
            with ConsoleClient(self.root, self.host) as console:
                console.config('requests/proxy-url', self.oauth.url)
            info['oauth_email'] = self.oauth.email
        if self.mode.startswith('https'):
            info.update(self.configure_https(port, environment))
        elif self.mode == 'http-remap':
            self.stage = 'tcp_relay'
            self.relay = Relay(port)
            info.update(dashboard_origin=f'http://127.0.0.1:{self.relay.port}', relay_port=self.relay.port)
        else:
            info['dashboard_origin'] = f'http://127.0.0.1:{port}'
        self.stage = 'backend_start'
        self.lock_fd = q.private_lock(self.root / q.BACKEND_LOCK)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise q.Refused('backend_already_running') from None
        os.set_inheritable(self.lock_fd, True)
        with open(self.log_path, 'wb') as log:
            self.backend = subprocess.Popen(
                [sys.executable, '-s', '-c', BACKEND, str(self.host), str(port)], env=environment,
                cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                pass_fds=(self.lock_fd,), start_new_session=True)
        self.processes.append(self.backend)
        self.stage = 'backend_ready'
        self.wait_ready(port)
        return info

    def configure_https(self, backend_port, environment):
        self.stage = 'tls_material'
        cert, key = self.scratch / 'ingress.pem', self.scratch / 'ingress.key'
        made = subprocess.run(
            [sys.executable, str(INGRESS), 'make-cert', '--cert', str(cert), '--key', str(key),
             '--dns', DASHBOARD_HOST, '--dns', '*.' + RESOURCE_SUFFIX],
            env=self.tool_env(), stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        require(made.returncode == 0, 'tls_certificate_failed')
        spki = json.loads(made.stdout)['spki_sha256_base64']
        self.stage = 'tls_ingress'
        ingress = subprocess.Popen(
            [sys.executable, str(INGRESS), 'serve', '--cert', str(cert), '--key', str(key),
             '--upstream-port', str(backend_port), '--listen-port', '0',
             '--allow-host', DASHBOARD_HOST, '--allow-host-suffix', '.' + RESOURCE_SUFFIX,
             *(['--forwarded-proto'] if self.mode == 'https' else [])],
            env=self.tool_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.processes.append(ingress)
        require(select.select([ingress.stdout], [], [], 20)[0], 'tls_ingress_not_ready')
        tls_port = json.loads(ingress.stdout.readline())['listening']['port']
        self.stage = 'dashboard_credentials'
        username = 'fixture-' + secrets.token_hex(4)
        self.password = secrets.token_urlsafe(24)
        environment.update(
            HERMES_DASHBOARD_BASIC_AUTH_USERNAME=username,
            HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=self.password,
            HERMES_DASHBOARD_BASIC_AUTH_SECRET=secrets.token_hex(32))
        self.stage = 'dashboard_config'
        template = f'https://p-{{token}}.{RESOURCE_SUFFIX}:{tls_port}'
        values = {'public_url': f'https://{DASHBOARD_HOST}:{tls_port}', 'resource_origin_template': template}
        with q.root_lock(self.root):
            done = subprocess.run(
                [sys.executable, '-s', '-c', CONFIG, json.dumps(values), str(self.root)],
                env=self.environment, cwd=self.root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        require(done.returncode == 0, 'dashboard_config_failed')
        return {'dashboard_origin': values['public_url'], 'resource_template': template,
                'tls_port': tls_port, 'username': username, 'password': self.password,
                'ca_pem': str(cert), 'spki': spki,
                'host_resolver_rules': f'MAP {DASHBOARD_HOST} 127.0.0.1,MAP *.{RESOURCE_SUFFIX} 127.0.0.1'}

    def wait_ready(self, port):
        deadline = time.monotonic() + 120
        while True:
            require(self.backend.poll() is None, 'dashboard_backend_exited')
            try:
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                try:
                    connection.request('GET', '/api/status')
                    # Any real HTTP answer proves the app is up (a gated one answers 401/302).
                    if connection.getresponse().status in (200, 302, 401):
                        return
                finally:
                    connection.close()
            except (OSError, http.client.HTTPException):
                pass
            require(time.monotonic() < deadline, 'dashboard_backend_not_ready')
            time.sleep(.25)

    @staticmethod
    def terminate(process):
        if process.poll() is not None:
            return
        for signum, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 10)):
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    def log_clean(self):
        """No capability host, resource query or credential may reach the app's own output."""
        try:
            with open(self.log_path, 'rb') as stream:
                data = stream.read(LOG_LIMIT + 1)
        except OSError:
            return True, 0
        forbidden = [re.compile(rb'p-[0-9a-f]{48}'), re.compile(rb'a=1&a=2'), re.compile(rb'source=button')]
        literals = [value.encode() for value in (self.token, self.password) if value]
        if self.oauth is not None:
            with self.oauth.lock:
                literals.extend(value.encode() for value in self.oauth.access | self.oauth.refresh)
        clean = (len(data) <= LOG_LIMIT and not any(pattern.search(data) for pattern in forbidden) and
                 not any(literal in data for literal in literals))
        return clean, min(len(data), LOG_LIMIT)

    def quiesce(self):
        try:
            done = subprocess.run(
                [sys.executable, str(q.ROOT / 'scripts/qualification.py'), 'quiesce', '--root',
                 str(self.root), '--host', str(self.host), '--consent'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
            return done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def stop(self):
        self.finished = True
        # A late SIGTERM must not abort the teardown half way and orphan a process that holds the lock
        # (a handler, not SIG_IGN: an ignored disposition would be inherited by the quiesce child).
        signal.signal(signal.SIGTERM, lambda *_: None)
        self.stage = 'stop_processes'
        try:
            try:
                if self.relay is not None:
                    self.relay.close()
            finally:
                for process in reversed(self.processes):
                    self.terminate(process)
            clean, size = self.log_clean()
            self.stage = 'stop_quiesce'
            quiesced = self.quiesce()
        finally:
            self.stage = 'stop_release'
            try:
                self.cleanup.close()
            finally:
                if self.lock_fd is not None:
                    os.close(self.lock_fd)
                    self.lock_fd = None
                shutil.rmtree(self.scratch, ignore_errors=True)
        return {'stopped': True, 'log_clean': clean, 'log_bytes': size, 'quiesced': quiesced}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--host', type=Path, default=q.ROOT / 'host')
    parser.add_argument('--mode', choices=('http', 'http-remap', 'https', 'https-bare'), required=True)
    parser.add_argument('--consent', action='store_true')
    args = parser.parse_args()
    scenario = None
    try:
        require(args.consent, 'consent_required')
        loopback_namespace()
        root, _ = q.load_root(args.root)
        require((root / '.desktop-smoke-used').is_file(), 'prepared_ui_root_required')
        scenario = Scenario(root, q.checked_path(args.host), args.mode)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
        emit(scenario.start())
        while True:
            scenario.stage = 'command_wait'
            line = sys.stdin.readline(4097)
            if not line:
                break
            scenario.stage = 'command_dispatch'
            command = json.loads(line)
            if command == {'command': 'stop'}:
                emit(scenario.stop())
                return 0
            if scenario.oauth is None:
                raise ValueError('invalid_scenario_command')
            if command == {'command': 'oauth-counts'}:
                scenario.stage = 'oauth_counts'
                with scenario.oauth.lock:
                    emit({'exchanges': scenario.oauth.exchanges, 'refreshes': scenario.oauth.refreshes})
            elif (isinstance(command, dict) and set(command) == {'command', 'url'} and
                  command['command'] == 'oauth-authorize' and isinstance(command['url'], str)):
                scenario.stage = 'oauth_authorize'
                emit({'callback': scenario.oauth.authorize(command['url'])})
            else:
                raise ValueError('invalid_scenario_command')
        scenario.stop()
        return 0
    except BaseException:
        emit({'error': 'dashboard_ui_scenario_failed', 'stage': scenario.stage if scenario else 'init'})
        return 1
    finally:
        if scenario is not None and not scenario.finished:
            try:
                scenario.stop()
            except BaseException:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
