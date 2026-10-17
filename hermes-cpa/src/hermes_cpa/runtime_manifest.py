"""Pinned identity of the CPA runtime this plugin installs and launches.

The runtime is the Hermes owner-integration build of CLIProxyAPI 8.0.21, built
from one pinned upstream commit plus one exported patch. It is never the
unchanged stock release. This module is stdlib-only on purpose: the packaged
``cpa-runtime.json`` is read by the installer and written by
``scripts/build_cpa_runtime.py``, and both share these constants and rules.

Identity that selects *what* may be fetched or launched (repository, tag, asset,
toolchain, ldflags) lives here in code. The JSON file carries only the data a
build produces (digests, sizes, provenance) and must agree with the constants.
"""
import json
import re
from dataclasses import dataclass

SCHEMA = 1
CPA_VERSION = '8.0.21'
PLATFORM = 'linux_amd64'
INTEGRATION = 'hermes-owner-v1'

UPSTREAM_REPOSITORY = 'https://github.com/router-for-me/CLIProxyAPI'
UPSTREAM_TAG = 'v8.0.21'
UPSTREAM_COMMIT = '54946fa3dfa29c6ca7312ac141a92cdd5e413771'
# Committer date of UPSTREAM_COMMIT in UTC; the deterministic build date.
UPSTREAM_COMMIT_DATE = '2026-10-08T12:04:31Z'
GO_MODULE = 'github.com/router-for-me/CLIProxyAPI/v8'
BUILD_PACKAGE = './cmd/server'
BINARY_NAME = 'cli-proxy-api'

# Official toolchain used for every release build (module floor is go 1.26.0).
GO_VERSION = 'go1.26.9'
GO_TOOLCHAIN_URL = 'https://go.dev/dl/go1.26.9.linux-amd64.tar.gz'
GO_TOOLCHAIN_SIZE = 66935201
GO_TOOLCHAIN_SHA256 = '42d158b4d8f7b61ac0a830567c940a86098fb7aac52e467a5ebec03ef5cc2f8d'

# The only remote the installer may contact, through an authenticated gh CLI.
CANDIDATE_HOST = 'github.com'
CANDIDATE_REPOSITORY = 'Codeseys-Labs/hermes-cpa'
CANDIDATE_TAG = f'cpa-runtime-v{CPA_VERSION}-{INTEGRATION}-candidate2'
ARCHIVE_NAME = f'CLIProxyAPI_{CPA_VERSION}_{INTEGRATION}_{PLATFORM}.tar.gz'

# Hard bound for the compressed archive and for the executable inside it.
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024

# The executable may link only the glibc family; plugins load through dlopen.
ALLOWED_NEEDED = frozenset({'libc.so.6', 'libdl.so.2', 'libpthread.so.0', 'libm.so.6',
                            'libresolv.so.2', 'librt.so.1'})

_GLIBC = re.compile(r'(?:glibc )?(\d+)\.(\d+)(?:\.(\d+))?')


def glibc_tuple(text):
    """'2.36', 'glibc 2.36' or '2.17.1' -> (2, 36) style tuple, else None."""
    match = _GLIBC.fullmatch(text) if isinstance(text, str) else None
    return tuple(int(part) for part in match.groups() if part is not None) if match else None

_HEX64 = re.compile(r'[0-9a-f]{64}')
_TOP = {'schema', 'integration', 'cpa_version', 'platform', 'source', 'build', 'artifact', 'candidate'}
_SOURCE = {'upstream_repository', 'upstream_tag', 'base_commit', 'base_commit_date',
           'patch_sha256', 'patch_size', 'changed_files'}
_BUILD = {'package', 'ldflags', 'cgo', 'trimpath', 'go_version', 'go_toolchain_sha256',
          'gcc_version', 'libc_version', 'glibc_max', 'reproducible'}
_ARTIFACT = {'name', 'archive_sha256', 'archive_size', 'binary_sha256', 'binary_size'}
_CANDIDATE = {'host', 'repository', 'tag', 'asset'}


def build_ldflags():
    """Exact linker flags. main.* are set because main.init() copies them over buildinfo."""
    return ' '.join((
        '-s', '-w',
        f'-X main.Version={CPA_VERSION}',
        f'-X main.Commit={UPSTREAM_COMMIT[:7]}',
        f'-X main.BuildDate={UPSTREAM_COMMIT_DATE}',
        f'-X {GO_MODULE}/internal/buildinfo.Version={CPA_VERSION}',
    ))


