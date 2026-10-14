#!/usr/bin/env python3
"""Loopback TLS test ingress for the real Dashboard resource-origin proof.

A test fixture, not a deployable proxy and not a daemon to leave running. It
terminates TLS on 127.0.0.1 and forwards every request, unmodified apart from
hop-by-hop headers, to ONE explicit Hermes port on 127.0.0.1. The original Host
header is preserved, because resource-origin routing depends on it. Nothing is
logged (resource capabilities live in hostnames and queries), no external network
or DNS is used, and everything is bounded.

  # certificate for the browser scenario (writes both files; never overwrites)
  python tls_ingress.py make-cert --cert /work/ingress.pem --key /work/ingress.key \\
      --dns dashboard.fixture.invalid --dns '*.resource.fixture.invalid'

  python tls_ingress.py serve --cert /work/ingress.pem --key /work/ingress.key \\
      --upstream-port 9119 --listen-port 0 \\
      --allow-host dashboard.fixture.invalid --allow-host-suffix .resource.fixture.invalid

`serve` prints one JSON line when ready: {"listening": {"address": "127.0.0.1",
"port": N}, "upstream_port": U, ...}; read N for https://<name>:N. A request whose
Host is not allowed gets 421 and is never forwarded. At least one allowed host is
required (fail closed). The browser needs the certificate trusted or its SPKI
pinned: make-cert prints spki_sha256_base64 for Chromium's
--ignore-certificate-errors-spki-list, and the resolver must map the fixture
names to 127.0.0.1 (for example --host-resolver-rules="MAP *.fixture.invalid 127.0.0.1").

Supported: HTTP/1.1 only (ALPN http/1.1), any method, Content-Length request
bodies up to --max-request-bytes (default 1 MiB), request targets up to
--max-target-bytes (default 16384, query included), streamed responses up to
--max-response-bytes (default 64 MiB) with their Content-Encoding and every
repeated response header preserved, and Upgrade (WebSocket) tunnels bounded by the
same byte limit and the idle timeout. Not supported: chunked request bodies (411).
Optional --forwarded-proto adds X-Forwarded-Proto: https; nothing else is added.
"""
import argparse
import base64
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import threading

HOP_BY_HOP = frozenset({'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te',
                        'trailer', 'transfer-encoding', 'upgrade', 'expect'})
DEFAULT_DNS = ('dashboard.fixture.invalid', 'resource.fixture.invalid', '*.resource.fixture.invalid')


class Config:
    def __init__(self, args):
        self.upstream_port = args.upstream_port
        self.hosts = frozenset(host.lower() for host in args.allow_host)
        self.suffixes = tuple(suffix.lower() for suffix in args.allow_host_suffix)
        self.max_request = args.max_request_bytes
        self.max_response = args.max_response_bytes
        self.max_target = args.max_target_bytes
        self.idle = args.idle_seconds
        self.forwarded_proto = args.forwarded_proto

    def host_allowed(self, value):
        host = (value or '').strip().lower()
        if host.startswith('['):
            return False  # IPv6 literals are never fixture names.
        host = host.rsplit(':', 1)[0] if ':' in host else host
        return host in self.hosts or any(host.endswith(suffix) and len(host) > len(suffix)
                                         for suffix in self.suffixes)


def make_handler(config):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'HermesFixtureIngress'
        sys_version = ''

        def setup(self):
            # Handshake here, with a bound, so a stalled client never blocks accept().
            self.request.settimeout(config.idle)
            self.request.do_handshake()
            super().setup()

        def log_message(self, *_args):
            pass

        def refuse(self, status):
            self.close_connection = True
            self.send_response_only(status)
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()

        def forward(self):
            self.headers_sent = False
            if not config.host_allowed(self.headers.get('Host')):
                return self.refuse(421)
            target = self.path
            if (not target.startswith('/') or len(target) > config.max_target or
                    any(ord(c) < 33 or ord(c) == 127 for c in target)):
                return self.refuse(414 if len(target) > config.max_target else 400)
            if 'transfer-encoding' in self.headers:
                return self.refuse(411)
            length = self.headers.get('Content-Length', '0')
            if not length.isdigit():
                return self.refuse(400)
            if int(length) > config.max_request:
                return self.refuse(413)
            body = self.rfile.read(int(length)) if int(length) else b''
            if len(body) != int(length):
                return self.refuse(400)
            if ('upgrade' in self.headers.get('Connection', '').lower() and self.headers.get('Upgrade')):
                return self.tunnel(target)
            headers = {}
            for name, value in self.headers.items():
                lower = name.lower()
                if lower in HOP_BY_HOP or lower == 'content-length':
                    continue
                headers[name] = headers[name] + (('; ' if lower == 'cookie' else ', ') + value) if name in headers else value
            if config.forwarded_proto:
                headers['X-Forwarded-Proto'] = 'https'
            headers['Connection'] = 'close'
            upstream = http.client.HTTPConnection('127.0.0.1', config.upstream_port, timeout=config.idle)
            try:
                upstream.request(self.command, target, body=body or None, headers=headers)
                response = upstream.getresponse()
                self.relay(response)
            except (OSError, http.client.HTTPException):
                if self.headers_sent:
                    self.close_connection = True  # Mid-response failure: truncate, never append a second response.
                else:
                    self.refuse(502)
            finally:
                upstream.close()

        def relay(self, response):
            status = response.status
            length = response.getheader('Content-Length')
            bodyless = self.command == 'HEAD' or status in (204, 304) or 100 <= status < 200
            fixed = length is not None and length.isdigit()
            if fixed and int(length) > config.max_response and not bodyless:
                return self.refuse(502)
            self.send_response_only(status, response.reason)
            for name, value in response.getheaders():
                if name.lower() not in HOP_BY_HOP and name.lower() != 'content-length':
                    self.send_header(name, value)
            chunked = False
            if bodyless:
                if fixed and self.command == 'HEAD':
                    self.send_header('Content-Length', length)
            elif fixed:
                self.send_header('Content-Length', length)
            elif self.request_version == 'HTTP/1.0':
                self.close_connection = True
                self.send_header('Connection', 'close')
            else:
                chunked = True
                self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.headers_sent = True
            if bodyless:
                return
            sent = 0
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                sent += len(chunk)
                if sent > config.max_response:
                    self.close_connection = True  # Abort: never send a terminating chunk.
                    return
                self.wfile.write(b'%x\r\n%s\r\n' % (len(chunk), chunk) if chunked else chunk)
            if fixed and sent != int(length):
                self.close_connection = True
            elif chunked:
                self.wfile.write(b'0\r\n\r\n')

        def tunnel(self, target):
            self.close_connection = True
            try:
                upstream = socket.create_connection(('127.0.0.1', config.upstream_port), timeout=config.idle)
            except OSError:
                return self.refuse(502)
            lines = [f'{self.command} {target} HTTP/1.1']
            lines += [f'{name}: {value}' for name, value in self.headers.items()]
            if config.forwarded_proto:
                lines.append('X-Forwarded-Proto: https')
            upstream.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))
            upstream.settimeout(config.idle)
            moved = {'bytes': 0}

            def pump(source, sink):
                try:
                    while True:
                        data = source.recv(65536)
                        moved['bytes'] += len(data)
                        if not data or moved['bytes'] > config.max_response:
                            break
                        sink.sendall(data)
                except (OSError, ssl.SSLError):
                    pass
                finally:
                    for end in (source, sink):
                        try:
                            end.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            reverse = threading.Thread(target=pump, args=(upstream, self.connection), daemon=True)
            reverse.start()
            pump(self.connection, upstream)
            reverse.join(timeout=config.idle)
            upstream.close()

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = forward

    return Handler


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, _request, _client_address):
        pass  # Never print client data or tracebacks.


