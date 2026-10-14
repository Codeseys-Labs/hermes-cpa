#!/usr/bin/env python3
"""Build the generated native resource plugin with the pinned CPA runtime settings.

Same toolchain, platform and flags as scripts/build_cpa_runtime.py: the exact
pinned Go release, GOOS=linux GOARCH=amd64 CGO_ENABLED=1, -trimpath,
-buildvcs=false, -mod=readonly, GOTOOLCHAIN=local, GOFLAGS empty, GOWORK=off,
C locale, UTC, an empty private build cache per attempt. The only differences
are -buildmode=c-shared (a plugin, not a server) and the per-variant -X flags.
Every variant is built twice with separate empty caches and must be
byte-identical. The plugin has no module dependencies, so no module cache or
network is used (GOPROXY=off).

Output (written only on success):
  <output>/hermes-resource-fixture_<version>_linux_amd64.so  one per variant
  <output>/fixture-manifest.json  digests, sizes, toolchain, source digest

Run where gcc, libc headers, readelf and the Go toolchain exist, i.e. the
Dockerfile.runtime toolchain image (see Dockerfile.fixture).
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / 'scripts'))
import fixture_common as fx  # noqa: E402
from cpa_runtime_common import fail, load_runtime_manifest_module  # noqa: E402

EXPORTS = (b'cliproxy_plugin_init', b'cliproxyPluginCall', b'cliproxyPluginFree', b'cliproxyPluginShutdown')
DEFAULT_GO_ROOT = HERE.parents[1] / '.verification/toolchains/go1.26.9'


def run(command, **kwargs):
    done = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip()[-4000:]
        fail(f'{Path(command[0]).name} {command[1] if len(command) > 1 else ""} failed:\n{tail}')
    return done.stdout


def locate_go(root):
    for candidate in (root, root / 'go'):
        if (candidate / 'bin/go').is_file():
            return candidate.resolve()
    fail(f'pinned Go toolchain not found under {root}')


def build_one(goroot, source, work, attempt, version, generation, jobs):
    env = {'HOME': str(work / 'home'), 'LANG': 'C', 'LC_ALL': 'C', 'TZ': 'UTC', 'GOWORK': 'off',
           'GOFLAGS': '', 'GOMAXPROCS': str(jobs), 'GOTOOLCHAIN': 'local', 'GOOS': fx.GOOS,
           'GOARCH': fx.GOARCH, 'CGO_ENABLED': '1', 'GOCACHE': str(work / f'gocache{attempt}'),
           'GOMODCACHE': str(work / 'modcache'), 'GOPATH': str(work / 'gopath'), 'GOPROXY': 'off',
           'GOROOT': str(goroot), 'PATH': f'{goroot}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}
    destination = work / f'{version}.{attempt}.so'
    run([str(goroot / 'bin/go'), 'build', f'-p={jobs}', '-trimpath', '-buildvcs=false', '-mod=readonly',
         '-buildmode=c-shared', '-ldflags', f'-X main.generation={generation} -X main.pluginVersion={version}',
         '-o', str(destination), '.'], cwd=source, env=env)
    return destination


def inspect(rm, path):
    """Require a glibc-only x86-64 shared object exporting the plugin ABI."""
    data = path.read_bytes()
    if data[:4] != b'\x7fELF' or data[4] != 2 or data[5] != 1 or int.from_bytes(data[16:18], 'little') != 3 \
            or int.from_bytes(data[18:20], 'little') != 0x3E:
        fail(f'{path.name} is not a little-endian x86-64 ELF shared object')
    needed = set(re.findall(r'\(NEEDED\)\s+Shared library: \[([^\]]+)\]', run(['readelf', '-d', str(path)])))
    if needed - rm.ALLOWED_NEEDED:
        fail(f'unexpected dynamic libraries {sorted(needed - rm.ALLOWED_NEEDED)}')
    symbols = run(['readelf', '--dyn-syms', '--wide', str(path)]).encode()
    for name in EXPORTS:
        if not re.search(rb'\b' + re.escape(name) + rb'\b', symbols):
            fail(f'{path.name} does not export {name.decode()}')
    versions = {tuple(int(part) for part in text.split('.')) for text in
                re.findall(r'Name: GLIBC_(\d+(?:\.\d+)*)', run(['readelf', '--version-info', str(path)]))}
    return '.'.join(map(str, max(versions))) if versions else None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--go-root', type=Path, default=DEFAULT_GO_ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 64:
        parser.error('--jobs must be between 1 and 64')
    if sys.platform != 'linux' or os.uname().machine not in ('x86_64', 'amd64'):
        fail('the fixture must be built on linux/amd64 (use the pinned toolchain image)')
    for tool in ('gcc', 'readelf'):
        if shutil.which(tool) is None:
            fail(f'{tool} is required (cgo c-shared build)')
    rm = load_runtime_manifest_module()
    goroot = locate_go(args.go_root.resolve())
    reported = run([str(goroot / 'bin/go'), 'version'],
                   env={'GOTOOLCHAIN': 'local', 'GOFLAGS': '', 'GOROOT': str(goroot),
                        'PATH': '/usr/bin:/bin', 'HOME': '/tmp'}).strip()
    if reported != f'go version {rm.GO_VERSION} linux/amd64':
        fail(f'toolchain reports {reported!r}; exactly "go version {rm.GO_VERSION} linux/amd64" is required')
    digest = fx.source_digest()
    rows, glibc = [], None
    with tempfile.TemporaryDirectory(prefix='native-resource-fixture-') as scratch:
        work = Path(scratch)
        (work / 'home').mkdir()
        source = work / 'src'
        source.mkdir()
        (source / 'assets').mkdir()
        for path in fx.source_files():
            shutil.copyfile(path, source / path.relative_to(fx.SOURCE_DIR))
        for version, generation in fx.VARIANTS:
            built = []
            for attempt in (1, 2):
                print(f'building {fx.PLUGIN_ID} {version} ({attempt}/2, empty Go build cache)...', file=sys.stderr)
                built.append(build_one(goroot, source, work, attempt, version, generation, args.jobs))
            first, second = (fx.sha256(path.read_bytes()) for path in built)
            if first != second:
                fail(f'build of {version} is not reproducible: {first} != {second}')
            glibc = inspect(rm, built[0])
            library = built[0].read_bytes()
            name = fx.library_name(version)
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / name).write_bytes(library)
            rows.append({'version': version, 'generation': generation, 'file': name,
                         'sha256': first, 'size': len(library)})
        gcc = run(['gcc', '--version']).splitlines()[0].strip()
        libc = os.confstr('CS_GNU_LIBC_VERSION')
    manifest = {'schema': 1, 'plugin_id': fx.PLUGIN_ID, 'go_version': rm.GO_VERSION,
                'go_toolchain_sha256': rm.GO_TOOLCHAIN_SHA256, 'cgo': True, 'trimpath': True,
                'buildmode': 'c-shared', 'goos': fx.GOOS, 'goarch': fx.GOARCH, 'gcc_version': gcc,
                'libc_version': libc, 'glibc_max': glibc, 'reproducible': True,
                'source_sha256': digest, 'variants': rows}
    (args.output / fx.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'output': str(args.output), 'variants': rows, 'source_sha256': digest,
                      'go': rm.GO_VERSION}, indent=2))


if __name__ == '__main__':
    main()
