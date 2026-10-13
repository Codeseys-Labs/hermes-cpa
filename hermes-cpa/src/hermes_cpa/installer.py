"""Compatibility gate, pinned acquisition and retain-data lifecycle operations.

Packaged pins are trusted inputs. The only network fetch is the exact pinned
upstream release URL below, after explicit setup consent; no latest/caller URL,
boot service, provider settings rewrite, OAuth-store copy or recursive removal.
"""
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .process_owner import require_platform, private_directory, private_open
from .config_store import ConfigStore

EXPECTED_TAR_SHA = 'a40164e25a60869df2c489702383d4e64dc27bb08eaafd7b04a101a2f5cfc625'
EXPECTED_BIN_SHA = '95435293c56e856db7d70b377d30b2b2c00e32ff1c06c41ae46758a9f6febdee'
MAX_ARCHIVE = 100 * 1024 * 1024
# Upstream release metadata for the single supported pair (router-for-me/CLIProxyAPI
# tag v8.0.21). Integrity comes from EXPECTED_TAR_SHA, never from the transport.
UPSTREAM_ARCHIVE_URL = ('https://github.com/router-for-me/CLIProxyAPI/releases/download/'
                        'v8.0.21/CLIProxyAPI_8.0.21_linux_amd64.tar.gz')
UPSTREAM_ARCHIVE_SIZE = 23119655


