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

The archive keeps exactly two members, the executable and LICENSE. LICENSE is the
unmodified upstream CLIProxyAPI LICENSE followed by the complete license, notice and
patent-grant files of every Go module and of the Go standard library that is linked
into the executable. The linked set comes from `go list -deps` for the exact build
settings and must equal the module list the executable itself reports (`go version
-m`); the texts are read from the verified module cache and the pinned GOROOT, so the
same inputs always give the same bytes. A linked module with no license file fails the
build rather than being listed without its text.

The binary is built twice, with separate empty Go build caches, and must be
byte-identical or the build fails. Without --check this then writes the artifact
digests and provenance into hermes-cpa/cpa-runtime.json and the matching pair in
hermes-cpa/compatibility.json (release-acceptance fields are never touched). With
--check nothing packaged is written; any difference from the packaged metadata fails.
"""
import argparse
import gzip
import hashlib
import io
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
# Files that carry licence terms: LICENSE, LICENCE, COPYING, COPYRIGHT, NOTICE, UNLICENSE and
# PATENTS, optionally followed by a separator and a suffix (LICENSE.md, LICENSE-MIT, COPYING.LESSER).
LICENSE_NAME = re.compile(r'(?:LICEN[CS]E|COPYING|COPYRIGHT|NOTICE|UNLICENSE|PATENTS)(?:[._-][A-Za-z0-9._-]+)?',
                          re.IGNORECASE)
SOURCE_SUFFIXES = frozenset(('.go', '.s', '.c', '.cc', '.cpp', '.h', '.hpp', '.py', '.js', '.ts', '.json',
                             '.yml', '.yaml', '.toml', '.sh', '.mod', '.sum', '.html', '.tmpl', '.tpl',
                             '.proto', '.sql'))
MAX_LICENSE_FILE = 1 << 20
# The installer admits at most 4 MiB of non-executable members beside the executable.
MAX_LICENSE_TEXT = 3 << 20
RULE = b'=' * 80 + b'\n'
THIN_RULE = b'-' * 80 + b'\n'


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


def license_names(directory):
    """License-bearing regular files directly inside DIRECTORY."""
    with os.scandir(directory) as entries:
        return sorted(entry.name for entry in entries
                      if entry.is_file(follow_symlinks=False) and LICENSE_NAME.fullmatch(entry.name) and
                      Path(entry.name).suffix.lower() not in SOURCE_SUFFIXES)


def gather_license_files(root, directories):
    """License files in each of DIRECTORIES and in every ancestor up to ROOT: {path relative to ROOT: path}."""
    names, found = {}, {}
    for directory in directories:
        if directory != root and root not in directory.parents:
            fail(f'{directory} is outside {root}')
        current = directory
        while True:
            if current not in names:
                names[current] = license_names(current)
            for name in names[current]:
                found[(current / name).relative_to(root).as_posix()] = current / name
            if current == root:
                break
            current = current.parent
    return found


def read_license(path):
    """The exact text of one license file; empty, oversized or non-UTF-8 files are refused."""
    if not 0 < path.stat().st_size <= MAX_LICENSE_FILE:
        fail(f'{path} is empty or larger than {MAX_LICENSE_FILE} bytes')
    data = path.read_bytes()
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        fail(f'{path} is not UTF-8 text')
    if not text.strip() or '\0' in text:
        fail(f'{path} has no license text')
    return data if data.endswith(b'\n') else data + b'\n'


def linked_packages(rm, go, tree, env):
    """Every package `go list -deps` reports for the exact build settings (cgo, GOOS, GOARCH, tags)."""
    listing = run([str(go), 'list', '-deps', '-json=ImportPath,Dir,Standard,Module', rm.BUILD_PACKAGE],
                  cwd=tree, env=env)
    decoder, position, packages = json.JSONDecoder(), 0, []
    while True:
        while position < len(listing) and listing[position].isspace():
            position += 1
        if position == len(listing):
            return packages
        package, position = decoder.raw_decode(listing, position)
        packages.append(package)


def reported_modules(go, binary, env):
    """(path, version) of every module the executable's own build info lists as a dependency."""
    modules = set()
    for line in run([str(go), 'version', '-m', str(binary)], env=env).splitlines():
        fields = line.split('\t')
        if len(fields) >= 4 and fields[1] == 'dep':
            modules.add((fields[2], fields[3]))
    return modules


