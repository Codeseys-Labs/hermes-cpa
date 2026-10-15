"""InstallPolicy reuses a settled config.yaml parse without reusing stale authority.

Generated homes only. The durable policy epoch is a generated in-memory stand-in
for the host module; every test asserts authorization outcomes at the public
call/capture/matches seams.
"""
import os
import sys
import threading
import types
from types import SimpleNamespace

import pytest

pytest.importorskip('ruamel.yaml')

import hermes_cpa.supervisor as supervisor
from hermes_cpa.supervisor import InstallPolicy

MANAGED = 'plugins:\n  enabled: [hermes-cpa]\n  entries:\n    hermes-cpa:\n      settings: {mode: managed}\n'
# Same size as MANAGED, so only content (and timestamps) differ.
DISABLED = MANAGED.replace('enabled: [hermes-cpa]', 'enabled: [hermes-cpz]')
UNRELATED = MANAGED + 'providers: {generated: {}}\n'
assert len(DISABLED) == len(MANAGED)


class Files:
    def __init__(self, tmp_path):
        self.home, self.package = tmp_path / 'home', tmp_path / 'package'
        self.home.mkdir()
        self.package.mkdir()
        self.config, self.manifest = self.home / 'config.yaml', self.package / 'plugin.yaml'
        self.config.write_text(MANAGED)
        self.manifest.write_text('name: hermes-cpa\n')
        self.clock = self.config.stat().st_mtime_ns

    def policy(self, cls=InstallPolicy):
        return cls(self.home, self.package)

    def write(self, path, text):
        """In-place rewrite, same inode, landing on a later timestamp tick."""
        path.write_text(text)
        self.clock += 1_000_000_000
        os.utime(path, ns=(self.clock, self.clock))

    def replace(self, path, text, *, keep_mtime=True):
        """Atomic replacement: new inode, optionally carrying the old mtime."""
        mtime = path.stat().st_mtime_ns
        staged = path.with_name(path.name + '.generated-tmp')
        staged.write_text(text)
        if keep_mtime:
            os.utime(staged, ns=(mtime, mtime))
        os.replace(staged, path)


@pytest.fixture
def files(tmp_path):
    return Files(tmp_path)


@pytest.fixture
def epoch(monkeypatch):
    """Durable per-plugin policy epoch; `hook` runs once inside the next read."""
    state = SimpleNamespace(value='generated-epoch-1', hook=None)

    def read_policy_epoch(home, key):
        hook, state.hook = state.hook, None
        if hook is not None:
            hook()
        return state.value

    module = types.ModuleType('hermes_cli.plugin_policy_epoch')
    module.read_policy_epoch = read_policy_epoch
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_policy_epoch', module)
    return state


@pytest.fixture
def settled(monkeypatch):
    """Treat every stamp as settled so the parse cache is always eligible."""
    monkeypatch.setattr(supervisor, '_POLICY_SETTLE_NS', -10 ** 18)


@pytest.mark.parametrize('replacement', [False, True])
def test_rewrite_and_exact_restore_revokes_without_epoch(files, epoch, settled, replacement):
    epoch.value = None
    policy = files.policy()
    assert policy() and policy()
    accepted = policy.capture()
    if replacement:
        files.replace(files.config, MANAGED)
    else:
        files.write(files.config, DISABLED)
        files.write(files.config, MANAGED)
    assert policy() is False
    assert policy.matches(accepted) is False
    assert policy.capture() is None
    assert files.policy()(), 'a new owner samples the current policy'


