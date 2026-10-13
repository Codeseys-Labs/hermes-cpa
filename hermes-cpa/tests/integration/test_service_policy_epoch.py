"""Long-lived service policy versus accepted-operation authority through Hermes writers.

Requires the patched host on PYTHONPATH (durable per-plugin policy epoch). Generated
homes only. Raw external same-projection ABA is documented as unsupported; the strict
fallback without an epoch is covered by test_owner_policy_incarnation.py.
"""
import pytest
from hermes_cpa.supervisor import InstallPolicy


@pytest.fixture
def policy_home(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config, save_config
    home, package = tmp_path / 'home', tmp_path / 'package'
    home.mkdir()
    package.mkdir()
    (package / 'plugin.yaml').write_text('name: hermes-cpa\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    token = set_hermes_home_override(home)
    try:
        cfg = load_config()
        cfg.setdefault('plugins', {})['enabled'] = ['hermes-cpa']
        cfg['plugins'].setdefault('entries', {})['hermes-cpa'] = {
            'settings': {'mode': 'managed', 'managed_port': 8318, 'autostart': True}}
        save_config(cfg)
        yield home, package
    finally:
        reset_hermes_home_override(token)


def edit(change, *, replace=False):
    from hermes_cli.config import atomic_config_replace, get_config_path, load_config, save_config
    cfg = load_config()
    change(cfg)
    # Removal is a deliberate full-state replacement, as `hermes plugins remove` does.
    if replace:
        atomic_config_replace(get_config_path(), cfg)
    else:
        save_config(cfg)


def enable(cfg, on):
    plugins = cfg['plugins']
    plugins['enabled'] = ['hermes-cpa'] if on else []
    plugins['disabled'] = [] if on else ['hermes-cpa']


def test_unrelated_edit_keeps_service_but_revokes_accepted_operation(policy_home):
    policy = InstallPolicy(*policy_home)
    assert policy()
    accepted = policy.capture()
    # The same kind of write provider enrollment performs after setup.
    edit(lambda cfg: cfg.setdefault('providers', {}).update(
        generated={'base_url': 'http://127.0.0.1:9/v1'}))
    edit(lambda cfg: cfg['plugins']['entries'].update(
        {'generated-other-plugin': {'settings': {'flag': True}}}))
    assert policy(), 'unrelated config edit revoked the long-lived owner'
    assert not policy.matches(accepted), 'accepted work survived a config rewrite'
    assert policy.matches(policy.capture()), 'a fresh operation under the same policy was refused'


def test_disable_reenable_through_hermes_revokes_without_intermediate_sample(policy_home):
    policy = InstallPolicy(*policy_home)
    assert policy()
    edit(lambda cfg: enable(cfg, False))
    edit(lambda cfg: enable(cfg, True))
    assert policy() is False
    assert policy() is False, 'revocation must stay latched'
    assert InstallPolicy(*policy_home)(), 'a new owner samples the current policy'


@pytest.mark.parametrize('change,replace', [
    (lambda cfg: cfg['plugins']['entries']['hermes-cpa']['settings'].update(mode='external'), False),
    (lambda cfg: cfg['plugins']['entries']['hermes-cpa']['settings'].update(managed_port=8320), False),
    (lambda cfg: cfg['plugins']['entries'].pop('hermes-cpa'), True),
    (lambda cfg: enable(cfg, False), False),
])
def test_relevant_policy_change_revokes(policy_home, change, replace):
    policy = InstallPolicy(*policy_home)
    assert policy()
    edit(change, replace=replace)
    assert policy() is False


def test_package_reinstall_with_same_bytes_still_revokes(policy_home):
    home, package = policy_home
    policy = InstallPolicy(home, package)
    assert policy()
    manifest = package / 'plugin.yaml'
    original = manifest.read_bytes()
    manifest.unlink()
    manifest.write_bytes(original)
    assert policy() is False
