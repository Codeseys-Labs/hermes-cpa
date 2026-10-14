"""Actual backend engine plus real generated children over the private owner channels."""
import json

import pytest
from hermes_cpa.config_store import ConfigStore
from hermes_cpa.controller import CpaController, build_owner
from test_lifecycle_safety import owner, ready, unavailable
from test_private_owner_channels import private_owner, put

pytestmark = pytest.mark.skipif(not hasattr(ConfigStore, 'transact'), reason='requires backend overlay')

PORT = [put('server/port', 18404)]


def test_transaction_real_restart_and_exact_rollback(tmp_path):
    with private_owner(tmp_path) as (owner, _):
        store = ConfigStore(owner.settings.config_path)
        assert owner.start_child()
        old_process = owner.child_process
        result = store.transact(store.compute_digest(), PORT, expected_generation=owner.generation,
                                owner=owner.transaction_owner(), restart_consent=True)
        assert result['ok'], result
        assert old_process.poll() is not None
        assert store.read_config()['server']['port'] == 18404
        original = owner.settings.config_path.read_bytes()
        good = owner.child_process
        failed_pid = None
        probe = owner.probe
        def fail_one_generation(timeout):
            nonlocal failed_pid
            if failed_pid is None:
                failed_pid = owner.child_process.pid
            return unavailable(timeout) if owner.child_process.pid == failed_pid else probe(timeout)
        owner.probe, owner.readiness_timeout = fail_one_generation, 3.
        result = store.transact(store.compute_digest(), [put('server/port', 18405)],
                                expected_generation=owner.generation,
                                owner=owner.transaction_owner(), restart_consent=True)
        assert result['code'] == 'RESTART_FAILED'
        assert result['rollback'] == 'recovered'
        assert good.poll() is not None
        assert owner.settings.config_path.read_bytes() == original
        assert owner.state.value == 'ready'
        assert owner.settings.config_path.stat().st_mode & 0o777 == 0o600


def test_legacy_apply_consent_generation_and_native_refusal(tmp_path):
    with private_owner(tmp_path) as (owner, _):
        store = ConfigStore(owner.settings.config_path)
        assert owner.start_child()
        request = dict(expected_revision=store.compute_digest(), expected_generation=owner.generation,
                       changes={'request_retry': 2}, restart_consent=False)
        assert owner.apply_transaction(request)['code'] == 'CONSENT_REQUIRED'
        request.update(restart_consent=True, expected_generation=0)
        assert owner.apply_transaction(request)['code'] == 'STALE_GENERATION'
        before = store.config_path.read_bytes()
        request.update(expected_generation=owner.generation, changes={'request_retry': 'x'})
        assert owner.apply_transaction(request)['code'] == 'INVALID_INPUT'
        assert store.config_path.read_bytes() == before


def test_restart_failure_is_reported_and_left_stopped(tmp_path):
    with private_owner(tmp_path) as (owner, _):
        store = ConfigStore(owner.settings.config_path)
        assert owner.start_child()
        owner.probe, owner.readiness_timeout = unavailable, 1.
        result = store.transact(store.compute_digest(), PORT, expected_generation=owner.generation,
                                owner=owner.transaction_owner(), restart_consent=True)
        assert result['code'] == 'RESTART_FAILED'
        assert result['rollback'] == 'restart_failed'
        assert owner.state.value == 'failed'
        assert owner.child_process.poll() is not None


def test_owner_factory_fails_closed_without_reading_credentials(owner, tmp_path):
    package = tmp_path / 'package'
    package.mkdir()
    (package / 'plugin.yaml').write_text('name: hermes-cpa')
    home = owner.settings.home
    (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings: {mode: managed}\n')
    (home / '.env').write_text('HERMES_CPA_MANAGEMENT_KEY=CANARY-DO-NOT-EMIT\n')
    # No accepted release metadata in this package: admission fails before any launch.
    built = build_owner(owner.settings, package, console_factory=lambda owner, transport: None)
    status = built.status()
    assert status['reason'] == 'secret_or_policy_unavailable' and status['state'] == 'failed'
    assert built.console is None and built.child_process is None
    assert built.submit({'action': 'start', 'request_id': 'r', 'expected_generation': built.generation})['code'] == 'POLICY_LOCKED'
    assert 'CANARY' not in json.dumps(status)


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
