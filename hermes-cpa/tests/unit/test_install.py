import pytest
from hermes_cpa.installer import CpaInstaller
from hermes_cpa.settings import CpaSettings

def test_installer_archive_verification(tmp_path):
    settings = CpaSettings(tmp_path)
    installer = CpaInstaller(settings)
    fake_tar = tmp_path / "fake.tar.gz"
    fake_tar.write_bytes(b"not-a-valid-release")
    with pytest.raises(ValueError, match="Archive SHA mismatch"):
        installer.install_from_archive(fake_tar)
