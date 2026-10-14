#!/usr/bin/env python3
"""Export the Hermes owner-integration patch against the pinned CLIProxyAPI base commit.

The patch is the diff between the working tree of the local CPA source checkout and
the pinned base commit (new files included, .gitignore honoured). A temporary index
is used, so the checkout's own index, refs and worktree are never modified; only
loose blob objects may be written to its object store. Binary files, symlinks,
submodules, renames and mode changes are refused: the patch stays reviewable text.

Writes runtime-integration/patches/0001-hermes-owner-v1.patch and
runtime-integration/provenance.json (base commit, patch digest, per-file digests).
With --check nothing is written; the command fails if either would change.
"""
import argparse
import json
import tempfile
from pathlib import Path, PurePosixPath

from cpa_runtime_common import (DEFAULT_SOURCE, PATCH, PROVENANCE, ROOT, atomic_write, fail, git,
                                load_runtime_manifest_module, sha256_bytes, verify_base)

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PATCH_BYTES = 64 * 1024 * 1024
FILE_MODES = {'100644', '100755'}


def changed_files(source, index, base):
    env = {'GIT_INDEX_FILE': str(index)}
    git(source, 'read-tree', base, env=env)
    git(source, 'add', '-A', '--', '.', env=env)
    raw = git(source, 'diff', '--cached', '--raw', '-z', '--no-renames', '--full-index', base,
              env=env).stdout.split(b'\0')
    files = []
    for meta, name in zip(raw[0::2], raw[1::2]):
        old_mode, new_mode, old_blob, new_blob, status = meta.decode().lstrip(':').split()
        path = name.decode()
        parts = PurePosixPath(path).parts
        if (status not in ('A', 'M', 'D') or path.startswith('/') or '..' in parts or
                (parts and parts[0] == '.git') or
                {old_mode, new_mode} - FILE_MODES - {'000000'} or
                (status == 'M' and old_mode != new_mode)):
            fail(f'unsupported change ({status} {old_mode}->{new_mode}) at {path}: only added, '
                 'modified or deleted regular files without mode changes are exported')
        record = {'path': path, 'status': {'A': 'added', 'M': 'modified', 'D': 'deleted'}[status],
                  'mode': new_mode if status != 'D' else old_mode,
                  'before_sha256': None, 'after_sha256': None}
        for key, blob in (('before_sha256', old_blob), ('after_sha256', new_blob)):
            if set(blob) == {'0'}:
                continue
            content = git(source, 'cat-file', 'blob', blob).stdout
            if b'\0' in content or len(content) > MAX_FILE_BYTES:
                fail(f'{path} is binary or larger than {MAX_FILE_BYTES} bytes; not exportable')
            record[key] = sha256_bytes(content)
        files.append(record)
    return sorted(files, key=lambda row: row['path']), env


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE,
                        help='CPA git checkout holding the integration changes')
    parser.add_argument('--check', action='store_true',
                        help='fail instead of writing when the committed export is stale')
    args = parser.parse_args()
    rm = load_runtime_manifest_module()
    source = args.source.resolve()
    verify_base(source, rm)
    with tempfile.TemporaryDirectory(prefix='cpa-export-') as scratch:
        files, env = changed_files(source, Path(scratch) / 'index', rm.UPSTREAM_COMMIT)
        if not files:
            fail('no integration changes found against the pinned base commit')
        patch = git(source, 'diff', '--cached', '--full-index', '--no-renames', '--no-color',
                    '--no-ext-diff', '--src-prefix=a/', '--dst-prefix=b/', rm.UPSTREAM_COMMIT,
                    env=env).stdout
    if not patch or len(patch) > MAX_PATCH_BYTES or b'GIT binary patch' in patch:
        fail('exported patch is empty, oversized or binary')
    provenance = json.dumps({
        'schema': 1, 'integration': rm.INTEGRATION, 'upstream_repository': rm.UPSTREAM_REPOSITORY,
        'upstream_tag': rm.UPSTREAM_TAG, 'base_commit': rm.UPSTREAM_COMMIT,
        'patch_path': PATCH.relative_to(ROOT).as_posix(), 'patch_sha256': sha256_bytes(patch),
        'patch_size': len(patch), 'files': files}, indent=2).encode() + b'\n'
    if args.check:
        current = (PATCH.read_bytes() if PATCH.is_file() else None,
                   PROVENANCE.read_bytes() if PROVENANCE.is_file() else None)
        if current != (patch, provenance):
            fail('committed runtime-integration export is stale; rerun without --check')
    else:
        atomic_write(PATCH, patch)
        atomic_write(PROVENANCE, provenance)
    print(json.dumps({'changed_files': len(files), 'patch_sha256': sha256_bytes(patch),
                      'patch_size': len(patch), 'written': not args.check}))


if __name__ == '__main__':
    main()
