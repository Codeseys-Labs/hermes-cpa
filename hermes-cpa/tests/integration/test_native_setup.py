"""Consented native setup, refusal before mutation and managed plugin lifetime.

Generated homes only; no live home, account or credential. Real-CPA cases need
CPA_TEST_ARCHIVE naming the original pinned linux_amd64 archive, the patched host
on PYTHONPATH and generated homes outside ~/.hermes. The permit below is an
expiring disposable validation permit, not release acceptance. Model listing is
served by the generated local CPA; nothing reaches an upstream provider.
"""
import hashlib
import json
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hermes_cpa import cli
from hermes_cpa.controller import controller_from_profile
from hermes_cpa.installer import EXPECTED_TAR_SHA, digest
from hermes_cpa.process_owner import check_process_alive
from hermes_cpa.settings import CpaSettings

PACKAGE = Path(__file__).resolve().parents[2]


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    assert predicate(), 'bounded wait failed'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def tree(root):
    """Every path, mode and file content under root, including root itself."""
    result = {'.': os.lstat(root).st_mode}
    for path in sorted(Path(root).rglob('*')):
        st = path.lstat()
        result[str(path.relative_to(root))] = (st.st_mode, path.read_bytes() if path.is_file() else None)
    return result


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text(
        'plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings: {mode: external}\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    return home


def permit_for(home, path):
    path.write_text(json.dumps(dict(version=1, purpose='isolated-candidate-test',
        home=str(home.resolve()), package_root=str(PACKAGE.resolve()),
        manifest_sha256=digest(PACKAGE / 'compatibility.json'), expires_at=time.time() + 600)))
    path.chmod(0o600)
    return path


def warm_host(home):
    """Import and read through the host once so snapshots see only setup effects."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config
    from hermes_cpa.host_compat import host_capabilities
    host_capabilities()
    token = set_hermes_home_override(home)
    try:
        load_config()
    finally:
        reset_hermes_home_override(token)


def test_unsupported_host_refuses_before_any_mutation(home, tmp_path, monkeypatch, capsys):
    from agent.command_token_source import CommandTokenSource
    warm_host(home)
    monkeypatch.setattr(CommandTokenSource, 'supports_single_request_grants', False)
    before = tree(home)
    assert cli.main(['setup', '--download', '--consent']) == 1
    assert 'Unsupported Hermes CPA host' in capsys.readouterr().out
    assert cli.main(['provider-enroll', '--consent', '--base-url', 'http://127.0.0.1:9/v1',
                     '--control', str(tmp_path / 'supervisor.sock')]) == 1
    assert 'Unsupported Hermes CPA host' in capsys.readouterr().out
    assert tree(home) == before, 'unsupported host changed home state'
    # Remembered managed startup is also refused before any lease or launch.
    (home / 'config.yaml').write_text(
        'plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n'
        '      settings: {mode: managed, autostart: true}\n')
    before = tree(home)
    from hermes_cpa.lifetime import RegistrationLifetime
    assert RegistrationLifetime(home, PACKAGE).begin() == 'unsupported_host'
    assert tree(home) == before


def test_occupied_port_refuses_before_credentials(home, tmp_path, capsys):
    permit = permit_for(home, tmp_path / 'permit.json')
    warm_host(home)
    with socket.socket() as outsider:
        outsider.bind(('127.0.0.1', 0))
        outsider.listen()
        port = outsider.getsockname()[1]
        before = tree(home)
        assert cli.main(['setup', '--archive', str(tmp_path / 'never-read.tar.gz'), '--consent',
                         '--port', str(port), '--broker-port', str(free_port()),
                         '--isolated-authorization', str(permit)]) == 1
        assert 'port_in_use' in capsys.readouterr().out
        assert tree(home) == before, 'an independent listener caused a partial setup'
        outsider.settimeout(.5)
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            pass  # The outsider is still serving; nothing adopted or replaced it.


@pytest.fixture
def archive():
    path = os.environ.get('CPA_TEST_ARCHIVE')
    if not path:
        pytest.skip('explicit offline pinned CPA archive required')
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == EXPECTED_TAR_SHA
    return path


def registration():
    from hermes_cpa import register
    callbacks, hooks = [], {}
    register(SimpleNamespace(on_unload=callbacks.append,
                             register_cli_command=lambda **kwargs: None,
                             register_hook=lambda name, callback: hooks.setdefault(name, callback)))
    return hooks, lambda: [callback() for callback in reversed(callbacks)]


def hermes_edit(home, change):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config, save_config
    token = set_hermes_home_override(home)
    try:
        cfg = load_config()
        change(cfg)
        save_config(cfg)
    finally:
        reset_hermes_home_override(token)


def test_consented_setup_then_plugin_lifetime(home, tmp_path, archive, capsys):
    # Hermes's own writer establishes the generated policy and its durable epoch.
    hermes_edit(home, lambda cfg: None)
    permit = permit_for(home, tmp_path / 'permit.json')
    settings = CpaSettings(home)
    port, broker_port = free_port(), free_port()
    owner_pid = None
    try:
        code = cli.main(['setup', '--archive', archive, '--consent', '--port', str(port),
                         '--broker-port', str(broker_port), '--isolated-authorization', str(permit)])
        output = capsys.readouterr().out
        assert code == 0, output
        assert 'Owner: running with persistent lifetime' in output, output
        assert 'Provider: custom:cpa enrolled' in output and 'Next Hermes load: resumes' in output
        controller = controller_from_profile(settings, PACKAGE)
        status = controller.snapshot()
        assert status['state'] == 'ready' and status['broker'] == 'ready', status
        assert status['lifetime'] == 'persistent'
        owner_pid, child_pid, generation = status['owner']['pid'], status['pid'], status['generation']
        text = (home / 'config.yaml').read_text()
        from ruamel.yaml import YAML
        provider = YAML(typ='safe').load(text)['providers']['cpa']
        assert provider['base_url'] == f'http://127.0.0.1:{broker_port}/v1' and 'key_cmd' in provider
        assert not {'api_key', 'key_env', 'api_key_env'} & set(provider)
        from agent.secret_scope import load_env_file
        keys = load_env_file(home / '.env')
        assert keys['HERMES_CPA_INFERENCE_KEY'] not in text
        assert keys['HERMES_CPA_MANAGEMENT_KEY'] != keys['HERMES_CPA_INFERENCE_KEY']

        # An unrelated Hermes config edit must not revoke the long-lived owner.
        hermes_edit(home, lambda cfg: cfg['plugins']['entries'].update(
            {'generated-other-plugin': {'settings': {'flag': True}}}))
        time.sleep(6)  # longer than the owner's watchdog interval
        status = controller.snapshot()
        assert (status['state'], status['generation'], status['pid']) == ('ready', generation, child_pid)

        # Next Hermes load: two concurrent registrations attach without a second owner.
        first_hooks, first_unload = registration()
        second_hooks, second_unload = registration()
        assert first_hooks['on_session_start']() is None
        second_hooks['on_session_start']()
        assert controller.snapshot()['owner']['pid'] == owner_pid
        first_unload()
        time.sleep(1)
        assert controller.snapshot()['pid'] == child_pid, 'unrelated unload stopped shared services'

        # Explicit stop ends the persistent owner; a remembered-autostart load resumes
        # with a plugin-lifetime owner that ends with its last registration.
        second_unload()
        assert cli.main(['stop']) == 0
        wait_for(lambda: not check_process_alive(owner_pid))
        assert not check_process_alive(child_pid)
        hooks, unload = registration()
        hooks['on_session_start']()
        status = controller.snapshot()
        assert status['state'] == 'ready' and status['lifetime'] == 'plugin', status
        owner_pid, child_pid = status['owner']['pid'], status['pid']
        unload()
        wait_for(lambda: not check_process_alive(owner_pid), seconds=20)
        assert not check_process_alive(child_pid)
        assert not (settings.run_dir / 'subscription' / 'supervisor.sock').exists()

        # An independent listener on the managed port is never adopted or stopped.
        with socket.socket() as outsider:
            outsider.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            outsider.bind(('127.0.0.1', port))
            outsider.listen()
            hooks, unload = registration()
            hooks['on_session_start']()
            status = controller.snapshot()
            assert status['state'] == 'failed' and status['reason'] == 'port_in_use', status
            owner_pid = status['owner']['pid']
            unload()
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass
            wait_for(lambda: not check_process_alive(owner_pid), seconds=20)

        # Actual disable revokes a running owner even with no unload.
        hooks, unload = registration()
        hooks['on_session_start']()
        status = controller.snapshot()
        owner_pid, child_pid = status['owner']['pid'], status['pid']
        hermes_edit(home, lambda cfg: cfg['plugins'].update(enabled=[], disabled=['hermes-cpa']))
        wait_for(lambda: not check_process_alive(owner_pid), seconds=15)
        assert not check_process_alive(child_pid)
        unload()
    finally:
        controller_from_profile(settings, PACKAGE).unload()
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', port))