def license_sections(rm, go, goroot, tree, env, binary):
    """[(title, [(file, text)])] for the standard library, the main module's extra notices and each linked module."""
    standard, main, modules = set(), set(), {}
    for package in linked_packages(rm, go, tree, env):
        name, module = package['ImportPath'], package.get('Module')
        if not package.get('Dir'):
            fail(f'go list reports no directory for linked package {name}; the module cache is incomplete')
        directory = Path(package['Dir'])
        if package.get('Standard'):
            standard.add(directory)
        elif not module:
            fail(f'linked package {name} belongs to no Go module')
        elif module.get('Main'):
            main.add(directory)
        else:
            origin = module.get('Replace') or module
            if not origin.get('Dir'):
                fail(f'go list reports no directory for module {module["Path"]}; the module cache is incomplete')
            entry = modules.setdefault((module['Path'], module.get('Version', '')),
                                       {'root': Path(origin['Dir']), 'directories': set()})
            entry['directories'].add(directory)
    reported = reported_modules(go, binary, env)
    if reported != set(modules):
        fail('modules linked according to `go list` differ from the executable build info: '
             f'{sorted(reported ^ set(modules))}')
    sections = []
    found = gather_license_files(goroot, standard | {goroot})
    if 'LICENSE' not in found:
        fail(f'{goroot}/LICENSE is missing; the standard library license cannot be bundled')
    sections.append((f'Go standard library ({rm.GO_VERSION})', found))
    extra = {name: path for name, path in gather_license_files(tree, main | {tree}).items() if name != 'LICENSE'}
    if extra:
        sections.append((f'{rm.GO_MODULE} {rm.CPA_VERSION} (additional notices)', extra))
    for (path, version), entry in sorted(modules.items()):
        found = gather_license_files(entry['root'], entry['directories'] | {entry['root']})
        if not found:
            fail(f'{path} {version} is linked into the executable but ships no license or notice file; '
                 'its license text cannot be bundled')
        sections.append((f'{path} {version}', found))
    return [(title, [(name, read_license(path)) for name, path in sorted(files.items())])
            for title, files in sections]


def render_license(rm, original, sections):
    """The archive LICENSE: the unmodified upstream license, then every bundled third-party text."""
    out = [original if original.endswith(b'\n') else original + b'\n', b'\n', RULE,
           b'THIRD-PARTY LICENSES AND NOTICES\n', RULE, b'\n',
           (f'The cli-proxy-api executable in this archive is built from the CLIProxyAPI source covered by\n'
            f'the license above, together with the Go standard library and the Go modules listed below.\n'
            f'Each section reproduces, unmodified, every license, notice and patent-grant file that the\n'
            f'toolchain ({rm.GO_VERSION}) or module distributes for the packages linked into the executable.\n'
            f'\nContents:\n').encode()]
    out.extend(f'  {title}\n'.encode() for title, _ in sections)
    for title, files in sections:
        for name, text in files:
            out.extend((b'\n', THIN_RULE, f'{title}: {name}\n'.encode(), THIN_RULE, b'\n', text))
    rendered = b''.join(out)
    if len(rendered) > MAX_LICENSE_TEXT:
        fail(f'the bundled license text is {len(rendered)} bytes; the installer admits at most {MAX_LICENSE_TEXT}')
    return rendered


def build_archive(destination, binary, license_text, epoch):
    """Deterministic tar.gz: fixed order, owners, modes, mtimes, no gzip name or time."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.tmp')
    try:
        with open(temporary, 'wb') as raw, open(binary, 'rb') as executable:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=9, mtime=0) as packed:
                with tarfile.open(fileobj=packed, mode='w', format=tarfile.USTAR_FORMAT) as bundle:
                    for name, size, stream, mode in (
                            ('cli-proxy-api', binary.stat().st_size, executable, 0o755),
                            ('LICENSE', len(license_text), io.BytesIO(license_text), 0o644)):
                        info = tarfile.TarInfo(name)
                        info.size, info.mode, info.mtime = size, mode, epoch
                        info.uid = info.gid = 0
                        info.uname = info.gname = ''
                        bundle.addfile(info, stream)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def verify_archive(rm, destination, binary_sha256, license_text):
    with tarfile.open(destination, 'r:gz') as bundle:
        names = bundle.getnames()
        if names != [rm.BINARY_NAME, 'LICENSE']:
            fail(f'unexpected archive members {names}')
        if hashlib.sha256(bundle.extractfile(rm.BINARY_NAME).read()).hexdigest() != binary_sha256:
            fail('archive executable does not match the built binary')
        if bundle.extractfile('LICENSE').read() != license_text:
            fail('archive LICENSE does not match the generated license text')


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
            build_env = go_build(rm, go, goroot, tree, env, destination, args.jobs)
            binaries.append(destination)
        first, second = (sha256_file(path) for path in binaries)
        if first != second:
            fail(f'build is not reproducible: {first} != {second}')
        glibc_max = inspect_binary(rm, binaries[0])
        upstream_license = tree / 'LICENSE'
        if not upstream_license.is_file():
            fail('the pinned CPA source has no LICENSE file; the archive could not carry its attribution')
        license_text = render_license(rm, read_license(upstream_license),
                                      license_sections(rm, go, goroot, tree, build_env, binaries[0]))
        archive_path = args.output_dir.resolve() / rm.ARCHIVE_NAME
        build_archive(archive_path, binaries[0], license_text, epoch)
        verify_archive(rm, archive_path, first, license_text)
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
