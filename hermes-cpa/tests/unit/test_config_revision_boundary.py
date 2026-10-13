import pytest
from hermes_cpa.config_store import ConfigStore
from hermes_cpa.contracts import ConfigApplyRequest
from pydantic import ValidationError


@pytest.fixture
def store(tmp_path):
    result = ConfigStore(tmp_path / 'config.yaml')
    result.create_initial_config(18318, 'generated-test-management', 'generated-test-inference')
    return result


@pytest.mark.parametrize('revision', ['', None, False, 0, 'stale'])
def test_missing_or_stale_revision_never_changes_bytes(store, revision):
    before = store.config_path.read_bytes()
    ok, _, code = store.apply_safe_changes(revision, {'strategy': 'fill-first'})
    assert not ok
    assert code == 'STALE_REVISION'
    assert store.config_path.read_bytes() == before


def test_unknown_change_rejected_without_rewriting(store):
    before = store.config_path.read_bytes()
    ok, _, code = store.apply_safe_changes(store.compute_digest(), {'server.host': '0.0.0.0'})
    assert not ok
    assert code == 'INVALID_INPUT'
    assert store.config_path.read_bytes() == before


def test_empty_revision_rejected_at_rest_boundary():
    with pytest.raises(ValidationError):
        ConfigApplyRequest(expected_revision='', expected_generation=1, changes={})
