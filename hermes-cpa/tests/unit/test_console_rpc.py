"""Console outer deadlines and truthful resource failures at the owner IPC seam."""
import json
import os
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from hermes_cpa import controller as controllers, host_binding, owner_transport
from hermes_cpa.controller import CONSOLE_RPC_SECONDS, CpaController
from hermes_cpa.settings import CpaSettings


class OwnerSocket:
    def __init__(self, clock, response, *, delay=0., failure=None):
        body = json.dumps(response).encode()
        self.buffer = json.dumps({'length': len(body)}).encode() + b'\n' + body
        if failure == 'oversized':
            self.buffer = json.dumps({'length': owner_transport.CONSOLE_IPC_LIMIT + 1}).encode() + b'\n'
        self.clock, self.delay, self.failure = clock, delay, failure
        self.sent, self.timeouts = [], []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, address):
        if self.failure == 'connect':
            raise ConnectionRefusedError('generated owner unavailable')

    def sendall(self, message):
        self.sent.append(message)
        if self.failure == 'send':
            raise TimeoutError('generated partial send')

    def recv(self, size):
        if self.failure == 'reply':
            return b''
        if self.delay > self.timeouts[-1]:
            self.clock.now += self.timeouts[-1]
            raise TimeoutError('generated outer deadline')
        self.clock.now += self.delay
        self.delay = 0.
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def recv_into(self, view, size):
        data = self.recv(size)
        view[:len(data)] = data
        return len(data)


@pytest.fixture
def rpc(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=100.)
    timer = SimpleNamespace(monotonic=lambda: clock.now)
    for module in (controllers, host_binding, owner_transport):
        monkeypatch.setattr(module, 'time', timer)
    identity = {'pid': 12345, 'start_ticks': 17}
    identity_file = tmp_path / 'generated-identity.json'
    identity_file.write_text(json.dumps({'owner': identity}))
    controller = CpaController(CpaSettings(tmp_path), mode='managed')
    monkeypatch.setattr(controllers, 'private_open', lambda path: os.open(identity_file, os.O_RDONLY))
    monkeypatch.setattr(controllers, 'process_identity', lambda pid: identity)
    monkeypatch.setattr(controllers, 'peer_pid', lambda sock: identity['pid'])
    monkeypatch.setattr(controllers, 'socket_address', lambda path: nullcontext('generated-private-address'))
    request = SimpleNamespace(state=SimpleNamespace(hermes_context={
        'version': 1, 'principal': 'generated-principal', 'host_incarnation': 'generated-host',
        'home': str(tmp_path)}))
    monkeypatch.setattr(host_binding, 'controller_from_profile', lambda settings, scope: controller)

    def connect(response=None, **options):
        sock = OwnerSocket(clock, response, **options)
        monkeypatch.setattr(controllers, 'socket', SimpleNamespace(
            AF_UNIX=1, SOCK_STREAM=1, socket=lambda *args: sock))
        return sock

    return controller, request, clock, connect


def resource(controller):
    return controller.console('resource', {'_scope': {'home': str(controller.settings.home)}})


def test_hosted_console_budget_contains_owner_dispatch_without_changing_ordinary_reads(rpc):
    controller, request, clock, _ = rpc
    host_binding.read_controller(request)
    assert controller.timeout == 2.
    assert controller.request_deadline == clock.now + 4.
    # Production creates a fresh facade for each request, not a widened old one.
    controller.timeout, controller.request_deadline = 35., None
    host_binding.read_controller(request, console_deadline=clock.now + CONSOLE_RPC_SECONDS)
    assert 3.5 < controller.remaining() == CONSOLE_RPC_SECONDS < 5.


@pytest.mark.parametrize('existing_timeout,existing_deadline,entry_deadline,expected', [
    (35., None, 102., 2.), (35., 101., 104.5, 1.), (0.5, None, 104.5, 0.5),
    (35., None, 99., 0.),
])
def test_console_budget_never_extends_an_earlier_deadline(rpc, existing_timeout, existing_deadline,
                                                         entry_deadline, expected):
    controller, request, _, _ = rpc
    controller.timeout, controller.request_deadline = existing_timeout, existing_deadline
    host_binding.read_controller(request, console_deadline=entry_deadline)
    assert controller.remaining() == expected


@pytest.mark.parametrize('failure,invoked', [('connect', False), ('send', True),
                                             ('reply', True), ('oversized', True)])
def test_resource_rpc_loss_distinguishes_no_send_from_possible_invocation_and_never_replays(rpc, failure, invoked):
    controller, _, _, connect = rpc
    sock = connect(failure=failure)
    assert resource(controller) == {
        'ok': False, 'code': 'OUTCOME_UNKNOWN' if invoked else 'UNAVAILABLE',
        'effect_invoked': invoked, 'response_withheld': False}
    assert len(sock.sent) == int(invoked)


def test_expired_console_deadline_refuses_before_owner_dispatch(rpc):
    controller, _, clock, connect = rpc
    sock = connect()
    controller.request_deadline = clock.now
    assert resource(controller) == {'ok': False, 'code': 'UNAVAILABLE',
                                    'effect_invoked': False, 'response_withheld': False}
    assert not sock.sent


@pytest.mark.parametrize('reply', [None, {}, {'ok': False, 'code': 'UNAVAILABLE'},
                                   {'ok': True, 'status': 200}, {'ok': False, 'effect_invoked': 'false'}])
def test_unproven_resource_reply_is_unknown_not_a_no_invocation_certificate(rpc, reply):
    controller, _, _, connect = rpc
    sock = connect(reply)
    assert resource(controller) == {'ok': False, 'code': 'OUTCOME_UNKNOWN',
                                    'effect_invoked': True, 'response_withheld': False}
    assert len(sock.sent) == 1


@pytest.mark.parametrize('reply', [
    {'ok': False, 'code': 'OUTCOME_UNKNOWN', 'effect_invoked': True, 'response_withheld': False},
    {'ok': False, 'code': 'RESOURCE_REVOKED', 'effect_invoked': False, 'response_withheld': False},
    {'ok': False, 'code': 'RESPONSE_TOO_LARGE', 'effect_invoked': True, 'response_withheld': True},
    {'ok': True, 'status': 503, 'headers': {'content-type': 'text/plain'}, 'body_base64': 'ZXJyb3I='},
])
def test_inner_timeout_or_observed_native_error_reaches_backend_after_two_seconds(rpc, reply):
    controller, request, clock, connect = rpc
    host_binding.read_controller(request, console_deadline=clock.now + CONSOLE_RPC_SECONDS)
    sock = connect(reply, delay=2.25)
    assert resource(controller) == reply
    assert clock.now == 102.25
    assert len(sock.sent) == 1
    assert max(sock.timeouts) <= CONSOLE_RPC_SECONDS


def test_native_cli_console_timeout_is_not_replaced_by_hosted_budget(rpc):
    controller, _, _, connect = rpc
    controller.timeout = 15.
    sock = connect({'ok': True, 'echo': 'generated'}, delay=6.)
    assert controller.console('echo', {'_scope': {'home': str(controller.settings.home)}}) == {
        'ok': True, 'echo': 'generated'}
    assert len(sock.sent) == 1


def test_lost_execute_reply_still_requires_receipt_reconciliation(rpc):
    controller, _, _, connect = rpc
    sock = connect(failure='reply')
    assert controller.console('execute', {'_scope': {'home': str(controller.settings.home)}}) == {
        'ok': False, 'code': 'RECONCILE_REQUIRED'}
    assert len(sock.sent) == 1
