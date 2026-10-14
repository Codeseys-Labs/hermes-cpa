"""Actual abrupt process exit at durable transaction phases, no fake result."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from hermes_cpa.config_store import ConfigStore
from test_transactions import Owner, PORT

PYTHONPATH = os.pathsep.join([str(Path(__file__).parents[2] / 'src'),
                              str(Path(__file__).parents[1] / 'backend')])


@pytest.mark.parametrize('phase', ['prepared', 'stopped', 'applying', 'applied', 'committed'])
def test_process_exit_after_durable_phase_requires_truthful_recovery(tmp_path, phase):
    config = tmp_path / 'config.yaml'
    store = ConfigStore(config)
    store.create_initial_config(18318, access_keys=['generated-public-client-key'])
    before = config.read_bytes()
    original = store.compute_digest()
    code = '''
import os,sys
from pathlib import Path
from hermes_cpa.config_store import ConfigStore
from test_transactions import Owner, PORT
store=ConfigStore(Path(sys.argv[1]))
owner=Owner(store); owner.running=False
def start():
    raise AssertionError('stopped transaction must not start')
owner.start_owned_and_poll_readiness=start
original=store._journal
def crash(phase, revision):
    original(phase, revision)
    if phase == sys.argv[2]: os._exit(73)
store._journal=crash
store.transact(store.compute_digest(), PORT, expected_generation=1, owner=owner)
raise AssertionError('phase was not reached')
'''
    proc = subprocess.run([sys.executable, '-c', code, str(config), phase],
        env=dict(PATH='/usr/bin:/bin', HOME=str(tmp_path), TMPDIR=str(tmp_path), PYTHONPATH=PYTHONPATH),
        capture_output=True, text=True, timeout=10)
    assert proc.returncode == 73, proc.stderr
    assert json.loads(config.with_suffix('.journal').read_bytes())['phase'] == phase
    assert config.stat().st_mode & 0o777 == 0o600
    expected_port = 18319 if phase in ('applied', 'committed') else 18318
    assert store.read_config()['server']['port'] == expected_port
    owner = Owner(store)
    owner.running = False
    if phase == 'committed':
        assert store.recover(expected_revision=store.compute_digest(), snapshot_revision=original,
            expected_generation=owner.generation, owner=owner, native_consent=True)['code'] == 'NO_RECOVERY_REQUIRED'
    else:
        outcome = store.transact(store.compute_digest(), PORT,
            expected_generation=owner.generation, owner=owner)
        assert outcome['code'] == 'RECOVERY_REQUIRED'
        assert store.recover(expected_revision=store.compute_digest(), snapshot_revision=original,
            expected_generation=owner.generation, owner=owner, native_consent=True)['ok']
        assert config.read_bytes() == before
        assert not owner.running


@pytest.mark.parametrize('phase', ['recovering', 'recovered'])
def test_process_exit_during_explicit_recovery(tmp_path, phase):
    store = ConfigStore(tmp_path / 'config.yaml')
    store.create_initial_config(18318, access_keys=['generated-public-client-key'])
    original = store.compute_digest()
    store.snapshot()
    store._write_in_place(store.config_path.read_bytes().replace(b'request-retry: 3', b'request-retry: 4'))
    store._journal('applying', original)
    code = '''
import os,sys
from pathlib import Path
from hermes_cpa.config_store import ConfigStore
from test_transactions import Owner
store=ConfigStore(Path(sys.argv[1]))
owner=Owner(store); owner.running=False
original=store._journal
def crash(phase, revision):
    original(phase, revision)
    if phase == sys.argv[2]: os._exit(74)
store._journal=crash
store.recover(expected_revision=store.compute_digest(), snapshot_revision=sys.argv[3],
    expected_generation=owner.generation, owner=owner, native_consent=True)
raise AssertionError('phase was not reached')
'''
    proc = subprocess.run([sys.executable, '-c', code, str(store.config_path), phase, original],
        env=dict(PATH='/usr/bin:/bin', HOME=str(tmp_path), TMPDIR=str(tmp_path), PYTHONPATH=PYTHONPATH),
        capture_output=True, text=True, timeout=10)
    assert proc.returncode == 74, proc.stderr
    assert json.loads(store.config_path.with_suffix('.journal').read_bytes())['phase'] == phase
    owner = Owner(store)
    owner.running = False
    if phase == 'recovering':
        assert store.read_config()['routing']['retry']['request-retry'] == 4
        assert store.recover(expected_revision=store.compute_digest(), snapshot_revision=original,
            expected_generation=owner.generation, owner=owner, native_consent=True)['ok']
    assert store.compute_digest() == original
    assert not owner.running
