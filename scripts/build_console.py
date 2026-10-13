#!/usr/bin/env python3
"""Build the paired CPAMC guest and record the exact shipped source provenance."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / 'upstream-console'
PACKAGE = ROOT / 'hermes-cpa'
EXCLUDED = {'node_modules', 'dist', 'dist-hermes', 'dist-reference', '.git', 'coverage'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bun', default=os.environ.get('BUN') or shutil.which('bun'))
    args = parser.parse_args()
    if not args.bun:
        parser.error('Bun is required; supply --bun or set BUN.')
    env = {**os.environ, 'VERSION': '1.25.6'}
    subprocess.run([args.bun, 'run', 'build'], cwd=CONSOLE, env=env, check=True)
    content = (CONSOLE / 'dist/index.html').read_bytes()
    if len(base64.b64encode(content)) + len('data:text/html;charset=utf-8;base64,') >= 2 * 1024 * 1024:
        raise RuntimeError('Console exceeds the supported data-URL delivery size; no asset published.')
    digest = hashlib.sha256(content).hexdigest()
    sources = {}
    published = subprocess.check_output([
        'git', '-C', str(ROOT), 'ls-files', '--cached', '--others',
        '--exclude-standard', '-z', '--', 'upstream-console',
    ]).decode().split('\0')
    for name in sorted(set(published) - {''}):
        path = ROOT / name
        relative = path.relative_to(CONSOLE)
        if not path.is_file() or path.is_symlink() or any(part in EXCLUDED for part in relative.parts):
            continue
        sources[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    asset_manifest_path = PACKAGE / 'assets/cpamc.manifest.json'
    manifest = json.loads(asset_manifest_path.read_text())
    manifest.update(
        adaptation_version='hermes.4-docked-consent', sha256=digest,
        size_bytes=len(content), source_hashes=sources,
        build=f"Bun {subprocess.check_output([args.bun, '--version'], text=True).strip()}; "
              'VERSION=1.25.6 bun run build; verification recorded separately',
        scope='docked console with bounded native consent; full mutation parity remains deferred',
    )
    compatibility_path = PACKAGE / 'compatibility.json'
    compatibility = json.loads(compatibility_path.read_text())
    pair = next(pair for pair in compatibility['supported_pairs']
                if pair['cpa_version'] == '8.0.21' and pair['console_version'] == '1.25.6'
                and pair['platform'] == 'linux_amd64')
    pair['console_sha256'] = digest
    (PACKAGE / 'assets/cpamc.html').write_bytes(content)
    asset_manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    compatibility_path.write_text(json.dumps(compatibility, indent=2) + '\n')
    print(json.dumps({'asset_sha256': digest, 'size_bytes': len(content),
                      'source_files': len(sources), 'release_flags_unchanged': True}))


if __name__ == '__main__':
    main()
