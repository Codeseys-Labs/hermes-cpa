"""Explicit disposable candidate authorization is not release acceptance."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hermes_cpa import authorization
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


@pytest.fixture
def authorization_clock(monkeypatch):
    clock = SimpleNamespace(wall=time.time(), ticks=time.monotonic())
    monkeypatch.setattr(authorization, 'time', SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.ticks))
    return clock


@pytest.mark.parametrize('change', [
    dict(expires_at=0), dict(home='/wrong-home'),
    dict(package_root='/wrong-package'), dict(manifest_sha256='0' * 64),
    dict(purpose='production'), dict(version=True),
])
def test_runtime_admission_rejects_invalid_permit(tmp_path, change):
    home = tmp_path / 'home'
    home.mkdir()
    path = home / 'authorization.json'
    authorize(home, path, **change)
    installer = CpaInstaller(CpaSettings(home), PACKAGE, isolated_authorization=path)
    with pytest.raises(ValueError, match='isolated_authorization'):
        installer.runtime_authorization()


def test_runtime_admission_requires_explicit_candidate_permit(tmp_path):
    path = tmp_path / 'authorization.json'
    authorize(tmp_path, path)
    installer = CpaInstaller(CpaSettings(tmp_path), PACKAGE)
    with pytest.raises(ValueError, match='unaccepted_release'):
        installer.runtime_authorization()


def test_permit_rewrite_only_authorizes_a_new_deadline(tmp_path, authorization_clock):
    clock = authorization_clock
    path = tmp_path / 'authorization.json'
    authorize(tmp_path, path, expires_at=clock.wall + 60)
    installer = CpaInstaller(CpaSettings(tmp_path), PACKAGE, isolated_authorization=path)
    captured = installer.runtime_authorization()
    assert captured.current()
    assert captured.remaining() == pytest.approx(60)

    authorize(tmp_path, path, expires_at=clock.wall + 3600)
    renewed = installer.runtime_authorization()
    clock.wall += 60
    clock.ticks += 60
    assert not captured.current()
    assert captured.remaining() == 0
    assert renewed.current()


@pytest.mark.parametrize('expired_by', ['wall', 'monotonic'])
def test_captured_deadline_obeys_both_clocks(authorization_clock, expired_by):
    clock = authorization_clock
    deadline = authorization.AuthorizationDeadline(clock.wall + 60)
    if expired_by == 'wall':
        clock.wall += 60
    else:
        clock.wall -= 3600
        clock.ticks += 60
    assert not deadline.current()
    assert deadline.remaining() == 0


@pytest.mark.parametrize('change', [
    {}, dict(candidate_status='candidate'), dict(adr_status='Proposed'),
    dict(tested_minimum_hermes=None), dict(tested_minimum_hermes='unqualified'),
])
def test_only_accepted_release_needs_no_candidate_lease(tmp_path, change):
    package = tmp_path / 'package'
    package.mkdir()
    manifest = json.loads((PACKAGE / 'compatibility.json').read_text())
    manifest.update(candidate_status='accepted', adr_status='Accepted',
                    tested_minimum_hermes='1.0.0')
    manifest.update(change)
    (package / 'compatibility.json').write_text(json.dumps(manifest))
    installer = CpaInstaller(CpaSettings(tmp_path / 'home'), package)
    if change:
        with pytest.raises(ValueError, match='unaccepted_release'):
            installer.runtime_authorization()
    else:
        assert installer.runtime_authorization() is None
