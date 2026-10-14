"""Private owner-only HTTP channels to the exact CPA child this supervisor launched.

The pinned integration build serves two Unix sockets inside a 0700 directory:
control (native management routes plus /hermes/*) and inference (exactly the
broker's three /v1 routes). Neither channel carries a bearer key; the Go side
authenticates this supervisor by SO_PEERCRED. In the other direction every
connection here must reach the live owned child: its SO_PEERCRED pid and the
/proc start identity captured at launch are compared before a request byte is
sent. A different listener at the same path is refused even if it answers.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from .process_owner import process_identity, socket_address

# Console IPC frame (encoded JSON bytes) and console HTTP body bounds. One
# 10 MiB upload is ~13.4 MiB of base64 inside its JSON frame.
CONSOLE_IPC_LIMIT = 48 * 1024 * 1024
CONSOLE_BODY_LIMIT = 32 * 1024 * 1024
CONSOLE_FRAME_SECONDS = 30.
# Ordinary owner IPC keeps its historical bounds.
ORDINARY_REQUEST_LIMIT = 64 * 1024
ORDINARY_REPLY_LIMIT = 1024 * 1024
# One explicit bound for a whole CPA config document (full YAML is native
# parity data); prepare/commit envelopes carry it base64-encoded.
CONFIG_LIMIT = CONSOLE_BODY_LIMIT
PREPARE_RESPONSE_LIMIT = CONSOLE_IPC_LIMIT
INFERENCE_BODY_LIMIT = 1024 * 1024
# CPA-relative request target bounds shared with the console service: the path
# and the total percent-encoded query. No narrower per-value cutoff exists.
PATH_LIMIT = 4096
QUERY_LIMIT = 65536
QUERY_PAIRS = 256

CONTROL_SOCKET = 'control.sock'
INFERENCE_SOCKET = 'inference.sock'
INFERENCE_ROUTES = frozenset({('GET', '/v1/models'), ('POST', '/v1/chat/completions'),
                              ('POST', '/v1/responses')})
METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE'})
# Owner code may add only these; authority never comes from caller headers.
CALLER_HEADERS = {'accept': 'Accept', 'x-hermes-egress-policy': 'X-Hermes-Egress-Policy'}
# Pins the reviewed registry revision; Go checks it atomically with the
# resource, declared management, fixed quota or plugin OAuth provider lookup.
RESOURCE_REVISION_HEADER = 'X-Hermes-Resource-Revision'
PLUGIN_REVISION_HEADER = 'X-Hermes-Plugin-Revision'
RESOURCE_PREFIX = '/v0/resource/plugins/'
# Native registry revision grammar: 16 hex nonce, '-', positive decimal
# counter; or "none-0". The existing resource and plugin contracts share it.
_REGISTRY_REVISION = re.compile(r'(?:[0-9a-f]{16}-[1-9][0-9]{0,63}|none-0)\Z')
# Host-set response facts the console relies on; a duplicate is never trusted.
SINGLE_VALUED_RESPONSE = frozenset({'x-hermes-resource-invoked', 'x-hermes-resource-revision',
                                    'x-hermes-plugin-invoked', 'x-hermes-plugin-revision',
                                    'x-hermes-config-revision'})
PREPARE_CODES = frozenset({'STALE_REVISION', 'INVALID_REQUEST', 'INVALID_CONFIG', 'NOT_FOUND',
                           'READ_ONLY_FIELD', 'READ_FAILED'})
REVISION = re.compile(r'[0-9a-f]{64}\Z')
_SEGMENT = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})+\Z")
_ENCODED_SEPARATOR = re.compile(r'%(?:2e|2f|5c|00)', re.IGNORECASE)
_TOKEN = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_BASE64 = re.compile(r'[A-Za-z0-9+/]*={0,2}\Z')


class TransportError(Exception):
    """Stable code only; never upstream text, headers or bodies."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b''


def _route_view(path):
    """Decoded path with repeated slashes collapsed, for conservative guard checks."""
    return re.sub('/+', '/', unquote(path))


def config_route(path):
    """Native config document routes; mutations go through owner transactions."""
    decoded = _route_view(path)
    return (decoded in ('/v8/management/config', '/v8/management/config.yaml') or
            decoded.startswith('/v8/management/config/'))


def owner_route(path):
    decoded = _route_view(path)
    return decoded == '/hermes' or decoded.startswith('/hermes/')


def resource_route(path):
    """Native plugin resource routes (pluginhost resourcePluginBasePath)."""
    return _route_view(path).startswith(RESOURCE_PREFIX)


