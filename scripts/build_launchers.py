#!/usr/bin/env python3
"""Bundle the shared trusted console bridge for the two Hermes plugin loaders."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'hermes-cpa'
TARGETS = (
    ('desktop/src/launcher.js', 'desktop/plugin.js', 'esm',
     ('@hermes/plugin-sdk', 'react', 'react/jsx-runtime')),
    ('dashboard/src/launcher.js', 'dashboard/index.js', 'iife', ()),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bun', default=os.environ.get('BUN') or shutil.which('bun'))
    args = parser.parse_args()
    if not args.bun:
        parser.error('Bun is required; supply --bun or set BUN.')
    version = subprocess.check_output([args.bun, '--version'], text=True).strip()
    sources = {}
    for directory in ('console', 'desktop/src', 'dashboard/src'):
        for path in sorted((PACKAGE / directory).rglob('*.js')):
            if path.is_symlink():
                raise ValueError('Launcher source must not be a symlink.')
            sources[str(path.relative_to(PACKAGE))] = hashlib.sha256(path.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='cpa-launchers-') as temporary:
        artifacts = {}
        staged = []
        for entry, output, form, externals in TARGETS:
            source = PACKAGE / entry
            if not source.is_file() or source.is_symlink():
                raise ValueError('Missing or unsafe launcher entry: ' + entry)
            target = Path(temporary) / output
            target.parent.mkdir(parents=True, exist_ok=True)
            command = [args.bun, 'build', str(source), '--target=browser',
                       '--format=' + form, '--minify', '--outfile=' + str(target)]
            command.extend('--external=' + name for name in externals)
            subprocess.run(command, cwd=ROOT, check=True)
            content = target.read_bytes()
            if not content or len(content) > 1024 * 1024:
                raise ValueError('Launcher bundle exceeds its 1 MiB limit: ' + output)
            artifacts[output] = {'sha256': hashlib.sha256(content).hexdigest(),
                                 'size_bytes': len(content)}
            staged.append((PACKAGE / output, content))
        manifest = {'protocol': 3, 'build': 'Bun ' + version,
                    'artifacts': artifacts, 'source_hashes': sources}
        for path, content in staged:
            path.write_bytes(content)
        (PACKAGE / 'assets/launchers.manifest.json').write_text(
            json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'artifacts': artifacts, 'source_files': len(sources)}))


if __name__ == '__main__':
    main()
