"""Real process guards survive transient callers but not supervisor death.

The generated CPA fixture uses the real process_owner launcher, private socket
peer checks and Linux parent-death signal. No public listener or provider runs.
"""
import ctypes
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading

from hermes_cpa.process_owner import check_process_alive, process_identity
from test_detached_owner import wait_for
from test_private_owner_channels import private_owner


def transient_call(action):
    outcome = {}

    def run():
        try:
            outcome['result'] = action()
        except Exception as exc:
            outcome['error'] = exc

    caller = threading.Thread(target=run)
    caller.start()
    caller.join(15)
    assert not caller.is_alive(), 'transient caller did not finish'
    if 'error' in outcome:
        raise outcome['error']
    assert outcome['result'] is True


def start_and_restart_from_transient_threads(owner):
    transient_call(owner.start_child)
    first = owner.child_process
    assert first.poll() is None
    assert owner.control.probe(timeout=2).management_ready, 'start caller exit killed CPA'
    transient_call(lambda: owner.stop_child() and owner.start_child())
    assert first.poll() is not None
    assert owner.child_process.pid != first.pid
    # A health response requires the child to run after the caller thread has
    # exited. A pre-existing readiness flag cannot satisfy this assertion.
    assert owner.control.probe(timeout=2).management_ready, 'restart caller exit killed CPA'
    assert owner.private_peer() == owner.child_identity


def test_private_health_survives_transient_start_and_restart_callers(tmp_path):
    with private_owner(tmp_path) as (owner, _):
        start_and_restart_from_transient_threads(owner)
        assert owner.stop_child()
        assert owner.child_process.poll() is not None


OWNER_SCRIPT = """
import json
from pathlib import Path
import sys
from test_child_creator_lifetime import start_and_restart_from_transient_threads
from test_private_owner_channels import private_owner

with private_owner(Path(sys.argv[1])) as (owner, _):
    start_and_restart_from_transient_threads(owner)
    print(json.dumps(owner.child_identity), flush=True)
    sys.stdin.buffer.read(1)
"""


def test_owner_sigkill_still_kills_child_after_transient_restart(tmp_path):
    # Adopt only this generated owner's child so the test can prove SIGKILL and
    # reap it instead of depending on the machine's init to reap grandchildren.
    libc = ctypes.CDLL(None)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    supervisor, child, reaped = None, None, False
    try:
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join([
            str(Path(__file__).parents[2] / 'src'), str(Path(__file__).parent),
            env.get('PYTHONPATH', '')])
        supervisor = subprocess.Popen([sys.executable, '-c', OWNER_SCRIPT, str(tmp_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        assert select.select([supervisor.stdout], [], [], 30)[0], 'owner did not report a live restarted child'
        packet = supervisor.stdout.readline(4096)
        assert packet, 'owner exited before proving private health after transient restart'
        child = json.loads(packet)
        assert process_identity(child['pid']) == child and check_process_alive(child['pid'])
        supervisor.kill()
        assert supervisor.wait(timeout=5) == -signal.SIGKILL
        wait_for(lambda: not check_process_alive(child['pid']), seconds=5)
        waited, status = os.waitpid(child['pid'], 0)
        reaped = True
        assert waited == child['pid'] and os.WIFSIGNALED(status)
        assert os.WTERMSIG(status) == signal.SIGKILL
        assert not Path(f"/proc/{child['pid']}").exists()
    finally:
        try:
            if supervisor is not None:
                if supervisor.poll() is None:
                    supervisor.kill()
                supervisor.wait(timeout=5)
                for stream in (supervisor.stdin, supervisor.stdout, supervisor.stderr):
                    stream.close()
            if child is not None and not reaped:
                # Cleanup even if the regression is a missing parent-death guard.
                if process_identity(child['pid']) == child and check_process_alive(child['pid']):
                    os.kill(child['pid'], signal.SIGKILL)
                wait_for(lambda: not check_process_alive(child['pid']), seconds=5)
                try:
                    os.waitpid(child['pid'], 0)
                except ChildProcessError:
                    pass
        finally:
            assert libc.prctl(36, previous.value, 0, 0, 0) == 0
