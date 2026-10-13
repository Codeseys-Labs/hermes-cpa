"""Generated archives and harmless owner; not upstream release acceptance."""
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from hermes_cpa import installer as module
from hermes_cpa.installer import CpaInstaller
from hermes_cpa.settings import CpaSettings



def archive(tmp_path, monkeypatch, extra=None, content=b'#!/bin/sh\nexit 0\n'):
    path = tmp_path / 'generated.tar.gz'
    with tarfile.open(path, 'w:gz') as tar:
        member = tarfile.TarInfo('cli-proxy-api')
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
        if extra:
            tar.addfile(extra)
    monkeypatch.setattr(module, 'EXPECTED_TAR_SHA', hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(module, 'EXPECTED_BIN_SHA', hashlib.sha256(content).hexdigest())
    return path


def package(tmp_path, binary):
    root = tmp_path / 'package'
    (root / 'assets').mkdir(parents=True)
    (root / 'assets/cpamc.html').write_text('<html>generated fixture</html>')
    sha = module.digest(root / 'assets/cpamc.html')
    (root / 'assets/cpamc.manifest.json').write_text(json.dumps(
        {'sha256': sha, 'version': '1.25.6', 'cpa_version': '8.0.21'}))
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        (root / name).write_text('generated fixture notice')
    # Generated accepted release; real candidate metadata remains unaccepted.
    (root / 'compatibility.json').write_text(json.dumps({
        'candidate_status': 'accepted', 'adr_status': 'Accepted',
        'tested_minimum_hermes': '0.21.5', 'supported_pairs': [{
        'platform': 'linux_amd64', 'cpa_version': '8.0.21', 'console_version': '1.25.6',
        'binary_sha256': module.digest(binary), 'console_sha256': sha}],
        'notice_sha256': {name: module.digest(root / name) for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md')}}))
    return root


def test_install_hash_permissions_and_compatibility(tmp_path, monkeypatch):
    settings = CpaSettings(tmp_path / 'home')
    source = archive(tmp_path, monkeypatch)
    fixture_binary = tmp_path / 'fixture-binary'
    fixture_binary.write_bytes(b'#!/bin/sh\nexit 0\n')
    installer = CpaInstaller(settings, package(tmp_path, fixture_binary))
    assert installer.install_from_archive(source)
    assert installer.verify_pair()
    assert settings.binary_path.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match='upgrade_required'):
        installer.install_from_archive(source)
    (installer.package_root / 'LICENSE').write_text('changed')
    with pytest.raises(ValueError, match='notice_mismatch'):
        installer.verify_pair()


@pytest.mark.parametrize('name,kind', [('../escape', 'file'), ('/absolute', 'file'),
                                      ('link', 'symlink'), ('cli-proxy-api', 'file')])
def test_full_archive_validation_before_any_write(tmp_path, monkeypatch, name, kind):
    extra = tarfile.TarInfo(name)
    if kind == 'symlink':
        extra.type, extra.linkname = tarfile.SYMTYPE, '/outside'
    source = archive(tmp_path, monkeypatch, extra)
    settings = CpaSettings(tmp_path / 'home')
    with pytest.raises(ValueError, match='unsafe_archive_member'):
        CpaInstaller(settings).install_from_archive(source)
    assert not settings.cpa_dir.exists()


def test_unsupported_architecture(tmp_path, monkeypatch):
    monkeypatch.setattr('hermes_cpa.process_owner.platform.machine', lambda: 'aarch64')
    with pytest.raises(RuntimeError, match='unsupported_platform'):
        CpaInstaller(CpaSettings(tmp_path)).verify_pair()


# The earlier raw-supervisor same-pin rollback test was superseded by
# tests/gates/test_gates.py: the frozen OwnerView seam has no active-pair
# recovery authority. A valid target MUST refuse before stop/stage instead.