def request_target(path, query=None):
    """CPA-relative absolute path plus encoded query; no scheme, host or dot segments.

    Inner empty segments are kept because native plugin resource routes may
    register them exactly; a leading ``//`` (network-path reference) is refused.
    """
    if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or
            len(path) > PATH_LIMIT):
        raise TransportError('INVALID_REQUEST')
    for segment in path[1:].split('/'):
        if segment == '':
            continue  # Root, inner duplicate or trailing slash; native routing decides.
        if (segment in ('.', '..') or not _SEGMENT.match(segment) or
                _ENCODED_SEPARATOR.search(segment)):
            raise TransportError('INVALID_REQUEST')
    if query is None:
        return path
    pairs = list(query.items()) if isinstance(query, dict) else query
    if not isinstance(pairs, list) or len(pairs) > QUERY_PAIRS:
        raise TransportError('INVALID_REQUEST')
    clean, raw = [], 0
    for pair in pairs:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2 or
                not all(isinstance(item, str) for item in pair)):
            raise TransportError('INVALID_REQUEST')
        # Encoding never shrinks text: bound raw size before allocating the encoding.
        raw += len(pair[0]) + len(pair[1]) + 2
        if raw > QUERY_LIMIT + 1:
            raise TransportError('INVALID_REQUEST')
        clean.append(tuple(pair))
    # Every byte (newline, NUL, '&', '=') is percent-encoded; duplicates keep order.
    try:
        encoded = urlencode(clean, quote_via=quote, safe='')
    except UnicodeEncodeError:  # Lone surrogates from JSON are not valid query text.
        raise TransportError('INVALID_REQUEST') from None
    if len(encoded) > QUERY_LIMIT:
        raise TransportError('INVALID_REQUEST')
    return path + ('?' + encoded if encoded else '')


