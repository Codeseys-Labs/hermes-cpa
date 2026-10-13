"""Run with prepare_backend_overlay.py: actual backend engine plus real children."""
import json
import time
from pathlib import Path

import pytest
from hermes_cpa.config_store import ConfigStore
from hermes_cpa.controller import CpaController, build_owner
from test_lifecycle_safety import owner, ready, unavailable

pytestmark = pytest.mark.skipif(not hasattr(ConfigStore, 'transact'), reason='requires backend overlay')


def test_authenticated_backend_probe_with_real_http_child(owner):
    import sys
    from hermes_cpa.controller import DeadlineAdapter as CpaAdapter
    from hermes_cpa.process_owner import owns_loopback_listener
    owner.command = [sys.executable, str(Path(__file__).with_name('fixture_http_child.py')),
                     str(owner.port), 'valid']
    adapter = CpaAdapter(f'http://127.0.0.1:{owner.port}', 'generated-management')
    owner.probe = lambda timeout: adapter.probe()
    owner.readiness_timeout = 1.
    assert owner.start_child()
    assert owns_loopback_listener(owner.child_process.pid, owner.port)
    assert owner.status()['inference_configured'] is None
    assert owner.stop_child()
    owner.command[-1] = 'wrong-service'
    assert not owner.start_child()
    assert owner.child_process.poll() is not None
    owner.command[-1] = 'valid'
    wrong = CpaAdapter(f'http://127.0.0.1:{owner.port}', 'wrong-generated-key')
    owner.probe = lambda timeout: wrong.probe()
    assert not owner.start_child()
    assert owner.child_process.poll() is not None


def test_transaction_real_restart_and_exact_rollback(owner):
    store = ConfigStore(owner.settings.config_path)
    owner.settings.config_path.chmod(0o600)
    owner.transaction = lambda sup, req: store.transact(req['expected_revision'], req['changes'],
        expected_generation=req['expected_generation'], owner=sup.transaction_owner(),
        restart_consent=req['restart_consent'])
    owner.probe = ready
    assert owner.start_child()
    old_process = owner.child_process
    request = dict(expected_revision=store.compute_digest(), expected_generation=owner.generation,
                   changes={'request_retry': 7}, restart_consent=True)
    result = owner.apply_transaction(request)
    assert result['ok']
    assert old_process.poll() is not None
    assert store.read_config()['routing']['retry']['request-retry'] == 7
    original = owner.settings.config_path.read_bytes()
    good = owner.child_process
    failed_pid = None
    def fail_one_generation(timeout):
        nonlocal failed_pid
        if failed_pid is None:
            failed_pid = owner.child_process.pid
        return unavailable(timeout) if owner.child_process.pid == failed_pid else ready(timeout)
    owner.probe = fail_one_generation
    request.update(expected_revision=store.compute_digest(), changes={'request_retry': 2})
    result = owner.apply_transaction(request)
    assert result['code'] == 'RESTART_FAILED'
    assert result['rollback'] == 'recovered'
    assert good.poll() is not None
    assert owner.settings.config_path.read_bytes() == original
    assert owner.state.value == 'ready'
    assert owner.settings.config_path.stat().st_mode & 0o777 == 0o600


def test_transaction_consent_generation_and_failure(owner):
    store = ConfigStore(owner.settings.config_path)
    owner.settings.config_path.chmod(0o600)
    owner.transaction = lambda sup, req: store.transact(req['expected_revision'], req['changes'],
        expected_generation=req['expected_generation'], owner=sup.transaction_owner(),
        restart_consent=req.get('restart_consent', False))
    owner.probe = ready
    assert owner.start_child()
    request = dict(expected_revision=store.compute_digest(), expected_generation=owner.generation,
                   changes={'request_retry': 2}, restart_consent=False)
    assert owner.apply_transaction(request)['code'] == 'CONSENT_REQUIRED'
    request.update(restart_consent=True, expected_generation=0)
    assert owner.apply_transaction(request)['code'] == 'STALE_GENERATION'
    request.update(expected_generation=owner.generation)
    owner.probe = unavailable
    result = owner.apply_transaction(request)
    assert result['code'] == 'RESTART_FAILED'
    assert result['rollback'] == 'restart_failed'
    assert owner.state.value == 'failed'
    assert owner.child_process.poll() is not None


def test_owner_factory_locked_secret_no_partial_launch(owner, tmp_path):
    package = tmp_path / 'package'
    package.mkdir()
    (package / 'plugin.yaml').write_text('name: hermes-cpa')
    (owner.settings.home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings: {mode: managed}\n')
    def locked(home):
        raise RuntimeError('CANARY-DO-NOT-EMIT')
    built = build_owner(owner.settings, package, secret_resolver=locked)
    assert built.probe is None
    assert built.child_process is None
    assert 'CANARY' not in json.dumps(built.status())


def test_native_installer_seam_uses_public_view_and_explicit_inputs(owner):
    class Installer:
        def upgrade(self, archive_path, view, *, expected_generation, expected_revision, native_consent=False):
            assert archive_path == 'generated.tar.gz'
            assert not hasattr(view, 'stop_child')
            assert expected_generation == view.generation
            assert expected_revision == 'a' * 64
            assert native_consent is True
            return {'ok': False, 'code': 'RECOVERY_REQUIRED'}

        def remove(self, view, *, expected_generation, expected_revision, native_consent=False):
            assert expected_generation == view.generation
            assert expected_revision == 'a' * 64
            assert native_consent is True
            view.assert_owned()
            assert view.stop_owned_and_reap()
            return {'ok': True, 'data_retained': True, 'package_removed': False}

    owner.probe = ready
    assert owner.start_child()
    kwargs = dict(expected_generation=owner.generation, expected_revision='a' * 64, native_consent=True)
    assert owner.upgrade_owned(Installer(), 'generated.tar.gz', **kwargs)['code'] == 'RECOVERY_REQUIRED'
    assert owner.child_process.poll() is None
    result = owner.prepare_removal(Installer(), **kwargs)
    assert result['data_retained'] and not result['package_removed']
    assert owner.child_process.poll() is not None
    assert owner._shutdown and owner._exit_deadline is not None


def test_transaction_while_stopped_stays_stopped(owner):
    store = ConfigStore(owner.settings.config_path)
    owner.settings.config_path.chmod(0o600)
    result = store.transact(store.compute_digest(), {'request_retry': 5},
        expected_generation=owner.generation, owner=owner.transaction_owner())
    assert result['ok']
    assert owner.child_process is None
    assert store.read_config()['routing']['retry']['request-retry'] == 5


def test_external_unknown_not_false(owner):
    from hermes_cpa.cpa_adapter import ProbeResult
    class Adapter:
        def probe(self):
            return ProbeResult(True, None, True, 'OK')
    controller = CpaController(owner.settings, adapter=Adapter())
    status = controller.get_status()
    assert status.management_ready
    assert status.inference_configured is None
    assert not controller.stop()
