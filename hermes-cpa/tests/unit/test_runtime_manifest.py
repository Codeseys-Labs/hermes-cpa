"""The packaged runtime manifest decides what may be fetched and launched.

Only digests, sizes and build provenance are data; everything that selects a source
(repository, tag, asset, base commit, toolchain, flags) must equal the pinned code
identity, so editing the packaged JSON can never redirect the installer.
"""
import copy
import json

import pytest
from hermes_cpa.runtime_manifest import (ARCHIVE_NAME, CANDIDATE_REPOSITORY, CANDIDATE_TAG,
                                         MAX_ARTIFACT_BYTES, dump_runtime_manifest, glibc_tuple,
                                         parse_runtime_manifest, unbuilt_manifest)
from runtime_fixtures import BINARY, pinned_manifest, write_archive


def pinned(tmp_path):
    archive = tmp_path / 'generated.tar.gz'
    return pinned_manifest(write_archive(archive, BINARY), BINARY)


def test_unbuilt_manifest_is_valid_but_not_pinned():
    runtime = parse_runtime_manifest(dump_runtime_manifest(unbuilt_manifest()))
    assert not runtime.pinned and runtime.artifact is None


def test_pinned_manifest_exposes_exact_digests_and_sizes(tmp_path):
    data = pinned(tmp_path)
    artifact = parse_runtime_manifest(dump_runtime_manifest(data)).artifact
    assert artifact.name == ARCHIVE_NAME
    assert artifact.archive_sha256 == data['artifact']['archive_sha256']
    assert (artifact.binary_sha256, artifact.binary_size) == (data['artifact']['binary_sha256'], len(BINARY))


def test_candidate_identity_is_fixed_to_the_approved_private_repository():
    candidate = unbuilt_manifest()['candidate']
    assert candidate == {'host': 'github.com', 'repository': 'baladithyab/hermes-cpa',
                         'tag': CANDIDATE_TAG, 'asset': ARCHIVE_NAME}
    assert CANDIDATE_REPOSITORY == 'baladithyab/hermes-cpa'


def mutate(path, value):
    def apply(data):
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return apply


def delete(path):
    def apply(data):
        target = data
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    return apply


@pytest.mark.parametrize('damage', [
    mutate(('candidate', 'repository'), 'attacker/other-repo'),
    mutate(('candidate', 'host'), 'ghe.example.com'),
    mutate(('candidate', 'tag'), 'v8.0.21'),
    mutate(('candidate', 'asset'), 'CLIProxyAPI_8.0.21_linux_amd64.tar.gz'),
    mutate(('integration',), 'stock'),
    mutate(('cpa_version',), '8.0.22'),
    mutate(('platform',), 'linux_arm64'),
    mutate(('schema',), True),
    mutate(('source', 'base_commit'), '0' * 40),
    mutate(('source', 'upstream_repository'), 'https://example.com/CLIProxyAPI'),
    mutate(('build', 'ldflags'), '-s -w'),
    mutate(('build', 'cgo'), False),
    mutate(('build', 'go_version'), 'go1.27.2'),
    mutate(('build', 'go_toolchain_sha256'), '0' * 64),
    mutate(('build', 'glibc_max'), 'musl'),
    mutate(('build', 'reproducible'), False),
    mutate(('build', 'reproducible'), 1),
    mutate(('artifact', 'name'), 'CLIProxyAPI_8.0.21_linux_amd64.tar.gz'),
    mutate(('artifact', 'archive_sha256'), 'A' * 64),
    mutate(('artifact', 'archive_sha256'), 'abc'),
    mutate(('artifact', 'binary_size'), 0),
    mutate(('artifact', 'binary_size'), True),
    mutate(('artifact', 'archive_size'), MAX_ARTIFACT_BYTES + 1),
    mutate(('source', 'patch_sha256'), None),
    mutate(('build', 'gcc_version'), None),
    delete(('artifact', 'binary_sha256')),
    delete(('candidate',)),
    lambda data: data.update(extra='key'),
    lambda data: data['artifact'].update(url='https://example.com/runtime.tar.gz'),
], ids=lambda f: getattr(f, '__name__', 'damage'))
def test_drift_from_the_pinned_identity_is_refused(tmp_path, damage):
    data = copy.deepcopy(pinned(tmp_path))
    damage(data)
    with pytest.raises(ValueError, match='invalid_runtime_manifest'):
        parse_runtime_manifest(json.dumps(data).encode())


def test_digest_without_a_reproduced_build_is_refused():
    data = unbuilt_manifest()
    data['artifact'] = dict(name=ARCHIVE_NAME, archive_sha256='1' * 64, archive_size=1,
                            binary_sha256='2' * 64, binary_size=1)
    with pytest.raises(ValueError, match='invalid_runtime_manifest'):
        parse_runtime_manifest(json.dumps(data).encode())


@pytest.mark.parametrize('raw', [b'', b'[]', b'null', b'{', b'{}'])
def test_unreadable_manifests_are_refused(raw):
    with pytest.raises(ValueError, match='invalid_runtime_manifest'):
        parse_runtime_manifest(raw)


def test_writer_refuses_to_emit_a_manifest_the_installer_would_refuse():
    data = unbuilt_manifest()
    data['candidate']['repository'] = 'attacker/other-repo'
    with pytest.raises(ValueError, match='invalid_runtime_manifest'):
        dump_runtime_manifest(data)


@pytest.mark.parametrize('text,expected', [
    ('2.36', (2, 36)), ('glibc 2.36', (2, 36)), ('2.17.1', (2, 17, 1)),
    ('', None), ('musl', None), ('2', None), ('2.x', None), (None, None)])
def test_glibc_versions_parse_or_are_refused(text, expected):
    assert glibc_tuple(text) == expected
