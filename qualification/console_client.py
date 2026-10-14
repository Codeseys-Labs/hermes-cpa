"""Real hosted-console client for generated, network-none qualification roots."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import qualification as q


class ConsoleFailure(q.Refused):
    def __init__(self, route, status, code):
        super().__init__('console_' + route + '_failed')
        self.http_status = status
        self.code = code if code in {
            'UNAUTHORIZED', 'UNAVAILABLE', 'STALE_SCOPE', 'STALE_GENERATION', 'STALE_REVISION',
            'INVALID_INPUT', 'READ_ONLY', 'POLICY_LOCKED', 'CONSENT_REQUIRED', 'RECONCILE_REQUIRED',
            'BUSY', 'CONFLICT', 'LEASE_REQUIRED', 'LEASE_EXPIRED', 'REVIEW_REQUIRED', 'REVIEW_STALE',
            'RESOURCE_CONFLICT', 'REQUEST_CONFLICT', 'OUTCOME_UNKNOWN', 'ROUTE_NOT_ALLOWED',
            'PAYLOAD_TOO_LARGE', 'RESPONSE_TOO_LARGE', 'BATCH_SPLIT_REQUIRED',
            'OAUTH_STATE_REQUIRED', 'OAUTH_STATE_MISMATCH', 'OAUTH_PROVIDER_MISMATCH',
            'INVALID_REQUEST', 'INVALID_CONFIG', 'NOT_FOUND', 'READ_ONLY_FIELD', 'READ_FAILED',
            'RECOVERY_REQUIRED', 'RESOURCE_GONE', 'RESOURCE_REVOKED', 'RESOURCE_PATH_DENIED',
            'REDIRECT_DENIED', 'INVALID_RESPONSE',
        } else 'UNAVAILABLE'


class ConsoleClient:
    def __init__(self, root, host):
        if sys.platform != 'linux' or {p.name for p in Path('/sys/class/net').iterdir()} != {'lo'}:
            raise q.Refused('network_none_namespace_required')
        import pwd
        self.root = q.checked_path(root)
        self.host = q.checked_path(host)
        self.control_env = q.runtime_environment(self.root, self.host)
        self.control_env['HOME'] = pwd.getpwuid(os.getuid()).pw_dir
        self.control_env.pop('HERMES_HOME', None)
        validation = subprocess.run(
            [sys.executable, '-I', '-S', '-c',
             'import json,sys; sys.path.insert(0,sys.argv[1]); import qualification as q; '
             'root,record=q.load_root(sys.argv[2]); print(json.dumps(record))',
             str(q.ROOT / 'scripts'), str(self.root)],
            env=self.control_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=15)
        if validation.returncode != 0:
            raise q.Refused('console_root_invalid')
        self.record = json.loads(validation.stdout)
        self.process = None
        self.headers = {}
        self.document = None
        self.lease = None
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]

    def http(self, path, payload=None, *, method=None, headers=None):
        body = None if payload is None else json.dumps(payload, separators=(',', ':')).encode()
        request_headers = dict(self.headers if headers is None else headers)
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=12)
        try:
            connection.request(method or ('GET' if body is None else 'POST'),
                               q.PLUGIN_API + path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read(48 * 1024 * 1024 + 1)
            if len(raw) > 48 * 1024 * 1024:
                raise q.Refused('console_smoke_response_too_large')
            return response.status, json.loads(raw) if raw else None
        finally:
            connection.close()

    def call(self, route, payload):
        status, envelope = self.http('/console/' + route, payload)
        if (status not in (200, 202) or not isinstance(envelope, dict) or
                envelope.get('api_version') != 1 or envelope.get('scope_id') != self.scope or
                not isinstance(envelope.get('data'), dict) or envelope['data'].get('ok') is False):
            error = envelope.get('error', {}) if isinstance(envelope, dict) else {}
            raise ConsoleFailure(route, status, error.get('code') if isinstance(error, dict) else None)
        return envelope['data']

    def unlock(self):
        status, snapshot = self.http('/status')
        if status != 200 or not isinstance(snapshot, dict) or type(snapshot.get('generation')) is not int:
            raise q.Refused('console_status_failed')
        self.document = str(uuid.uuid4())
        result = self.call('unlock', {'request_id': str(uuid.uuid4()),
            'expected_generation': snapshot['generation'], 'document_id': self.document, 'consent': True})
        if not isinstance(result.get('lease_id'), str):
            raise q.Refused('console_unlock_invalid')
        self.lease = result['lease_id']

    def scoped(self, **values):
        return {'document_id': self.document, 'lease_id': self.lease, **values}

    def prepare(self, request=None, *, requests=None, revision=None):
        identifier = str(uuid.uuid4())
        payload = self.scoped(request_id=identifier)
        if requests is None:
            payload['request'] = request
        else:
            payload.update(requests=requests, expected_revision=revision)
        review = self.call('prepare', payload)
        if not isinstance(review.get('review_id'), str) or not isinstance(review.get('effects'), list):
            raise q.Refused('console_review_invalid')
        return identifier, review

    def execute(self, identifier, review):
        # The invoking smoke requires --consent and generated data in a network-none root.
        receipt = self.call('execute', self.scoped(request_id=identifier,
            review_id=review['review_id'], approved_effects=review['effects']))
        if receipt.get('operation_id') != identifier:
            raise q.Refused('console_acceptance_invalid')
        deadline = time.monotonic() + 130
        while time.monotonic() < deadline:
            result = self.call('result', self.scoped(operation_id=identifier))
            if result.get('state') == 'succeeded':
                native = result.get('result')
                if not isinstance(native, dict) or type(native.get('status')) is not int:
                    raise q.Refused('console_result_unavailable')
                if result.get('requires_unlock'):
                    self.unlock()
                return native
            if result.get('state') not in ('pending', 'running'):
                raise ConsoleFailure('result', 200, result.get('code'))
            time.sleep(.05)
        raise q.Refused('console_operation_unknown')

    def request(self, method, path, value=None, *, query=None, revision=None, response_kind='json', body=None):
        request = {'method': method, 'path': path, 'response_kind': response_kind,
                   'body': body if body is not None else
                           ({'kind': 'empty'} if value is None else {'kind': 'json', 'value': value})}
        if query is not None:
            request['query'] = query
        if revision is not None:
            request['expected_revision'] = revision
        return self.execute(*self.prepare(request))

    @staticmethod
    def json_result(response):
        if (not 200 <= response['status'] < 300 or response.get('body', {}).get('kind') != 'json'):
            raise q.Refused('native_console_request_failed')
        return response['body']['value']

    def config(self, path, value):
        snapshot = self.request('GET', '/v8/management/config')
        revision = snapshot.get('revision')
        if not q.hex_value(revision, 64):
            raise q.Refused('native_config_revision_missing')
        result = self.json_result(self.request('PUT', '/v8/management/config/' + path,
                                               value, revision=revision))
        if result.get('ok') is not True:
            raise q.Refused('native_config_commit_failed')
        return result

    def __enter__(self):
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(q.ROOT / 'scripts/qualification.py'), 'serve', '--root', str(self.root),
                 '--host', str(self.host), '--port', str(self.port)],
                env=self.control_env, cwd=self.root,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 40
            token = q.read_file(self.root / q.TOKEN, private=True, limit=128).decode().strip()
            while True:
                if self.process.poll() is not None:
                    raise q.Refused('console_backend_exited')
                try:
                    backend = q.backend_state(self.root, self.record, self.port)
                    headers, snapshot, _ = q.hosted_selection(
                        self.root, self.record, backend, self.port, token, deadline)
                    self.headers = headers
                    self.scope = snapshot['scope_id']
                    break
                except (OSError, q.Refused):
                    if time.monotonic() >= deadline:
                        raise q.Refused('console_backend_not_ready') from None
                    time.sleep(.1)
            self.unlock()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.lease is not None:
            try:
                self.call('lock', self.scoped())
            except Exception:
                pass
            self.lease = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)
            self.process = None

    def __exit__(self, *_args):
        self.close()
