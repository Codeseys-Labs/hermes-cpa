"""Short-lived delegation of one installed plugin's exact native GET resources.

A GET handler can change credentials or spend account entitlements. These grants
are reviewed plugin-code authority, not a read-only proxy or a console lease.
The host presents a different random resource-origin handle to resource scripts.
"""
import base64
import json
import secrets
import threading
import time
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

from .console_protocol import ConsoleError, RESULT_LIMIT, WIRE_LIMIT, encoded, require

RESOURCE_SECONDS = 300
RESOURCE_GRANTS = 32
RESOURCE_INFLIGHT = 2
RESOURCE_BUFFER_BYTES = RESULT_LIMIT + WIRE_LIMIT
RESOURCE_GROUPS = frozenset({'plugins', 'credentials', 'config', 'egress', 'oauth'})
RESOURCE_MARKER = 'resource-delegation'
RESOURCE_PREFIX = '/v0/resource/plugins/'
RESOURCE_HEADERS = frozenset({'content-type', 'content-encoding', 'content-disposition',
                              'content-language', 'etag', 'last-modified'})


def native_resource_path(value):
    """Host sends decoded URL.Path exactly once, matching native registry keys."""
    require(isinstance(value, str) and value.startswith(RESOURCE_PREFIX) and
            len(value) <= 4096 and not any(c in value for c in '\r\n\0'), 'RESOURCE_PATH_DENIED')
    return value


def resource_uri(value):
    try:
        result = quote(native_resource_path(value), safe='/')
    except UnicodeError:
        raise ConsoleError('INVALID_INPUT') from None
    require(len(result) <= 4096, 'PAYLOAD_TOO_LARGE')
    return result


