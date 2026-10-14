"""Additional bounded refusal/fault cases, all filesystem fixtures only."""
import errno
import os
import tarfile

import pytest
from test_gates import case, invoke, make_archive
from hermes_cpa import installer as module


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'device', 'special', 'backslash', 'absolute', 'traversal'])
def test_archive_unsafe_kinds(case, kind):
    info = tarfile.TarInfo('extra')
    if kind in ('symlink', 'hardlink'):
        info.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
        info.linkname = 'cli-proxy-api'
    elif kind == 'fifo':
        info.type = tarfile.FIFOTYPE
    elif kind == 'device':
        info.type = tarfile.CHRTYPE
    elif kind == 'special':
        info.mode = 0o4755
    else:
        info.name = {'backslash': 'a\\b', 'absolute': '/escape', 'traversal': '../escape'}[kind]
    make_archive(case.archive, [info], root=case.root)
    assert invoke(case, 'upgrade')['code'] == 'INVALID_TARGET'
    assert not case.owner.calls and not case.settings.bin_dir.exists()


@pytest.mark.parametrize('target', ['compatibility.json', 'cpa-runtime.json', 'assets/cpamc.manifest.json',
                                    'assets/cpamc.html', 'LICENSE'])
def test_package_symlinks_rejected(case, target):
    path = case.root / target
    outside = case.tmp / 'outside'
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    assert invoke(case, 'upgrade')['code'] == 'INVALID_TARGET'
    assert not case.owner.calls


def test_archive_fifo_does_not_block(case):
    case.archive.unlink()
    os.mkfifo(case.archive)
    assert invoke(case, 'upgrade')['code'] == 'INVALID_TARGET'


@pytest.mark.parametrize('phase', ['fsync', 'publish'])
def test_disk_fault_never_reports_install_success(case, monkeypatch, phase):
    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'generated disk full')
    monkeypatch.setattr(module.os, 'fsync' if phase == 'fsync' else 'link', fail)
    with pytest.raises(OSError):
        case.installer.install_from_archive(case.archive)
    assert not case.settings.binary_path.exists()
    assert not list(case.settings.bin_dir.glob('.stage-*'))


def test_changed_revision_at_gate(case):
    case.owner.on_enter = lambda: case.settings.config_path.write_bytes(b'changed: true\n')
    assert invoke(case, 'remove')['code'] == 'STALE_REVISION'
    assert not case.owner.calls


def test_stopped_removal_never_starts(case):
    case.owner.running = False
    assert invoke(case, 'remove')['removal_prepared']
    assert case.owner.calls == ['stop']


def test_default_consent_refused(case):
    del case.args['native_consent']
    assert invoke(case, 'remove')['code'] == 'CONSENT_REQUIRED'


def test_symlink_config_fails_closed(case):
    source = case.tmp / 'outside-config'
    source.write_bytes(case.settings.config_path.read_bytes())
    case.settings.config_path.unlink()
    case.settings.config_path.symlink_to(source)
    assert invoke(case, 'remove')['code'] == 'INVALID_INPUT'
    assert not case.owner.calls


def test_install_publish_does_not_replace_concurrent_target(case, monkeypatch):
    real_link = module.os.link
    def raced(source, target):
        target.write_bytes(b'concurrent sentinel')
        return real_link(source, target)
    monkeypatch.setattr(module.os, 'link', raced)
    with pytest.raises(FileExistsError):
        case.installer.install_from_archive(case.archive)
    assert case.settings.binary_path.read_bytes() == b'concurrent sentinel'
    assert not list(case.settings.bin_dir.glob('.stage-*'))
