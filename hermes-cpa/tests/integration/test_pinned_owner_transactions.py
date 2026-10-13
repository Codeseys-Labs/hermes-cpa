"""Actual pinned CPA under owner stop/apply/restart, not Desktop acceptance.

CPA_TEST_ARCHIVE must name the retained verified offline release archive.
The command injection authorizes this isolated test only, never release metadata.
"""
import hashlib
import os
from pathlib import Path
import secrets
import socket
import tarfile

import pytest
from hermes_cpa.config_store import ConfigStore
from hermes_cpa.controller import DeadlineAdapter
from hermes_cpa.settings import CpaSettings
from hermes_cpa.supervisor import CpaSupervisor, InstallPolicy


@pytest.fixture
def pinned_owner(tmp_path):
    archive_path = os.environ.get('CPA_TEST_ARCHIVE')
    if not archive_path:
        pytest.skip('explicit offline pinned CPA archive required')
    archive = Path(archive_path)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == 'a40164e25a60869df2c489702383d4e64dc27bb08eaafd7b04a101a2f5cfc625'
    with tarfile.open(archive) as tar:
        member = tar.getmember('cli-proxy-api')
        assert member.isfile() and member.size < 100 * 1024 * 1024
        extracted = tar.extractfile(member)
        assert extracted is not None
        raw = extracted.read()
    assert hashlib.sha256(raw).hexdigest() == '95435293c56e856db7d70b377d30b2b2c00e32ff1c06c41ae46758a9f6febdee'
    settings = CpaSettings(tmp_path / 'home')
    settings.ensure_directories()
    settings.binary_path.write_bytes(raw)
    settings.binary_path.chmod(0o700)
    package = tmp_path / 'package'
    package.mkdir()
    (package / 'plugin.yaml').write_text('name: hermes-cpa\n')
    policy_file = settings.home / 'config.yaml'
    policy_file.write_text('plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings: {mode: managed}\n')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    management, inference = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    store = ConfigStore(settings.config_path)
    store.create_initial_config(port, management, inference)
    adapter = DeadlineAdapter(f'http://127.0.0.1:{port}', secret=lambda: management)
    owner = CpaSupervisor(settings, mode='managed', port=port,
        policy=InstallPolicy(settings.home, package), probe=adapter.probe,
        command=[str(settings.binary_path), '-config', str(settings.config_path), '-local-model'],
        readiness_timeout=10, stop_grace=2)
    assert owner.acquire_lock()
    try:
        assert owner.start_child(), owner.sanitized_reason
        yield owner, store, adapter, policy_file
    finally:
        child = owner.child_process
        owner.cleanup()
        assert child is None or child.poll() is not None
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))


def test_actual_cpa_owner_restart_persists(pinned_owner):
    owner, store, adapter, _ = pinned_owner
    first = owner.child_process
    result = store.transact(store.compute_digest(), {'request_retry': 4},
        expected_generation=owner.generation, owner=owner.transaction_owner(), restart_consent=True)
    assert result['ok'], result
    assert first.poll() is not None
    assert owner.child_process.pid != first.pid
    assert adapter.probe().management_ready
    assert store.read_config()['routing']['retry']['request-retry'] == 4
    assert owner.stop_child()
    assert owner.start_child()
    assert store.read_config()['routing']['retry']['request-retry'] == 4
    assert adapter.probe().management_ready


def test_actual_cpa_revocation_after_apply_restores_stopped(pinned_owner, monkeypatch):
    owner, store, adapter, policy_file = pinned_owner
    first = owner.child_process
    original = store.config_path.read_bytes()
    policy = policy_file.read_bytes()
    journal = store._journal
    def revoke(phase, revision):
        journal(phase, revision)
        if phase == 'applied':
            policy_file.write_text('plugins: {enabled: []}\n')
            policy_file.write_bytes(policy)
    monkeypatch.setattr(store, '_journal', revoke)
    result = store.transact(store.compute_digest(), {'request_retry': 4},
        expected_generation=owner.generation, owner=owner.transaction_owner(), restart_consent=True)
    assert result['code'] == 'POLICY_LOCKED', result
    assert result['rollback'] == 'restored_stopped', result
    assert first.poll() is not None
    assert store.config_path.read_bytes() == original
    assert not owner.start_child()
    assert not adapter.probe().management_ready
