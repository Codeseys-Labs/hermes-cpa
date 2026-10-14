"""Generated stand-in for the CPA integration build's private owner channels.

Speaks the agreed contract only: --hermes-control-socket/--hermes-inference-
socket/--hermes-owner-pid, SO_PEERCRED owner checks, /hermes/health, native
prepare/commit, the three inference routes and --hermes-prepare-config offline
mode. It is never a real CPA and holds no credentials.

Variants: normal; grandchild (a different process serves the sockets);
wrong-service (health answers another integration); stream-hold (chat streams
wait for <state>/release before their terminal event).
"""
import argparse
import ctypes
import hashlib
import json
import os
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import native_config_fixture as native  # noqa: E402

PREFIX = b'data: {"delta":"' + b'x' * 256 + b'"}\n\n'
TERMINAL = b'data: [DONE]\n\n'

parser = argparse.ArgumentParser()
parser.add_argument('-config', dest='config', required=True)
parser.add_argument('-local-model', dest='local_model', action='store_true')
parser.add_argument('--state')
parser.add_argument('--variant', default='normal')
parser.add_argument('--hermes-prepare-config', dest='offline', action='store_true')
parser.add_argument('--hermes-control-socket', dest='control')
parser.add_argument('--hermes-inference-socket', dest='inference')
parser.add_argument('--hermes-owner-pid', dest='owner', type=int)
args = parser.parse_args()
config = Path(args.config)

if args.offline:
    status, body = native.prepare(config.read_bytes(), sys.stdin.buffer.read())
    sys.stdout.buffer.write(body)
    sys.exit({200: 0, 409: 3}.get(status, 2))


def record(channel, handler):
    if args.state:
        with open(Path(args.state) / 'hits.jsonl', 'a') as log:
            log.write(json.dumps({'channel': channel, 'method': handler.command,
                                  'path': handler.path, 'pid': os.getpid(),
                                  'authorization': 'Authorization' in handler.headers}) + '\n')


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def verify_request(self, request, client_address):
        pid, uid, _ = struct.unpack('3i', request.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        return pid == args.owner and uid == os.geteuid()


def handler(channel):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def address_string(self):
            return 'owner'

        def reply(self, status, body, content_type='application/json', headers=()):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def body(self):
            return self.rfile.read(int(self.headers.get('Content-Length', '0')))

        def do_GET(self):
            self.route()

        def do_POST(self):
            self.route()

        def do_PUT(self):
            self.route()

        def do_DELETE(self):
            self.route()

        def route(self):
            record(channel, self)
            path = urlsplit(self.path).path
            payload = self.body()
            if channel == 'inference':
                return self.inference(path, payload)
            revision = hashlib.sha256(config.read_bytes()).hexdigest()
            if (self.command, path) == ('GET', '/hermes/health'):
                integration = 'other-integration' if args.variant == 'wrong-service' else 'hermes-owner-v1'
                return self.reply(200, json.dumps({'protocol': 1, 'integration': integration,
                    'cpa_version': '8.0.21', 'config_revision': revision}).encode())
            if (self.command, path) == ('POST', '/hermes/config/prepare'):
                return self.reply(*native.prepare(config.read_bytes(), payload))
            if (self.command, path) == ('POST', '/hermes/config/commit'):
                status, body, persist = native.commit(config.read_bytes(), payload)
                if persist is not None:
                    native.write_in_place(config, persist)
                return self.reply(status, body)
            if (self.command, path) == ('GET', '/v8/management/config'):
                return self.reply(200, b'{"config-version":8}', headers=(
                    ('X-CPA-VERSION', '8.0.21'), ('X-Hermes-Config-Revision', revision)))
            if (self.command, path) == ('GET', '/v8/management/observability/usage/api-keys'):
                return self.reply(200, b'{}', headers=(('X-CPA-VERSION', '8.0.21'),))
            if (self.command, path) == ('DELETE', '/v8/management/oauth/session'):
                return self.reply(200, b'{"status":"ok"}')
            if (self.command, path) == ('POST', '/v8/management/echo'):
                return self.reply(200, payload, 'application/octet-stream')
            if self.command == 'GET' and path.startswith('/v0/resource/plugins/generated/'):
                # Agreed Go contract: exact registry revision check with route lookup.
                current = 'none-0'
                if self.headers.get('X-Hermes-Resource-Revision') != current:
                    return self.reply(412, json.dumps({'error': 'stale_resource_revision',
                        'registry_revision': current}).encode(), headers=(
                        ('X-Hermes-Resource-Invoked', '0'), ('X-Hermes-Resource-Revision', current)))
                invoked = (('X-Hermes-Resource-Invoked', '1'),)
                if path.endswith('/forged'):  # A plugin echoing its own trust header.
                    invoked += (('X-Hermes-Resource-Invoked', '0'),)
                return self.reply(200, b'{"item":"generated"}', headers=invoked + (
                    ('X-Hermes-Resource-Revision', current),))
            return self.reply(404, b'{"error":"not_found"}')

        def inference(self, path, payload):
            if (self.command, path) == ('GET', '/v1/models'):
                return self.reply(200, json.dumps({'object': 'list', 'data': [
                    {'id': 'generated-model', 'owned_by': os.getpid()}]}).encode())
            if (self.command, path) != ('POST', '/v1/chat/completions'):
                return self.reply(404, b'{"error":"not_found"}')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(f'{len(PREFIX):x}\r\n'.encode() + PREFIX + b'\r\n')
                self.wfile.flush()
                if args.variant == 'stream-hold':
                    release = Path(args.state) / 'release'
                    deadline = time.monotonic() + 10
                    while not release.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                self.wfile.write(f'{len(TERMINAL):x}\r\n'.encode() + TERMINAL + b'\r\n0\r\n\r\n')
                self.wfile.flush()
            except OSError:
                pass  # The broker deliberately cut this stream.
    return Handler


def bind(path, channel):
    """Bind through a directory fd: only the basename must fit sun_path."""
    path = Path(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    if os.path.lexists(path):
        raise SystemExit('socket path exists')
    server = Server(f'/proc/self/fd/{fd}/{path.name}', handler(channel), bind_and_activate=False)
    server.server_bind()
    os.chmod(path, 0o600)
    server.server_activate()
    return server, os.stat(path).st_ino


def serve():
    parent_pid = os.getppid()
    servers = [bind(args.control, 'control'), bind(args.inference, 'inference')]
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    for server, _ in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    while not done.wait(.1):
        if os.getppid() != parent_pid:
            break
    for (server, inode), path in zip(servers, (args.control, args.inference)):
        server.shutdown()
        try:
            if os.stat(path).st_ino == inode:
                os.unlink(path)
        except OSError:
            pass


if args.variant == 'grandchild':
    # The owned child stays alive while a DIFFERENT process answers its sockets.
    if os.fork() == 0:
        ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0)
        serve()
        os._exit(0)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    time.sleep(30)
else:
    serve()
