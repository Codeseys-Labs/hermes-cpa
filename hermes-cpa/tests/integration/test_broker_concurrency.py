"""Default-owner broker under ordinary parallel consumers; generated service only.

Requires the patched host on PYTHONPATH. No real provider, account or credential.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from hermes_cpa.subscription_broker import SubscriptionBroker
from hermes_cpa.subscription_client import grant_token

KEY = 'generated-inference-key'


@pytest.fixture
def inference_service():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            hits.append(body['model'])
            ok = self.headers.get('Authorization') == 'Bearer ' + KEY
            raw = json.dumps({'model': body['model'], 'choices': []}).encode()
            self.send_response(200 if ok else 401)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.fixture
def owner_home(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import save_env_value
    home = (tmp_path / 'owner').resolve()
    workers = []
    for name in ('alpha', 'beta'):
        worker = home / 'profiles' / name
        worker.mkdir(parents=True)
        (worker / 'profile.yaml').write_text(f'name: {name}\n')
        workers.append(worker)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    token = set_hermes_home_override(home)
    try:
        save_env_value('HERMES_CPA_INFERENCE_KEY', KEY)
    finally:
        reset_hermes_home_override(token)
    return home, workers


def test_parallel_consumers_wait_for_resolution_and_revocation_fails_closed(
        owner_home, inference_service):
    home, (alpha, beta) = owner_home
    origin, hits = inference_service
    with SubscriptionBroker(home, origin, port=0) as broker:
        tokens = {worker: grant_token(broker.control_path, worker) for worker in (alpha, beta)}
        # One slow filesystem resolution for alpha holds the single resolver slot.
        resolving, proceed = threading.Event(), threading.Event()
        consumer = broker._consumer

        def slow_consumer(path):
            if str(path) == str(alpha):
                resolving.set()
                proceed.wait(1.)
            return consumer(path)
        broker._consumer = slow_consumer
        results = {}

        def send(worker):
            with httpx.Client(trust_env=False, timeout=10) as client:
                results[worker] = client.post(broker.base_url + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + tokens[worker]},
                    json={'model': worker.name, 'messages': []}).status_code

        first = threading.Thread(target=send, args=(alpha,))
        first.start()
        assert resolving.wait(2), 'slow resolution never started'
        second = threading.Thread(target=send, args=(beta,))
        second.start()
        time.sleep(.3)  # beta is now queued behind alpha's in-flight resolution
        proceed.set()
        first.join(10)
        second.join(10)
        assert results == {alpha: 200, beta: 200}, 'overlapping consumer falsely rejected'
        assert sorted(hits) == ['alpha', 'beta']
        broker._consumer = consumer

        # Revocation still fails closed for every outstanding grant.
        tokens = {worker: grant_token(broker.control_path, worker) for worker in (alpha, beta)}
        broker.revoke()
        for worker in (alpha, beta):
            send(worker)
        assert results == {alpha: 401, beta: 401}
        assert sorted(hits) == ['alpha', 'beta'], 'revoked grant reached the upstream'


def test_resolution_past_budget_fails_closed_without_unbounded_wait(owner_home, inference_service):
    home, (alpha, beta) = owner_home
    origin, hits = inference_service
    with SubscriptionBroker(home, origin, port=0) as broker:
        tokens = {worker: grant_token(broker.control_path, worker) for worker in (alpha, beta)}
        consumer, release = broker._consumer, threading.Event()

        def stuck_consumer(path):
            if str(path) == str(alpha):
                release.wait(broker.RESOLVE_TIMEOUT + 1)
            return consumer(path)
        broker._consumer = stuck_consumer
        results = {}

        def send(worker):
            with httpx.Client(trust_env=False, timeout=10) as client:
                results[worker] = client.post(broker.base_url + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + tokens[worker]},
                    json={'model': worker.name, 'messages': []}).status_code

        first = threading.Thread(target=send, args=(alpha,))
        first.start()
        time.sleep(.2)
        started = time.monotonic()
        send(beta)
        elapsed = time.monotonic() - started
        first.join(10)
        release.set()
        assert results == {alpha: 401, beta: 401}
        assert elapsed < broker.RESOLVE_TIMEOUT + 1, 'queued consumer waited past the shared budget'
        assert hits == []
        broker._consumer = consumer
        time.sleep(1.2)  # the abandoned job finishes and frees the single slot
        tokens[beta] = grant_token(broker.control_path, beta)
        send(beta)
        assert results[beta] == 200
