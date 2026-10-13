"""Explicit disposable candidate authorization is not release acceptance."""
import json
import time
from pathlib import Path

import pytest
from hermes_cpa.installer import CpaInstaller, digest
from hermes_cpa.settings import CpaSettings

PACKAGE = Path(__file__).resolve().parents[2]


def authorize(profile_home, path, **changes):
    record = dict(version=1, purpose='isolated-candidate-test', home=str(profile_home.resolve()),
                  package_root=str(PACKAGE.resolve()),
                  manifest_sha256=digest(PACKAGE / 'compatibility.json'),
                  expires_at=time.time() + 300)
    record.update(changes)
    path.write_text(json.dumps(record))
    path.chmod(0o600)


def test_default_still_rejects_even_with_authorization_file(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    path = home / 'authorization.json'
    authorize(home, path)
    installer = CpaInstaller(CpaSettings(home), PACKAGE)
    with pytest.raises(ValueError, match='unaccepted_release'):
        installer._verify_content(b'not a binary')


@pytest.mark.parametrize('change', [dict(expires_at=0), dict(home='/wrong-home'),
    dict(package_root='/wrong-package'), dict(manifest_sha256='0' * 64),
    dict(purpose='production'), dict(version=True)])
def test_wrong_authorization_fails_closed(tmp_path, change):
    home = tmp_path / 'home'
    home.mkdir()
    path = home / 'authorization.json'
    authorize(home, path, **change)
    installer = CpaInstaller(CpaSettings(home), PACKAGE, isolated_authorization=path)
    with pytest.raises(ValueError, match='isolated_authorization'):
        installer._verify_content(b'not a binary')


def test_explicit_authorization_does_not_skip_artifact_hashes(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    path = home / 'authorization.json'
    authorize(home, path)
    installer = CpaInstaller(CpaSettings(home), PACKAGE, isolated_authorization=path)
    with pytest.raises(ValueError, match='binary_mismatch'):
        installer._verify_content(b'not a binary')
    path.chmod(0o644)
    with pytest.raises(ValueError, match='isolated_authorization'):
        installer._verify_content(b'not a binary')
