"""Opt-in native inference broker. Upstream key stays in the owner backend.

The private local control socket issues short-lived, incarnation/epoch-bound
consumer grants. HTTP accepts those grants, never management credentials.
No request retries, upstream redirects, renderer endpoints or disk key copies.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from .cpa_adapter import CpaAdapter
from .process_owner import private_directory, private_open, socket_address
from .settings import INFERENCE_SECRET_ENV


class _BoundedHTTPServer(ThreadingHTTPServer):
    """Bound connection threads before spawning, including slow header readers."""
    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def _credential_safe_chunks(chunks, credential):
    """Hold a suffix so a credential split across transport chunks cannot escape.

    Only the backend-owned inference credential is known here. This is not a
    general sanitizer for unknown provider credentials or arbitrary encodings.
    """
    secret = credential.encode('ascii')
    pending = b''
    keep = len(secret) - 1
    for chunk in chunks:
        pending += chunk
        if secret in pending:
            raise ValueError('upstream_credential_echo')
        if len(pending) > keep:
            end = len(pending) - keep
            yield pending[:end]
            pending = pending[end:]
    if pending:
        yield pending


class _ResponseDeadline:
    """One timer per admitted request interrupts both sides of blocked I/O."""

    def __init__(self, downstream, timeout, authorization=None):
        now = time.monotonic()
        self.authorization = authorization
        if authorization is not None:
            timeout = min(timeout, authorization.remaining())
        self.deadline = now + timeout
        self._lock = threading.Lock()
        self._sockets = [downstream]
        self._done = False
        self._expired = False
        self._timer = threading.Timer(max(0., self.deadline - time.monotonic()), self._expire)
        self._timer.daemon = True

    def __enter__(self):
        self._timer.start()
        return self

    @staticmethod
    def _shutdown(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # Already closed; never look up or reuse a numeric descriptor.

    def _expire(self):
        with self._lock:
            if self._done:
                return
            self._expired = True
            for connection in self._sockets:
                self._shutdown(connection)

    def _deadline_expired(self):
        return (self._expired or time.monotonic() >= self.deadline or
                (self.authorization is not None and not self.authorization.current()))

    def trace(self, event, info):
        if event == 'connection.connect_tcp.started':
            self.check()
        # httpcore's documented trace extension exposes the connected stream
        # before request writes/response headers, unlike response extensions.
        if event == 'connection.connect_tcp.complete':
            connection = info['return_value'].get_extra_info('socket')
            if connection is None:
                raise ValueError('response_transport_unavailable')
            with self._lock:
                if self._deadline_expired():
                    self._shutdown(connection)
                    connection.close()  # Trace failed before the pool owned this stream.
                    raise TimeoutError('response_deadline')
                self._sockets.append(connection)

    def check(self):
        with self._lock:
            if self._deadline_expired():
                raise TimeoutError('response_deadline')

    def __exit__(self, *args):
        with self._lock:
            self._done = True
            self._timer.cancel()
            self._sockets.clear()


class SubscriptionBroker:
    INGRESS_TIMEOUT = 10
    RESOLVE_TIMEOUT = 2
    RESPONSE_TIMEOUT = 300

    def __init__(self, owner_home, upstream, *, port=8319, authorization=None):
        self.home = Path(owner_home)
        if not self.home.is_absolute() or self.home.resolve() != self.home:
            raise ValueError('invalid_owner')
        # Reuse the existing strict literal-loopback origin validator.
        self.upstream = CpaAdapter(upstream).base_url
        self.directory = self.home / 'cpa/run/subscription'
        self.control_path = self.directory / 'supervisor.sock'
        self.port = port
        self.authorization = authorization
        self._signing = secrets.token_bytes(32)
        self._epoch = 0
        self._fingerprint = None
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._responses = set()
        self._slots = threading.BoundedSemaphore(32)
        # One unresolved filesystem job at most, even after caller timeout.
        self._resolver_slot = threading.BoundedSemaphore(1)

    def _require_authorization(self):
        if self.authorization is not None and not self.authorization.current():
            raise ValueError('authorization_expired')

    def _consumer(self, home):
        from hermes_constants import named_profile_has_identity, named_profile_is_deleted
        path = Path(home)
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ValueError('unauthorized_consumer')
        if path == self.home:
            return str(path)
        if (path.parent != self.home / 'profiles' or path.name == 'default' or
                not named_profile_has_identity(path) or named_profile_is_deleted(path)):
            raise ValueError('unauthorized_consumer')
        return str(path)

    def _credential(self):
        from agent.secret_scope import load_env_file
        try:
            path = self.home / '.env'
            if self.home.resolve() != self.home or path.is_symlink():
                raise ValueError('invalid_owner')
            before = path.stat()
            value = load_env_file(path).get(INFERENCE_SECRET_ENV)
            after = path.stat()
            stamp = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            if stamp(before) != stamp(after):
                raise ValueError('credential_changed')
            if (not isinstance(value, str) or not value or value.startswith('op://') or
                    any(ord(c) < 33 or ord(c) > 126 for c in value)):
                raise ValueError('inference_unavailable')
        except Exception:
            raise ValueError('inference_unavailable') from None
        # Include write identity: A→B→A between reads still revokes old grants.
        return value, (hashlib.sha256(value.encode()).digest(), stamp(after))

    def _enrollment(self, home):
        """Fresh policy at both mint and HTTP admission; never execute config commands."""
        from ruamel.yaml import YAML
        path = Path(home) / 'config.yaml'
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError('invalid_enrollment')
        before = path.stat()
        config = YAML(typ='safe').load(path.read_text())
        plugins = config.get('plugins') or {}
        plugin = plugins.get('hermes-cpa') or {}
        if ('hermes-cpa' in plugins.get('disabled', []) or
                plugin.get('enabled') is False or
                ('enabled' in plugins and 'hermes-cpa' not in plugins['enabled'])):
            raise ValueError('plugin_disabled')
        if not Path(__file__).is_file() or not Path(__file__).with_name('subscription_client.py').is_file():
            raise ValueError('plugin_removed')
        entry = config['providers']['cpa']
        if (entry.get('enabled', True) is not True or
                (entry.get('base_url') or entry.get('api') or entry.get('url')) != self.base_url or not entry.get('key_cmd')):
            raise ValueError('disabled_enrollment')
        after = path.stat()
        stamp = lambda st: [st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns]
        if stamp(before) != stamp(after):
            raise ValueError('changed_enrollment')
        return stamp(after)

    def _resolve(self, home, *, enrolled=False):
        """Bound callers, not arbitrary filesystem calls; never publish late work.

        Overlapping consumers queue for the single resolver slot, but only
        within the same RESOLVE_TIMEOUT budget that bounds the resolution.
        The wait happens outside _lock, so revocation never blocks behind it.
        """
        deadline = time.monotonic() + self.RESOLVE_TIMEOUT
        if self._closed.is_set() or not self._resolver_slot.acquire(timeout=self.RESOLVE_TIMEOUT):
            raise ValueError('resolver_unavailable')
        with self._lock:
            if self._closed.is_set() or time.monotonic() >= deadline:
                # No budget left to resolve: refuse without starting a job.
                self._resolver_slot.release()
                raise ValueError('resolver_unavailable')
            epoch = self._epoch
        done = threading.Event()
        abandoned = threading.Event()
        result = []

        def resolve():
            try:
                consumer = self._consumer(home)
                identity = Path(consumer).stat()
                binding: list = [identity.st_dev, identity.st_ino]
                marker = Path(consumer) / 'cpa/provider-enrollment.json'
                if enrolled or marker.exists() or marker.is_symlink():
                    binding.append(self._enrollment(consumer))
                key, fingerprint = self._credential()
                with self._lock:
                    if abandoned.is_set() or self._closed.is_set() or epoch != self._epoch:
                        return
                    if fingerprint != self._fingerprint:
                        self._epoch += 1
                        self._fingerprint = fingerprint
                    result.append((consumer, binding,
                                   key, self._epoch))
            except Exception:
                with self._lock:
                    if not abandoned.is_set() and epoch == self._epoch:
                        self._epoch += 1
                        self._fingerprint = None
            finally:
                self._resolver_slot.release()
                done.set()

        thread = threading.Thread(target=resolve, daemon=True)
        try:
            thread.start()
        except Exception:
            self._resolver_slot.release()
            raise ValueError('resolver_unavailable') from None
        completed = done.wait(max(0., deadline - time.monotonic()))
        with self._lock:
            if not completed:
                abandoned.set()
                self._epoch += 1
                self._fingerprint = None
            if not completed or not result or self._closed.is_set():
                raise ValueError('resolver_unavailable')
            return result[0]

    def revoke(self):
        """Explicitly revoke every outstanding grant; never replay an inference."""
        with self._lock:
            self._epoch += 1

    def grant(self, home, *, enrolled=False):
        self._require_authorization()
        consumer, identity, _key, epoch = self._resolve(home, enrolled=enrolled)
        with self._lock:
            self._require_authorization()
            if self._closed.is_set() or epoch != self._epoch:
                raise ValueError('revoked_grant')
            expiry = time.time() + 30
            if self.authorization is not None:
                expiry = min(expiry, self.authorization.expires_at)
            record = json.dumps([consumer, identity,
                                 epoch, expiry,
                                 secrets.token_hex(16)], separators=(',', ':')).encode().hex()
            signature = hmac.new(self._signing, record.encode(), 'sha256').hexdigest()
            return record + '.' + signature

    def authorize(self, token):
        try:
            self._require_authorization()
            if not isinstance(token, str) or len(token) > 8192:
                raise ValueError('invalid_grant')
            record, signature = token.split('.')
            expected = hmac.new(self._signing, record.encode(), 'sha256').hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError('invalid_grant')
            home, identity, epoch, expiry, _connection = json.loads(bytes.fromhex(record))
            _consumer, current, key, resolved_epoch = self._resolve(home, enrolled=len(identity) == 3)
            with self._lock:
                self._require_authorization()
                if (identity != current or epoch != resolved_epoch or epoch != self._epoch or
                        time.time() >= expiry or self._closed.is_set()):
                    raise ValueError('revoked_grant')
                return key
        except Exception:
            raise ValueError('invalid_grant') from None

    def _control(self):
        while not self._closed.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1)
                try:
                    _, uid, _ = struct.unpack('3i', connection.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                    if uid != os.getuid():
                        raise ValueError('unauthorized_peer')
                    raw = bytearray()
                    deadline = time.monotonic() + 1
                    while not raw.endswith(b'\n'):
                        connection.settimeout(max(.001, deadline - time.monotonic()))
                        part = connection.recv(4096)
                        if not part or len(raw) + len(part) > 8192 or time.monotonic() >= deadline:
                            raise ValueError('invalid_request')
                        raw.extend(part)
                    request = json.loads(raw)
                    result = {'token': self.grant(request['home'], enrolled=request.get('enrolled') is True)}
                except Exception:
                    result = {'error': 'subscription_unavailable'}
                try:
                    connection.sendall(json.dumps(result).encode() + b'\n')
                except OSError:
                    pass  # Peer disconnected; no inference has been sent.

    def _reclaim_stale_control(self):
        """Remove a control socket only when no process is listening on it.

        Caller holds broker.lock, so no locked broker is alive. A connectable
        listener (an older unlocked broker) is refused, never adopted.
        """
        path = self.control_path
        if path.is_symlink() or (path.exists() and not path.is_socket()):
            raise ValueError('unsafe_control')
        if not path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(.5)
        try:
            with socket_address(self.directory) as address:
                probe.connect(address)
        except ConnectionRefusedError:
            path.unlink()
            return
        except FileNotFoundError:
            return
        finally:
            probe.close()
        raise ValueError('broker_running')

    def __enter__(self):
        import fcntl
        self._require_authorization()
        private_directory(self.directory)
        # Broker lifetime lock: one broker per owner home across CLI/owner hosts.
        lock = private_open(self.directory / 'broker.lock', os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock)
            raise ValueError('broker_running') from None
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            self._reclaim_stale_control()
            with socket_address(self.directory) as address:
                self._socket.bind(address)  # Never unlink/adopt a live broker.
            bound = True
            self.control_path.chmod(0o600)
            self._socket.listen(8)
            self._socket.settimeout(.1)
            self._http = _BoundedHTTPServer(('127.0.0.1', self.port), self._handler())
        except Exception:
            self._socket.close()
            if bound:
                self.control_path.unlink(missing_ok=True)
            os.close(lock)
            raise
        self._lifetime_lock = lock
        self.base_url = f'http://127.0.0.1:{self._http.server_port}/v1'
        self._control_thread = threading.Thread(target=self._control, daemon=True)
        self._http_thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._control_thread.start()
        self._http_thread.start()
        return self

    @contextmanager
    def _response_budget(self, connection):
        with _ResponseDeadline(connection, self.RESPONSE_TIMEOUT, self.authorization) as budget:
            with self._lock:
                self._require_authorization()
                if self._closed.is_set():
                    raise ValueError('broker_closed')
                self._responses.add(budget)
            try:
                budget.check()
                yield budget
            finally:
                with self._lock:
                    self._responses.discard(budget)

    def __exit__(self, *args):
        with self._lock:
            self._closed.set()
            self._epoch += 1
            responses = tuple(self._responses)
        # Interrupt captured transports, never the independently owned service.
        # No I/O or deadline callbacks under the authority lock.
        for budget in responses:
            budget._expire()
        self._http.shutdown()
        self._http.server_close()
        self._socket.close()
        self._control_thread.join(3)
        self._http_thread.join(3)
        self.control_path.unlink(missing_ok=True)
        os.close(self._lifetime_lock)

    def _handler(self):
        broker = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def end_headers(self):
                # One admission per connection; failed streams close without a
                # terminal chunk, never recycle the socket for another request.
                self.close_connection = True
                self.send_header('Connection', 'close')
                super().end_headers()

            def log_message(self, format, *args):
                pass  # Never log bearer tokens, bodies or upstream errors.

            def setup(self):
                super().setup()
                self.connection.settimeout(10)
                self._ingress_lock = threading.Lock()
                self._ingress_done = False
                self._ingress_expired = False
                self._ingress_timer = threading.Timer(broker.INGRESS_TIMEOUT, self._expire_ingress)
                self._ingress_timer.daemon = True
                self._ingress_timer.start()

            def _expire_ingress(self):
                with self._ingress_lock:
                    if self._ingress_done:
                        return
                    self._ingress_expired = True
                    try:
                        # shutdown, not close: wake a buffered read even while
                        # trickled bytes keep resetting its inactivity timeout.
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass  # The peer already closed its connection.

            def _finish_ingress(self):
                with self._ingress_lock:
                    if self._ingress_expired:
                        raise TimeoutError('ingress_deadline')
                    self._ingress_done = True
                    self._ingress_timer.cancel()

            def finish(self):
                with self._ingress_lock:
                    self._ingress_done = True
                    self._ingress_timer.cancel()
                super().finish()

            def handle(self):
                try:
                    super().handle()
                except OSError:
                    # A disconnected/deadline-closed peer cannot receive an
                    # error. Never retry dispatch or dump request diagnostics.
                    self.close_connection = True

            def do_GET(self):
                self._dispatch()

            def do_POST(self):
                self._dispatch()

            def _error(self, status):
                raw = b'{"error":{"message":"subscription request refused"}}'
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _dispatch(self):
                allowed = {('GET', '/v1/models'), ('POST', '/v1/chat/completions'),
                           ('POST', '/v1/responses')}
                if (self.command, self.path) not in allowed:
                    return self._error(404)
                if (self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site') or
                        self.headers.get('Host') != f'127.0.0.1:{broker._http.server_port}'):
                    return self._error(403)
                if not broker._slots.acquire(blocking=False):
                    return self._error(429)
                try:
                    self._forward()
                finally:
                    broker._slots.release()

            def _forward(self):
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if self.headers.get('Transfer-Encoding') or not 0 <= size <= 1024 * 1024:
                        return self._error(413)
                    body = self.rfile.read(size)
                    if len(body) != size:
                        return self._error(400)
                    self._finish_ingress()
                    auth = self.headers.get('Authorization', '')
                    if not auth.startswith('Bearer ') or len(auth) > 8192:
                        return self._error(401)
                    key = broker.authorize(auth[7:])
                except Exception:
                    return self._error(401)
                # Admission is the revalidation linearization point. Rotation cannot
                # unsend a dispatched request; it prohibits every later admission.
                started = False
                try:
                    with (broker._response_budget(self.connection) as budget,
                          httpx.Client(trust_env=False, follow_redirects=False,
                                       timeout=min(10, broker.RESPONSE_TIMEOUT)) as client):
                        budget.check()
                        with client.stream(self.command, broker.upstream + self.path,
                                content=body, headers={'Authorization': 'Bearer ' + key,
                                                       'Content-Type': 'application/json'},
                                extensions={'trace': budget.trace}) as response:
                            budget.check()
                            if response.status_code != 200:
                                return self._error(502)  # No raw upstream diagnostics or retry.
                            self.send_response(200)
                            media = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                            self.send_header('Content-Type', 'text/event-stream'
                                             if media == 'text/event-stream' else 'application/json')
                            self.send_header('Cache-Control', 'no-store')
                            # Explicit framing makes aborted/oversize/leaking streams
                            # incomplete, never a successful EOF-delimited response.
                            self.send_header('Transfer-Encoding', 'chunked')
                            self.end_headers()
                            started = True
                            total = 0
                            for part in _credential_safe_chunks(response.iter_bytes(), key):
                                budget.check()
                                total += len(part)
                                if total > 8 * 1024 * 1024:
                                    raise ValueError('response_limit')
                                self.wfile.write(f'{len(part):x}\r\n'.encode() + part + b'\r\n')
                                self.wfile.flush()
                            budget.check()
                            self.wfile.write(b'0\r\n\r\n')
                            self.wfile.flush()
                except Exception:
                    if not started:
                        self._error(502)
        return Handler


def serve_native(opts):
    """Explicit foreground lifetime, default root only; never start/stop CPA."""
    import signal
    from hermes_constants import get_hermes_home, get_default_hermes_root
    home = get_hermes_home()
    if not opts.consent or home != get_default_hermes_root():
        print('Subscription broker requires default profile and explicit --consent.')
        return 1
    from .host_compat import require_supported_host
    try:
        # Consumers rely on single-request grants; an older host would cache them.
        require_supported_host()
    except RuntimeError as exc:
        print(str(exc))
        return 1
    stopped = threading.Event()
    old = {}
    try:
        with SubscriptionBroker(home, opts.upstream, port=opts.port) as broker:
            for sig in (signal.SIGTERM, signal.SIGINT):
                old[sig] = signal.signal(sig, lambda *_: stopped.set())
            print('Subscription broker ready at ' + broker.base_url, flush=True)
            stopped.wait()
        return 0
    except Exception:
        print('Subscription broker unavailable; no automatic retry or CPA lifecycle change.')
        return 1
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
