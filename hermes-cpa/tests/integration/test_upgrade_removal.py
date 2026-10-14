"""Generated archives and harmless owner; not upstream release acceptance."""
import os
import tarfile

import pytest
from hermes_cpa import installer as module
from hermes_cpa.installer import CpaInstaller
from hermes_cpa.settings import CpaSettings
from runtime_fixtures import make_package, write_archive

CONTENT = b'#!/bin/sh\nexit 0\n'


def archive(tmp_path, extra=None, content=CONTENT):
    path = tmp_path / 'generated.tar.gz'
    write_archive(path, content, [extra] if extra else [])
    return path


def package(tmp_path, source, content=CONTENT):
    root = tmp_path / 'package'
    make_package(root, source, content)
    return root


def test_install_hash_permissions_and_compatibility(tmp_path):
    settings = CpaSettings(tmp_path / 'home')
    source = archive(tmp_path)
    installer = CpaInstaller(settings, package(tmp_path, source))
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
def test_full_archive_validation_before_any_write(tmp_path, name, kind):
    extra = tarfile.TarInfo(name)
    if kind == 'symlink':
        extra.type, extra.linkname = tarfile.SYMTYPE, '/outside'
    source = archive(tmp_path, extra)
    settings = CpaSettings(tmp_path / 'home')
    with pytest.raises(ValueError, match='unsafe_archive_member'):
        CpaInstaller(settings, package(tmp_path, source)).install_from_archive(source)
    assert not settings.cpa_dir.exists()


def test_installed_executable_must_equal_pinned_executable(tmp_path):
    """A different executable at the install path never passes the launch-time pair check."""
    settings = CpaSettings(tmp_path / 'home')
    source = archive(tmp_path)
    installer = CpaInstaller(settings, package(tmp_path, source))
    installer.install_from_archive(source)
    settings.binary_path.unlink()
    settings.binary_path.write_bytes(CONTENT + b'#')
    settings.binary_path.chmod(0o700)
    with pytest.raises(ValueError, match='binary_mismatch'):
        installer.verify_pair()


def test_unsupported_architecture(tmp_path, monkeypatch):
    monkeypatch.setattr('hermes_cpa.process_owner.platform.machine', lambda: 'aarch64')
    with pytest.raises(RuntimeError, match='unsupported_platform'):
        CpaInstaller(CpaSettings(tmp_path)).verify_pair()


def host_libc(monkeypatch, report):
    """Make the host report REPORT as its glibc; None means a non-glibc libc."""
    real = os.confstr

    def confstr(name):
        if name != 'CS_GNU_LIBC_VERSION':
            return real(name)
        if report is None:
            raise ValueError(name)
        return report
    monkeypatch.setattr(os, 'confstr', confstr)


def test_host_older_than_runtime_glibc_refuses_before_any_write(tmp_path, monkeypatch):
    settings = CpaSettings(tmp_path / 'home')
    source = archive(tmp_path)
    root = tmp_path / 'package'
    make_package(root, source, CONTENT, glibc_max='2.36')
    host_libc(monkeypatch, 'glibc 2.35')
    with pytest.raises(module.UnsupportedLibc) as refusal:
        CpaInstaller(settings, root).install_from_archive(source)
    assert str(refusal.value) == 'unsupported_libc'
    assert (refusal.value.required, refusal.value.found) == ((2, 36), (2, 35))
    assert not settings.cpa_dir.exists()
    host_libc(monkeypatch, 'glibc 2.36')
    assert CpaInstaller(settings, root).install_from_archive(source)


def test_non_glibc_host_refuses(tmp_path, monkeypatch):
    source = archive(tmp_path)
    host_libc(monkeypatch, None)
    with pytest.raises(module.UnsupportedLibc):
        CpaInstaller(CpaSettings(tmp_path / 'home'), package(tmp_path, source)).verify_release()


# The earlier raw-supervisor same-pin rollback test was superseded by
# tests/gates/test_gates.py: the frozen OwnerView seam has no active-pair
# recovery authority. A valid target MUST refuse before stop/stage instead.