def connect_private(directory, name, expected, *, timeout, register=None):
    """Connect to directory/name and require the peer to be exactly ``expected``."""
    if not isinstance(expected, dict) or timeout <= 0:
        raise TransportError('UNAVAILABLE')
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if register is not None:
            register(sock)  # Deadline owners may interrupt connect and every read.
        sock.settimeout(timeout)
        with socket_address(directory, name) as address:
            sock.connect(address)
        pid, uid, _ = struct.unpack('3i', sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        if uid != os.getuid() or pid != expected.get('pid') or process_identity(pid) != expected:
            raise TransportError('UNAVAILABLE')
        return sock
    except TransportError:
        sock.close()
        raise
    except Exception:
        sock.close()
        raise TransportError('UNAVAILABLE') from None


class _PeerConnection(http.client.HTTPConnection):
    """HTTP/1.1 on an already verified socket; never reconnects by itself."""
    def __init__(self, sock):
        super().__init__('localhost')
        self.sock = sock

    def connect(self):
        raise TransportError('UNAVAILABLE')


class _Expiry:
    """Total wall bound: shut the socket down even while bytes trickle in."""
    def __init__(self, sock, deadline):
        self._sock = sock
        self._timer = threading.Timer(max(0., deadline - time.monotonic()), self._expire)
        self._timer.daemon = True

    def _expire(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *args):
        self._timer.cancel()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('transport_deadline')
    return remaining


class PrivateChannel:
    def __init__(self, directory, name, peer):
        self.directory, self.name = Path(directory), name
        self._peer = peer  # Returns the live owned child's identity, or None.

    def _exchange(self, method, target, headers, body, *, timeout, max_response):
        if timeout <= 0:
            raise TransportError('UNAVAILABLE')
        deadline = time.monotonic() + timeout
        sock = connect_private(self.directory, self.name, self._peer(), timeout=timeout)
        connection = _PeerConnection(sock)
        sent = False
        try:
            with _Expiry(sock, deadline):
                sock.settimeout(_remaining(deadline))
                connection.putrequest(method, target, skip_accept_encoding=True)
                for name, value in headers:
                    connection.putheader(name, value)
                connection.putheader('Content-Length', str(len(body)))
                connection.putheader('Connection', 'close')
                sent = True
                connection.endheaders(body or None)
                sock.settimeout(_remaining(deadline))
                response = connection.getresponse()
                length = response.getheader('Content-Length')
                if length is not None and (not length.isdigit() or int(length) > max_response):
                    raise TransportError('RESPONSE_TOO_LARGE')
                data = bytearray()
                while not response.isclosed():
                    sock.settimeout(_remaining(deadline))
                    chunk = response.read(min(65536, max_response + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > max_response:
                        raise TransportError('RESPONSE_TOO_LARGE')
                headers, repeated = {}, set()
                for name, value in response.getheaders():
                    name = name.lower()
                    if name in headers and name in SINGLE_VALUED_RESPONSE:
                        repeated.add(name)
                    if name == 'content-encoding' and name in headers:
                        # Stacked codings (gzip then br) are order-significant:
                        # RFC 9110 5.3 combines repeated fields in wire order.
                        value = f'{headers[name]}, {value}'
                    headers[name] = value
                for name in repeated:
                    del headers[name]  # Ambiguous: report absent, never pick one.
                return TransportResponse(response.status, headers, bytes(data))
        except TransportError:
            raise
        except Exception:
            raise TransportError('OUTCOME_UNKNOWN' if sent else 'UNAVAILABLE') from None
        finally:
            connection.close()


class ControlChannel(PrivateChannel):
    """Native management HTTP for trusted owner code (console service)."""

    def __init__(self, directory, peer):
        super().__init__(directory, CONTROL_SOCKET, peer)

    def request(self, method, path, *, query=None, body=b'', content_type=None, headers=None,
                timeout=10., max_response=CONSOLE_BODY_LIMIT):
        if method not in METHODS or not isinstance(body, (bytes, bytearray)):
            raise TransportError('INVALID_REQUEST')
        target = request_target(path, query)
        # Config writes and owner endpoints (prepare/commit) belong to the
        # supervisor's gated transaction; read-only owner routes stay usable.
        if method != 'GET' and (owner_route(path) or config_route(path)):
            raise TransportError('CONFIG_TRANSACTION_REQUIRED')
        if (len(body) > CONSOLE_BODY_LIMIT or type(max_response) is not int or
                not 0 < max_response <= CONSOLE_BODY_LIMIT or
                (headers is not None and not isinstance(headers, dict))):
            raise TransportError('INVALID_REQUEST')
        wire = []
        if content_type is not None:
            if (not isinstance(content_type, str) or not 0 < len(content_type) <= 256 or
                    any(ord(c) < 32 or ord(c) > 126 for c in content_type)):
                raise TransportError('INVALID_REQUEST')
            wire.append(('Content-Type', content_type))
        seen = set()
        for name, value in (headers or {}).items():
            key = name.lower() if isinstance(name, str) else None
            canonical = CALLER_HEADERS.get(key)
            if key == RESOURCE_REVISION_HEADER.lower():
                if (method != 'GET' or not resource_route(path) or
                        not isinstance(value, str) or not _REGISTRY_REVISION.match(value)):
                    raise TransportError('INVALID_REQUEST')
                canonical = RESOURCE_REVISION_HEADER
            elif key == PLUGIN_REVISION_HEADER.lower():
                route = _route_view(path)
                plugin_route = (route.startswith('/v0/management/') or
                    method == 'GET' and route == '/v8/management/oauth/auth-url' or
                    method in ('GET', 'POST', 'DELETE') and
                    re.fullmatch(r'/v8/management/plugins/[^/]+/quota', route) is not None)
                if (not plugin_route or not isinstance(value, str) or not _REGISTRY_REVISION.match(value)):
                    raise TransportError('INVALID_REQUEST')
                canonical = PLUGIN_REVISION_HEADER
            if (canonical is None or canonical in seen or not isinstance(value, str) or
                    len(value) > 16384 or any(ord(c) < 32 or ord(c) > 126 for c in value)):
                raise TransportError('INVALID_REQUEST')
            seen.add(canonical)
            wire.append((canonical, value))
        if resource_route(path) and RESOURCE_REVISION_HEADER not in seen:
            raise TransportError('INVALID_REQUEST')
        return self._exchange(method, target, wire, bytes(body), timeout=timeout,
                              max_response=max_response)

    def probe(self, timeout=1.):
        """Typed readiness from the exact child's private health route."""
        from .cpa_adapter import ProbeResult
        try:
            response = self._exchange('GET', '/hermes/health', [('Accept', 'application/json')],
                                      b'', timeout=timeout, max_response=4096)
        except TransportError:
            return ProbeResult()
        if response.status != 200:
            return ProbeResult()
        try:
            data = json.loads(response.body)
        except ValueError:
            return ProbeResult(reason='INCOMPATIBLE')
        if (not isinstance(data, dict) or type(data.get('protocol')) is not int or
                data['protocol'] != 1 or data.get('integration') != 'hermes-owner-v1' or
                data.get('cpa_version') != '8.0.21' or
                not isinstance(data.get('config_revision'), str) or
                not REVISION.match(data['config_revision'])):
            return ProbeResult(reason='INCOMPATIBLE')
        return ProbeResult(True, None, True, 'OK')

    def prepare(self, payload, *, timeout):
        """Owner-only native candidate preparation; returns (status, body)."""
        response = self._exchange('POST', '/hermes/config/prepare',
                                  [('Content-Type', 'application/json'), ('Accept', 'application/json')],
                                  payload, timeout=timeout, max_response=PREPARE_RESPONSE_LIMIT)
        return response.status, response.body

    def commit(self, expected_revision, candidate_revision, candidate, *, timeout):
        """Owner-only native live compare-and-commit of a prepared candidate."""
        payload = json.dumps({'expected_revision': expected_revision,
                              'candidate_revision': candidate_revision,
                              'candidate_base64': base64.b64encode(candidate).decode('ascii')},
                             separators=(',', ':')).encode()
        response = self._exchange('POST', '/hermes/config/commit',
                                  [('Content-Type', 'application/json'), ('Accept', 'application/json')],
                                  payload, timeout=timeout, max_response=65536)
        return response.status, response.body


class _InferenceResponse:
    """Minimal streaming view shared with the broker's httpx path."""
    def __init__(self, response):
        self._response = response
        self.status_code = response.status
        self.headers = response.headers  # Case-insensitive lookup.

    def iter_bytes(self):
        while True:
            part = self._response.read1(65536)
            if not part:
                return
            yield part


class InferenceChannel(PrivateChannel):
    """The broker's only upstream in managed mode; inference routes only."""

    def __init__(self, directory, peer):
        super().__init__(directory, INFERENCE_SOCKET, peer)

    @contextmanager
    def stream(self, method, path, body, *, timeout, register=None):
        if (method, path) not in INFERENCE_ROUTES or len(body) > INFERENCE_BODY_LIMIT:
            raise TransportError('INVALID_REQUEST')
        sock = connect_private(self.directory, self.name, self._peer(), timeout=timeout,
                               register=register)
        connection = _PeerConnection(sock)
        sent = False
        try:
            sock.settimeout(timeout)
            connection.putrequest(method, path, skip_accept_encoding=True)
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', str(len(body)))
            connection.putheader('Connection', 'close')
            sent = True
            connection.endheaders(body or None)
            response = connection.getresponse()
        except TransportError:
            connection.close()
            raise
        except Exception:
            connection.close()
            raise TransportError('OUTCOME_UNKNOWN' if sent else 'UNAVAILABLE') from None
        try:
            yield _InferenceResponse(response)
        finally:
            connection.close()

    def request(self, method, path, *, body=b'', timeout=10., max_response=CONSOLE_BODY_LIMIT):
        """Buffered inference call (e.g. model discovery) for trusted owner code."""
        if ((method, path) not in INFERENCE_ROUTES or not isinstance(body, (bytes, bytearray)) or
                len(body) > INFERENCE_BODY_LIMIT or type(max_response) is not int or
                not 0 < max_response <= CONSOLE_BODY_LIMIT):
            raise TransportError('INVALID_REQUEST')
        return self._exchange(method, path, [('Content-Type', 'application/json'),
                                             ('Accept', 'application/json')],
                              bytes(body), timeout=timeout, max_response=max_response)


def encode_prepare(expected_revision, requests):
    """Structural Go wire envelope; native preparation owns every config rule."""
    if not isinstance(expected_revision, str) or not REVISION.match(expected_revision):
        raise TransportError('STALE_REVISION')
    if not isinstance(requests, list) or not 1 <= len(requests) <= 64:
        raise TransportError('INVALID_REQUEST')
    clean = []
    limit = 4 * ((CONFIG_LIMIT + 2) // 3)
    for item in requests:
        if (not isinstance(item, dict) or
                set(item) - {'method', 'path', 'query', 'content_type', 'body_base64'} or
                item.get('method') not in ('PUT', 'PATCH', 'DELETE')):
            raise TransportError('INVALID_REQUEST')
        path = item.get('path')
        request_target(path)
        if not config_route(path):
            raise TransportError('INVALID_REQUEST')
        entry = {'method': item['method'], 'path': path}
        content_type = item.get('content_type')
        if content_type is not None:
            if content_type not in ('application/json', 'application/yaml'):
                raise TransportError('INVALID_REQUEST')
            entry['content_type'] = content_type
        body = item.get('body_base64')
        if body is not None:
            if not isinstance(body, str) or len(body) > limit or not _BASE64.match(body):
                raise TransportError('INVALID_REQUEST')
            entry['body_base64'] = body
        clean.append(entry)
    payload = json.dumps({'expected_revision': expected_revision, 'requests': clean},
                         separators=(',', ':')).encode()
    if len(payload) > CONSOLE_IPC_LIMIT:
        raise TransportError('INVALID_REQUEST')
    return payload


def decode_prepare(status, body):
    """Validate a native prepare reply (live or offline) into owner terms."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return {'ok': False, 'code': 'UNAVAILABLE'}
    if not isinstance(data, dict):
        return {'ok': False, 'code': 'UNAVAILABLE'}
    if status == 200:
        base, revision = data.get('base_revision'), data.get('candidate_revision')
        encoded, effects, paths = (data.get(k) for k in ('candidate_base64', 'effects', 'changed_paths'))
        if (not isinstance(base, str) or not REVISION.match(base) or
                not isinstance(revision, str) or not REVISION.match(revision) or
                not isinstance(encoded, str) or not isinstance(effects, list) or
                not isinstance(paths, list) or len(effects) > 64 or len(paths) > 4096 or
                any(not isinstance(e, str) or not _TOKEN.match(e) for e in effects) or
                any(not isinstance(p, str) or not 0 < len(p) <= 1024 for p in paths)):
            return {'ok': False, 'code': 'UNAVAILABLE'}
        if len(encoded) > 4 * ((CONFIG_LIMIT + 2) // 3):
            return {'ok': False, 'code': 'INVALID_CONFIG', 'detail': {'error': 'config_too_large'}}
        try:
            candidate = base64.b64decode(encoded, validate=True)
        except ValueError:
            return {'ok': False, 'code': 'UNAVAILABLE'}
        if hashlib.sha256(candidate).hexdigest() != revision:
            return {'ok': False, 'code': 'UNAVAILABLE'}
        return {'ok': True, 'base_revision': base, 'candidate_revision': revision,
                'candidate': candidate, 'effects': list(effects), 'changed_paths': list(paths)}
    code = data.get('code') if data.get('code') in PREPARE_CODES else 'UNAVAILABLE'
    detail = {'status': status} if type(status) is int and status > 0 else {}
    for key, kind, limit in (('status', int, None), ('error', str, 64), ('field', str, 256),
                             ('message', str, 2048), ('request_index', int, None),
                             ('base_revision', str, 64)):
        value = data.get(key)
        if type(value) is kind and (limit is None or len(value) <= limit):
            detail[key] = value
    return {'ok': False, 'code': code, 'detail': detail}


def read_line(conn, limit, deadline):
    """First newline-terminated line (≤ ``limit`` bytes) and the bytes read past it."""
    raw = bytearray()
    while True:
        index = raw.find(b'\n')
        if index >= 0:
            return bytes(raw[:index + 1]), bytes(raw[index + 1:])
        if len(raw) >= limit:
            raise ValueError('frame_too_large')
        conn.settimeout(_remaining(deadline))
        chunk = conn.recv(min(65536, limit - len(raw)))
        if not chunk:
            raise ValueError('truncated_frame')
        raw.extend(chunk)


def read_exact(conn, size, deadline, prefix=b''):
    """Exactly ``size`` bytes, allocated only after the caller bounded ``size``."""
    if len(prefix) > size:
        raise ValueError('invalid_frame')
    data = bytearray(size)
    data[:len(prefix)] = prefix
    view, offset = memoryview(data), len(prefix)
    while offset < size:
        conn.settimeout(_remaining(deadline))
        count = conn.recv_into(view[offset:], min(1024 * 1024, size - offset))
        if not count:
            raise ValueError('truncated_frame')
        offset += count
    return bytes(data)


def frame_length(line):
    """Validated length from a console frame header line."""
    header = json.loads(line)
    length = header.get('length') if isinstance(header, dict) else None
    if type(length) is not int or not 0 <= length <= CONSOLE_IPC_LIMIT:
        raise ValueError('invalid_frame')
    return header, length


def frame_console(cmd, args):
    """Length-framed console IPC message; raises ValueError past the bound."""
    body = json.dumps(args, separators=(',', ':'), allow_nan=False).encode()
    if len(body) > CONSOLE_IPC_LIMIT:
        raise ValueError('frame_too_large')
    return json.dumps({'cmd': cmd, 'length': len(body)}).encode() + b'\n' + body