def unbuilt_manifest():
    """Packaged state before scripts/build_cpa_runtime.py has produced an artifact."""
    return {
        'schema': SCHEMA, 'integration': INTEGRATION, 'cpa_version': CPA_VERSION,
        'platform': PLATFORM,
        'source': {
            'upstream_repository': UPSTREAM_REPOSITORY, 'upstream_tag': UPSTREAM_TAG,
            'base_commit': UPSTREAM_COMMIT, 'base_commit_date': UPSTREAM_COMMIT_DATE,
            'patch_sha256': None, 'patch_size': None, 'changed_files': None,
        },
        'build': {
            'package': BUILD_PACKAGE, 'ldflags': build_ldflags(), 'cgo': True, 'trimpath': True,
            'go_version': GO_VERSION, 'go_toolchain_sha256': GO_TOOLCHAIN_SHA256,
            'gcc_version': None, 'libc_version': None, 'glibc_max': None, 'reproducible': None,
        },
        'artifact': None,
        'candidate': {'host': CANDIDATE_HOST, 'repository': CANDIDATE_REPOSITORY,
                      'tag': CANDIDATE_TAG, 'asset': ARCHIVE_NAME},
    }


@dataclass(frozen=True)
class RuntimeArtifact:
    name: str
    archive_sha256: str
    archive_size: int
    binary_sha256: str
    binary_size: int


@dataclass(frozen=True)
class RuntimeManifest:
    data: dict
    artifact: 'RuntimeArtifact | None'

    @property
    def pinned(self):
        return self.artifact is not None


def _invalid():
    return ValueError('invalid_runtime_manifest')


def _int(value, low=1, high=MAX_ARTIFACT_BYTES):
    if type(value) is not int or not low <= value <= high:
        raise _invalid()
    return value


def _hex(value):
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise _invalid()
    return value


def _section(data, name, keys):
    section = data.get(name)
    if not isinstance(section, dict) or set(section) != keys:
        raise _invalid()
    return section


def parse_runtime_manifest(raw):
    """Validate packaged bytes. Unknown keys, identity drift or odd types are refused."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise _invalid() from None
    if not isinstance(data, dict) or set(data) != _TOP:
        raise _invalid()
    if (type(data['schema']) is not int or data['schema'] != SCHEMA or
            data['integration'] != INTEGRATION or data['cpa_version'] != CPA_VERSION or
            data['platform'] != PLATFORM):
        raise _invalid()

    source = _section(data, 'source', _SOURCE)
    if (source['upstream_repository'] != UPSTREAM_REPOSITORY or source['upstream_tag'] != UPSTREAM_TAG or
            source['base_commit'] != UPSTREAM_COMMIT or source['base_commit_date'] != UPSTREAM_COMMIT_DATE):
        raise _invalid()
    patch = (source['patch_sha256'], source['patch_size'], source['changed_files'])
    if all(value is None for value in patch):
        patched = False
    else:
        _hex(source['patch_sha256'])
        _int(source['patch_size'], 1, 64 * 1024 * 1024)
        _int(source['changed_files'], 1, 100000)
        patched = True

    build = _section(data, 'build', _BUILD)
    if (build['package'] != BUILD_PACKAGE or build['ldflags'] != build_ldflags() or
            build['cgo'] is not True or build['trimpath'] is not True or
            build['go_version'] != GO_VERSION or build['go_toolchain_sha256'] != GO_TOOLCHAIN_SHA256):
        raise _invalid()
    for name in ('gcc_version', 'libc_version', 'glibc_max'):
        if build[name] is not None and (not isinstance(build[name], str) or not 0 < len(build[name]) <= 256):
            raise _invalid()
    if build['glibc_max'] is not None and glibc_tuple(build['glibc_max']) is None:
        raise _invalid()
    if build['reproducible'] is not None and build['reproducible'] is not True:
        raise _invalid()

    candidate = _section(data, 'candidate', _CANDIDATE)
    if (candidate['host'] != CANDIDATE_HOST or candidate['repository'] != CANDIDATE_REPOSITORY or
            candidate['tag'] != CANDIDATE_TAG or candidate['asset'] != ARCHIVE_NAME):
        raise _invalid()

    artifact = None
    if data['artifact'] is not None:
        row = _section(data, 'artifact', _ARTIFACT)
        if row['name'] != ARCHIVE_NAME:
            raise _invalid()
        # A pinned artifact must come from a verified patch and a reproduced build.
        if (not patched or build['reproducible'] is not True or
                None in (build['gcc_version'], build['libc_version'], build['glibc_max'])):
            raise _invalid()
        artifact = RuntimeArtifact(
            row['name'], _hex(row['archive_sha256']), _int(row['archive_size']),
            _hex(row['binary_sha256']), _int(row['binary_size']))
    elif build['reproducible'] is not None:
        raise _invalid()
    return RuntimeManifest(data, artifact)


def dump_runtime_manifest(data):
    """Canonical bytes for a manifest that parses; the build script writes only these."""
    raw = (json.dumps(data, indent=2) + '\n').encode()
    parse_runtime_manifest(raw)
    return raw
