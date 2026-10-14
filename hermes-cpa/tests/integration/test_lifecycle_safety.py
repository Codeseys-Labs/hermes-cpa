"""Generated harmless processes; these do not assert real CPA compatibility."""
from contextlib import contextmanager
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cpa import authorization
from hermes_cpa.settings import CpaSettings
from hermes_cpa.supervisor import CpaSupervisor
from hermes_cpa.controller import CpaController


def ready(timeout=1):
    return SimpleNamespace(management_ready=True, compatible=True,
                           inference_configured=False, reason="")


def unavailable(timeout=1):
    return SimpleNamespace(management_ready=False, compatible=False,
                           inference_configured=None, reason="unavailable")


@contextmanager
def harmless_owner(tmp_path, *, authorization=None):
    settings = CpaSettings(tmp_path)
    settings.ensure_directories()
    settings.config_path.write_text('server: {host: 127.0.0.1, port: 8318}\n')
    sup = CpaSupervisor(settings, mode='managed', probe=unavailable,
                        policy=lambda: True, readiness_timeout=.25,
                        authorization=authorization,
                        command=[sys.executable, '-c', 'import time; time.sleep(30)'])
    assert sup.acquire_lock()
    try:
        yield sup
    finally:
        child = sup.child_process
        sup.cleanup()
        if sup._worker is not None:
            sup._worker.join(2)
            assert not sup._worker.is_alive()
        assert child is None or child.poll() is not None


@pytest.fixture
def owner(tmp_path):
    with harmless_owner(tmp_path) as supervisor:
        yield supervisor


@pytest.fixture
def leased_owner(tmp_path, monkeypatch):
    clock = SimpleNamespace(wall=time.time(), ticks=time.monotonic())
    monkeypatch.setattr(authorization, 'time', SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.ticks))
    deadline = authorization.AuthorizationDeadline(clock.wall + 60)
    with harmless_owner(tmp_path, authorization=deadline) as supervisor:
        yield supervisor, clock


def test_no_ready_on_popen_and_timeout_reaps(owner):
    assert not owner.start_child()
    assert owner.state.value == 'failed'
    assert owner.sanitized_reason == 'readiness_timeout'
    assert owner.child_process.poll() is not None


def test_ready_requires_typed_compatible_probe(owner):
    owner.probe = lambda timeout: (True, {}, {})
    assert not owner.start_child()
    owner.probe = ready
    assert owner.start_child()
    assert owner.status()['management_ready'] is True
    assert owner.status()['inference_configured'] is False


def test_early_exit_and_explicit_restart(owner):
    owner.readiness_timeout = 5
    owner.command = [sys.executable, '-c', 'raise SystemExit(7)']
    assert not owner.start_child()
    assert owner.sanitized_reason == 'child_exited'
    assert owner.child_process.returncode == 7
    owner.command = [sys.executable, '-c', 'import time; time.sleep(30)']
    owner.probe = ready
    assert owner.start_child()
    owner.child_process.kill()
    owner.child_process.wait(timeout=2)
    assert owner.status()['state'] == 'failed'
    assert owner.start_child()


def test_foreign_private_listener_is_replaced_never_adopted(owner):
    from hermes_cpa.owner_transport import TransportError
    from hermes_cpa.process_owner import private_directory, socket_address
    private_directory(owner.settings.private_dir)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as outsider:
        with socket_address(owner.settings.private_dir, 'control.sock') as address:
            outsider.bind(address)
        outsider.listen()
        outsider.settimeout(.2)
        owner.probe = ready
        assert owner.start_child()
        # The stale path was the lock holder's to remove; nothing reaches the outsider.
        with pytest.raises(TransportError) as refused:
            owner.control.request('GET', '/v8/management/config', timeout=.5)
        assert refused.value.code == 'UNAVAILABLE'
        with pytest.raises(socket.timeout):
            outsider.accept()


def test_unsafe_private_socket_path_refuses_launch(owner, tmp_path):
    from hermes_cpa.process_owner import private_directory
    private_directory(owner.settings.private_dir)
    owner.settings.control_socket_path.symlink_to(tmp_path / 'elsewhere')
    owner.probe = ready
    assert not owner.start_child()
    assert owner.sanitized_reason == 'launch_failed'
    assert owner.child_process is None


def test_external_default_never_mutates_or_launches(tmp_path):
    controller = CpaController(CpaSettings(tmp_path))
    assert not controller.start()
    assert not controller.stop()
    assert not controller.restart()
    assert controller.apply_config('a' * 64, {'request_retry': 1})['code'] == 'UNOWNED'
    assert not (tmp_path / 'cpa').exists()


def test_lock_loser_cleanup_does_not_remove_owner_socket(owner):
    owner.settings.socket_path.write_text('sentinel')
    contender = CpaSupervisor(owner.settings)
    assert not contender.acquire_lock()
    contender.cleanup()
    assert owner.settings.socket_path.read_text() == 'sentinel'


def test_identity_mismatch_never_signals_outsider(owner):
    owner.probe = ready
    assert owner.start_child()
    real = owner.child_process
    owner.child_identity = {'pid': os.getpid(), 'start': 'invalid'}
    assert not owner.stop_child(grace_period=.1)
    assert real.poll() is None
    # Restore the trusted handle's identity solely to clean our test child.
    from hermes_cpa.process_owner import process_identity
    owner.child_identity = process_identity(real.pid)


def test_unfinished_journal_blocks_inference(owner):
    journal = owner.settings.config_path.with_suffix('.journal')
    journal.write_text('{"phase":"applied"}')
    journal.chmod(0o600)
    owner.probe = ready
    assert not owner.start_child()
    assert owner.sanitized_reason == 'recovery_required'
    assert owner.child_process is None


