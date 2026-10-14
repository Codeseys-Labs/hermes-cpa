"""Actual hermes-owner-v1 CPA under the owner's private channels, not Desktop acceptance.

CPA_TEST_ARCHIVE must name the pinned hermes-owner-v1 runtime archive recorded in
cpa-runtime.json. Command injection authorizes this isolated test only, never
release metadata. Generated homes and keys only; no provider account is used.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket

import httpx
import pytest
from hermes_cpa.config_store import ConfigStore
from hermes_cpa.installer import CpaInstaller
from hermes_cpa.runtime_manifest import parse_runtime_manifest
from hermes_cpa.settings import CpaSettings
from hermes_cpa.supervisor import CpaSupervisor, InstallPolicy

PACKAGE = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def put(path, value):
    return {'method': 'PUT', 'path': '/v8/management/config/' + path,
            'content_type': 'application/json',
            'body_base64': base64.b64encode(json.dumps(value).encode()).decode()}


@pytest.fixture
def pinned_owner(tmp_path):
    archive_path = os.environ.get('CPA_TEST_ARCHIVE')
    if not archive_path:
        pytest.skip('explicit hermes-owner-v1 runtime archive required')
    artifact = parse_runtime_manifest((PACKAGE / 'cpa-runtime.json').read_bytes()).artifact
    if artifact is None:
        pytest.skip('hermes-owner-v1 runtime is not built and pinned yet')
    raw = Path(archive_path).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == artifact.archive_sha256
    settings = CpaSettings((tmp_path / 'home').resolve())
    settings.ensure_directories()
    settings.binary_path.write_bytes(CpaInstaller(settings, PACKAGE).archive_binary_bytes(raw))
    settings.binary_path.chmod(0o700)
    package = tmp_path / 'package'
    package.mkdir()
    (package / 'plugin.yaml').write_text('name: hermes-cpa\n')
    policy_file = settings.home / 'config.yaml'
    policy_file.write_text('plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings: {mode: managed}\n')
    port = free_port()
    store = ConfigStore(settings.config_path)
    store.create_initial_config(port, access_keys=['generated-public-client-key'])
    binary, config = str(settings.binary_path), str(settings.config_path)
    owner = CpaSupervisor(settings, mode='managed', policy=InstallPolicy(settings.home, package),
        command=[binary, '-config', config, '-local-model'],
        prepare_command=[binary, '-config', config, '--hermes-prepare-config'],
        readiness_timeout=15, stop_grace=2)
    assert owner.acquire_lock()
    try:
        assert owner.start_child(), owner.sanitized_reason
        yield owner, store, policy_file
    finally:
        child = owner.child_process
        owner.cleanup()
        assert child is None or child.poll() is not None


def test_actual_cpa_private_control_without_keys(pinned_owner):
    owner, store, _ = pinned_owner
    assert owner.status()['management_ready']
    assert not (owner.settings.home / '.env').exists()
    response = owner.control.request('GET', '/v8/management/config', headers={'Accept': 'application/json'})
    assert response.status == 200 and json.loads(response.body)['config-version'] == 8
    assert response.headers['x-hermes-config-revision'] == store.compute_digest()


def test_actual_cpa_hot_and_restart_transactions_keep_the_native_inode(pinned_owner):
    owner, store, _ = pinned_owner
    first, inode = owner.child_process, store.config_path.stat().st_ino
    result = owner.apply_transaction(dict(expected_revision=store.compute_digest(),
        expected_generation=owner.generation, changes={'request_retry': 4}, restart_consent=True))
    assert result['ok'], result
    assert owner.child_process is first and first.poll() is None, 'hot change restarted CPA'
    assert store.read_config()['routing']['retry']['request-retry'] == 4
    port = free_port()
    result = store.transact(store.compute_digest(), [put('server/port', port)],
        expected_generation=owner.generation, owner=owner.transaction_owner(), restart_consent=True)
    assert result['ok'] and 'restart_required' in result['effects'], result
    assert first.poll() is not None and owner.child_process.pid != first.pid
    assert owner.status()['management_ready']
    assert store.config_path.stat().st_ino == inode
    assert owner.stop_child() and owner.start_child()
    config = store.read_config()
    assert config['server']['port'] == port and config['routing']['retry']['request-retry'] == 4


def test_actual_cpa_stopped_transaction_prepares_offline(pinned_owner):
    owner, store, _ = pinned_owner
    assert owner.stop_child()
    port = free_port()
    result = store.transact(store.compute_digest(), [put('server/port', port)],
        expected_generation=owner.generation, owner=owner.transaction_owner())
    assert result['ok'], result
    assert owner.child_process.poll() is not None
    assert store.read_config()['server']['port'] == port
    refused = store.transact(store.compute_digest(), [put('server/port', 'not-a-port')],
        expected_generation=owner.generation, owner=owner.transaction_owner())
    assert not refused['ok'] and refused['code'] in ('INVALID_CONFIG', 'INVALID_REQUEST'), refused


def test_actual_cpa_broker_models_over_private_inference(pinned_owner):
    from hermes_cpa.subscription_broker import SubscriptionBroker
    from hermes_cpa.subscription_client import grant_token
    owner, _, _ = pinned_owner
    home = owner.settings.home
    with SubscriptionBroker(home, owner.inference, port=0) as broker:
        token = grant_token(broker.control_path, home)
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.get(broker.base_url + '/models', headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200, response.text
        assert isinstance(response.json().get('data'), list)


def test_actual_cpa_revocation_after_apply_restores_stopped(pinned_owner, monkeypatch):
    owner, store, policy_file = pinned_owner
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
    result = store.transact(store.compute_digest(), [put('server/port', free_port())],
        expected_generation=owner.generation, owner=owner.transaction_owner(), restart_consent=True)
    assert result['code'] == 'POLICY_LOCKED', result
    assert result['rollback'] == 'restored_stopped', result
    assert first.poll() is not None
    assert store.config_path.read_bytes() == original
    assert not owner.start_child()
    assert not owner.control.probe().management_ready