def test_same_size_disable_revokes_even_with_unchanged_epoch(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()
    files.write(files.config, DISABLED)
    assert policy() is False
    files.write(files.config, MANAGED)
    assert policy() is False, 'revocation must stay latched'


def test_replacement_inode_with_disabled_policy_revokes(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()
    files.replace(files.config, DISABLED)
    assert policy() is False


def test_replacement_inode_with_same_projection_keeps_service_only(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()
    accepted = policy.capture()
    files.replace(files.config, MANAGED)
    assert policy(), 'same projection under the same durable epoch keeps the service'
    assert policy.matches(accepted) is False, 'accepted work survived atomic replacement'
    assert policy.matches(policy.capture())


def test_disable_reenable_epoch_revokes_with_unchanged_config(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()
    accepted = policy.capture()
    epoch.value = 'generated-epoch-3'  # Disable then re-enable through Hermes writers.
    assert policy() is False
    assert policy.matches(accepted) is False
    assert files.policy()()


def test_disable_reenable_epoch_with_restored_config_revokes(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()
    files.write(files.config, DISABLED)
    epoch.value = 'generated-epoch-2'
    files.write(files.config, MANAGED)
    epoch.value = 'generated-epoch-3'
    assert policy() is False


def test_unreadable_epoch_after_cached_parse_revokes_rewrite(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()
    epoch.value = None
    assert policy() is False


@pytest.mark.parametrize('change', ['rewrite', 'replace', 'remove', 'symlink'])
def test_manifest_change_or_removal_revokes(files, epoch, settled, change):
    policy = files.policy()
    assert policy() and policy()
    accepted = policy.capture()
    original = files.manifest.read_text()
    if change == 'rewrite':
        files.write(files.manifest, 'name: hermes-cpz\n')
    elif change == 'replace':
        files.replace(files.manifest, original)
    elif change == 'remove':
        files.manifest.unlink()
    else:
        target = files.package / 'generated-manifest.yaml'
        target.write_text(original)
        files.manifest.unlink()
        files.manifest.symlink_to(target)
    assert policy() is False
    assert policy.matches(accepted) is False
    if change in ('remove', 'symlink'):
        assert files.policy()() is False
        if files.manifest.is_symlink():
            files.manifest.unlink()
        files.manifest.write_text(original)
    assert files.policy()()


@pytest.mark.parametrize('change', ['remove', 'symlink'])
def test_config_removal_or_symlink_fails_closed(files, epoch, settled, change):
    policy = files.policy()
    assert policy() and policy()
    accepted = policy.capture()
    target = files.home / 'generated-config.yaml'
    target.write_text(MANAGED)
    files.config.unlink()
    if change == 'symlink':
        files.config.symlink_to(target)
    assert policy() is False
    assert policy.matches(accepted) is False


@pytest.mark.parametrize('broken', ['plugins: [hermes-cpa\n', 'plugins: 7\n'])
def test_parse_error_after_cached_parse_fails_closed(files, epoch, settled, broken):
    epoch.value = None
    policy = files.policy()
    assert policy() and policy()
    accepted = policy.capture()
    files.write(files.config, broken)
    with pytest.raises(Exception):
        policy()
    with pytest.raises(Exception):
        policy.capture()
    with pytest.raises(Exception):
        policy.matches(accepted)
    files.write(files.config, MANAGED)
    assert policy() is False, 'restored bytes after a rewrite without an epoch must not revive'
    assert policy.matches(accepted) is False


def test_config_mutation_during_cached_sample_revokes(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()
    # Runs after the pre-sample identities matched the cached parse.
    epoch.hook = lambda: files.write(files.config, DISABLED)
    assert policy() is False


def test_manifest_mutation_during_cached_sample_revokes(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()
    epoch.hook = lambda: files.replace(files.manifest, 'name: hermes-cpa\n')
    assert policy() is False


def test_epoch_change_during_cached_sample_revokes(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()

    def bump():
        epoch.value = 'generated-epoch-2'
    # The pre-sample read still sees the cached epoch; the post-sample read does not.
    epoch.hook = lambda: setattr(epoch, 'hook', bump)
    assert policy() is False


def test_unrelated_changes_keep_service_but_not_accepted_work(files, epoch, settled):
    policy = files.policy()
    assert policy() and policy()
    accepted = policy.capture()
    (files.home / 'generated-runtime-file').write_text('unrelated')
    assert policy.matches(accepted), 'unrelated home files are not policy'
    files.write(files.config, UNRELATED)
    assert policy() and policy(), 'unrelated config edit revoked the long-lived owner'
    assert policy.matches(accepted) is False, 'accepted work survived a config rewrite'
    fresh = policy.capture()
    assert policy.matches(fresh) and policy.matches(fresh)


class SameTickFilesystem(InstallPolicy):
    """Rewrites inside one timestamp tick leave config.yaml's stamp unchanged."""
    frozen = None

    def _identities(self):
        current = super()._identities()
        if current is None:
            return None
        if self.frozen is None:
            self.frozen = current[3]
        return current[:3] + (self.frozen,)


def test_recently_written_config_is_reparsed_until_settled(files, epoch):
    # Real settle window: a just-written stamp may still be shared by a rewrite.
    files.config.write_text(MANAGED)
    policy = files.policy(SameTickFilesystem)
    assert policy()
    files.config.write_text(DISABLED)
    assert policy() is False

@pytest.mark.parametrize('check', ['service', 'accepted'])
def test_clock_rollback_rechecks_cached_parse(files, epoch, monkeypatch, check):
    policy = files.policy(SameTickFilesystem)
    stamp = max(files.config.stat().st_mtime_ns, files.config.stat().st_ctime_ns)
    clock = SimpleNamespace(now=stamp + 3_000_000_000)
    monkeypatch.setattr(supervisor.time, 'time_ns', lambda: clock.now)
    captured = policy.capture()
    assert captured is not None and policy.matches(captured)
    clock.now = stamp + 1_000_000_000
    files.config.write_text(DISABLED)
    if check == 'service':
        assert policy() is False
    else:
        assert policy.matches(captured) is False



def test_concurrent_checks_share_cache_and_revocation(files, epoch, settled):
    policy = files.policy()
    assert policy()
    results, errors = [], []
    start = threading.Barrier(8)

    def worker():
        try:
            start.wait()
            for _ in range(25):
                captured = policy.capture()
                results.append(policy() and captured is not None and policy.matches(captured))
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    def run():
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)
        assert not errors

    run()
    assert results and all(results)
    files.write(files.config, DISABLED)
    results.clear()
    run()
    assert results and not any(results)
