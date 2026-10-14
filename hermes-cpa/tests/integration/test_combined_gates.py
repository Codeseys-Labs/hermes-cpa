"""Real supervisor + revised installer + pinned backend, generated assets only."""
import threading
from pathlib import Path

from hermes_cpa.config_store import ConfigStore
from test_lifecycle_safety import owner, ready
from test_private_owner_channels import private_owner, put
import importlib.util

_gate_spec = importlib.util.spec_from_file_location(
    'combined_gate_fixtures', Path(__file__).parents[1] / 'gates/test_gates.py')
assert _gate_spec is not None and _gate_spec.loader is not None
_gate_fixtures = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(_gate_fixtures)
case = _gate_fixtures.case


def wire(case, owner):
    # Keep generated assets but point the real installer at the real owner home.
    case.installer.settings = owner.settings
    owner.settings.config_path.chmod(0o600)
    return dict(expected_generation=owner.generation,
                expected_revision=ConfigStore(owner.settings.config_path).compute_digest(),
                native_consent=True)


def test_valid_upgrade_refuses_without_stopping_real_child(case, owner):
    args = wire(case, owner)
    owner.probe = ready
    assert owner.start_child()
    child = owner.child_process
    before = owner.settings.config_path.read_bytes()
    result = owner.upgrade_owned(case.installer, case.archive, **args)
    assert result['code'] == 'RECOVERY_REQUIRED' and not result['ok']
    assert child.poll() is None
    assert not owner.settings.binary_path.exists()
    assert owner.settings.config_path.read_bytes() == before


def test_actual_removal_retains_data_and_exits_owner_loop(case, owner):
    args = wire(case, owner)
    paths = [owner.settings.config_path, owner.settings.instances_dir / 'auth-sentinel',
             owner.settings.home / 'provider-sentinel', case.root / 'LICENSE']
    for path in paths[1:3]:
        path.write_bytes(b'generated retained sentinel')
    before = {path: path.read_bytes() for path in paths}
    owner.probe = ready
    assert owner.start_child()
    child = owner.child_process
    loop = threading.Thread(target=owner.run_ipc)
    loop.start()
    try:
        result = owner.prepare_removal(case.installer, **args)
        assert result['ok'] and result['removal_prepared']
        assert result['package_removed'] is False and result['supervisor_terminated'] is None
        assert child.poll() is not None
        loop.join(timeout=3)
        assert not loop.is_alive()
        assert all(path.read_bytes() == raw for path, raw in before.items())
    finally:
        owner.running = False
        loop.join(timeout=3)


def test_refused_removal_does_not_latch_shutdown(case, owner):
    args = wire(case, owner)
    args['native_consent'] = False
    owner.probe = ready
    assert owner.start_child()
    assert owner.prepare_removal(case.installer, **args)['code'] == 'CONSENT_REQUIRED'
    assert not owner._shutdown and owner.child_process.poll() is None


def test_private_channel_transaction_restart_persists(tmp_path):
    with private_owner(tmp_path) as (owner, _):
        store = ConfigStore(owner.settings.config_path)
        assert owner.start_child()
        old = owner.child_process
        result = store.transact(store.compute_digest(), [put('server/port', 18406)],
            expected_generation=owner.generation, owner=owner.transaction_owner(), restart_consent=True)
        assert result['ok'] and old.poll() is not None, str(result) + str(owner.status())
        assert owner.status()['management_ready']
        assert owner.child_process.pid != old.pid
        fresh_store = ConfigStore(owner.settings.config_path)
        assert fresh_store.read_config()['server']['port'] == 18406
        assert fresh_store.compute_digest() == result['revision']
