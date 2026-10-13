"""Read-only recovery discovery and explicit lost-owner bootstrap contracts."""
import hashlib
import json
from hermes_cpa.config_store import ConfigStore
from hermes_cpa.controller import CpaController
from hermes_cpa.settings import CpaSettings
from hermes_cpa import cli


def test_recovery_discovery_is_safe_read_only_and_identifies_exact_bytes(tmp_path):
    settings = CpaSettings(tmp_path)
    settings.ensure_directories()
    store = ConfigStore(settings.config_path)
    store._atomic(settings.config_path, b'server: {}\nsecret: GENERATED-CANARY\n')
    original = store.compute_digest()
    store.snapshot()
    store._journal('applying', original)
    controller = CpaController(settings, mode='managed')
    before = {p: p.read_bytes() for p in settings.cpa_dir.rglob('*') if p.is_file()}
    report = controller.recovery_status()
    assert report == dict(available=True, generation=0, owner_available=False,
                         current_revision=original, snapshot_revision=original,
                         journal_phase='applying', recovery_required=True)
    assert 'GENERATED-CANARY' not in json.dumps(report)
    assert before == {p: p.read_bytes() for p in settings.cpa_dir.rglob('*') if p.is_file()}
    assert controller._launched == []


def test_recovery_discovery_refuses_symlink_and_sanitizes_journal(tmp_path):
    settings = CpaSettings(tmp_path)
    settings.ensure_directories()
    store = ConfigStore(settings.config_path)
    store._atomic(settings.config_path, b'server: {}\n')
    store._journal('GENERATED-CANARY', store.compute_digest())
    controller = CpaController(settings, mode='managed')
    report = controller.recovery_status()
    assert report['journal_phase'] == 'invalid' and report['recovery_required']
    assert 'GENERATED-CANARY' not in json.dumps(report)
    settings.config_path.with_suffix('.snapshot').symlink_to(settings.config_path)
    report = controller.recovery_status()
    assert not report['available'] and report['recovery_required']


def test_bootstrap_cli_requires_consent_before_launch(monkeypatch, tmp_path, capsys):
    controller = CpaController(CpaSettings(tmp_path), mode='managed')
    monkeypatch.setattr(cli, 'controller_from_profile', lambda: controller)
    launched = []
    monkeypatch.setattr(controller, 'bootstrap_owner', lambda: launched.append(True) or {'ok': True, 'generation': 7})
    assert cli.main(['bootstrap-owner']) == 1
    assert launched == []
    assert cli.main(['bootstrap-owner', '--consent']) == 0
    assert launched == [True]
    assert 'Generation: 7' in capsys.readouterr().out


def test_doctor_reports_safe_recovery_inputs(monkeypatch, tmp_path, capsys):
    controller = CpaController(CpaSettings(tmp_path), mode='managed')
    monkeypatch.setattr(cli, 'controller_from_profile', lambda: controller)
    assert cli.main(['doctor']) == 0
    output = capsys.readouterr().out
    assert 'Owner Available: False' in output and 'Generation: 0' in output
    assert 'Recovery Required:' in output and 'Snapshot Revision:' in output


def test_bootstrap_does_not_report_failed_owner_as_available(monkeypatch, tmp_path):
    controller = CpaController(CpaSettings(tmp_path), mode='managed')
    monkeypatch.setattr(controller, 'ensure_supervisor', lambda: True)
    monkeypatch.setattr(controller, 'snapshot', lambda: dict(generation=1,
        state='failed', reason='secret_or_policy_unavailable'))
    assert controller.bootstrap_owner() == {'ok': False, 'code': 'UNAVAILABLE'}

