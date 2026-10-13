"""Policy removal/disable ABA cannot revive an existing owner's authority."""
from hermes_cpa.supervisor import InstallPolicy


def policy_files(tmp_path):
    home, package = tmp_path / 'home', tmp_path / 'package'
    home.mkdir()
    package.mkdir()
    config = home / 'config.yaml'
    config.write_text('plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings: {mode: managed}\n')
    manifest = package / 'plugin.yaml'
    manifest.write_text('name: hermes-cpa\n')
    return home, package, config, manifest


def test_disable_reenable_between_samples_rejects_old_owner(tmp_path):
    home, package, config, _ = policy_files(tmp_path)
    policy = InstallPolicy(home, package)
    assert policy()
    original = config.read_bytes()
    config.write_text('plugins: {enabled: []}\n')
    config.write_bytes(original)
    assert policy() is False
    assert policy() is False
    assert InstallPolicy(home, package)()


def test_package_remove_reinstall_between_samples_rejects_old_owner(tmp_path):
    home, package, _, manifest = policy_files(tmp_path)
    policy = InstallPolicy(home, package)
    assert policy()
    original = manifest.read_bytes()
    manifest.unlink()
    manifest.write_bytes(original)
    assert policy() is False
    assert InstallPolicy(home, package)()


def test_unrelated_runtime_file_does_not_revoke_owner(tmp_path):
    home, package, _, _ = policy_files(tmp_path)
    policy = InstallPolicy(home, package)
    assert policy()
    (home / 'new-runtime-file').write_text('unrelated')
    assert policy()
