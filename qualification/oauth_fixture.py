"""Generated Claude OAuth authority behind a loopback CONNECT proxy; never forwards."""
import base64
import hashlib
import http.server
import json
from pathlib import Path
import secrets
import ssl
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid


class OAuthFixture:
    CLIENT = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
    HOSTS = {'platform.claude.com:443', 'api.anthropic.com:443'}

    def __init__(self, certificate_dir=Path('/opt/q/oauth-fixture')):
        if {p.name for p in Path('/sys/class/net').iterdir()} != {'lo'}:
            raise ValueError('network_none_namespace_required')
        self.lock = threading.Lock()
        self.pending = {}
        self.access = set()
        self.refresh = set()
        self.exchanges = 0
        self.refreshes = 0
        self.denied = 0
        self.email = 'qualification-' + secrets.token_hex(8) + '@example.invalid'
        self.account = str(uuid.uuid4())
        self.organization = str(uuid.uuid4())
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(certificate_dir / 'cert.pem', certificate_dir / 'key.pem')
        fixture = self

        class Provider(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def reply(self, status, value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

            def do_POST(self):
                if self.path != '/v1/oauth/token' or self.headers.get('Host') != 'platform.claude.com':
                    return self.reply(404, {'error': 'unexpected_endpoint'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 16384:
                        return self.reply(400, {'error': 'invalid_length'})
                    body = json.loads(self.rfile.read(size))
                except (ValueError, OSError):
                    return self.reply(400, {'error': 'invalid_request'})
                with fixture.lock:
                    if body.get('grant_type') == 'authorization_code':
                        pending = fixture.pending.get(body.get('state'))
                        verifier = body.get('code_verifier')
                        challenge = base64.urlsafe_b64encode(hashlib.sha256(
                            verifier.encode()).digest()).decode().rstrip('=') if isinstance(verifier, str) else None
                        if (not pending or pending['used'] or body.get('code') != pending['code'] or
                                body.get('client_id') != fixture.CLIENT or
                                body.get('redirect_uri') != pending['redirect'] or
                                challenge != pending['challenge']):
                            return self.reply(400, {'error': 'invalid_grant'})
                        pending['used'] = True
                        fixture.exchanges += 1
                    elif body.get('grant_type') == 'refresh_token':
                        refresh = body.get('refresh_token')
                        if refresh not in fixture.refresh or body.get('client_id') != fixture.CLIENT:
                            return self.reply(400, {'error': 'invalid_grant'})
                        fixture.refresh.remove(refresh)
                        fixture.refreshes += 1
                    else:
                        return self.reply(400, {'error': 'unsupported_grant_type'})
                    access = 'generated-access-' + secrets.token_urlsafe(32)
                    refresh = 'generated-refresh-' + secrets.token_urlsafe(32)
                    fixture.access.add(access)
                    fixture.refresh.add(refresh)
                self.reply(200, {'access_token': access, 'refresh_token': refresh,
                    'token_type': 'Bearer', 'expires_in': 86400,
                    'account': {'uuid': fixture.account, 'email_address': fixture.email},
                    'organization': {'uuid': fixture.organization, 'name': 'Generated qualification'}})

            def do_GET(self):
                if self.headers.get('Host') != 'api.anthropic.com':
                    return self.reply(404, {'error': 'unexpected_endpoint'})
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                with fixture.lock:
                    valid = token in fixture.access
                if not valid:
                    return self.reply(401, {'error': 'unauthorized'})
                if self.path == '/api/oauth/profile':
                    return self.reply(200, {'account': {'uuid': fixture.account, 'email': fixture.email},
                        'organization': {'uuid': fixture.organization, 'name': 'Generated qualification'}})
                if self.path == '/api/oauth/claude_cli/roles':
                    return self.reply(200, {'roles': []})
                return self.reply(404, {'error': 'unexpected_endpoint'})

        class Proxy(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_CONNECT(self):
                if self.path not in fixture.HOSTS:
                    with fixture.lock:
                        fixture.denied += 1
                    self.send_error(403, 'Fixture destination refused')
                    return
                self.connection.settimeout(10)
                self.send_response(200, 'Connection established')
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                try:
                    with fixture.tls.wrap_socket(self.connection, server_side=True) as secured:
                        Provider(secured, self.client_address, self.server)
                except (OSError, ssl.SSLError):
                    pass

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, _request, _client_address):
                pass

        self.server = Server(('127.0.0.1', 0), Proxy)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def authorize(self, authorization_url):
        parsed = urlsplit(authorization_url)
        query = parse_qs(parsed.query, strict_parsing=True)
        if (parsed.scheme != 'https' or parsed.netloc != 'claude.ai' or
                parsed.path != '/oauth/authorize' or query.get('client_id') != [self.CLIENT] or
                query.get('code_challenge_method') != ['S256'] or
                any(len(query.get(key, [])) != 1 for key in ('state', 'code_challenge', 'redirect_uri'))):
            raise ValueError('native_oauth_url_invalid')
        redirect = query['redirect_uri'][0]
        target = urlsplit(redirect)
        if target.scheme != 'http' or target.hostname not in ('localhost', '127.0.0.1') or target.path != '/callback':
            raise ValueError('native_callback_not_loopback')
        state = query['state'][0]
        code = secrets.token_urlsafe(32)
        with self.lock:
            if state in self.pending:
                raise ValueError('oauth_state_reused')
            self.pending[state] = {'code': code, 'challenge': query['code_challenge'][0],
                                   'redirect': redirect, 'used': False}
        return redirect + '?' + urlencode({'code': code, 'state': state})

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