def download_pinned_archive(timeout=120.):
    """Fetch the fixed upstream archive into memory and verify size and digest.

    Redirects may only stay on HTTPS. Proxy/netrc environment is ignored so no
    ambient credential is attached. Nothing is written to disk here.
    """
    import httpx

    def https_only(request):
        if request.url.scheme != 'https':
            raise ValueError('download_insecure_redirect')

    data = bytearray()
    try:
        with httpx.Client(trust_env=False, follow_redirects=True, max_redirects=5,
                          timeout=timeout, event_hooks={'request': [https_only]}) as client:
            with client.stream('GET', UPSTREAM_ARCHIVE_URL) as response:
                if response.status_code != 200:
                    raise ValueError('download_failed')
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > UPSTREAM_ARCHIVE_SIZE:
                        raise ValueError('download_size_mismatch')
    except ValueError:
        raise
    except Exception:
        raise ValueError('download_failed') from None
    if len(data) != UPSTREAM_ARCHIVE_SIZE:
        raise ValueError('download_size_mismatch')
    if hashlib.sha256(data).hexdigest() != EXPECTED_TAR_SHA:
        raise ValueError('Archive SHA mismatch')
    return bytes(data)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bounded_file(path, limit):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('unsafe_file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('unsafe_file')
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError('file_too_large')
    return content


def atomic_bytes(path, content, mode=0o700):
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError('unsafe_target')
    fd, name = tempfile.mkstemp(prefix='.stage-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), mode)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        # Initial immutable publication: a concurrent install must not replace
        # somebody else's executable after the existence check above.
        os.link(name, path)
        Path(name).unlink()
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


class CpaInstaller:
    def __init__(self, settings, package_root=None, *, isolated_authorization=None):
        self.settings = settings
        self.package_root = Path(package_root or Path(__file__).resolve().parents[2])
        self.isolated_authorization = isolated_authorization

    def _authorize_isolated_candidate(self):
        """Explicit backend-only, home/package/digest-bound disposable test permit.

        No implicit file discovery and no renderer path input. This never edits
        release metadata or relaxes executable/console/license verification.
        """
        import time
        try:
            path = Path(self.isolated_authorization)
            raw = bounded_file(path, 16384)
            with os.fdopen(private_open(path), 'rb') as stream:
                if stream.read(16385) != raw:
                    raise ValueError('changed_authorization')
            record = json.loads(raw)
            if (type(record.get('version')) is not int or record['version'] != 1 or
                    record.get('purpose') != 'isolated-candidate-test' or
                    record.get('home') != str(self.settings.home.resolve()) or
                    record.get('package_root') != str(self.package_root.resolve()) or
                    record.get('manifest_sha256') != digest(self.package_root / 'compatibility.json') or
                    type(record.get('expires_at')) not in (int, float) or
                    not time.time() < record['expires_at'] <= time.time() + 86400):
                raise ValueError('invalid_authorization')
        except Exception as exc:
            raise ValueError('isolated_authorization_invalid') from exc

    def verify_release(self):
        """Release gate, pair, console and notices without an executable.

        Setup calls this before any download or write so an unaccepted or
        mismatched package refuses with the home unchanged.
        """
        return self._verify_content(None)

    def _verify_content(self, content):
        require_platform()
        manifest = json.loads(bounded_file(self.package_root / 'compatibility.json', 65536))
        if not isinstance(manifest, dict):
            raise ValueError('invalid_manifest')
        # Artifact compatibility is not release approval. Missing metadata and
        # a Proposed ADR fail closed, including initial offline installation.
        if (manifest.get('candidate_status') != 'accepted' or
                manifest.get('adr_status') != 'Accepted' or
                not isinstance(manifest.get('tested_minimum_hermes'), str) or
                not re.fullmatch(r'\d+\.\d+\.\d+', manifest['tested_minimum_hermes'])):
            if self.isolated_authorization is None:
                raise ValueError('unaccepted_release')
            self._authorize_isolated_candidate()
        pairs = manifest.get('supported_pairs', [])
        if not isinstance(pairs, list) or any(not isinstance(p, dict) for p in pairs):
            raise ValueError('invalid_manifest')
        pair = next((p for p in pairs if p.get('cpa_version') == '8.0.21' and
                     p.get('console_version') == '1.25.6' and p.get('platform') == 'linux_amd64'), None)
        if pair is None:
            raise ValueError('unsupported_pair')
        pinned_archive = pair.get('archive_sha256')
        if pinned_archive is not None and pinned_archive != EXPECTED_TAR_SHA:
            raise ValueError('archive_pin_mismatch')
        if content is not None and hashlib.sha256(content).hexdigest() != pair['binary_sha256']:
            raise ValueError('binary_mismatch')
        asset = self.package_root / 'assets/cpamc.html'
        # Full adapted-source provenance is 71KB for the pinned console. This
        # offline manifest is not a guest message; keep the 1MiB input bound.
        asset_manifest = json.loads(bounded_file(self.package_root / 'assets/cpamc.manifest.json', 1024 * 1024))
        if not isinstance(asset_manifest, dict):
            raise ValueError('invalid_manifest')
        if (hashlib.sha256(bounded_file(asset, 8 * 1024 * 1024)).hexdigest() != pair.get('console_sha256') or
                asset_manifest.get('sha256') != pair.get('console_sha256') or
                asset_manifest.get('version') != pair['console_version'] or
                asset_manifest.get('cpa_version') != pair['cpa_version']):
            raise ValueError('console_mismatch')
        notices = manifest.get('notice_sha256', {})
        if not isinstance(notices, dict):
            raise ValueError('invalid_manifest')
        for name, expected in notices.items():
            if (name not in ('LICENSE', 'THIRD_PARTY_NOTICES.md') or
                    hashlib.sha256(bounded_file(self.package_root / name, 1024 * 1024)).hexdigest() != expected):
                raise ValueError('notice_mismatch')
        if set(manifest.get('notice_sha256', {})) != {'LICENSE', 'THIRD_PARTY_NOTICES.md'}:
            raise ValueError('notice_missing')
        return True

    def verify_pair(self, *, binary=None):
        require_platform()
        binary = Path(binary or self.settings.binary_path)
        if any(p.is_symlink() for p in (binary, *binary.parents)):
            raise ValueError('unsafe_binary')
        fd = private_open(binary, os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not mode & stat.S_IXUSR or mode & 0o7000:
                raise ValueError('binary_mismatch')
            content = stream.read(MAX_ARCHIVE + 1)
        return self._verify_content(content)

    def _archive_binary(self, archive_path):
        require_platform()
        return self.archive_binary_bytes(bounded_file(archive_path, MAX_ARCHIVE))

    def archive_binary_bytes(self, raw):
        """Extract the pinned executable from already-read archive bytes."""
        require_platform()
        if len(raw) > MAX_ARCHIVE:
            raise ValueError('file_too_large')
        if hashlib.sha256(raw).hexdigest() != EXPECTED_TAR_SHA:
            raise ValueError('Archive SHA mismatch')
        # Parse the exact bytes hashed, not a path reopened after verification.
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            seen, files, binary, expanded = set(), set(), None, 0
            for index, member in enumerate(archive):
                path = PurePosixPath(member.name)
                if (index >= 10000 or path.is_absolute() or '..' in path.parts or
                    '\\' in member.name or str(path) in seen or not path.parts or
                    not (member.isfile() or member.isdir()) or member.mode & 0o7000):
                    raise ValueError('unsafe_archive_member')
                if (any(str(parent) in files for parent in path.parents) or
                        (member.isfile() and any(name.startswith(str(path) + '/') for name in seen))):
                    raise ValueError('unsafe_archive_member')
                seen.add(str(path))
                if member.isfile():
                    files.add(str(path))
                expanded += member.size
                if expanded > MAX_ARCHIVE:
                    raise ValueError('archive_too_large')
                if member.name == 'cli-proxy-api' and member.isfile():
                    binary = archive.extractfile(member).read(MAX_ARCHIVE + 1)
            if binary is None or hashlib.sha256(binary).hexdigest() != EXPECTED_BIN_SHA:
                raise ValueError('binary_mismatch')
            return binary

    def install_from_archive(self, archive_path):
        """Initial offline install only; replacing an existing executable requires owner gate."""
        return self.install_binary(self._archive_binary(archive_path))

    def install_binary(self, content):
        """Publish verified executable bytes once; never replace an existing one."""
        self._verify_content(content)
        if self.settings.binary_path.exists() or self.settings.binary_path.is_symlink():
            raise ValueError('upgrade_required')
        atomic_bytes(self.settings.binary_path, content)
        return True

    def _result(self, owner, code, **extra):
        try:
            revision = ConfigStore(self.settings.config_path).compute_digest()
        except Exception:
            revision = ''
        return dict(ok=code == 'OK', code=code, revision=revision,
                    generation=owner.generation, rollback='not_attempted', **extra)

    def _precondition(self, owner, generation, revision, consent):
        # Caller holds the SAME gate used by backend config transactions.
        if owner.mode != 'managed':
            return 'READ_ONLY'
        if owner.policy_locked:
            return 'POLICY_LOCKED'
        if type(generation) is not int or generation < 1 or generation != owner.generation:
            return 'STALE_GENERATION'
        if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{64}', revision):
            return 'STALE_REVISION'
        try:
            if ConfigStore(self.settings.config_path).compute_digest() != revision:
                return 'STALE_REVISION'
        except Exception:
            return 'INVALID_INPUT'
        if consent is not True:
            return 'CONSENT_REQUIRED'
        try:
            owner.assert_owned()
        except Exception:
            return 'OWNERSHIP_LOST'
        return None

    def upgrade(self, archive_path, owner, *, expected_generation,
                expected_revision, native_consent=False):
        """Validate target, then refuse absent backend active-pair recovery authority.

        This is NOT a crash-safe upgrade implementation. Never stop, stage, or
        rewrite config while that integration is missing, even for the same pin.
        """
        with owner.mutation_gate:
            code = self._precondition(owner, expected_generation, expected_revision, native_consent)
            if code:
                return self._result(owner, code)
            try:
                self._verify_content(self._archive_binary(archive_path))
            except Exception:
                return self._result(owner, 'INVALID_TARGET')
            code = self._precondition(owner, expected_generation, expected_revision, native_consent)
            return self._result(owner, code or 'RECOVERY_REQUIRED')

    def remove(self, owner, *, expected_generation, expected_revision, native_consent=False):
        """Retained-data owned-stop PREPARATION, not package uninstall.

        OwnerView cannot prove idle supervisor termination. Report that unknown;
        the supported host installer remains responsible for removing package code.
        """
        with owner.mutation_gate:
            extra = dict(data_retained=True, package_removed=False,
                         supervisor_terminated=None, removal_prepared=False, running_state='unknown')
            code = self._precondition(owner, expected_generation, expected_revision, native_consent)
            if code:
                return self._result(owner, code, **extra)
            try:
                stopped = owner.stop_owned_and_reap() is True
            except Exception:
                stopped = False
            if not stopped:
                return self._result(owner, 'STOP_FAILED', **extra)
            extra.update(removal_prepared=True, running_state='stopped')
            return self._result(owner, 'OK', **extra)
