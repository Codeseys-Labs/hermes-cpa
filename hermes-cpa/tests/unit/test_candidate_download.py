"""Pinned candidate acquisition through gh: fixed source, pinned bytes, no side effects.

A generated stand-in `gh` is the only executable on PATH; nothing reaches the network.
"""
import pytest
from hermes_cpa.candidate_download import AcquisitionError
from hermes_cpa.installer import CpaInstaller
from hermes_cpa.runtime_manifest import ARCHIVE_NAME, CANDIDATE_REPOSITORY, CANDIDATE_TAG
from hermes_cpa.settings import CpaSettings
from runtime_fixtures import BINARY, install_fake_gh, make_package, write_archive


@pytest.fixture
def pinned(tmp_path):
    source = tmp_path / 'generated.tar.gz'
    write_archive(source)
    root = tmp_path / 'package'
    make_package(root, source)
    return tmp_path, source, CpaInstaller(CpaSettings(tmp_path / 'home'), root)


def test_download_requests_only_the_fixed_candidate_and_returns_pinned_bytes(pinned, monkeypatch):
    tmp_path, source, installer = pinned
    # Ambient overrides must not retarget gh to another repository or host.
    monkeypatch.setenv('GH_REPO', 'attacker/other-repo')
    monkeypatch.setenv('GH_HOST', 'ghe.example.com')
    calls = install_fake_gh(tmp_path, monkeypatch, asset=source)
    raw = installer.download_candidate_archive()
    assert raw == source.read_bytes()
    assert installer.archive_binary_bytes(raw) == BINARY
    status, download = calls()
    assert status['argv'] == ['auth', 'status', '--hostname', 'github.com']
    assert download['argv'] == ['release', 'download', CANDIDATE_TAG, '--repo',
                                f'github.com/{CANDIDATE_REPOSITORY}', '--pattern', ARCHIVE_NAME,
                                '--output', '-']
    for call in (status, download):
        assert call['env'] == {'GH_HOST': 'github.com', 'GH_REPO': None, 'GH_PROMPT_DISABLED': '1'}
    assert not installer.settings.cpa_dir.exists(), 'download wrote to the profile'


def test_missing_gh_is_an_actionable_prerequisite_not_a_fallback(pinned, monkeypatch):
    tmp_path, _, installer = pinned
    (tmp_path / 'empty').mkdir()
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    with pytest.raises(AcquisitionError) as refusal:
        installer.download_candidate_archive()
    assert str(refusal.value) == 'gh_unavailable'
    assert not installer.settings.cpa_dir.exists()


def test_unauthenticated_gh_stops_before_any_download(pinned, monkeypatch):
    tmp_path, source, installer = pinned
    calls = install_fake_gh(tmp_path, monkeypatch, asset=source, auth=1)
    with pytest.raises(AcquisitionError) as refusal:
        installer.download_candidate_archive()
    assert str(refusal.value) == 'gh_unauthenticated'
    assert [call['argv'][:2] for call in calls()] == [['auth', 'status']]
    assert not installer.settings.cpa_dir.exists()


def test_unavailable_candidate_is_reported_without_side_effects(pinned, monkeypatch):
    tmp_path, source, installer = pinned
    install_fake_gh(tmp_path, monkeypatch, asset=source, download_exit=1)
    with pytest.raises(AcquisitionError) as refusal:
        installer.download_candidate_archive()
    assert str(refusal.value) == 'candidate_unavailable'
    assert not installer.settings.cpa_dir.exists()


@pytest.mark.parametrize('change', ['same_size_other_bytes', 'longer', 'shorter', 'empty'])
def test_downloaded_bytes_must_match_the_pinned_size_and_digest(pinned, monkeypatch, change):
    tmp_path, source, installer = pinned
    size = source.stat().st_size
    bad = tmp_path / 'bad.tar.gz'
    bad.write_bytes({'same_size_other_bytes': b'\1' * size, 'longer': source.read_bytes() + b'\0',
                     'shorter': source.read_bytes()[:-1], 'empty': b''}[change])
    install_fake_gh(tmp_path, monkeypatch, asset=bad)
    with pytest.raises(AcquisitionError) as refusal:
        installer.download_candidate_archive()
    assert str(refusal.value) == 'candidate_mismatch'
    assert not installer.settings.cpa_dir.exists()
