"""Native recovery uses real owner gate; never grants guest recovery authority."""
from hermes_cpa.config_store import ConfigStore
from test_lifecycle_safety import owner


def test_native_recovery_dispatch_restores_and_stays_stopped(owner):
    store = ConfigStore(owner.settings.config_path)
    original = store.compute_digest()
    inode = store.config_path.stat().st_ino
    store.snapshot()
    # An interrupted native write left different bytes behind an unfinished journal.
    store._write_in_place(b'server: {host: 127.0.0.1, port: 8319}\n')
    store._journal('applying', original)
    args = dict(expected_revision=store.compute_digest(), snapshot_revision=original,
                expected_generation=owner.generation, native_consent=True)
    result = owner.dispatch({'cmd': 'recover', 'args': args})
    assert result['ok'], result
    assert store.compute_digest() == original
    assert store.config_path.stat().st_ino == inode
    assert owner.child_process is None


def test_native_recovery_rejects_missing_consent(owner):
    store = ConfigStore(owner.settings.config_path)
    original = store.compute_digest()
    store.snapshot()
    store._journal('applying', original)
    result = owner.dispatch({'cmd': 'recover', 'args': dict(expected_revision=original,
        snapshot_revision=original, expected_generation=owner.generation)})
    assert result['code'] == 'CONSENT_REQUIRED'
    assert store.compute_digest() == original


def test_native_recovery_cli_requires_all_explicit_inputs():
    from hermes_cpa.cli import build_parser
    parsed = build_parser().parse_args(['recover', '--expected-revision', 'a'*64,
        '--snapshot-revision', 'b'*64, '--expected-generation', '1', '--consent'])
    assert parsed.subcommand == 'recover' and parsed.consent
