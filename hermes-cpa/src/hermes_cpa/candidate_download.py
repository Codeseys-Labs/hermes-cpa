"""Fetch the fixed private candidate runtime asset through the authenticated GitHub CLI.

The repository, tag and asset name come from ``runtime_manifest`` constants, never
from the caller or the environment. Authentication belongs to ``gh``: this module
never reads, prints or forwards a token, and never falls back to another source.
The asset streams into memory only, bounded by the pinned size, and must match the
pinned size and sha256. Nothing is written to disk.
"""
import hashlib
import os
import select
import shutil
import subprocess
import time

from .runtime_manifest import ARCHIVE_NAME, CANDIDATE_HOST, CANDIDATE_REPOSITORY, CANDIDATE_TAG

# Settings that could redirect gh to another repository/host or make it log requests.
_DROPPED = ('GH_REPO', 'GH_DEBUG', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_PATH',
            'GH_FORCE_TTY', 'GH_BROWSER', 'GH_PAGER', 'GH_EDITOR')


class AcquisitionError(ValueError):
    """str() is the stable code: gh_unavailable, gh_unauthenticated,
    candidate_unavailable or candidate_mismatch."""


def _environment():
    env = {name: value for name, value in os.environ.items() if name not in _DROPPED}
    env.update(GH_HOST=CANDIDATE_HOST, GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1',
               GH_NO_EXTENSION_UPDATE_NOTIFIER='1', GH_SPINNER_DISABLED='1', NO_COLOR='1')
    return env


def require_candidate_access(timeout=30.):
    """Return (gh path, environment) once gh exists and is logged in; no side effects."""
    gh = shutil.which('gh')
    if gh is None:
        raise AcquisitionError('gh_unavailable')
    env = _environment()
    try:
        status = subprocess.run([gh, 'auth', 'status', '--hostname', CANDIDATE_HOST], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AcquisitionError('gh_unauthenticated') from None
    except OSError:
        raise AcquisitionError('gh_unavailable') from None
    if status.returncode != 0:
        raise AcquisitionError('gh_unauthenticated')
    return gh, env


def download_candidate(artifact, timeout=300.):
    """Return the verified archive bytes for the pinned ``RuntimeArtifact``."""
    gh, env = require_candidate_access()
    command = [gh, 'release', 'download', CANDIDATE_TAG, '--repo',
               f'{CANDIDATE_HOST}/{CANDIDATE_REPOSITORY}', '--pattern', ARCHIVE_NAME, '--output', '-']
    deadline = time.monotonic() + timeout
    data, digest = bytearray(), hashlib.sha256()
    try:
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise AcquisitionError('gh_unavailable') from None
    try:
        descriptor = process.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcquisitionError('candidate_unavailable')
            if not select.select([descriptor], [], [], min(remaining, 1.))[0]:
                continue
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            data.extend(chunk)
            digest.update(chunk)
            if len(data) > artifact.archive_size:
                raise AcquisitionError('candidate_mismatch')
        try:
            code = process.wait(timeout=max(1., deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AcquisitionError('candidate_unavailable') from None
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        process.wait()
    if code != 0:
        raise AcquisitionError('candidate_unavailable')
    if len(data) != artifact.archive_size or digest.hexdigest() != artifact.archive_sha256:
        raise AcquisitionError('candidate_mismatch')
    return bytes(data)
