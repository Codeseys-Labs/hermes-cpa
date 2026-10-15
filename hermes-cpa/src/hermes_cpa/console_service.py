"""Scoped native console sessions owned by the existing supervisor.

Only opaque review references enter the supervisor's accepted-operation ledger.
Requests, OAuth states, candidate reviews and native results live here temporarily.
"""
import base64
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

from .console_protocol import (ConsoleError, PREFIX, RESULT_LIMIT, WIRE_LIMIT,
                               classify, content_type, decode_bytes, encoded, identifier,
                               management_response, parse_request, part_name, path_value, require)
from . import console_quota
from .console_resources import ConsoleResources, RESOURCE_BUFFER_BYTES, native_resource_path

LEASE_SECONDS = 30 * 60
REVIEW_SECONDS = 120
RESULT_SECONDS = 10 * 60
PRIVATE_BYTES = 192 * 1024 * 1024
MAX_LEASES = 16
MAX_REVIEWS = 64
MAX_RESULTS = 64
# Keep these aligned with StartOAuthV8's fixed native provider switch. Other
# provider names dispatch through the plugin registry, even without HTTP routes.
BUILTIN_OAUTH_PROVIDERS = frozenset({
    'claude', 'codex', 'antigravity', 'kimi', 'kimi-ai', 'xai', 'devin', 'meta'})
BUILTIN_OAUTH_PATHS = frozenset('/v0/management/' + provider + '-auth-url'
    for provider in (BUILTIN_OAUTH_PROVIDERS - {'claude'}) | {'anthropic'})


def _builtin_oauth(request, provider):
    return provider is not None and (
        request.path == PREFIX + '/oauth/auth-url' and provider.strip().lower() in BUILTIN_OAUTH_PROVIDERS or
        unquote(request.path) in BUILTIN_OAUTH_PATHS)


def _quota_plugin(request):
    if request.method not in ('GET', 'POST', 'DELETE'):
        return None
    match = re.fullmatch(PREFIX + r'/plugins/([^/]+)/quota', request.path)
    return unquote(match[1]).strip() if match else None


def unlock_intent(args):
    return {'action': 'console.unlock', 'request_id': args['request_id'],
            'expected_generation': args['expected_generation'], 'consent': True,
            '_scope': args['_scope']}


def execute_intent(args, generation):
    return {'action': 'console.execute', 'request_id': args['request_id'],
            'expected_generation': generation, 'lease_id': args['lease_id'],
            'review_id': args['review_id'], '_scope': args['_scope']}


