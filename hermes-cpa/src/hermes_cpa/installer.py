"""Compatibility gate, pinned acquisition and retain-data lifecycle operations.

Packaged pins are trusted inputs. The executable is the Hermes owner-integration
build of CPA 8.0.21 described by ``cpa-runtime.json``, never the stock release.
It comes from a local --archive or from the one fixed private candidate asset
fetched through an authenticated gh CLI after explicit setup consent; both must
match the packaged size and sha256. No latest/caller URL, stock fallback, boot
service, provider settings rewrite, OAuth-store copy or recursive removal.
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
from .authorization import AuthorizationDeadline
from .candidate_download import download_candidate
from .runtime_manifest import (BINARY_NAME, CPA_VERSION, INTEGRATION, MAX_ARTIFACT_BYTES, PLATFORM,
                               glibc_tuple, parse_runtime_manifest)

MAX_ARCHIVE = MAX_ARTIFACT_BYTES
# Notices and other small members that may travel beside the executable.
AUXILIARY_BYTES = 4 * 1024 * 1024


class UnsupportedLibc(ValueError):
    """Host C library cannot run the pinned executable; str() is the stable code."""

    def __init__(self, required, found):
        super().__init__('unsupported_libc')
        self.required, self.found = required, found


def _version(parts):
    return tuple(parts) + (0,) * (3 - len(parts))


def host_glibc():
    """Host glibc version tuple, or None for musl/unknown C libraries."""
    try:
        return glibc_tuple(os.confstr('CS_GNU_LIBC_VERSION'))
    except (ValueError, OSError, AttributeError, TypeError):
        return None


def require_runtime_libc(runtime):
    """Refuse before launch when the host glibc is older than the build's max GLIBC symbol."""
    required = glibc_tuple(runtime.data['build']['glibc_max'])
    found = host_glibc()
    if found is None or _version(found) < _version(required):
        raise UnsupportedLibc(required, found)


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

    def _authorize_isolated_candidate(self, manifest_content=None):
        """Explicit backend-only, home/package/digest-bound disposable test permit.

        No implicit file discovery and no renderer path input. This never edits
        release metadata or relaxes executable/console/license verification.
        """
        import time
        try:
            if manifest_content is None:
                manifest_content = bounded_file(self.package_root / 'compatibility.json', 65536)
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
                    record.get('manifest_sha256') != hashlib.sha256(manifest_content).hexdigest() or
                    type(record.get('expires_at')) not in (int, float) or
                    not time.time() < record['expires_at'] <= time.time() + 86400):
                raise ValueError('invalid_authorization')
            return record['expires_at']
        except Exception as exc:
            raise ValueError('isolated_authorization_invalid') from exc

    @staticmethod
    def _release_accepted(manifest):
        return (manifest.get('candidate_status') == 'accepted' and
                manifest.get('adr_status') == 'Accepted' and
                isinstance(manifest.get('tested_minimum_hermes'), str) and
                re.fullmatch(r'\d+\.\d+\.\d+', manifest['tested_minimum_hermes']) is not None)

    def _release_authorization(self, manifest, manifest_content):
        if self._release_accepted(manifest):
            return None
        if self.isolated_authorization is None:
            raise ValueError('unaccepted_release')
        return self._authorize_isolated_candidate(manifest_content)

    def runtime_authorization(self):
        """Capture admission once; executable integrity stays on launch checks."""
        require_platform()
        raw = bounded_file(self.package_root / 'compatibility.json', 65536)
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError('invalid_manifest')
        expiry = self._release_authorization(manifest, raw)
        if expiry is None:
            return None
        authorization = AuthorizationDeadline(expiry)
        if not authorization.current():
            raise ValueError('isolated_authorization_invalid')
        return authorization

    def verify_release(self):
        """Release gate, pair, console and notices without an executable.

        Setup calls this before any download or write so an unaccepted or
        mismatched package refuses with the home unchanged.
        """
        return self._verify_content(None)

    def runtime_manifest(self):
        """Packaged runtime identity; refuses a missing, symlinked or drifted manifest."""
        return parse_runtime_manifest(bounded_file(self.package_root / 'cpa-runtime.json', 65536))

    def _pinned_artifact(self):
        runtime = self.runtime_manifest()
        if not runtime.pinned:
            # The integration build has not been produced and pinned yet. There is
            # no stock fallback to install instead.
            raise ValueError('runtime_unpinned')
        return runtime

    def _verify_content(self, content):
        require_platform()
        raw = bounded_file(self.package_root / 'compatibility.json', 65536)
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError('invalid_manifest')
        # Artifact compatibility is not release approval. Missing metadata and
        # a Proposed ADR fail closed, including initial offline installation.
        self._release_authorization(manifest, raw)
        pairs = manifest.get('supported_pairs', [])
        if not isinstance(pairs, list) or any(not isinstance(p, dict) for p in pairs):
            raise ValueError('invalid_manifest')
        pair = next((p for p in pairs if p.get('cpa_version') == CPA_VERSION and
                     p.get('console_version') == '1.25.6' and p.get('platform') == PLATFORM and
                     p.get('integration') == INTEGRATION), None)
        if pair is None:
            raise ValueError('unsupported_pair')
        runtime = self._pinned_artifact()
        artifact = runtime.artifact
        if pair.get('archive_sha256') != artifact.archive_sha256:
            raise ValueError('archive_pin_mismatch')
        if pair.get('binary_sha256') != artifact.binary_sha256:
            raise ValueError('binary_pin_mismatch')
        require_runtime_libc(runtime)
        if content is not None and (len(content) != artifact.binary_size or
                                    hashlib.sha256(content).hexdigest() != artifact.binary_sha256):
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

    def download_candidate_archive(self):
        """Fetch the fixed private candidate asset through gh; verified bytes, no disk writes."""
        require_platform()
        return download_candidate(self._pinned_artifact().artifact)

    def archive_binary_bytes(self, raw):
        """Extract the pinned executable from already-read archive bytes."""
        require_platform()
        if len(raw) > MAX_ARCHIVE:
            raise ValueError('file_too_large')
        artifact = self._pinned_artifact().artifact
        if len(raw) != artifact.archive_size or hashlib.sha256(raw).hexdigest() != artifact.archive_sha256:
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
                if expanded > artifact.binary_size + AUXILIARY_BYTES:
                    raise ValueError('archive_too_large')
                if member.name == BINARY_NAME and member.isfile():
                    if member.size != artifact.binary_size:
                        raise ValueError('binary_mismatch')
                    binary = archive.extractfile(member).read(artifact.binary_size + 1)
            if (binary is None or len(binary) != artifact.binary_size or
                    hashlib.sha256(binary).hexdigest() != artifact.binary_sha256):
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
