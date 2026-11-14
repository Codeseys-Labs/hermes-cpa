"""A completion slower than the connect bound is relayed, not refused; generated loopback only."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from hermes_cpa.subscription_broker import SubscriptionBroker
from hermes_cpa.subscription_client import grant_token

KEY = 'generated-slow-inference-key'
REPLY = json.dumps({'id': 'generated', 'object': 'chat.completion', 'choices': []}).encode()


@pytest.fixture
def owner_home(tmp_path, monkeypatch):
    from hermes_cli.config import save_env_value
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

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
def slow_service():
    """Answers a non-streaming completion only after a delay, like a reasoning model."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        delay = 1.5

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            time.sleep(self.delay)
            self.send_response(200 if self.headers.get('Authorization') == 'Bearer ' + KEY else 401)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(REPLY)))
            self.end_headers()
            self.wfile.write(REPLY)
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_completion_slower_than_connect_bound_is_relayed(owner_home, slow_service, monkeypatch):
    origin, _ = slow_service
    monkeypatch.setattr(SubscriptionBroker, 'CONNECT_TIMEOUT', .5)
    with SubscriptionBroker(owner_home, origin, port=0) as broker:
        token = grant_token(broker.control_path, owner_home)
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.post(broker.base_url + '/chat/completions',
                                   headers={'Authorization': 'Bearer ' + token},
                                   json={'model': 'generated-model', 'messages': [], 'stream': False})
    assert response.status_code == 200
    assert response.content == REPLY


def test_completion_past_response_deadline_is_still_refused(owner_home, slow_service, monkeypatch):
    origin, _ = slow_service
    monkeypatch.setattr(SubscriptionBroker, 'CONNECT_TIMEOUT', .5)
    monkeypatch.setattr(SubscriptionBroker, 'RESPONSE_TIMEOUT', 1)
    with SubscriptionBroker(owner_home, origin, port=0) as broker:
        token = grant_token(broker.control_path, owner_home)
        with httpx.Client(trust_env=False, timeout=10) as client:
            try:
                response = client.post(broker.base_url + '/chat/completions',
                                       headers={'Authorization': 'Bearer ' + token},
                                       json={'model': 'generated-model', 'messages': [], 'stream': False})
            except httpx.RemoteProtocolError:
                return  # The deadline closed the connection before a status line.
    assert response.status_code == 502