def test_probe_success_after_deadline_is_not_ready(owner):
    def late_probe(timeout):
        time.sleep(.3)
        return ready()
    owner.probe = late_probe
    assert not owner.start_child()
    assert owner.sanitized_reason == 'readiness_timeout'
    assert owner.child_process.poll() is not None


def test_watchdog_latches_shutdown_before_policy_can_reenable(owner):
    owner.probe = ready
    assert owner.start_child()
    owner.policy = lambda: False
    assert not owner.watchdog()
    assert owner.child_process.poll() is not None
    owner.policy = lambda: True
    assert not owner.start_child()


def test_watchdog_preserves_ownership_failure(owner):
    owner.probe = ready
    assert owner.start_child()
    identity = owner.child_identity
    owner.child_identity = {'pid': os.getpid(), 'start': 'invalid'}
    owner.policy = lambda: False
    try:
        assert not owner.watchdog()
        assert owner.state.value == 'ownership_lost'
        assert owner.child_process.poll() is None
    finally:
        owner.child_identity = identity


def test_live_child_probe_failure_degrades_without_restart(owner):
    owner.probe = ready
    assert owner.start_child()
    child = owner.child_process
    owner.probe = unavailable
    owner.check_health()
    assert owner.status()['state'] == 'degraded'
    assert not owner.status()['management_ready']
    assert child.poll() is None
    owner.probe = ready
    owner.check_health()
    assert owner.status()['state'] == 'ready'
    assert owner.child_process is child


def test_crash_from_degraded_is_terminal(owner):
    owner.probe = ready
    assert owner.start_child()
    owner.probe = unavailable
    owner.check_health()
    owner.child_process.kill()
    owner.child_process.wait(timeout=2)
    assert owner.status()['state'] == 'failed'


def test_owner_view_revalidates_generation_and_shutdown(owner):
    view = owner.transaction_owner()
    owner.generation += 1
    assert view.generation == owner.generation
    owner._shutdown = True
    assert view.policy_locked


def test_stop_escalates_and_reaps(owner):
    marker = owner.settings.instances_dir / 'sigterm-ready'
    owner.command = [sys.executable, '-c',
                     'import signal,time; from pathlib import Path; '
                     'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                     f'Path({str(marker)!r}).touch(); time.sleep(30)']
    owner.probe = lambda timeout=1: ready(timeout) if marker.is_file() else unavailable(timeout)
    owner.readiness_timeout = 2
    assert owner.start_child()
    started = time.monotonic()
    assert owner.stop_child(grace_period=.1)
    assert time.monotonic() - started < 2
    assert owner.child_process.returncode == -9


def test_expired_authorization_denies_start_without_launch(leased_owner):
    owner, clock = leased_owner
    owner.probe = ready
    clock.ticks += 60
    assert not owner.start_child()
    assert owner.child_process is None


def test_authorization_expiring_during_probe_never_reports_ready(leased_owner):
    owner, clock = leased_owner

    def expiring_probe(timeout):
        clock.ticks += 60
        return ready()

    owner.probe = expiring_probe
    assert not owner.start_child()
    assert not owner.status()['management_ready']
    assert owner.child_process.poll() is not None


def test_expiry_reaps_child_without_transient_policy_grace(leased_owner):
    owner, clock = leased_owner
    owner.probe = ready
    assert owner.start_child()
    child = owner.child_process

    def unreadable_policy():
        raise OSError('generated unavailable policy')

    owner.policy = unreadable_policy
    assert not owner.watchdog()
    assert child.poll() is None  # Ordinary policy read failure still gets its grace.
    clock.wall -= 3600
    clock.ticks += 60
    assert not owner.watchdog()
    assert child.poll() is not None
    owner.policy = lambda: True
    assert not owner.start_child()


def test_accepted_operation_cannot_start_after_authorization_expires(leased_owner):
    owner, clock = leased_owner
    owner.probe = ready
    # Queue the operation behind an unrelated mutation, then expire its lease.
    with owner.mutation_gate:
        submitted = owner.submit(dict(action='start', request_id='lease-expiry',
                                      expected_generation=owner.generation))
        assert submitted['ok']
        clock.ticks += 60
    until = time.monotonic() + 2
    while True:
        result = owner.dispatch({'cmd': 'operation', 'args': {'operation_id': 'lease-expiry'}})
        if result['state'] in ('succeeded', 'failed') or time.monotonic() >= until:
            break
        time.sleep(.01)
    assert result['state'] == 'failed'
    assert result['code'] == 'POLICY_LOCKED'
    assert owner.child_process is None


def test_ipc_accept_tick_reaps_before_policy_watchdog_interval(leased_owner):
    owner, clock = leased_owner
    owner.probe = ready
    assert owner.start_child()
    child = owner.child_process
    checked = threading.Event()
    owner.policy = lambda: checked.set() or True
    owner.watchdog_interval = 30
    failures = []

    def serve():
        try:
            owner.run_ipc()
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert checked.wait(2), 'owner never reached its initial policy check'
        controller = CpaController(owner.settings, mode='managed', timeout=2)
        assert controller.snapshot()['management_ready']
        # A served request proves the initial policy/health pass has completed.
        clock.ticks += 60
        thread.join(3)
        assert not thread.is_alive(), 'expiry waited for the policy watchdog interval'
        assert not failures
        assert child.poll() is not None
        assert not owner.settings.socket_path.exists()
    finally:
        owner.running = False
        thread.join(3)


def test_no_candidate_lease_preserves_ordinary_owner_lifetime(owner):
    owner.probe = ready
    assert owner.start_child()
    child = owner.child_process
    assert owner.watchdog()
    assert owner.status()['management_ready']
    assert child.poll() is None
    assert owner.stop_child()
    assert child.poll() is not None
