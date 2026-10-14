#!/usr/bin/env python3
"""Build the pinned Hermes owner-integration CPA runtime, deterministically.

Inputs are exactly: the pinned upstream base commit (read from a local clone with
`git archive`, never its working tree), the exported patch in runtime-integration/
(digest and per-file results verified), and the pinned Go toolchain. Output is the
deterministic archive CLIProxyAPI_8.0.21_hermes-owner-v1_linux_amd64.tar.gz.

CGO is required, not optional: the dynamic plugin loader (internal/pluginhost) is
cgo-only (`#cgo LDFLAGS: -ldl`); without cgo plugins are refused. The build is
therefore dynamic against glibc and must run where gcc and libc headers exist, in
the image that matches the runtime hosts. The binary's max GLIBC symbol version is
recorded, and the installer refuses hosts with an older glibc.

The binary is built twice, with separate empty Go build caches, and must be
byte-identical or the build fails. Without --check this then writes the artifact
digests and provenance into hermes-cpa/cpa-runtime.json and the matching pair in
hermes-cpa/compatibility.json (release-acceptance fields are never touched). With
--check nothing packaged is written; any difference from the packaged metadata fails.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from cpa_runtime_common import (COMPATIBILITY, DEFAULT_SOURCE, MANIFEST, ROOT, atomic_write, fail,
                                load_runtime_manifest_module, read_provenance, verify_base)

TOOLCHAIN_ROOT = ROOT / '.verification/toolchains/go1.26.9'
DEFAULT_OUTPUT = ROOT / 'dist/cpa-runtime'
DEFAULT_MODCACHE = ROOT / '.verification/go-mod-cache'
PASSTHROUGH = ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
               'SSL_CERT_FILE', 'SSL_CERT_DIR', 'GOPROXY', 'GOSUMDB', 'GOPRIVATE', 'GOINSECURE')
INTEGRATION_STRINGS = (b'hermes-owner-v1', b'hermes-control-socket')


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def require_host():
    if sys.platform != 'linux' or os.uname().machine not in ('x86_64', 'amd64'):
        fail('the runtime must be built on linux/amd64 (use the pinned qualification build image)')
    for tool in ('git', 'gcc', 'readelf'):
        if shutil.which(tool) is None:
            fail(f'{tool} is required (the cgo plugin loader needs gcc and libc headers)')
    if not hasattr(tarfile, 'data_filter'):
        fail('Python with tarfile data filters (3.11.4+, 3.12+) is required')


def locate_toolchain(rm, root, archive):
    """GOROOT of the pinned toolchain; provisioned from the pinned tarball when absent."""
    for candidate in (root, root / 'go'):
        if (candidate / 'bin/go').is_file():
            return candidate.resolve()
    if not archive.is_file():
        fail(f'pinned {rm.GO_VERSION} toolchain not found under {root}; place {rm.GO_TOOLCHAIN_URL} '
             f'at {archive} (size {rm.GO_TOOLCHAIN_SIZE}, sha256 {rm.GO_TOOLCHAIN_SHA256})')
    if archive.stat().st_size != rm.GO_TOOLCHAIN_SIZE or sha256_file(archive) != rm.GO_TOOLCHAIN_SHA256:
        fail(f'{archive} does not match the pinned {rm.GO_VERSION} toolchain size/sha256')
    target = root.parent if root.name == 'go' else root
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as bundle:
        members = bundle.getmembers()
        if any(member.name != 'go' and not member.name.startswith('go/') for member in members):
            fail(f'{archive} has members outside go/')
        bundle.extractall(target, members, filter='data')
    if not (target / 'go/bin/go').is_file():
        fail(f'{archive} did not provide go/bin/go')
    return (target / 'go').resolve()


def base_environment(work, cache, modcache, offline, jobs):
    env = {name: os.environ[name] for name in PASSTHROUGH if name in os.environ}
    env.update(HOME=str(work / 'home'), LANG='C', LC_ALL='C', TZ='UTC', GOWORK='off', GOFLAGS='',
               GOMAXPROCS=str(jobs),
               GOTOOLCHAIN='local', GOOS='linux', GOARCH='amd64', CGO_ENABLED='1',
               GOCACHE=str(cache), GOMODCACHE=str(modcache), GOPATH=str(work / 'gopath'))
    if offline:
        env['GOPROXY'] = 'off'
    return env


def run(command, **kwargs):
    done = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip()[-4000:]
        fail(f'{Path(command[0]).name} {command[1] if len(command) > 1 else ""} failed:\n{tail}')
    return done.stdout


def export_base(source, rm, tree):
    """Extract the pinned base commit's tree (never the working tree) into TREE."""
    tree.mkdir(parents=True)
    process = subprocess.Popen(
        ['git', '-c', f'safe.directory={source}', '-C', str(source), 'archive', '--format=tar',
         rm.UPSTREAM_COMMIT], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    with tarfile.open(fileobj=process.stdout, mode='r|') as bundle:
        bundle.extractall(tree, filter='data')
    error = process.stderr.read().decode(errors='replace')
    if process.wait() != 0:
        fail(f'git archive failed: {error.strip()}')


def apply_patch(rm, tree, provenance, patch):
    """Apply the exported patch and prove it changed exactly the declared files to the declared bytes."""
    patch_file = tree.parent / 'integration.patch'
    patch_file.write_bytes(patch)
    base = ['git', '-C', str(tree)]
    run([*base, 'apply', '--check', '--whitespace=nowarn', str(patch_file)])
    touched = run([*base, 'apply', '--numstat', '-z', str(patch_file)]).split('\0')
    paths = sorted(row.split('\t', 2)[2] for row in touched if row)
    declared = sorted(row['path'] for row in provenance['files'])
    if paths != declared:
        fail('patch touches different files than runtime-integration/provenance.json declares')
    run([*base, 'apply', '--whitespace=nowarn', str(patch_file)])
    for row in provenance['files']:
        path = tree / row['path']
        if row['after_sha256'] is None:
            if path.exists():
                fail(f'{row["path"]} should be deleted by the patch')
        elif not path.is_file() or path.is_symlink() or sha256_file(path) != row['after_sha256']:
            fail(f'{row["path"]} differs from the declared post-patch digest')
    patch_file.unlink()


def go_build(rm, go, goroot, tree, env, destination, jobs):
    full = {**env, 'PATH': f'{goroot}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'GOROOT': str(goroot)}
    run([str(go), 'build', f'-p={jobs}', '-trimpath', '-buildvcs=false', '-mod=readonly',
         '-ldflags', rm.build_ldflags(), '-o', str(destination), rm.BUILD_PACKAGE],
        cwd=tree, env=full)
    return full


def inspect_binary(rm, path):
    """Require the glibc-only cgo ELF this installer expects; return the max GLIBC symbol version."""
    with open(path, 'rb') as stream:
        header = stream.read(20)
    if (header[:4] != b'\x7fELF' or header[4] != 2 or header[5] != 1 or
            int.from_bytes(header[18:20], 'little') != 0x3E):
        fail('built binary is not a little-endian x86-64 ELF executable')
    needed = set(re.findall(r'\(NEEDED\)\s+Shared library: \[([^\]]+)\]',
                            run(['readelf', '-d', str(path)])))
    if not needed or needed - rm.ALLOWED_NEEDED:
        fail(f'unexpected dynamic libraries {sorted(needed - rm.ALLOWED_NEEDED) or "(none: not cgo?)"}; '
             'the runtime must be the cgo plugin-capable build linked only against glibc')
    versions = {tuple(int(part) for part in text.split('.')) for text in
                re.findall(r'Name: GLIBC_(\d+(?:\.\d+)*)', run(['readelf', '--version-info', str(path)]))}
    if not versions:
        fail('binary requires no GLIBC symbol versions; it is not the expected dynamic cgo build')
    data = path.read_bytes()
    for marker in INTEGRATION_STRINGS:
        if marker not in data:
            fail(f'built binary lacks the integration marker {marker.decode()!r}; the exported patch '
                 'is not part of this build')
    return '.'.join(map(str, max(versions)))


def build_archive(destination, binary, license_file, epoch):
    """Deterministic tar.gz: fixed order, owners, modes, mtimes, no gzip name or time."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.tmp')
    try:
        with open(temporary, 'wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=9, mtime=0) as packed:
                with tarfile.open(fileobj=packed, mode='w', format=tarfile.USTAR_FORMAT) as bundle:
                    for name, source, mode in (('cli-proxy-api', binary, 0o755),
                                               ('LICENSE', license_file, 0o644)):
                        info = tarfile.TarInfo(name)
                        info.size, info.mode, info.mtime = source.stat().st_size, mode, epoch
                        info.uid = info.gid = 0
                        info.uname = info.gname = ''
                        with open(source, 'rb') as stream:
                            bundle.addfile(info, stream)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def verify_archive(rm, destination, binary_sha256):
    with tarfile.open(destination, 'r:gz') as bundle:
        names = bundle.getnames()
        if names != [rm.BINARY_NAME, 'LICENSE']:
            fail(f'unexpected archive members {names}')
        if hashlib.sha256(bundle.extractfile(rm.BINARY_NAME).read()).hexdigest() != binary_sha256:
            fail('archive executable does not match the built binary')


def compatibility_with(rm, artifact):
    data = json.loads(COMPATIBILITY.read_bytes())
    pairs = data.get('supported_pairs') if isinstance(data, dict) else None
    index = next((i for i, pair in enumerate(pairs or []) if isinstance(pair, dict) and
                  pair.get('cpa_version') == rm.CPA_VERSION and pair.get('console_version') == '1.25.6' and
                  pair.get('platform') == rm.PLATFORM), None)
    if index is None:
        fail('compatibility.json has no CPA 8.0.21 / console 1.25.6 / linux_amd64 pair')
    updated = {}
    for key, value in pairs[index].items():
        updated[key] = value
        if key == 'platform':
            updated['integration'] = rm.INTEGRATION
    updated.update(integration=rm.INTEGRATION, binary_sha256=artifact['binary_sha256'],
                   archive_sha256=artifact['archive_sha256'])
    pairs[index] = updated
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE,
                        help='local CPA git clone containing the pinned base commit')
    parser.add_argument('--go-root', type=Path, default=TOOLCHAIN_ROOT,
                        help='pinned Go toolchain (GOROOT, or its parent); provisioned from the '
                             'pinned tarball next to it when absent')
    parser.add_argument('--go-archive', type=Path, default=TOOLCHAIN_ROOT.with_name('go1.26.9.tar.gz'))
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--modcache', type=Path, default=DEFAULT_MODCACHE,
                        help='Go module cache (go.sum still verifies every module)')
    parser.add_argument('--offline', action='store_true', help='GOPROXY=off; modules must be cached')
    parser.add_argument('--jobs', type=int, default=4,
                        help='bounded compiler concurrency (GOMAXPROCS and go build -p); default 4')
    parser.add_argument('--check', action='store_true',
                        help='compare against the packaged metadata instead of writing it')
    args = parser.parse_args()
    if not 1 <= args.jobs <= 64:
        parser.error('--jobs must be between 1 and 64')
    require_host()
    rm = load_runtime_manifest_module()
    source = args.source.resolve()
    epoch = verify_base(source, rm)
    provenance, patch = read_provenance(rm)
    goroot = locate_toolchain(rm, args.go_root.resolve(), args.go_archive.resolve())
    go = goroot / 'bin/go'
    base_env = {'GOTOOLCHAIN': 'local', 'GOFLAGS': '', 'GOROOT': str(goroot)}
    reported = run([str(go), 'version'], env={**base_env, 'PATH': '/usr/bin:/bin', 'HOME': '/tmp'}).strip()
    if reported != f'go version {rm.GO_VERSION} linux/amd64':
        fail(f'toolchain reports {reported!r}; exactly "go version {rm.GO_VERSION} linux/amd64" is required')
    args.modcache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cpa-runtime-') as scratch:
        work = Path(scratch)
        (work / 'home').mkdir()
        tree = work / 'src'
        export_base(source, rm, tree)
        apply_patch(rm, tree, provenance, patch)
        binaries = []
        for attempt in (1, 2):
            print(f'building {rm.BUILD_PACKAGE} ({attempt}/2, empty Go build cache)...', file=sys.stderr)
            env = base_environment(work, work / f'gocache{attempt}', args.modcache.resolve(),
                                   args.offline, args.jobs)
            destination = work / f'cli-proxy-api.{attempt}'
            go_build(rm, go, goroot, tree, env, destination, args.jobs)
            binaries.append(destination)
        first, second = (sha256_file(path) for path in binaries)
        if first != second:
            fail(f'build is not reproducible: {first} != {second}')
        glibc_max = inspect_binary(rm, binaries[0])
        archive_path = args.output_dir.resolve() / rm.ARCHIVE_NAME
        build_archive(archive_path, binaries[0], tree / 'LICENSE', epoch)
        verify_archive(rm, archive_path, first)
        gcc = run(['gcc', '--version']).splitlines()[0].strip()
        libc = os.confstr('CS_GNU_LIBC_VERSION')
        artifact = {'name': rm.ARCHIVE_NAME, 'archive_sha256': sha256_file(archive_path),
                    'archive_size': archive_path.stat().st_size, 'binary_sha256': first,
                    'binary_size': binaries[0].stat().st_size}
    data = rm.unbuilt_manifest()
    data['source'].update(patch_sha256=provenance['patch_sha256'], patch_size=provenance['patch_size'],
                          changed_files=len(provenance['files']))
    data['build'].update(gcc_version=gcc, libc_version=libc, glibc_max=glibc_max, reproducible=True)
    data['artifact'] = artifact
    manifest_bytes = rm.dump_runtime_manifest(data)  # also proves it satisfies the installer's rules
    compatibility = compatibility_with(rm, artifact)
    if args.check:
        packaged = json.loads(MANIFEST.read_bytes()) if MANIFEST.is_file() else None
        if packaged != data or json.loads(COMPATIBILITY.read_bytes()) != compatibility:
            fail('packaged cpa-runtime.json / compatibility.json differ from this build')
    else:
        atomic_write(MANIFEST, manifest_bytes)
        atomic_write(COMPATIBILITY, (json.dumps(compatibility, indent=2) + '\n').encode())
    print(json.dumps({**artifact, 'archive_path': str(archive_path), 'glibc_max': glibc_max,
                      'gcc': gcc, 'libc': libc, 'go': rm.GO_VERSION,
                      'patch_sha256': provenance['patch_sha256'], 'reproducible': True,
                      'packaged_metadata': 'verified' if args.check else 'written'}, indent=2))


if __name__ == '__main__':
    main()
