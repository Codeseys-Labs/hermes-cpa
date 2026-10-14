"""Candidate leases at native grant and HTTP boundaries; generated loopback only."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from hermes_cpa import authorization
from hermes_cpa.subscription_broker import SubscriptionBroker
from hermes_cpa.subscription_client import grant_token

KEY = 'generated-lease-inference-key'
PREFIX = b'data: {"delta":"' + b'x' * 256 + b'"}\n\n'
TERMINAL = b'data: [DONE]\n\n'


@pytest.fixture
def authorization_clock(monkeypatch):
    # Replace only this module's clock, never threading/httpx/process clocks.
    clock = SimpleNamespace(wall=time.time(), ticks=time.monotonic())
    monkeypatch.setattr(authorization, 'time', SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.ticks))
    return clock


@pytest.fixture
def owner_home(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import save_env_value

    home = (tmp_path / 'owner').resolve()
    home.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    token = set_hermes_home_override(home)
    try:
        save_env_value('HERMES_CPA_INFERENCE_KEY', KEY)
    finally:
        reset_hermes_home_override(token)
    return home


@pytest.fixture
def inference_service():
    state = SimpleNamespace(hits=[], release=threading.Event(), finished=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            state.hits.append(self.path)
            raw = b'{"object":"list","data":[{"id":"generated-model"}]}'
            self.send_response(200 if self.headers.get('Authorization') == 'Bearer ' + KEY else 401)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            state.hits.append(self.path)
            self.send_response(200 if self.headers.get('Authorization') == 'Bearer ' + KEY else 401)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            try:
                self.wfile.write(f'{len(PREFIX):x}\r\n'.encode() + PREFIX + b'\r\n')
                self.wfile.flush()
                if state.release.wait(10):
                    self.wfile.write(f'{len(TERMINAL):x}\r\n'.encode() + TERMINAL + b'\r\n0\r\n\r\n')
                    self.wfile.flush()
            except OSError:
                pass  # A broker deadline deliberately disconnects this fixture.
            finally:
                self.close_connection = True
                state.finished.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    state.origin = f'http://127.0.0.1:{server.server_port}'
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


def enroll(home, broker):
    # The actual broker validates this generated provider entry at every mint/admission.
    (home / 'config.yaml').write_text(json.dumps({
        'providers': {'cpa': {'base_url': broker.base_url, 'key_cmd': 'generated-command'}}
    }))


def models(client, broker, token):
    return client.get(broker.base_url + '/models', headers={'Authorization': 'Bearer ' + token})


@pytest.mark.parametrize('expired_by', ['wall', 'monotonic_rollback'])
def test_expiry_denies_new_enrolled_grants_and_held_tokens(
        owner_home, inference_service, authorization_clock, expired_by):
    clock = authorization_clock
    deadline = authorization.AuthorizationDeadline(clock.wall + 60)
    service = inference_service
    with SubscriptionBroker(owner_home, service.origin, port=0, authorization=deadline) as broker:
        enroll(owner_home, broker)
        token = grant_token(broker.control_path, owner_home, enrolled=True)
        with httpx.Client(trust_env=False, timeout=3) as client:
            response = models(client, broker, token)
            assert response.status_code == 200
            assert response.json()['data'][0]['id'] == 'generated-model'
            if expired_by == 'wall':
                clock.wall += 60
            else:
                clock.wall -= 3600
                clock.ticks += 60
            with pytest.raises(RuntimeError, match='subscription_unavailable'):
                grant_token(broker.control_path, owner_home, enrolled=True)
            assert models(client, broker, token).status_code == 401
        assert service.hits == ['/v1/models'], 'expired authority reached the upstream'


@pytest.mark.parametrize('operation', ['grant', 'request'])
def test_expiry_during_resolution_is_rechecked_at_admission(
        owner_home, inference_service, authorization_clock, monkeypatch, operation):
    clock = authorization_clock
    deadline = authorization.AuthorizationDeadline(clock.wall + 60)
    service = inference_service
    with SubscriptionBroker(owner_home, service.origin, port=0, authorization=deadline) as broker:
        enroll(owner_home, broker)
        token = grant_token(broker.control_path, owner_home, enrolled=True)
        entered, release = threading.Event(), threading.Event()
        consumer = broker._consumer

        def blocked_consumer(home):
            entered.set()
            release.wait(5)
            return consumer(home)

        monkeypatch.setattr(broker, '_consumer', blocked_consumer)
        result = {}

        def consume():
            try:
                if operation == 'grant':
                    result['token'] = grant_token(broker.control_path, owner_home, enrolled=True)
                else:
                    with httpx.Client(trust_env=False, timeout=3) as client:
                        result['status'] = models(client, broker, token).status_code
            except Exception as exc:
                result['error'] = exc

        thread = threading.Thread(target=consume)
        thread.start()
        try:
            assert entered.wait(1), 'consumer did not enter resolution'
            clock.ticks += 60
            release.set()
            thread.join(3)
            assert not thread.is_alive()
            if operation == 'grant':
                assert isinstance(result.get('error'), RuntimeError)
                assert 'token' not in result
            else:
                assert result == {'status': 401}
            assert service.hits == []
        finally:
            release.set()
            thread.join(3)


def test_admitted_stream_stops_at_expiry_without_terminal_success(
        owner_home, inference_service, authorization_clock):
    clock = authorization_clock
    deadline = authorization.AuthorizationDeadline(clock.wall + 60)
    service = inference_service
    with SubscriptionBroker(owner_home, service.origin, port=0, authorization=deadline) as broker:
        token = grant_token(broker.control_path, owner_home)
        with httpx.Client(trust_env=False, timeout=3) as client:
            with client.stream('POST', broker.base_url + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + token},
                    json={'model': 'generated-model', 'messages': [], 'stream': True}) as response:
                assert response.status_code == 200
                parts = response.iter_bytes()
                received = bytearray(next(parts))
                assert received.startswith(b'data: ')
                clock.ticks += 60
                service.release.set()
                with pytest.raises(httpx.RemoteProtocolError):
                    for part in parts:
                        received.extend(part)
                assert TERMINAL not in received
        assert service.hits == ['/v1/chat/completions']
        assert service.finished.wait(2)


def test_lease_response_budget_interrupts_a_blocked_stream(
        owner_home, inference_service, authorization_clock):
    clock = authorization_clock
    deadline = authorization.AuthorizationDeadline(clock.wall + 60)
    service = inference_service
    with SubscriptionBroker(owner_home, service.origin, port=0, authorization=deadline) as broker:
        token = grant_token(broker.control_path, owner_home)
        # Leave one second of captured lease. The response timer must wake a
        # blocked read even though no more upstream chunks arrive to check it.
        clock.ticks += 59
        with httpx.Client(trust_env=False, timeout=4) as client:
            with client.stream('POST', broker.base_url + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + token},
                    json={'model': 'generated-model', 'messages': [], 'stream': True}) as response:
                assert response.status_code == 200
                parts = response.iter_bytes()
                assert next(parts).startswith(b'data: ')
                with pytest.raises(httpx.RemoteProtocolError):
                    list(parts)
                assert not service.release.is_set()
        assert service.hits == ['/v1/chat/completions']
        service.release.set()
        assert service.finished.wait(2)


def test_no_candidate_lease_keeps_grants_and_complete_streams_available(
        owner_home, inference_service, authorization_clock):
    clock = authorization_clock
    service = inference_service
    with SubscriptionBroker(owner_home, service.origin, port=0, authorization=None) as broker:
        enroll(owner_home, broker)
        held = grant_token(broker.control_path, owner_home, enrolled=True)
        clock.wall += 86400
        clock.ticks += 86400
        fresh = grant_token(broker.control_path, owner_home, enrolled=True)
        with httpx.Client(trust_env=False, timeout=3) as client:
            assert models(client, broker, held).status_code == 200
            assert models(client, broker, fresh).status_code == 200
            service.release.set()
            response = client.post(broker.base_url + '/chat/completions',
                headers={'Authorization': 'Bearer ' + fresh},
                json={'model': 'generated-model', 'messages': [], 'stream': True})
            assert response.status_code == 200
            assert response.content == PREFIX + TERMINAL
        assert service.hits == ['/v1/models', '/v1/models', '/v1/chat/completions']
