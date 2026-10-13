"""Safety expectations against immutable integrated inputs; known failures are retained.
Generated assets only. Run with backend/gate fixture directories on PYTHONPATH.
"""
import json

from test_transactions import Owner, apply, store
from test_gates import case


def test_sec1_unaccepted_manifest_blocks_install(case):
    case.manifest.update(candidate_status='unaccepted', adr_status='Proposed',
                         tested_minimum_hermes=None)
    (case.root / 'compatibility.json').write_text(json.dumps(case.manifest))
    try:
        case.installer.install_from_archive(case.archive)
    except ValueError:
        pass
    assert not case.settings.binary_path.exists(), 'Unaccepted fixture was installed'


def test_sec2_post_replace_failure_preserves_external_edit(store, monkeypatch):
    owner = Owner()
    original = store._atomic
    external = b'config-version: 8\nexternal: retained\n'
    failed = False

    def partial_failure(path, raw):
        nonlocal failed
        original(path, raw)
        if path == store.config_path and not failed:
            failed = True
            # Replacement succeeded, but durability acknowledgement fails after
            # an independent writer changes bytes. No real disk fault claimed.
            path.write_bytes(external)
            raise OSError('generated directory fsync failure')

    monkeypatch.setattr(store, '_atomic', partial_failure)
    result = apply(store, owner)
    assert not result['ok']
    assert store.config_path.read_bytes() == external, result


def test_sec3_policy_revoked_during_stop_prevents_apply(store):
    owner = Owner()
    before = store.config_path.read_bytes()
    stop = owner.stop_owned_and_reap

    def revoke():
        result = stop()
        owner.policy_locked = True
        return result

    owner.stop_owned_and_reap = revoke
    result = apply(store, owner)
    assert not result['ok'], result
    assert store.config_path.read_bytes() == before
    assert 'start/probe' not in owner.calls