def _digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class OwnerConsoleService:
    def __init__(self, owner, transport):
        self.owner, self.transport = owner, transport
        self._gate = threading.RLock()
        self._leases = {}
        self._reviews = {}
        self._results = {}
        self._active = {}
        self._versions = {}
        self._oauth = {}
        self._oauth_starting = 0
        self._closed = False
        self._call = threading.local()
        self.resources = ConsoleResources(self)
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._watch_leases, daemon=True)
        self._monitor.start()

    def _scope(self, args):
        scope = args.get('_scope')
        require(isinstance(scope, dict) and set(scope) == {
            'principal', 'home', 'scope_id', 'connection_id', 'selection_epoch', 'document_id'}, 'STALE_SCOPE')
        require(all(isinstance(scope[k], str) and 0 < len(scope[k]) <= 4096
                    for k in ('principal', 'home', 'scope_id', 'connection_id', 'document_id')), 'STALE_SCOPE')
        require(type(scope['selection_epoch']) is int and scope['selection_epoch'] >= 0, 'STALE_SCOPE')
        require(scope['home'] == str(self.owner.settings.home), 'STALE_SCOPE')
        try:
            require(str(uuid.UUID(scope['document_id'])) == scope['document_id'], 'STALE_SCOPE')
        except ValueError:
            raise ConsoleError('STALE_SCOPE') from None
        require(args.get('document_id', scope['document_id']) == scope['document_id'], 'STALE_SCOPE')
        return scope

    def _policy(self, lease=None):
        require(not self._closed and self.owner.operation_current(), 'POLICY_LOCKED')
        if lease is not None:
            policy = getattr(self.owner, 'policy', None)
            if lease['policy'] is not None:
                require(policy is not None and policy.matches(lease['policy']), 'POLICY_LOCKED')
            if lease.get('grant'):
                from .operation_guard import revalidate_grant
                require(revalidate_grant(lease['grant'], lease['intent'], timeout=self._timeout(1.)), 'STALE_SCOPE')

    def _lease(self, args, *, receipt=False):
        scope = self._scope(args)
        lease_id = args.get('lease_id')
        require(isinstance(lease_id, str), 'LEASE_REQUIRED')
        with self._gate:
            lease = self._leases.get(lease_id)
            require(lease is not None and lease['scope'] == scope, 'STALE_SCOPE')
            if not receipt:
                require(not lease['revoked'] and time.monotonic() < lease['expires'], 'LEASE_EXPIRED')
                require(lease['generation'] == self.owner.generation, 'STALE_GENERATION')
        if not receipt:
            self._policy(lease)
        return lease

    def _collect(self, *, cancel=False):
        now = time.monotonic()
        with self._gate:
            for key, review in list(self._reviews.items()):
                operation = review.get('accepted')
                retained = operation and operation in self._results
                if review['expires'] <= now and not retained:
                    del self._reviews[key]
            for key, result in list(self._results.items()):
                if result['expires'] <= now and result['state'] not in ('pending', 'running'):
                    del self._results[key]
            expired_states = [state for state, record in self._oauth.items()
                              if record['expires'] <= now or record['lease_id'] not in self._leases or
                              self._leases[record['lease_id']]['revoked'] or
                              self._leases[record['lease_id']]['expires'] <= now]
            for key, lease in list(self._leases.items()):
                if (lease['revoked'] or lease['expires'] <= now) and not any(
                        r['lease_id'] == key for r in self._reviews.values()):
                    del self._leases[key]
        self.resources.collect()
        if cancel:
            self._cancel_states(expired_states)

    def _watch_leases(self):
        while not self._stop.wait(1.):
            with self._gate:
                leases = [lease for lease in self._leases.values() if not lease['revoked']]
            for lease in leases:
                if self._stop.is_set():
                    return
                try:
                    self._policy(lease)
                    current = lease['expires'] > time.monotonic() and lease['generation'] == self.owner.generation
                except Exception:
                    current = False
                if not current:
                    with self._gate:
                        lease['revoked'] = True
            self._collect(cancel=True)

    def _space(self, additional=0):
        size = sum(r['size'] for r in self._reviews.values())
        size += sum(r['size'] for r in self._results.values())
        size += self.resources.inflight * RESOURCE_BUFFER_BYTES
        require(size + additional <= PRIVATE_BYTES, 'BUSY')

    def _timeout(self, seconds):
        deadline = getattr(self._call, 'deadline', None)
        remaining = seconds if deadline is None else min(seconds, deadline - time.monotonic())
        require(remaining > 0, 'UNAVAILABLE')
        return self.owner.authorized_timeout(remaining)

    def dispatch(self, op, args):
        previous_deadline = getattr(self._call, 'deadline', None)
        deadline = time.monotonic() + 3.5
        self._call.deadline = min(deadline, previous_deadline) if previous_deadline is not None else deadline
        try:
            require(isinstance(args, dict))
            self._scope(args)
            self._collect()
            if op == 'unlock':
                return self._unlock(args)
            if op == 'resource_close':
                return self.resources.close(args)
            if op == 'resource':
                return self.resources.fetch(args)
            if op == 'authority_terminal':
                with self._gate:
                    lease = next((v for v in self._leases.values()
                                  if v['intent']['request_id'] == args.get('request_id') and
                                  v['scope'] == args['_scope']), None)
                    return {'terminal': lease is None or lease['revoked'] or lease['expires'] <= time.monotonic()}
            lease = self._lease(args, receipt=op == 'result')
            if op == 'lock':
                return self._lock(args, lease)
            if op == 'prepare':
                return self._prepare(args, lease)
            if op == 'execute':
                return self._execute(args, lease)
            if op == 'result':
                return self._result(args, lease)
            if op in ('browser', 'plugin', 'file'):
                return self._capability(op, args, lease)
            raise ConsoleError('INVALID_INPUT')
        except ConsoleError as exc:
            return {'ok': False, 'code': exc.code}
        except Exception:
            # Native errors may contain provider secrets or private file paths.
            return {'ok': False, 'code': 'UNAVAILABLE'}
        finally:
            self._call.deadline = previous_deadline

    def _unlock(self, args):
        identifier(args.get('request_id'))
        require(args.get('consent') is True, 'CONSENT_REQUIRED')
        require(type(args.get('expected_generation')) is int and
                args['expected_generation'] == self.owner.generation, 'STALE_GENERATION')
        self._policy()
        intent = unlock_intent(args)
        grant = args.get('_host_grant')
        if grant:
            from .operation_guard import revalidate_grant
            require(revalidate_grant(grant, intent, timeout=self._timeout(1.)), 'STALE_SCOPE')
        policy = getattr(self.owner, 'policy', None)
        captured = policy.capture() if hasattr(policy, 'capture') else None
        require(not hasattr(policy, 'capture') or captured is not None, 'POLICY_LOCKED')
        with self._gate:
            for key, lease in self._leases.items():
                if lease['intent']['request_id'] == intent['request_id']:
                    require(lease['intent'] == intent and not lease['revoked'] and
                            lease['expires'] > time.monotonic(), 'REQUEST_CONFLICT')
                    return self._lease_response(key, lease)
            require(len(self._leases) < MAX_LEASES, 'BUSY')
            # A new verified mount cannot adopt the previous mount's authority.
            for lease in self._leases.values():
                if (lease['scope']['principal'] == args['_scope']['principal'] and
                        lease['scope']['connection_id'] == args['_scope']['connection_id'] and
                        lease['scope']['document_id'] != args['_scope']['document_id']):
                    lease['revoked'] = True
            lease_id = secrets.token_urlsafe(32)
            lease = {'scope': dict(args['_scope']), 'generation': self.owner.generation,
                     'expires': time.monotonic() + LEASE_SECONDS, 'expires_at': time.time() + LEASE_SECONDS,
                     'revoked': False, 'policy': captured, 'intent': intent, 'grant': grant,
                     'quota_grants': {}}
            self._leases[lease_id] = lease
            return self._lease_response(lease_id, lease)

    def _lease_response(self, key, lease):
        return {'ok': True, 'lease_id': key, 'generation': lease['generation'],
                'document_id': lease['scope']['document_id'], 'expires_at': lease['expires_at']}

    def _lock(self, args, lease):
        lease_id = args['lease_id']
        with self._gate:
            lease['revoked'] = True
            self.resources.revoke(lease_id)
            for key, review in list(self._reviews.items()):
                if review['lease_id'] == lease_id and not review.get('accepted'):
                    del self._reviews[key]
            states = [key for key, record in self._oauth.items() if record['lease_id'] == lease_id]
        # Cancel only states this lease started, never unrelated native sessions.
        self._cancel_states(states)
        return {'ok': True, 'locked': True, 'accepted_operations_retained': True}

    def _revision(self):
        from .config_store import ConfigStore
        return ConfigStore(self.owner.settings.config_path).compute_digest()

    def _registry_snapshot(self):
        response = self.transport.request('GET', '/hermes/plugin-routes', timeout=self._timeout(1.),
                                          max_response=RESULT_LIMIT)
        require(response.status == 200, 'UNAVAILABLE')
        value = json.loads(response.body)
        incarnation = value.get('registry_revision')
        require(isinstance(incarnation, str) and 0 < len(incarnation) <= 256, 'UNAVAILABLE')
        routes = {'management': {}, 'resources': {}}
        for category in routes:
            entries = value.get(category)
            require(isinstance(entries, list) and len(entries) <= 4096, 'UNAVAILABLE')
            for entry in entries:
                require(isinstance(entry, dict) and entry.get('method') in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'))
                path = (native_resource_path(entry.get('path')) if category == 'resources'
                        else path_value(entry.get('path')))
                require(path.startswith('/v0/management/') if category == 'management'
                        else path.startswith('/v0/resource/plugins/'), 'UNAVAILABLE')
                require(isinstance(entry.get('plugin_id'), str) and entry['plugin_id'], 'UNAVAILABLE')
                routes[category][(entry['method'], path)] = entry['plugin_id']
        dispatches = {'quota': set(), 'oauth': {}}
        entries = value.get('dispatches', [])
        require(isinstance(entries, list) and len(entries) <= 4096, 'UNAVAILABLE')
        for entry in entries:
            require(isinstance(entry, dict) and isinstance(entry.get('plugin_id'), str) and
                    entry['plugin_id'] and type(entry.get('quota')) is bool, 'UNAVAILABLE')
            if entry['quota']:
                dispatches['quota'].add(entry['plugin_id'])
            provider = entry.get('oauth_provider', '')
            require(isinstance(provider, str), 'UNAVAILABLE')
            if provider:
                require(provider not in dispatches['oauth'], 'UNAVAILABLE')
                dispatches['oauth'][provider] = entry['plugin_id']
        return incarnation, routes['management'], routes['resources'], dispatches

    def _oauth_provider(self, request):
        if request.method != 'GET':
            return None
        path = request.path
        if path == PREFIX + '/oauth/auth-url':
            provider = request.wire.get('query', {}).get('provider')
            require(isinstance(provider, str) and provider)
            return provider
        if path.startswith('/v0/management/') and path.endswith('-auth-url'):
            return unquote(path[len('/v0/management/'):-len('-auth-url')])
        return None

    @staticmethod
    def _plugin_binding(request, provider, incarnation, routes, dispatches, *, code='ROUTE_NOT_ALLOWED'):
        if _builtin_oauth(request, provider):
            return None
        oauth_plugin = dispatches['oauth'].get(provider.strip().lower()) if provider is not None else None
        plugin_id = routes.get((request.method, request.path))
        if oauth_plugin is not None:
            # Native suffix auth URLs select an auth provider before a declared
            # handler at the same path. Review the code native will actually run.
            plugin_id, kind = oauth_plugin, 'oauth'
        elif plugin_id is not None:
            kind = 'management'
        elif (plugin_id := _quota_plugin(request)) is not None:
            require(plugin_id in dispatches['quota'], code)
            kind = 'quota'
        elif provider is not None:
            raise ConsoleError(code)
        else:
            return None
        return {'kind': kind, 'plugin_id': plugin_id, 'registry_revision': incarnation}

    def _oauth_check(self, request, lease_id):
        if request.path not in (PREFIX + '/oauth/status', PREFIX + '/oauth/session', PREFIX + '/oauth/callback'):
            return
        value = request.wire.get('query', {}) if request.method in ('GET', 'DELETE') else request.wire['body'].get('value')
        require(isinstance(value, dict))
        state, provider = value.get('state'), value.get('provider')
        if value.get('redirect_url'):
            require(isinstance(value['redirect_url'], str))
            parsed = urlsplit(value['redirect_url'])
            require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username)
            states = parse_qs(parsed.query).get('state', [])
            require(len(states) == 1)
            require(state is None or state == states[0])
            state = states[0]
        require(isinstance(state, str) and state, 'OAUTH_STATE_REQUIRED')
        with self._gate:
            record = self._oauth.get(state)
            require(record is not None and record['lease_id'] == lease_id and
                    record['expires'] > time.monotonic(), 'OAUTH_STATE_MISMATCH')
            require(provider is None or provider == record['provider'], 'OAUTH_PROVIDER_MISMATCH')
            if request.path.endswith('/callback'):
                require(not record.get('complete'), 'OAUTH_STATE_MISMATCH')

    def _prepare(self, args, lease):
        request_id = identifier(args.get('request_id'))
        require(('request' in args) != ('requests' in args))
        values = args.get('requests') if 'requests' in args else [args['request']]
        require(isinstance(values, list) and 1 <= len(values) <= 256)
        raw_size = len(encoded(values))
        require(raw_size < WIRE_LIMIT - 65536, 'PAYLOAD_TOO_LARGE')
        # Own immutable copies; callers cannot change an approved native body.
        values = json.loads(encoded(values))
        revision = args.get('expected_revision')
        if 'requests' in args:
            require(isinstance(revision, str), 'STALE_REVISION')
            for value in values:
                require(isinstance(value, dict))
                require(value.get('expected_revision', revision) == revision, 'STALE_REVISION')
                value['expected_revision'] = revision
        requests = [parse_request(value) for value in values]
        config = requests[0].config_write
        require(len(requests) == 1 or all(r.config_write for r in requests))
        require('requests' not in args or all(r.config_write for r in requests))
        revision = revision or requests[0].wire.get('expected_revision') or self._revision()
        require(all(not r.config_write or r.wire.get('expected_revision') == revision for r in requests), 'STALE_REVISION')
        effects, resources, policies = set(), set(), []
        incarnation, routes, dispatches = None, {}, {'quota': set(), 'oauth': {}}
        providers = [self._oauth_provider(request) for request in requests]
        quota, quota_version = None, 0
        if len(requests) == 1 and 'request' in args and args.get('egress_destinations') is None:
            # Sample the credential version BEFORE reading trusted inventory: a replacement completing after
            # this point is then visible as a version change instead of being stamped onto old facts.
            with self._gate:
                quota_version = self._versions.get('credentials', 0)
            quota = self._quota_read(requests[0])
        if any((r.path.startswith('/v0/management/') and r.path != '/v0/management/config' and
                not _builtin_oauth(r, provider)) or _quota_plugin(r) is not None or
               (provider is not None and not _builtin_oauth(r, provider))
               for r, provider in zip(requests, providers)):
            incarnation, routes, _, dispatches = self._registry_snapshot()
        plugin_binding = None
        for request, provider in zip(requests, providers):
            plugin_binding = self._plugin_binding(request, provider, incarnation, routes, dispatches)
            if plugin_binding and plugin_binding['kind'] == 'management':
                provider = None
            if quota:
                group, extra, policy = 'egress', {'network_destination', console_quota.SCOPE_EFFECT}, quota.policy
            elif provider and request.path.startswith('/v0/'):
                group, extra, policy = 'oauth', {'oauth_start'}, None
            else:
                group, extra, policy = classify(request, routes)
            if plugin_binding:
                extra.update(('plugin_code', 'network_destination'))
            if policy and args.get('egress_destinations') is not None:
                destinations = args['egress_destinations']
                require(isinstance(destinations, list) and len(destinations) <= 16)
                for destination in destinations:
                    require(isinstance(destination, dict) and
                            not set(destination) - {'scheme', 'host', 'port'})
                    scheme, host = destination.get('scheme'), destination.get('host')
                    port = destination.get('port', 443 if scheme == 'https' else 80)
                    require(scheme in ('https', 'http') and isinstance(host, str) and
                            host and host == host.lower() and
                            not any(c in host for c in '/\\@?# \r\n') and
                            type(port) is int and 0 < port <= 65535)
                    policy['destinations'].append({'scheme': scheme, 'host': host, 'port': port, 'path_prefix': '/'})
                if destinations:
                    extra.add('cross_origin_redirect')
            self._oauth_check(request, args['lease_id'])
            if not quota:
                resources.add(group)
            effects.update(extra)
            policies.append(policy)
        prepared = None
        if config:
            require(revision == self._revision(), 'STALE_REVISION')
            prepared = self.owner.prepare_config(revision, [r.config_wire() for r in requests], timeout=self._timeout(2.))
            require(prepared.get('ok'), prepared.get('code', 'INVALID_CONFIG'))
            effects.update(prepared['effects'])
            changed = prepared['changed_paths']
            if any(p.startswith(('server/', 'discovery/', 'management/')) for p in changed):
                effects.add('public_exposure')
            if any(p.startswith(('access/', 'management/', 'server/', 'discovery/')) for p in changed):
                effects.add('authentication_changed')
            if 'auth_dir_changed' in effects or any(
                    any(part in p for part in ('path', 'dir', 'cert', 'key-file')) for p in changed):
                effects.add('filesystem_path_changed')
            if 'plugins_changed' in effects:
                effects.add('plugin_code')
            if 'provider_credentials_changed' in effects or any(
                    p.startswith(('api-keys/', 'requests/', 'discovery/')) for p in changed):
                effects.add('network_destination')
            require(prepared['base_revision'] == revision, 'STALE_REVISION')
            # Keep digests and native requests, never a second persisted candidate.
            prepared = {k: prepared[k] for k in ('base_revision', 'candidate_revision', 'effects', 'changed_paths')}
            resources.update(('config', 'credentials', 'oauth', 'plugins', 'egress'))
        elif not quota and (requests[0].method != 'GET' or effects.intersection({'oauth_start', 'plugin_code'})):
            # Native credential/plugin handlers may also rewrite config.yaml.
            resources.add('config')
        mutating = config or requests[0].method != 'GET' or bool(effects.intersection({'oauth_start', 'plugin_code'}))
        write_resources = resources if mutating else set()
        fingerprint = _digest({'requests': values, 'revision': revision, 'policies': policies,
                               'plugin_binding': plugin_binding, 'quota': quota.scope() if quota else None})
        self._lease(args)
        with self._gate:
            if quota:
                require(self._versions.get('credentials', 0) == quota_version, 'REVIEW_STALE')
            for key, review in self._reviews.items():
                if review['lease_id'] == args['lease_id'] and review.get('request_id') == request_id:
                    require(review['fingerprint'] == fingerprint, 'REQUEST_CONFLICT')
                    return self._review_response(key, review, lease)
            require(len(self._reviews) < MAX_REVIEWS, 'BUSY')
            size = raw_size + sum(len(r.body) for r in requests)
            self._space(size)
            review_id = secrets.token_urlsafe(32)
            summary = quota.summary if quota else self._summary(requests, effects, policies)
            if plugin_binding:
                summary += ('\nInstalled plugin: ' + plugin_binding['plugin_id'] +
                            '\nRegistry revision: ' + plugin_binding['registry_revision'])
            review = {'kind': 'management', 'lease_id': args['lease_id'], 'request_id': request_id,
                      'fingerprint': fingerprint, 'requests': requests, 'effects': sorted(effects),
                      'policies': policies, 'prepared': prepared, 'revision': revision,
                      'plugin_binding': plugin_binding, 'quota': quota.scope() if quota else None,
                      'quota_version': quota_version,
                      'oauth_provider': provider,
                      'resources': write_resources, 'versions': {r: self._versions.get(r, 0) for r in resources},
                      'expires': time.monotonic() + REVIEW_SECONDS, 'summary': summary, 'size': size}
            self._reviews[review_id] = review
            return self._review_response(review_id, review, lease)

    @staticmethod
    def _summary(requests, effects, policies):
        summary = 'Apply one native configuration transaction' if len(requests) > 1 else (
            requests[0].method + ' ' + requests[0].path)
        targets = []
        for policy in policies:
            if policy:
                # Approvals bind origins, not credential-bearing query strings.
                for destination in policy['destinations']:
                    host = destination['host']
                    display_host = '[' + host + ']' if ':' in host else host
                    targets.append(destination['scheme'] + '://' + display_host + ':' + str(destination['port']))
                if policy.get('host_header'):
                    targets.append('Host override ' + ', '.join(policy['host_header']))
                proxy = policy['proxy']
                if proxy['mode'] == 'explicit':
                    parsed = urlsplit(proxy['url'])
                    targets.append('Proxy ' + parsed.scheme + '://' + str(parsed.hostname) +
                                   (':' + str(parsed.port) if parsed.port else ''))
                else:
                    targets.append('Proxy mode ' + proxy['mode'])
        if targets:
            summary += '\nApproved destinations: ' + ', '.join(targets) + (
                '\nAt most five redirects within these origins; only the original method and GET are permitted.')
        if effects:
            summary += '\nEffects: ' + ', '.join(sorted(effects))
        return summary

    def _quota_read(self, request):
        """Trusted-inventory proof that a fixed quota template belongs to its credential, or None."""
        candidate = console_quota.match(request)
        if candidate is None:
            return None
        try:
            response = self.transport.request('GET', PREFIX + '/credentials', timeout=self._timeout(2.),
                                              max_response=RESULT_LIMIT)
            if response.status != 200:
                return None
            return console_quota.verify(candidate, json.loads(response.body).get('files'))
        except Exception:
            return None

    def _quota_current(self, review):
        """Caller holds the gate. A quota read depends on, but never reserves or bumps, the credentials version."""
        return (self._versions.get('credentials', 0) == review['quota_version'] and
                'credentials' not in self._active)

    def _quota_granted(self, lease, review):
        """True while this lease holds an accepted approval for this review's identity and credential version."""
        quota, current = review['quota'], self._versions.get('credentials', 0)
        grant = lease['quota_grants'].get(quota['key'])
        if grant is None:
            return False
        fresh = review['quota_version'] == current
        if grant[1] != current or fresh and grant[0] != quota['identity']:
            del lease['quota_grants'][quota['key']]
            return False
        return fresh and grant[0] == quota['identity']

    def _grant_quota(self, review, lease):
        """Remember the human approval only after the owner accepted the execute."""
        quota = review.get('quota')
        if not quota:
            return
        try:
            self._policy(lease)
        except ConsoleError:
            return
        with self._gate:
            grants = lease['quota_grants']
            if lease['revoked'] or lease['expires'] <= time.monotonic() or \
                    lease['generation'] != self.owner.generation or not self._quota_current(review):
                return
            if quota['key'] in grants or len(grants) < console_quota.MAX_GRANTS_PER_LEASE:
                # Never a newer version than the one the identity was proven under.
                grants[quota['key']] = (quota['identity'], review['quota_version'])

    def _review_response(self, key, review, lease=None):
        quota = review.get('quota')
        granted = quota is not None and lease is not None and self._quota_granted(lease, review)
        response = {'ok': True, 'review_id': key, 'summary': review['summary'],
                    'effects': review['effects'], 'requires_confirmation': bool(review['effects']) and not granted,
                    'revision': review.get('revision')}
        if quota:
            response['approval_scope'] = {'kind': 'quota_reads', 'provider': quota['provider'], 'granted': granted}
        return response

    def _approved_review(self, args, kind):
        review_id = args.get('review_id')
        require(isinstance(review_id, str), 'REVIEW_REQUIRED')
        approved = args.get('approved_effects')
        require(isinstance(approved, list) and all(isinstance(e, str) for e in approved))
        with self._gate:
            review = self._reviews.get(review_id)
            require(review is not None and review['kind'] == kind and
                    review['lease_id'] == args['lease_id'], 'REVIEW_STALE')
            require(review.get('accepted') or time.monotonic() < review['expires'], 'REVIEW_STALE')
            require(set(approved) == set(review['effects']), 'CONSENT_REQUIRED')
            require(not review.get('invalidated'), 'REVIEW_STALE')
            return review

    def _execute(self, args, lease):
        request_id = identifier(args.get('request_id'))
        review = self._approved_review(args, 'management')
        require(review['request_id'] == request_id, 'REQUEST_CONFLICT')
        intent = execute_intent(args, lease['generation'])
        if '_host_grant' in args:
            from .operation_guard import revalidate_grant
            require(revalidate_grant(args['_host_grant'], intent, timeout=self._timeout(1.)), 'POLICY_LOCKED')
            intent['_host_grant'] = args['_host_grant']
        with self._gate:
            if not review.get('accepted'):
                require(request_id not in self._results, 'REQUEST_CONFLICT')
                require(all(self._versions.get(r, 0) == v for r, v in review['versions'].items()), 'RESOURCE_CONFLICT')
                require(not any(r in self._active for r in review['resources']), 'RESOURCE_CONFLICT')
                require(not review.get('quota') or self._quota_current(review), 'REVIEW_STALE')
                require(len(self._results) < MAX_RESULTS, 'BUSY')
                self._space(WIRE_LIMIT)
                require(review['revision'] == self._revision(), 'STALE_REVISION')
                for resource in review['resources']:
                    self._active[resource] = request_id
                review['accepted'] = request_id
                self._results[request_id] = {'lease_id': args['lease_id'], 'scope': dict(lease['scope']),
                    'state': 'pending', 'size': WIRE_LIMIT, 'expires': time.monotonic() + RESULT_SECONDS}
            else:
                require(review['accepted'] == request_id, 'REQUEST_CONFLICT')
                # Query the original acceptance; never resubmit uncertain work.
                return self.owner.dispatch({'cmd': 'operation', 'args': {'operation_id': request_id}})
        receipt = self.owner.submit(intent)
        if receipt.get('ok') is False:
            self.job_rejected(intent, receipt.get('code', 'UNAVAILABLE'))
        else:
            self._grant_quota(review, lease)
        return receipt

    def _result(self, args, lease):
        operation_id = identifier(args.get('operation_id'))
        current = (not lease['revoked'] and lease['expires'] > time.monotonic() and
                   lease['generation'] == self.owner.generation)
        if current:
            try:
                self._policy(lease)
            except ConsoleError:
                current = False
        with self._gate:
            result = self._results.get(operation_id)
            require(result is not None and result['lease_id'] == args['lease_id'] and
                    result['scope'] == lease['scope'], 'RECONCILE_REQUIRED')
            receipt = {k: v for k, v in dict(result, operation_id=operation_id).items()
                       if k in ('operation_id', 'state', 'result', 'code', 'result_withheld', 'response_status')}
            receipt.update(generation=self.owner.generation, requires_unlock=not current)
            if not current and 'result' in receipt:
                if result.get('configuration'):
                    # Configuration receipts contain no candidate bytes or native
                    # error detail after revocation, but retain the actual outcome.
                    native = receipt['result']
                    outcome = native['body']['value']
                    safe = {k: v for k, v in outcome.items() if k in {
                        'ok', 'code', 'revision', 'generation', 'rollback', 'base_revision',
                        'candidate_revision', 'effects', 'changed_paths'}}
                    receipt['result'] = dict(native, body={'kind': 'json', 'value': safe})
                else:
                    receipt.pop('result')
                    receipt['result_withheld'] = True
            return receipt

    def execute_job(self, args):
        oauth_reserved = False
        try:
            lease = self._lease(args)
            with self._gate:
                review = self._reviews.get(args['review_id'])
                require(review is not None and review.get('accepted') == args['request_id'], 'REVIEW_STALE')
                require(review['lease_id'] == args['lease_id'] and not review.get('invalidated'), 'REVIEW_STALE')
                self._results[args['request_id']]['state'] = 'running'
            require(self.owner.operation_current(), 'POLICY_LOCKED')
            require(review['revision'] == self._revision(), 'STALE_REVISION')
            if review['prepared']:
                with self._gate:
                    self._results[args['request_id']]['dispatched'] = True
                outcome = self.owner.config_transaction(review['revision'],
                    [r.config_wire() for r in review['requests']],
                    reviewed_candidate_revision=review['prepared']['candidate_revision'],
                    restart_consent='restart_required' in review['effects'])
                result = {'status': 200 if outcome.get('ok') else 409, 'headers': {},
                          'body': {'kind': 'json', 'value': outcome}}
                if outcome.get('revision'):
                    result['revision'] = outcome['revision']
                with self._gate:
                    self._results[args['request_id']]['response_observed'] = True
            else:
                request = review['requests'][0]
                self._oauth_check(request, args['lease_id'])
                if review.get('quota'):
                    # The credential may have been replaced since the review; verify before anything is sent,
                    # with the credential version unchanged on both sides of the trusted inventory read.
                    with self._gate:
                        require(self._quota_current(review), 'REVIEW_STALE')
                    current = self._quota_read(request)
                    with self._gate:
                        require(self._quota_current(review), 'REVIEW_STALE')
                    require(current is not None and current.identity == review['quota']['identity'], 'REVIEW_STALE')
                binding = review['plugin_binding']
                provider = review['oauth_provider']
                headers = {}
                if binding:
                    incarnation, routes, _, dispatches = self._registry_snapshot()
                    require(self._plugin_binding(request, provider, incarnation, routes, dispatches,
                                                 code='REVIEW_STALE') == binding, 'REVIEW_STALE')
                    # Go compares this revision atomically with handler lookup.
                    # The fresh snapshot above alone would leave a replacement race.
                    headers['X-Hermes-Plugin-Revision'] = binding['registry_revision']
                policy = review['policies'][0]
                if policy:
                    headers['X-Hermes-Egress-Policy'] = base64.urlsafe_b64encode(encoded(policy)).decode().rstrip('=')
                self._lease(args)
                inference = request.path == '/v1/models'
                timeout = self.owner.authorized_timeout(10. if inference else 120.)
                require(timeout > 0, 'UNAVAILABLE')
                with self._gate:
                    if provider:
                        require(len(self._oauth) + self._oauth_starting < 128, 'BUSY')
                        self._oauth_starting += 1
                        oauth_reserved = True
                    self._results[args['request_id']]['dispatched'] = True
                if inference:
                    response = self.owner.inference.request('GET', '/v1/models',
                        timeout=timeout, max_response=RESULT_LIMIT)
                else:
                    response = self.transport.request(request.method, request.path,
                        query=request.wire.get('query'), body=request.body, content_type=request.content_type,
                        headers=headers, timeout=timeout, max_response=RESULT_LIMIT)
                if binding:
                    native_headers = {name.lower(): value for name, value in response.headers.items()}
                    invocation = native_headers.get('x-hermes-plugin-invoked')
                    if invocation == '0':
                        with self._gate:
                            self._results[args['request_id']]['dispatched'] = False
                        if response.status == 412 or binding['kind'] == 'management' and response.status == 404:
                            raise ConsoleError('REVIEW_STALE')
                        require(400 <= response.status <= 599, 'INVALID_RESPONSE')
                    else:
                        require(invocation == '1' and
                                native_headers.get('x-hermes-plugin-revision') == binding['registry_revision'],
                                'OUTCOME_UNKNOWN')
                with self._gate:
                    self._results[args['request_id']].update(
                        response_observed=True, response_status=response.status)
                self._record_native(request, response, args['lease_id'], provider)
                result = management_response(response, request.wire['response_kind'])
            # Accepted remote effects are never replayed or claimed undone if the
            # document disappears while a request is in flight.
            with self._gate:
                record = self._results[args['request_id']]
                record['size'] = 0
                result_size = len(encoded(result))
                self._space(result_size)
                record.update(state='succeeded', result=result, size=result_size,
                              configuration=bool(review['prepared']), expires=time.monotonic() + RESULT_SECONDS)
            return {'ok': True, 'code': 'OK', 'result_ready': True}
        except ConsoleError as exc:
            return self.job_rejected(args, exc.code)
        except Exception as exc:
            code = getattr(exc, 'code', 'OUTCOME_UNKNOWN')
            if code not in ('UNAVAILABLE', 'OUTCOME_UNKNOWN', 'RESPONSE_TOO_LARGE', 'INVALID_REQUEST'):
                code = 'OUTCOME_UNKNOWN'
            return self.job_rejected(args, code)
        finally:
            if oauth_reserved:
                with self._gate:
                    self._oauth_starting -= 1
            self._release(args)

    def _release(self, args):
        with self._gate:
            for resource, operation in list(self._active.items()):
                if operation == args['request_id']:
                    del self._active[resource]
                    self._versions[resource] = self._versions.get(resource, 0) + 1
            review = self._reviews.get(args.get('review_id'))
            if review and review.get('accepted') == args['request_id']:
                # Keep the exact acceptance references for deduplication, not native data.
                review['requests'] = []
                review['policies'] = []
                review['prepared'] = None
                review['size'] = len(encoded({'summary': review['summary'], 'effects': review['effects']}))

    def job_rejected(self, args, code):
        with self._gate:
            result = self._results.get(args.get('request_id'))
            if result is not None:
                if result['state'] in ('pending', 'running'):
                    if result.get('response_observed'):
                        # The handler answered. Projection/storage failure is not
                        # a failed native operation, even for an HTTP error reply.
                        result.update(state='succeeded', code='RESPONSE_WITHHELD', result_withheld=True)
                    elif result.get('dispatched') or code in ('OUTCOME_UNKNOWN', 'RECONCILE_REQUIRED'):
                        result.update(state='unknown', code='OUTCOME_UNKNOWN')
                    else:
                        result.update(state='failed', code=code)
                    result.update(size=0, expires=time.monotonic() + RESULT_SECONDS)
                reply = {'ok': result['state'] == 'succeeded',
                         'code': result.get('code', 'OK'), 'result_ready': True}
            else:
                reply = {'ok': False, 'code': code, 'result_ready': False}
        self._release(args)
        return reply

    def _record_native(self, request, response, lease_id, provider):
        success = 200 <= response.status < 300
        if not success and not provider:
            return
        if not provider and request.path not in (PREFIX + '/oauth/status', PREFIX + '/oauth/session'):
            return
        try:
            value = json.loads(response.body)
        except (ValueError, UnicodeError):
            require(not provider or not success, 'OUTCOME_UNKNOWN')
            return
        if not isinstance(value, dict):
            require(not provider or not success, 'OUTCOME_UNKNOWN')
            return
        if provider:
            state = value.get('state')
            if not isinstance(state, str) or not 0 < len(state) <= 4096:
                require(not success, 'OUTCOME_UNKNOWN')
                return
            # Admission reserved capacity before starting native OAuth. Record
            # every observed state before validating or exposing presentation data.
            with self._gate:
                require(state not in self._oauth, 'OUTCOME_UNKNOWN')
                record = {'lease_id': lease_id, 'provider': provider,
                          'expires': time.monotonic() + 1800, 'values': []}
                self._oauth[state] = record
            try:
                ttl = value.get('expires_in', 1800)
                ttl = min(1800, max(1, ttl)) if isinstance(ttl, (int, float)) else 1800
                values = [value[k] for k in ('url', 'user_code', 'verification_uri', 'verification_uri_complete')
                          if isinstance(value.get(k), str) and value[k]]
                require(sum(len(v.encode()) for v in values) <= 65536, 'RESPONSE_TOO_LARGE')
                with self._gate:
                    record.update(expires=time.monotonic() + ttl, values=values)
            finally:
                with self._gate:
                    lease = self._leases.get(lease_id)
                    cancel = (lease is None or lease['revoked'] or lease['expires'] <= time.monotonic() or
                              lease['generation'] != self.owner.generation)
                if cancel:
                    self._cancel_states([state])
        elif request.path == PREFIX + '/oauth/status':
            state = request.wire.get('query', {}).get('state')
            if value.get('status') != 'wait':
                with self._gate:
                    record = self._oauth.get(state)
                    if record:
                        record['complete'] = True
                        record['expires'] = min(record['expires'], time.monotonic() + 60)
        elif request.path == PREFIX + '/oauth/session':
            with self._gate:
                self._oauth.pop(request.wire.get('query', {}).get('state'), None)

    def _capability(self, op, args, lease):
        if 'review_id' in args:
            review = self._approved_review(args, op)
            if op == 'plugin':
                value = review['value']
                response = self.transport.request('GET', PREFIX + '/plugins',
                    timeout=self._timeout(1.), max_response=RESULT_LIMIT)
                require(response.status == 200, 'UNAVAILABLE')
                entries = json.loads(response.body).get('plugins', [])
                plugin = next((p for p in entries if p.get('id') == value['plugin_id'] and p.get('effective_enabled')), None)
                require(plugin is not None and value['menu_index'] < len(plugin.get('menus', [])) and
                        plugin['menus'][value['menu_index']].get('path') == value['path'], 'REVIEW_STALE')
                if value['path'].startswith('/'):
                    incarnation, _, routes, _ = self._registry_snapshot()
                    require(incarnation == value['registry_incarnation'] and
                            routes.get(('GET', value['path'])) == value['plugin_id'] and
                            all(routes.get(('GET', path)) == value['plugin_id']
                                for path in value['resource_paths']), 'REVIEW_STALE')
                self._lease(args)
            with self._gate:
                require(not review.get('used'), 'REVIEW_STALE')
                require(review.get('revision') == self._revision(), 'STALE_REVISION')
                review['used'] = True
                if op == 'plugin' and review['value']['path'].startswith('/'):
                    result = self.resources.mint(args, lease, review['value'])
                else:
                    result = dict(review['value'], ok=True)
                    if op == 'plugin':
                        result['kind'] = 'external-resource'
                review['value'] = {}
                review['size'] = 0
                return result
        effects = []
        if op == 'browser':
            action, value = args.get('action'), args.get('value')
            require(action in ('open', 'copy') and isinstance(value, str) and len(value.encode()) <= RESULT_LIMIT)
            if action == 'open':
                target = urlsplit(value)
                require(target.scheme in ('http', 'https') and target.hostname and not target.username and not target.password)
            with self._gate:
                recorded = any(record['lease_id'] == args['lease_id'] and record['expires'] > time.monotonic() and
                               value in record['values'] for record in self._oauth.values())
            if not recorded:
                effects = ['network_destination' if action == 'open' else 'data_export']
            payload = {'action': action, 'value': value}
            summary = ('Open recorded OAuth authorization URL' if recorded and action == 'open' else
                       'Copy recorded OAuth device value' if recorded else
                       'Open this exact external URL' if action == 'open' else 'Copy console data to the clipboard')
            if action == 'open' and not recorded:
                summary += '\nDestination: ' + value
        elif op == 'file':
            filename = part_name(args.get('filename'))
            require('/' not in filename)
            media = content_type(args.get('content_type'))
            decode_bytes(args.get('base64'), RESULT_LIMIT)
            payload = {'filename': filename, 'content_type': media, 'base64': args['base64']}
            effects, summary = ['data_export'], 'Save console data as ' + filename
        else:
            plugin_id, index = args.get('plugin_id'), args.get('menu_index')
            require(isinstance(plugin_id, str) and type(index) is int and index >= 0)
            response = self.transport.request('GET', PREFIX + '/plugins', timeout=self._timeout(1.),
                                              max_response=RESULT_LIMIT)
            require(response.status == 200, 'UNAVAILABLE')
            entries = json.loads(response.body).get('plugins', [])
            plugin = next((p for p in entries if p.get('id') == plugin_id and p.get('effective_enabled')), None)
            require(plugin is not None and index < len(plugin.get('menus', [])), 'ROUTE_NOT_ALLOWED')
            target = plugin['menus'][index].get('path')
            require(isinstance(target, str) and len(target) <= RESULT_LIMIT)
            if target.startswith('/'):
                native_resource_path(target)
                incarnation, _, routes, _ = self._registry_snapshot()
                require(routes.get(('GET', target)) == plugin_id, 'ROUTE_NOT_ALLOWED')
            else:
                url = urlsplit(target)
                require(url.scheme in ('https', 'http', 'data') and not url.username and not url.password)
            effects = ['plugin_code']
            if target.startswith('/'):
                summary = ('Open the isolated resource for installed plugin ' + plugin_id +
                           '. This delegates its registered GET resource handlers, which may access or change ' +
                           'credentials, save state, perform network calls, and consume account entitlements. ' +
                           'GET is not read-only. No console, management or inference authority is given to the page.')
            else:
                effects.append('network_destination' if url.scheme in ('http', 'https') else 'embedded_code')
                summary = ('Open isolated external code recorded for installed plugin ' + plugin_id +
                           '. No native resource, console, management or inference authority is given to the page.')
            payload = {'plugin_id': plugin_id, 'menu_index': index, 'path': target}
            if target.startswith('/'):
                payload['registry_incarnation'] = incarnation
                payload['resource_paths'] = [path for (method, path), owner in routes.items()
                    if method == 'GET' and owner == plugin_id and path.startswith('/v0/resource/plugins/')]
        with self._gate:
            require(len(self._reviews) < MAX_REVIEWS, 'BUSY')
            size = len(encoded(payload))
            self._space(size)
            key = secrets.token_urlsafe(32)
            review = {'kind': op, 'lease_id': args['lease_id'], 'effects': effects, 'summary': summary,
                      'revision': self._revision(), 'value': payload, 'size': size,
                      'expires': time.monotonic() + REVIEW_SECONDS}
            self._reviews[key] = review
            return self._review_response(key, review)

    def _cancel_states(self, states):
        deadline = time.monotonic() + 2.
        for state in states:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self.transport.request('DELETE', PREFIX + '/oauth/session', query={'state': state},
                                       timeout=remaining, max_response=4096)
            except Exception:
                pass
            finally:
                with self._gate:
                    self._oauth.pop(state, None)

    def cancel_pending_oauth(self, reason):
        with self._gate:
            self.resources.revoke()
            states = list(self._oauth)
        self._cancel_states(states)

    def config_committed(self, base_revision, revision, effects):
        with self._gate:
            for review in self._reviews.values():
                if review.get('revision') != revision and not review.get('accepted'):
                    review['invalidated'] = True

    def close(self):
        self._stop.set()
        with self._gate:
            self._closed = True
            self.resources.revoke()
            self._leases.clear()
            self._reviews.clear()
            self._results.clear()
            self._oauth.clear()
            self._active.clear()