class ConsoleResources:
    def __init__(self, service):
        self.service = service
        # Guarded by the same service gate as console resource reservations.
        self.grants = {}
        self.inflight = 0
        self._slots = threading.BoundedSemaphore(RESOURCE_INFLIGHT)

    def collect(self):
        now = time.monotonic()
        with self.service._gate:
            for token, grant in list(self.grants.items()):
                lease = self.service._leases.get(grant['lease_id'])
                if (grant['expires'] <= now or lease is None or lease['revoked'] or
                        lease['expires'] <= now or grant['generation'] != self.service.owner.generation):
                    del self.grants[token]

    def mint(self, args, lease, value):
        self.collect()
        require(len(self.grants) < RESOURCE_GRANTS, 'BUSY')
        expires = min(time.monotonic() + RESOURCE_SECONDS, lease['expires'])
        require(expires > time.monotonic(), 'LEASE_EXPIRED')
        routes = frozenset(value['resource_paths'])
        require(value['path'] in routes, 'RESOURCE_PATH_DENIED')
        entry_path = resource_uri(value['path'])
        token = secrets.token_urlsafe(32)
        expires_at = min(lease['expires_at'], time.time() + max(0., expires - time.monotonic()))
        self.grants[token] = {
            'lease_id': args['lease_id'], 'scope': dict(lease['scope']), 'generation': lease['generation'],
            'plugin_id': value['plugin_id'], 'incarnation': value['registry_incarnation'],
            'routes': routes, 'expires': expires, 'expires_at': expires_at,
        }
        return {'ok': True, 'kind': 'native-resource', 'grant_id': token,
                'entry_path': entry_path, 'plugin_id': value['plugin_id'], 'expires_at': expires_at}

    def close(self, args):
        require(not set(args) - {'_scope', 'document_id', 'grant_id'})
        token = args.get('grant_id')
        require(isinstance(token, str) and 1 <= len(token) <= 128)
        with self.service._gate:
            grant = self.grants.get(token)
            if grant is not None:
                require(grant['scope'] == args['_scope'], 'STALE_SCOPE')
                del self.grants[token]
        return {'ok': True, 'closed': True}

    def revoke(self, lease_id=None):
        with self.service._gate:
            for token, grant in list(self.grants.items()):
                if lease_id is None or grant['lease_id'] == lease_id:
                    del self.grants[token]

    def _resolve(self, args):
        token = args.get('grant_id')
        require(isinstance(token, str) and 1 <= len(token) <= 128, 'RESOURCE_GONE')
        with self.service._gate:
            grant = self.grants.get(token)
            require(grant is not None, 'RESOURCE_GONE')
            require(grant['scope'] == args['_scope'], 'STALE_SCOPE')
        return token, grant

    def _current(self, token, grant, args, path):
        with self.service._gate:
            require(self.grants.get(token) is grant and grant['expires'] > time.monotonic(), 'RESOURCE_REVOKED')
            require(grant['generation'] == self.service.owner.generation, 'RESOURCE_REVOKED')
            require(path in grant['routes'], 'RESOURCE_PATH_DENIED')
        # Resolve the original core lease internally. It never enters a resource URL
        # or the resource request body sent by the host presentation adapter.
        try:
            self.service._lease({'_scope': args['_scope'], 'document_id': args['document_id'],
                                 'lease_id': grant['lease_id']})
        except ConsoleError as exc:
            if exc.code in ('LEASE_EXPIRED', 'STALE_SCOPE', 'STALE_GENERATION', 'POLICY_LOCKED'):
                with self.service._gate:
                    self.grants.pop(token, None)
            raise
        incarnation, _, routes, _ = self.service._registry_snapshot()
        if (incarnation != grant['incarnation'] or routes.get(('GET', path)) != grant['plugin_id'] or
                any(routes.get(('GET', route)) != grant['plugin_id'] for route in grant['routes'])):
            with self.service._gate:
                self.grants.pop(token, None)
            raise ConsoleError('RESOURCE_REVOKED')

    def _admit(self):
        require(self._slots.acquire(timeout=self.service._timeout(1.)), 'BUSY')
        try:
            with self.service._gate:
                require(not any(group in self.service._active and self.service._active[group] != RESOURCE_MARKER
                                for group in RESOURCE_GROUPS), 'RESOURCE_CONFLICT')
                self.service._space(RESOURCE_BUFFER_BYTES)
                self.inflight += 1
                for group in RESOURCE_GROUPS:
                    self.service._active[group] = RESOURCE_MARKER
        except Exception:
            self._slots.release()
            raise

    def _release(self, invoked):
        with self.service._gate:
            self.inflight -= 1
            # Every invocation may have changed native state. Prepared console
            # mutations must be reviewed again, including after an uncertain reply.
            for group in RESOURCE_GROUPS:
                if invoked:
                    self.service._versions[group] = self.service._versions.get(group, 0) + 1
                if self.inflight == 0 and self.service._active.get(group) == RESOURCE_MARKER:
                    del self.service._active[group]
        self._slots.release()

    @staticmethod
    def _query(value):
        require(isinstance(value, list) and len(value) <= 256)
        size = 0
        query = []
        for pair in value:
            require(isinstance(pair, list) and len(pair) == 2 and
                    all(isinstance(part, str) for part in pair))
            name, text = pair
            size += len(name.encode()) + len(text.encode())
            query.append((name, text))
        require(size <= 65536, 'PAYLOAD_TOO_LARGE')
        require(len(urlencode(query)) <= 65536, 'PAYLOAD_TOO_LARGE')
        return query

    @staticmethod
    def _headers(response, path, grant, query):
        headers = {}
        for name, value in response.headers.items():
            key = name.lower()
            require(isinstance(value, str) and not any(c in value for c in '\r\n\0'), 'INVALID_RESPONSE')
            if key in RESOURCE_HEADERS:
                headers[key] = value
            elif key == 'location':
                try:
                    target = urlsplit(value)
                    require(not target.scheme and not target.netloc and
                            not value.lstrip().startswith('//'), 'REDIRECT_DENIED')
                    base = resource_uri(path)
                    route = target.path or base
                    if not route.startswith('/'):
                        route = base.rsplit('/', 1)[0] + '/' + route
                    # urllib.urljoin drops empty inner path segments. Native route
                    # keys and browser relative URLs preserve them; remove only
                    # literal/percent-encoded dot segments, not duplicate slashes.
                    parts, resolved = route.split('/'), []
                    for index, part in enumerate(parts):
                        dot = part.lower().replace('%2e', '.')
                        if dot in ('.', '..'):
                            if dot == '..' and len(resolved) > 1:
                                resolved.pop()
                            if index == len(parts) - 1:
                                resolved.append('')
                        else:
                            resolved.append(part)
                    native_path = unquote('/'.join(resolved), errors='strict')
                    require(native_path in grant['routes'], 'REDIRECT_DENIED')
                    target_query = target.query
                    if not target.path and '?' not in value.split('#', 1)[0]:
                        target_query = urlencode(query)
                    headers['location'] = urlunsplit(('', '', resource_uri(native_path), target_query, target.fragment))
                except (ValueError, UnicodeError):
                    raise ConsoleError('REDIRECT_DENIED') from None
        # The presentation host recomputes Content-Length, supplies origin-bound
        # CSP/CORS and strips browser credentials. Never trust native policies.
        headers.update({'cache-control': 'no-store', 'referrer-policy': 'no-referrer',
                        'x-content-type-options': 'nosniff'})
        return headers

    def fetch(self, args):
        invoked, admitted, observed = False, False, False
        try:
            require(not set(args) - {'_scope', 'document_id', 'grant_id', 'path', 'query', 'accept'})
            token, grant = self._resolve(args)
            path = native_resource_path(args.get('path'))
            target = resource_uri(path)
            query = self._query(args.get('query', []))
            accept = args.get('accept', '*/*')
            require(isinstance(accept, str) and len(accept) <= 4096 and
                    not any(c in accept for c in '\r\n\0'))
            self._admit()
            admitted = True
            self._current(token, grant, args, path)
            timeout = self.service._timeout(2.)
            invoked = True
            # Exactly one call. Neither native redirects nor ambiguous failures
            # are followed/replayed by this endpoint.
            response = self.service.transport.request('GET', target, query=query,
                headers={'Accept': accept, 'X-Hermes-Resource-Revision': grant['incarnation']},
                timeout=timeout, max_response=RESULT_LIMIT)
            observed = True
            require(100 <= response.status <= 599 and len(response.body) <= RESULT_LIMIT, 'RESPONSE_TOO_LARGE')
            native_headers = {name.lower(): value for name, value in response.headers.items()}
            invocation = native_headers.get('x-hermes-resource-invoked')
            if invocation == '0':
                invoked = False  # Only the native host can write this marker.
                if response.status == 412:
                    try:
                        rejection = json.loads(response.body)
                    except (ValueError, UnicodeError):
                        raise ConsoleError('INVALID_RESPONSE') from None
                    require(isinstance(rejection, dict) and
                            rejection.get('error') == 'stale_resource_revision', 'INVALID_RESPONSE')
                else:
                    require(response.status == 404, 'INVALID_RESPONSE')
                with self.service._gate:
                    self.grants.pop(token, None)
                raise ConsoleError('RESOURCE_REVOKED')
            # A missing/duplicated marker is uncertainty, never evidence that an
            # apparent native 4xx/5xx happened before an effectful GET handler.
            require(invocation == '1', 'OUTCOME_UNKNOWN')
            if native_headers.get('x-hermes-resource-revision') != grant['incarnation']:
                with self.service._gate:
                    self.grants.pop(token, None)
                raise ConsoleError('RESOURCE_REVOKED')
            headers = self._headers(response, path, grant, query)
            result = {'ok': True, 'status': response.status, 'headers': headers,
                      'body_base64': base64.b64encode(response.body).decode()}
            require(len(result['body_base64']) + len(encoded(headers)) + 128 < WIRE_LIMIT - 65536,
                    'RESPONSE_TOO_LARGE')
            # Encoding can take time for a large resource; close/expiry during it
            # must not reveal bytes under the former grant.
            self._current(token, grant, args, path)
            return result
        except ConsoleError as exc:
            return {'ok': False, 'code': exc.code, 'effect_invoked': invoked,
                    'response_withheld': invoked and observed}
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if code in ('UNAVAILABLE', 'INVALID_REQUEST') and not observed:
                invoked = False  # Transport certifies that no request bytes were sent.
            if code == 'RESPONSE_TOO_LARGE':
                observed = True  # A native response was received, but cannot be exposed.
            if code not in ('UNAVAILABLE', 'OUTCOME_UNKNOWN', 'RESPONSE_TOO_LARGE', 'INVALID_REQUEST'):
                code = 'OUTCOME_UNKNOWN' if invoked else 'UNAVAILABLE'
            return {'ok': False, 'code': code, 'effect_invoked': invoked,
                    'response_withheld': invoked and observed}
        finally:
            if admitted:
                self._release(invoked)
