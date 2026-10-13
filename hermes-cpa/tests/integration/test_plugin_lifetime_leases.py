"""Plugin-lifetime ownership follows registration leases; generated harmless child only."""
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from hermes_cpa.process_owner import acquire_lease
from hermes_cpa.settings import CpaSettings
from hermes_cpa.supervisor import CpaSupervisor, InstallPolicy

MANAGED = 'plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings: {mode: managed}\n'


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), 'bounded wait failed'


@pytest.fixture
def make_owner(tmp_path):
    started = []

    def make(lifetime, child='import time; time.sleep(30)'):
        settings = CpaSettings(tmp_path / 'home')
        settings.ensure_directories()
        package = tmp_path / 'package'
        package.mkdir(exist_ok=True)
        (package / 'plugin.yaml').write_text('name: hermes-cpa\n')
        (settings.home / 'config.yaml').write_text(MANAGED)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        owner = CpaSupervisor(settings, mode='managed', port=port,
            probe=lambda timeout: SimpleNamespace(management_ready=True, compatible=True,
                                                 inference_configured=False),
            policy=InstallPolicy(settings.home, package), command=[sys.executable, '-c', child],
            readiness_timeout=.5, stop_grace=.5, watchdog_interval=.05,
            lifetime=lifetime, lease_grace=.3)
        assert owner.acquire_lock()
        thread = threading.Thread(target=owner.run_ipc)
        started.append((owner, thread))
        return owner, thread

    yield make
    for owner, thread in started:
        owner.running = False
        if thread.ident is not None:
            thread.join(timeout=5)
        owner.cleanup()
        assert owner.child_process is None or owner.child_process.poll() is not None


def test_one_registration_ending_keeps_owner_for_another(make_owner):
    owner, thread = make_owner('plugin')
    first = acquire_lease(owner.settings.run_dir)
    second = acquire_lease(owner.settings.run_dir)
    assert first is not None and second is not None
    assert owner.start_child()
    child = owner.child_process
    thread.start()
    os.close(first)
    time.sleep(owner.lease_grace * 4)
    assert thread.is_alive() and child.poll() is None, 'unrelated release stopped a held lifetime'
    os.close(second)
    wait_for(lambda: not thread.is_alive())
    assert child.poll() is not None, 'owned child must be reaped when the last lifetime ends'
    assert not owner.settings.socket_path.exists()


def test_returning_registration_within_grace_keeps_owner(make_owner):
    owner, thread = make_owner('plugin')
    lease = acquire_lease(owner.settings.run_dir)
    assert owner.start_child()
    thread.start()
    os.close(lease)  # Forced rediscovery: unload then reload.
    lease = acquire_lease(owner.settings.run_dir, timeout=1.)
    assert lease is not None
    time.sleep(owner.lease_grace * 4)
    assert thread.is_alive() and owner.child_process.poll() is None
    os.close(lease)
    wait_for(lambda: not thread.is_alive())


def test_closing_owner_is_never_reattached(make_owner):
    # The child ignores SIGTERM, so the closing owner holds its decision while it escalates.
    owner, thread = make_owner(
        'plugin', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)')
    lease = acquire_lease(owner.settings.run_dir)
    assert owner.start_child()
    thread.start()
    os.close(lease)
    wait_for(lambda: owner._shutdown)
    assert acquire_lease(owner.settings.run_dir) is None, 'attached to an owner that decided to exit'
    wait_for(lambda: not thread.is_alive())
    fresh = acquire_lease(owner.settings.run_dir)
    assert fresh is not None
    os.close(fresh)


def test_persistent_owner_ignores_leases(make_owner):
    owner, thread = make_owner('persistent')
    assert owner.start_child()
    thread.start()
    time.sleep(owner.lease_grace * 4)
    assert thread.is_alive() and owner.child_process.poll() is None
    assert owner.status()['lifetime'] == 'persistent'
