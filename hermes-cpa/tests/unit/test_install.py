import pytest
from hermes_cpa.installer import CpaInstaller
from hermes_cpa.runtime_manifest import dump_runtime_manifest, unbuilt_manifest
from hermes_cpa.settings import CpaSettings
from runtime_fixtures import BINARY, install_fake_gh, make_package, write_archive


@pytest.fixture
def pinned(tmp_path):
    source = tmp_path / 'generated.tar.gz'
    write_archive(source)
    root = tmp_path / 'package'
    make_package(root, source)
    return tmp_path, source, CpaInstaller(CpaSettings(tmp_path / 'home'), root)


def test_installer_archive_verification(pinned):
    tmp_path, _, installer = pinned
    fake_tar = tmp_path / 'fake.tar.gz'
    fake_tar.write_bytes(b'not-a-valid-release')
    with pytest.raises(ValueError, match='Archive SHA mismatch'):
        installer.install_from_archive(fake_tar)
    assert not installer.settings.cpa_dir.exists()


def test_other_release_with_same_member_layout_is_refused(pinned):
    """Any archive that is not exactly the pinned runtime (for example a stock release) is refused."""
    tmp_path, _, installer = pinned
    stock_like = tmp_path / 'stock-like.tar.gz'
    write_archive(stock_like, b'a different executable with the same archive layout')
    with pytest.raises(ValueError, match='Archive SHA mismatch'):
        installer.install_from_archive(stock_like)
    assert not installer.settings.cpa_dir.exists()


def test_pinned_archive_installs_exactly_the_pinned_executable(pinned):
    _, source, installer = pinned
    assert installer.install_from_archive(source)
    assert installer.settings.binary_path.read_bytes() == BINARY
    assert installer.verify_pair()


def test_unbuilt_runtime_refuses_every_acquisition_without_side_effects(pinned, monkeypatch):
    """Before the integration build is pinned there is nothing to install: no stock fallback."""
    tmp_path, source, installer = pinned
    (installer.package_root / 'cpa-runtime.json').write_bytes(dump_runtime_manifest(unbuilt_manifest()))
    calls = install_fake_gh(tmp_path, monkeypatch, asset=source)
    with pytest.raises(ValueError, match='runtime_unpinned'):
        installer.verify_release()
    with pytest.raises(ValueError, match='runtime_unpinned'):
        installer.install_from_archive(source)
    with pytest.raises(ValueError, match='runtime_unpinned'):
        installer.download_candidate_archive()
    assert calls() == [], 'gh must not be contacted without a pinned digest'
    assert not installer.settings.cpa_dir.exists()
