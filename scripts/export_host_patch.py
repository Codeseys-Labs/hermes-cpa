#!/usr/bin/env python3
"""Export declared host integration files against the pinned Hermes Git revision."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / 'host-integration/provenance.json'
PATCH = ROOT / 'host-integration/patches/0001-scoped-cpa-host-contract.patch'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=Path, help='Modified qualification host source')
    parser.add_argument('--base-repo', required=True, type=Path, help='Git checkout containing the pinned base')
    parser.add_argument('--add-path', action='append', default=[])
    args = parser.parse_args()
    manifest = json.loads(PROVENANCE.read_text())
    base = manifest['hermes_base_commit']
    raw = subprocess.check_output(['git', '-C', str(args.base_repo), 'ls-tree', '-r', '-z', base])
    tree = {}
    for row in raw.split(b'\0'):
        if row:
            metadata, name = row.split(b'\t', 1)
            mode, _, digest = metadata.decode().split()
            tree[name.decode()] = mode, digest
    paths = sorted({row['path'] for row in manifest['files']} | set(args.add_path))
    chunks, records = [], []
    host = args.host.resolve()
    for relative in paths:
        path = host / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(host):
            raise ValueError('Declared host integration source is missing or unsafe: ' + relative)
        content = path.read_bytes()
        digest = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        old = tree.get(relative)
        if old and old[1] == digest:
            continue
        before = subprocess.check_output(['git', '-C', str(args.base_repo), 'show', base + ':' + relative]) if old else b''
        chunks.append(f'diff --git a/{relative} b/{relative}\n')
        if old is None:
            chunks.append('new file mode 100644\n')
        chunks.append('index ' + (old[1][:7] if old else '0000000') + '..' + digest[:7] +
                      (' ' + old[0] if old else '') + '\n')
        chunks.extend(difflib.unified_diff(before.decode().splitlines(True), content.decode().splitlines(True),
                                         fromfile='a/' + relative if old else '/dev/null',
                                         tofile='b/' + relative))
        records.append({'path': relative,
                        'before_sha256': hashlib.sha256(before).hexdigest() if old else None,
                        'after_sha256': hashlib.sha256(content).hexdigest()})
    patch = ''.join(chunks).encode()
    PATCH.write_bytes(patch)
    manifest.update(host_contract_version=1, files=records, patch_sha256=hashlib.sha256(patch).hexdigest())
    PROVENANCE.write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'changed_files': len(records), 'patch_sha256': manifest['patch_sha256']}))


if __name__ == '__main__':
    main()
