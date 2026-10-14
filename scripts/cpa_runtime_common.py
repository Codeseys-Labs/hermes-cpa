"""Shared helpers for the pinned CPA runtime scripts (stdlib only).

``export_cpa_patch.py`` and ``build_cpa_runtime.py`` agree on one set of constants
and validation rules by loading ``hermes-cpa/src/hermes_cpa/runtime_manifest.py``
by path: importing the ``hermes_cpa`` package would pull in the whole plugin.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'hermes-cpa'
MANIFEST = PACKAGE / 'cpa-runtime.json'
COMPATIBILITY = PACKAGE / 'compatibility.json'
RUNTIME_DIR = ROOT / 'runtime-integration'
PATCH = RUNTIME_DIR / 'patches/0001-hermes-owner-v1.patch'
PROVENANCE = RUNTIME_DIR / 'provenance.json'
DEFAULT_SOURCE = ROOT / '.verification/cpa-source'


def fail(message):
    raise SystemExit(f'error: {message}')


def load_runtime_manifest_module():
    spec = importlib.util.spec_from_file_location(
        'hermes_cpa_runtime_manifest', PACKAGE / 'src/hermes_cpa/runtime_manifest.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(source, *args, env=None, check=True):
    """Run git against SOURCE without taking any index/ref lock or prompting."""
    done = subprocess.run(
        ['git', '-c', f'safe.directory={source}', '-C', str(source), *args], capture_output=True,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0', **(env or {})})
    if check and done.returncode != 0:
        fail(f'git {args[0]} failed: {done.stderr.decode(errors="replace").strip()}')
    return done


def verify_base(source, rm):
    """Require the pinned base commit (and tag, when present) in SOURCE; return its epoch."""
    if not Path(source).is_dir():
        fail(f'CPA source repository not found: {source}')
    commit = git(source, 'rev-parse', '--verify', '--quiet', rm.UPSTREAM_COMMIT + '^{commit}',
                 check=False)
    if commit.returncode != 0 or commit.stdout.decode().strip() != rm.UPSTREAM_COMMIT:
        fail(f'{source} does not contain pinned base commit {rm.UPSTREAM_COMMIT}')
    tag = git(source, 'rev-parse', '--verify', '--quiet', f'refs/tags/{rm.UPSTREAM_TAG}^{{commit}}',
              check=False)
    if tag.returncode == 0 and tag.stdout.decode().strip() != rm.UPSTREAM_COMMIT:
        fail(f'tag {rm.UPSTREAM_TAG} in {source} does not point at {rm.UPSTREAM_COMMIT}')
    epoch = int(git(source, 'show', '-s', '--format=%ct', rm.UPSTREAM_COMMIT).stdout.decode())
    stamp = datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if stamp != rm.UPSTREAM_COMMIT_DATE:
        fail(f'base commit date {stamp} differs from pinned {rm.UPSTREAM_COMMIT_DATE}')
    return epoch


def read_provenance(rm):
    """Validated exported-patch provenance; the patch bytes must match their recorded digest."""
    try:
        provenance = json.loads(PROVENANCE.read_bytes())
        patch = PATCH.read_bytes()
    except (OSError, ValueError):
        fail('runtime-integration/provenance.json or its patch is missing; run '
             'scripts/export_cpa_patch.py first')
    files = provenance.get('files') if isinstance(provenance, dict) else None
    if (not isinstance(files, list) or not files or
            provenance.get('schema') != 1 or provenance.get('integration') != rm.INTEGRATION or
            provenance.get('upstream_repository') != rm.UPSTREAM_REPOSITORY or
            provenance.get('upstream_tag') != rm.UPSTREAM_TAG or
            provenance.get('base_commit') != rm.UPSTREAM_COMMIT or
            provenance.get('patch_sha256') != sha256_bytes(patch) or
            provenance.get('patch_size') != len(patch)):
        fail('runtime-integration provenance does not match its patch or the pinned base; '
             're-export with scripts/export_cpa_patch.py')
    return provenance, patch


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.stage-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
