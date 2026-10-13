"""Real detached processes, sockets, locks and authoritative disposable policy."""
import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from hermes_cpa.controller import CpaController
from hermes_cpa.settings import CpaSettings
from hermes_cpa.process_owner import check_process_alive, process_identity


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), 'bounded wait failed'


@pytest.fixture
def detached(tmp_path):
    # The test runner adopts killed-owner grandchildren so we can prove reaping,
    # not rely on the host's init to reap zombies eventually.
    libc = ctypes.CDLL(None)
    prior = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(prior), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    home = tmp_path / ('long-home-' * 12)
    settings = CpaSettings(home)
    settings.ensure_directories()
    settings.config_path.write_text('generated-only: true\n')
    package = tmp_path / 'installed'
    package.mkdir()
    (package / 'plugin.yaml').write_text('name: hermes-cpa\n')
    (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings: {mode: managed}\n')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    cmd = [sys.executable, str(Path(__file__).with_name('fixture_owner.py')), str(home), str(package), str(port)]
    controller = CpaController(settings, mode='managed', launch_command=cmd, timeout=2.)
    pids = []
    yield controller, package, pids
    controller.unload()
    for process in controller._launched:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    for pid in pids:
        wait_for(lambda: not check_process_alive(pid))
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        assert not Path(f'/proc/{pid}').exists()
    libc.prctl(36, prior.value, 0, 0, 0)


def test_explicit_bootstrap_returns_generation_without_starting_cpa(detached):
    controller, _, pids = detached
    assert controller.snapshot()['generation'] == 0
    boot = controller.bootstrap_owner()
    assert boot['ok'] and boot['generation'] > 0
    assert controller.snapshot()['pid'] is None
    assert controller.bootstrap_owner()['generation'] == boot['generation']
    result = controller.submit_operation(dict(action='start', request_id='bootstrap-test',
                                              expected_generation=boot['generation']))
    assert controller.wait_operation(result)['ok']
    pids.append(controller.snapshot()['pid'])


def test_concurrent_clients_one_owner_and_long_socket(detached):
    controller, _, pids = detached
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert all(pool.map(lambda _: controller.start(), range(5)))
    status = controller.snapshot()
    pids.append(status['pid'])
    assert os.getsid(status['owner']['pid']) == status['owner']['pid']
    assert len(str(controller.settings.socket_path)) > 108
    before = status['pid']
    # An independent short-lived client can connect then exit without stopping owner.
    code = ('from pathlib import Path; from hermes_cpa.settings import CpaSettings; '
            'from hermes_cpa.controller import CpaController; '
            'import sys; c=CpaController(CpaSettings(Path(sys.argv[1])),mode="managed"); '
            'assert c.snapshot()["pid"] == int(sys.argv[2])')
    subprocess.run([sys.executable, '-c', code, str(controller.settings.home), str(before)],
                   env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(Path(__file__).parents[2] / 'src')},
                   check=True, timeout=3)
    assert controller.snapshot()['pid'] == before
    assert controller.stop()


@pytest.mark.parametrize('started', [False, True])
def test_explicit_stop_exits_idle_supervisor(detached, started):
    controller, _, pids = detached
    assert controller.bootstrap_owner()['ok']
    if started:
        assert controller.start()
        pids.append(controller.snapshot()['pid'])
    owner_pid = controller.snapshot()['owner']['pid']
    assert controller.stop()
    wait_for(lambda: not check_process_alive(owner_pid))
    assert not controller.settings.socket_path.exists()


def test_trickling_control_request_does_not_hold_owner(detached):
    from hermes_cpa.process_owner import socket_address
    controller, _, _ = detached
    assert controller.ensure_supervisor()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(.4)
        with socket_address(controller.settings.run_dir) as address:
            client.connect(address)
        start = time.monotonic()
        closed = False
        for _ in range(16):
            try:
                client.sendall(b' ')
            except OSError:
                closed = True
                break
            time.sleep(.1)
        assert closed
        assert time.monotonic() - start < 1.6
    assert controller.snapshot()['generation'] > 0


@pytest.mark.parametrize('change', ['disable', 'remove', 'corrupt'])
def test_watchdog_stops_and_reaps(detached, change):
    controller, package, pids = detached
    assert controller.start()
    pids.append(controller.snapshot()['pid'])
    started = time.monotonic()
    if change == 'remove':
        (package / 'plugin.yaml').unlink()
    else:
        (controller.settings.home / 'config.yaml').write_text(
            'plugins: {enabled: []}' if change == 'disable' else 'broken: [')
    wait_for(lambda: not check_process_alive(pids[0]))
    assert time.monotonic() - started < 2
    assert controller.settings.config_path.exists()


def test_owner_sigkill_parent_death_reaped(detached):
    controller, _, pids = detached
    assert controller.start()
    status = controller.snapshot()
    pids.append(status['pid'])
    os.kill(status['owner']['pid'], signal.SIGKILL)
    wait_for(lambda: not check_process_alive(pids[0]))
    os.waitpid(pids[0], 0)
    assert not Path(f'/proc/{pids[0]}').exists()
    # The test independently reaped the child; a disconnected client cannot
    # infer that observation from loss of its owner transport.
    assert controller.snapshot()['state'] == 'failed'
    assert controller.snapshot()['reason'] == 'supervisor_unavailable'


def test_stale_identity_never_signals_current_process(detached):
    controller, _, pids = detached
    assert controller.start()
    pids.append(controller.snapshot()['pid'])
    path = controller.settings.identity_path
    saved = path.read_bytes()
    path.write_text(json.dumps({'owner': process_identity(os.getpid()), 'generation': 1}))
    try:
        assert not controller.stop()
        assert check_process_alive(pids[0])
    finally:
        path.write_bytes(saved)


def test_idempotence_collision_generation_and_event_bounds(detached):
    controller, _, pids = detached
    assert controller.ensure_supervisor()
    req = {'request_id': 'same', 'expected_generation': controller.generation, 'action': 'start'}
    first = controller.submit_operation(req)
    assert first['ok']
    assert first['state'] in ('pending', 'running')
    done = controller.wait_operation(first)
    assert done['ok']
    pids.append(controller.snapshot()['pid'])
    assert controller.submit_operation(req) == done
    assert controller.submit_operation(dict(req, action='stop'))['code'] == 'REQUEST_CONFLICT'
    assert controller.submit_operation(dict(req, request_id='new', expected_generation=0))['code'] == 'STALE_GENERATION'
    assert len(controller.get_events()['events']) <= 100
    assert controller.get_operation('missing')['code'] == 'RECONCILE_REQUIRED'
