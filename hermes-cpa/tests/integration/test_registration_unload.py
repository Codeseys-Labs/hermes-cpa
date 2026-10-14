"""Native registration unload releases only its own claim; actual disable revokes.

Generated home and a harmless child owned by an in-process owner. No real CPA.
"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from hermes_cpa.settings import CpaSettings
from hermes_cpa.supervisor import CpaSupervisor, InstallPolicy

# The registered entrypoint resolves this package root for its policy checks.
PACKAGE = Path(__file__).resolve().parents[2]
ENABLED = ('plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n'
           '      settings: {mode: MODE}\n')


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), 'bounded wait failed'


def registration():
    from hermes_cpa import register
    callbacks, hooks = [], {}
    register(SimpleNamespace(on_unload=callbacks.append,
                             register_cli_command=lambda **kwargs: None,
                             register_hook=lambda name, callback: hooks.setdefault(name, callback)))
    return lambda: [callback() for callback in reversed(callbacks)]


def test_unrelated_unload_keeps_shared_owner_and_disable_revokes(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    settings = CpaSettings(home)
    settings.ensure_directories()
    (home / 'config.yaml').write_text(ENABLED.replace('MODE', 'managed'))
    # A long watchdog interval isolates unload: the owner samples policy once at start.
    owner = CpaSupervisor(settings, mode='managed',
        probe=lambda timeout: SimpleNamespace(management_ready=True, compatible=True,
                                             inference_configured=False),
        policy=InstallPolicy(home, PACKAGE), command=[sys.executable, '-c', 'import time; time.sleep(30)'],
        readiness_timeout=.5, stop_grace=.5, watchdog_interval=30.)
    assert owner.acquire_lock() and owner.start_child()
    child = owner.child_process
    thread = threading.Thread(target=owner.run_ipc)
    thread.start()
    try:
        wait_for(lambda: settings.socket_path.exists())
        monkeypatch.setenv('HERMES_HOME', str(home))
        native, hosted = registration(), registration()
        native()  # e.g. forced rediscovery of one registration while another is live
        time.sleep(.5)
        assert thread.is_alive() and child.poll() is None, 'unrelated unload stopped a shared owner'
        (home / 'config.yaml').write_text(
            'plugins:\n  enabled: []\n  disabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n'
            '      settings: {mode: managed}\n')
        hosted()  # unload under revoked policy contacts the verified owner
        wait_for(lambda: not thread.is_alive())
        assert child.poll() is not None
        assert not settings.socket_path.exists()
    finally:
        owner.running = False
        thread.join(timeout=5)
        owner.cleanup()


def test_external_registration_stays_inert(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text(
        ENABLED.replace('MODE', 'external').replace('{mode: external}', '{mode: external, autostart: true}'))
    monkeypatch.setenv('HERMES_HOME', str(home))
    unload = registration()
    unload()
    assert not (home / 'cpa').exists(), 'external registration created managed state'
