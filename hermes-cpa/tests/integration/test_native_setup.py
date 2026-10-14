"""Consented native setup, refusal before mutation and managed plugin lifetime.

Generated homes only; no live home, account or credential. Real-CPA cases need
CPA_TEST_ARCHIVE naming the pinned hermes-owner-v1 runtime archive (built by
scripts/build_cpa_runtime.py), the patched host on PYTHONPATH and generated homes
outside ~/.hermes. The permit below is an expiring disposable validation permit, not
release acceptance. Model listing is served by the generated local CPA; nothing
reaches an upstream provider.
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
from hermes_cpa.installer import digest
from hermes_cpa.onboarding import SetupRefused, initialize_managed, preflight
from hermes_cpa.process_owner import check_process_alive
from hermes_cpa.runtime_manifest import dump_runtime_manifest, parse_runtime_manifest, unbuilt_manifest
from hermes_cpa.settings import CpaSettings
from runtime_fixtures import BINARY, install_fake_gh, make_package, write_archive

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


@pytest.fixture
def generated(tmp_path):
    """Generated ACCEPTED package pinning a generated archive (a stand-in executable)."""
    source = tmp_path / 'generated.tar.gz'
    write_archive(source)
    package = tmp_path / 'package'
    make_package(package, source)
    return package, source


def refusal_of(settings, package, **kwargs):
    with pytest.raises(SetupRefused) as refusal:
        preflight(settings, package, consent=True, port=free_port(), **kwargs)
    return refusal.value


def test_occupied_port_refuses_before_credentials(home, tmp_path, generated):
    package, _ = generated
    warm_host(home)
    with socket.socket() as outsider:
        outsider.bind(('127.0.0.1', 0))
        outsider.listen()
        port = outsider.getsockname()[1]
        before = tree(home)
        with pytest.raises(SetupRefused) as refusal:
            preflight(CpaSettings(home), package, str(tmp_path / 'never-read.tar.gz'), consent=True,
                      port=port, broker_port=free_port())
        assert refusal.value.code == 'port_in_use'
        assert tree(home) == before, 'an independent listener caused a partial setup'
        outsider.settimeout(.5)
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            pass  # The outsider is still serving; nothing adopted or replaced it.


@pytest.mark.parametrize('failure,code,needle', [
    ('no_gh', 'gh_unavailable', 'gh auth login'),
    ('logged_out', 'gh_unauthenticated', 'gh auth login'),
    ('no_asset', 'candidate_unavailable', 'not published'),
    ('wrong_bytes', 'candidate_mismatch', 'sha256')])
def test_candidate_download_prerequisites_refuse_before_any_mutation(
        home, tmp_path, monkeypatch, generated, failure, code, needle):
    package, source = generated
    wrong = tmp_path / 'wrong.tar.gz'
    wrong.write_bytes(b'\1' * source.stat().st_size)
    if failure == 'no_gh':
        (tmp_path / 'empty').mkdir()
        monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    else:
        install_fake_gh(tmp_path, monkeypatch, asset=wrong if failure == 'wrong_bytes' else source,
                        auth=1 if failure == 'logged_out' else 0,
                        download_exit=1 if failure == 'no_asset' else 0)
    warm_host(home)
    before = tree(home)
    refusal = refusal_of(CpaSettings(home), package, download=True)
    assert refusal.code == code and needle in refusal.message
    assert '--archive' in refusal.message, 'the local archive path must stay the stated alternative'
    assert tree(home) == before
    assert not CpaSettings(home).cpa_dir.exists()


def test_unbuilt_runtime_refuses_setup_without_a_stock_fallback(home, tmp_path, monkeypatch, generated):
    package, source = generated
    (package / 'cpa-runtime.json').write_bytes(dump_runtime_manifest(unbuilt_manifest()))
    calls = install_fake_gh(tmp_path, monkeypatch, asset=source)
    warm_host(home)
    before = tree(home)
    for kwargs in (dict(archive=str(source)), dict(download=True)):
        refusal = refusal_of(CpaSettings(home), package, **kwargs)
        assert refusal.code == 'runtime_unpinned' and 'build_cpa_runtime.py' in refusal.message
    assert calls() == [] and tree(home) == before


def test_host_older_than_pinned_runtime_refuses_setup(home, tmp_path, monkeypatch, generated):
    package, source = generated
    warm_host(home)
    before = tree(home)
    real = os.confstr
    monkeypatch.setattr(os, 'confstr', lambda name: 'glibc 2.10' if name == 'CS_GNU_LIBC_VERSION'
                        else real(name))
    refusal = refusal_of(CpaSettings(home), package, archive=str(source))
    assert refusal.code == 'unsupported_libc' and '2.17' in refusal.message and '2.10' in refusal.message
    assert tree(home) == before


def test_setup_creates_no_private_key_and_a_fresh_public_access_key(home, tmp_path, generated):
    from ruamel.yaml import YAML
    package, source = generated
    hermes_edit(home, lambda cfg: None)
    settings, port = CpaSettings(home), free_port()
    initialize_managed(settings, package, str(source), consent=True, port=port, autostart=False)
    assert settings.binary_path.read_bytes() == BINARY
    assert settings.config_path.stat().st_mode & 0o777 == 0o600
    cpa = YAML(typ='safe').load(settings.config_path.read_text())
    (access_key,) = cpa['access']['api-keys']
    assert access_key.startswith('sk-') and len(access_key) > 40
    assert cpa['server']['port'] == port and 'secret-key' not in cpa['management']
    from agent.secret_scope import load_env_file
    env_file = home / '.env'
    leaked = {'HERMES_CPA_MANAGEMENT_KEY', 'HERMES_CPA_INFERENCE_KEY'} & set(
        load_env_file(env_file) if env_file.exists() else {})
    assert not leaked, 'private CPA keys were written to the Hermes credential store'
    hermes = YAML(typ='safe').load((home / 'config.yaml').read_text())
    plugin = hermes['plugins']['entries']['hermes-cpa']['settings']
    assert plugin['mode'] == 'managed' and plugin['autostart'] is False and 'managed_port' not in plugin
    assert access_key not in (home / 'config.yaml').read_text()
    # A second setup never rotates: the first key stays, nothing is rewritten.
    before = settings.config_path.read_bytes()
    with pytest.raises(SetupRefused) as again:
        initialize_managed(settings, package, str(source), consent=True, port=free_port())
    assert again.value.code == 'already_managed'
    assert settings.config_path.read_bytes() == before


def test_legacy_private_key_entries_are_never_reused_rotated_or_deleted(home, generated):
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import save_env_value
    package, source = generated
    hermes_edit(home, lambda cfg: None)
    home_token, scope_token = set_hermes_home_override(home), set_multiplex_context(True)
    try:
        save_env_value('HERMES_CPA_INFERENCE_KEY', 'legacy-fixture-value')
    finally:
        reset_multiplex_context(scope_token)
        reset_hermes_home_override(home_token)
    before = tree(home)
    refusal = refusal_of(CpaSettings(home), package, archive=str(source))
    assert refusal.code == 'recovery_required'
    assert tree(home) == before, 'legacy key entries were changed or deleted'


@pytest.fixture
def archive():
    path = os.environ.get('CPA_TEST_ARCHIVE')
    if not path:
        pytest.skip('explicit hermes-owner-v1 runtime archive required')
    artifact = parse_runtime_manifest((PACKAGE / 'cpa-runtime.json').read_bytes()).artifact
    if artifact is None:
        pytest.skip('hermes-owner-v1 runtime is not built and pinned yet')
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == artifact.archive_sha256
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
        assert 'Public access: a fresh public API key' in output and 'public management is disabled' in output
        from agent.secret_scope import load_env_file
        env_file = home / '.env'
        assert not ({'HERMES_CPA_INFERENCE_KEY', 'HERMES_CPA_MANAGEMENT_KEY'} &
                    set(load_env_file(env_file) if env_file.exists() else {})), 'private CPA keys were written'
        cpa = YAML(typ='safe').load(settings.config_path.read_text())
        (access_key,) = cpa['access']['api-keys']
        assert access_key.startswith('sk-') and access_key not in output and access_key not in text
        assert 'secret-key' not in cpa['management']
        assert 'managed_port' not in YAML(typ='safe').load(text)['plugins']['entries']['hermes-cpa']['settings']

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
            assert status['state'] == 'failed' and status['pid'] is None, status
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
