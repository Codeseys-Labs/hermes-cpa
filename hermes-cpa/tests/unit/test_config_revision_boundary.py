import threading

import pytest
from hermes_cpa.config_store import ConfigStore
from hermes_cpa.contracts import ConfigApplyRequest
from pydantic import ValidationError


class NeverPrepare:
    """A stale or missing revision must be refused before native preparation."""
    mutation_gate = threading.Lock()
    mode, generation, policy_locked, running = 'managed', 1, False, True

    def prepare_config(self, *args):
        raise AssertionError('prepared against an unverified revision')


@pytest.fixture
def store(tmp_path):
    result = ConfigStore(tmp_path / 'config.yaml')
    result.create_initial_config(18318, access_keys=['generated-test-client-key'])
    return result


@pytest.mark.parametrize('revision', ['', None, False, 0, 'stale', 'a' * 64])
def test_missing_or_stale_revision_never_prepares_or_changes_bytes(store, revision):
    before = store.config_path.read_bytes()
    result = store.transact(revision, [{'method': 'PUT', 'path': '/v8/management/config/routing/strategy',
                                        'content_type': 'application/json', 'body_base64': 'ImZpbGwtZmlyc3Qi'}],
                            expected_generation=1, owner=NeverPrepare())
    assert not result['ok']
    assert result['code'] == 'STALE_REVISION'
    assert store.config_path.read_bytes() == before


def test_empty_revision_rejected_at_rest_boundary():
    with pytest.raises(ValidationError):
        ConfigApplyRequest(expected_revision='', expected_generation=1, changes={})
