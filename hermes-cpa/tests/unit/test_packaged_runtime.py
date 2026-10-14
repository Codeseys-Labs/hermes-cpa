"""The shipped runtime metadata: integration identity, no stock pins, no divergent digests."""
import json
from pathlib import Path

from hermes_cpa.runtime_manifest import INTEGRATION, glibc_tuple, parse_runtime_manifest

PACKAGE = Path(__file__).resolve().parents[2]
# Digests of the unchanged upstream CPA 8.0.21 linux_amd64 release archive and executable.
STOCK_DIGESTS = ('a40164e25a60869df2c489702383d4e64dc27bb08eaafd7b04a101a2f5cfc625',
                 '95435293c56e856db7d70b377d30b2b2c00e32ff1c06c41ae46758a9f6febdee')


def packaged():
    raw = (PACKAGE / 'cpa-runtime.json').read_bytes()
    return parse_runtime_manifest(raw), json.loads((PACKAGE / 'compatibility.json').read_bytes()), raw


def test_packaged_metadata_names_only_the_integration_runtime():
    runtime, compatibility, raw = packaged()
    pairs = compatibility['supported_pairs']
    assert pairs and all(pair['integration'] == INTEGRATION == runtime.data['integration']
                         for pair in pairs)
    text = raw.decode() + json.dumps(compatibility)
    assert not [digest for digest in STOCK_DIGESTS if digest in text], 'stock release pin shipped'


def test_compatibility_pair_digests_agree_with_the_runtime_manifest():
    runtime, compatibility, _ = packaged()
    (pair,) = compatibility['supported_pairs']
    if runtime.artifact is None:
        # Unbuilt: nothing pinned anywhere, so the installer refuses rather than guessing.
        assert pair['archive_sha256'] is None and pair['binary_sha256'] is None
        assert runtime.data['source']['patch_sha256'] is None
    else:
        assert (pair['archive_sha256'], pair['binary_sha256']) == (
            runtime.artifact.archive_sha256, runtime.artifact.binary_sha256)
        assert glibc_tuple(runtime.data['build']['glibc_max'])