def serve(args):
    config = Config(args)
    if not config.hosts and not config.suffixes:
        raise SystemExit('refused: at least one --allow-host or --allow-host-suffix is required')
    for path in (args.cert, args.key):
        if not path.is_file():
            raise SystemExit('refused: certificate and key files must exist')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(['http/1.1'])
    context.load_cert_chain(args.cert, args.key)
    server = Server(('127.0.0.1', args.listen_port), make_handler(config))
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(json.dumps({'listening': {'address': '127.0.0.1', 'port': server.server_port},
                      'upstream_port': config.upstream_port, 'allow_hosts': sorted(config.hosts),
                      'allow_host_suffixes': list(config.suffixes)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def run(command, data=None):
    done = subprocess.run(command, input=data, capture_output=True)
    if done.returncode != 0:
        raise SystemExit('refused: openssl failed')
    return done.stdout


def make_cert(args):
    for path in (args.cert, args.key):
        if path.exists() or path.is_symlink():
            raise SystemExit('refused: certificate and key paths must not exist')
    names = args.dns or list(DEFAULT_DNS)
    alt = ','.join('DNS:' + name for name in names)
    old = os.umask(0o077)
    try:
        run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
             '-days', str(args.days), '-subj', '/CN=HermesResourceFixtureIngress',
             '-addext', 'subjectAltName=' + alt, '-addext', 'basicConstraints=critical,CA:FALSE',
             '-addext', 'extendedKeyUsage=serverAuth', '-keyout', str(args.key), '-out', str(args.cert)])
    finally:
        os.umask(old)
    public = run(['openssl', 'x509', '-in', str(args.cert), '-pubkey', '-noout'])
    der = run(['openssl', 'pkey', '-pubin', '-outform', 'DER'], public)
    print(json.dumps({'cert': str(args.cert), 'key': str(args.key), 'dns': names,
                      'spki_sha256_base64': base64.b64encode(hashlib.sha256(der).digest()).decode()}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    server = commands.add_parser('serve')
    server.add_argument('--cert', type=Path, required=True)
    server.add_argument('--key', type=Path, required=True)
    server.add_argument('--upstream-port', type=int, required=True)
    server.add_argument('--listen-port', type=int, default=0)
    server.add_argument('--allow-host', action='append', default=[])
    server.add_argument('--allow-host-suffix', action='append', default=[])
    server.add_argument('--max-request-bytes', type=int, default=1024 * 1024)
    server.add_argument('--max-response-bytes', type=int, default=64 * 1024 * 1024)
    server.add_argument('--max-target-bytes', type=int, default=16384)
    server.add_argument('--idle-seconds', type=float, default=60.)
    server.add_argument('--forwarded-proto', action='store_true')
    cert = commands.add_parser('make-cert')
    cert.add_argument('--cert', type=Path, required=True)
    cert.add_argument('--key', type=Path, required=True)
    cert.add_argument('--dns', action='append', default=[])
    cert.add_argument('--days', type=int, default=1)
    args = parser.parse_args(argv)
    if args.command == 'serve':
        if not (1 <= args.upstream_port <= 65535 and 0 <= args.listen_port <= 65535 and
                0 < args.max_request_bytes <= 64 * 1024 * 1024 and 0 < args.max_target_bytes <= 65536 and
                0 < args.max_response_bytes <= 1024 * 1024 * 1024 and args.idle_seconds > 0):
            parser.error('a port or bound is out of range')
        serve(args)
    else:
        if not 1 <= args.days <= 30:
            parser.error('--days must be between 1 and 30')
        make_cert(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
