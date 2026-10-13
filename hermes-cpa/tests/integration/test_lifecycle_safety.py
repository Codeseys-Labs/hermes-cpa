"""Generated harmless processes; these do not assert real CPA compatibility."""
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from hermes_cpa.settings import CpaSettings
from hermes_cpa.supervisor import CpaSupervisor
from hermes_cpa.controller import CpaController


def ready(timeout=1):
    return SimpleNamespace(management_ready=True, compatible=True,
                           inference_configured=False, reason="")


def unavailable(timeout=1):
    return SimpleNamespace(management_ready=False, compatible=False,
                           inference_configured=None, reason="unavailable")


@pytest.fixture
def owner(tmp_path):
    settings = CpaSettings(tmp_path)
    settings.ensure_directories()
    settings.config_path.write_text('server: {host: 127.0.0.1, port: 8318}\n')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    sup = CpaSupervisor(settings, mode='managed', port=port, probe=unavailable,
                        policy=lambda: True, readiness_timeout=.25,
                        command=[sys.executable, '-c', 'import time; time.sleep(30)'])
    assert sup.acquire_lock()
    yield sup
    child = sup.child_process
    sup.cleanup()
    assert child is None or child.poll() is not None


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
    owner.command = [sys.executable, '-c', 'raise SystemExit(7)']
    assert not owner.start_child()
    assert owner.sanitized_reason == 'child_exited'
    owner.command = [sys.executable, '-c', 'import time; time.sleep(30)']
    owner.probe = ready
    assert owner.start_child()
    owner.child_process.kill()
    owner.child_process.wait(timeout=2)
    assert owner.status()['state'] == 'failed'
    assert owner.start_child()


def test_occupied_port_never_adopted(owner):
    owner.probe = ready
    with socket.socket() as outsider:
        outsider.bind(('127.0.0.1', owner.port))
        outsider.listen()
        assert not owner.start_child()
        assert owner.sanitized_reason == 'port_in_use'
        assert owner.child_process is None
        assert outsider.getsockname()[1] == owner.port


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
    owner.command = [sys.executable, '-c',
                     'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']
    owner.probe = ready
    assert owner.start_child()
    time.sleep(.15)
    started = time.monotonic()
    assert owner.stop_child(grace_period=.1)
    assert time.monotonic() - started < 2
    assert owner.child_process.returncode == -9
