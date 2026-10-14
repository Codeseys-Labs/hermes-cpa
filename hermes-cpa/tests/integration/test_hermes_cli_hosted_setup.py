"""The actual Hermes CLI must work with a host-isolated native CPA plugin."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest
from ruamel.yaml import YAML

from hermes_cpa.controller import controller_from_profile
from hermes_cpa.process_owner import check_process_alive
from hermes_cpa.settings import CpaSettings
from test_native_setup import free_port, wait_for


@pytest.mark.skipif(not os.environ.get('CPA_TEST_ARCHIVE'), reason='Pinned hermes-owner-v1 runtime archive required')
def test_hermes_setup_preserves_isolation_and_outlives_command(tmp_path):
    root = tmp_path / 'root'
    home = root / '.hermes'
    package = home / 'plugins/hermes-cpa'
    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source, package, ignore=shutil.ignore_patterns(
        '__pycache__', '.pytest_cache', '.venv', 'tests', 'uv.lock'))
    (home / 'config.yaml').write_text(
        'plugins:\n  isolation: host\n  enabled: [hermes-cpa]\n'
        '  entries:\n    hermes-cpa:\n      settings: {mode: external}\n')
    permit = tmp_path / 'permit.json'
    permit.write_text(json.dumps(dict(version=1, purpose='isolated-candidate-test',
        home=str(home.resolve()), package_root=str(package.resolve()),
        manifest_sha256=hashlib.sha256((package / 'compatibility.json').read_bytes()).hexdigest(),
        expires_at=time.time() + 600)))
    permit.chmod(0o600)
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(root),
           'HERMES_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'}
    settings = CpaSettings(home)
    controller = controller_from_profile(settings, package)
    owner = child = None
    try:
        result = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'cpa', 'setup',
            '--archive', os.environ['CPA_TEST_ARCHIVE'], '--consent', '--port', str(free_port()),
            '--broker-port', str(free_port()), '--isolated-authorization', str(permit)],
            env=env, capture_output=True, text=True, timeout=150)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert 'CPA: ready' in result.stdout and 'Broker: ready' in result.stdout
        controller = controller_from_profile(settings, package)
        status = controller.snapshot()
        owner, child = status['owner']['pid'], status['pid']
        assert status['state'] == 'ready' and status['lifetime'] == 'persistent'
        config = YAML(typ='safe').load((home / 'config.yaml').read_text())
        assert config['plugins']['isolation'] == 'host'
        assert config['providers']['cpa']['discover_models'] is True
        assert not {'api_key', 'key_env', 'api_key_env'} & config['providers']['cpa'].keys()
        env_file = home / '.env'
        assert not env_file.exists() or 'HERMES_CPA_' not in env_file.read_text()
        assert check_process_alive(owner) and check_process_alive(child)
    finally:
        controller_from_profile(settings, package).unload()
    if owner is not None:
        wait_for(lambda: not check_process_alive(owner))
        assert not check_process_alive(child)
