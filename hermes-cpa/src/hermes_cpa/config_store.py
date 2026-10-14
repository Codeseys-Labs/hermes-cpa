"""Owned CPA config document: exact native candidates, journal, exact recovery.

Python never interprets CPA settings for writes. The supervisor's native
preparer (the pinned CPA integration build, live control socket or offline
mode) returns canonical candidate bytes. This store snapshots the base,
journals before every write, persists in place on the existing inode exactly
like CPA's own WriteConfig, and restores exact bytes. Neither the journal nor
snapshots include or restore the independent auth directory: OAuth and
credential files are never copied, moved or rolled back here.
"""
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict

from ruamel.yaml import YAML
from ruamel.yaml.tokens import AliasToken, AnchorToken

from .owner_transport import CONFIG_LIMIT

TERMINAL_PHASES = ('committed', 'recovered', 'aborted')
UNFINISHED_PHASES = ('prepared', 'stopped', 'applying', 'applied', 'restored',
                     'recovery_failed', 'recovering')
REVISION = re.compile(r'[0-9a-f]{64}\Z')
_PUBLIC_KEY = re.compile(r'[\x21-\x7e]{1,512}\Z')


class ConfigStore:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.candidate_path = config_path.with_suffix('.candidate')
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.indent(mapping=2, sequence=4, offset=2)

    def _read_bytes(self, path=None):
        path = path or self.config_path
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Unsafe configuration path')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return b''
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Not a regular configuration')
            # Explicit whole-document bound shared with native prepare/commit.
            raw = stream.read(CONFIG_LIMIT + 1)
        if len(raw) > CONFIG_LIMIT:
            raise ValueError('Configuration too large')
        return raw

    def compute_digest(self) -> str:
        raw = self._read_bytes()
        return hashlib.sha256(raw).hexdigest() if raw else ''

    def read_config(self) -> Dict[str, Any]:
        """Read-only projection parse; never used to construct a write."""
        raw = self._read_bytes()
        if not raw:
            return {}
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in self.yaml.scan(raw)):
            raise ValueError('Aliases are unsupported')
        data = self.yaml.load(raw)
        if not isinstance(data, dict):
            raise ValueError('Configuration must be a mapping')
        return data

    def _atomic(self, path, raw):
        """Owner-private sibling files (snapshot, journal, candidate) only."""
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Unsafe configuration path')
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.cpa-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _write_in_place(self, raw):
        """Native persistence: truncate and rewrite the SAME inode, then fsync.

        Mounted single-file configs and file watchers keep their identity. A
        crash mid-write leaves a torn file behind an unfinished journal and an
        exact snapshot, never a silently accepted document.
        """
        path = self.config_path
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Unsafe configuration path')
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError('Not a regular configuration')
            os.ftruncate(fd, 0)
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _publish_new(self, raw):
        """Exclusive first publication; an existing document is never replaced."""
        path = self.config_path
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Unsafe configuration path')
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.cpa-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(tmp, path)
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            os.unlink(tmp)

    def create_initial_config(self, port, *, access_keys, management_key=None):
        """First user configuration for a managed CPA. Public keys are USER config.

        ``access_keys`` are user-managed public client keys and must be non-empty:
        native CPA admits unauthenticated requests when no access provider is
        configured. ``management_key`` is an optional user-managed public
        management key stored bcrypt-hashed; None leaves public management
        disabled (native 403). The owner's private broker and control channels
        need no key, so none is written here.
        """
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid_port')
        keys = [] if isinstance(access_keys, (str, bytes)) else list(access_keys)
        if (not keys or len(keys) > 64 or len(set(keys)) != len(keys) or
                any(not isinstance(key, str) or not _PUBLIC_KEY.match(key) for key in keys)):
            raise ValueError('access_keys_required')
        management = ['management:', '  allow-remote: false']
        if management_key is not None:
            if (not isinstance(management_key, str) or not _PUBLIC_KEY.match(management_key) or
                    len(management_key) > 72):
                raise ValueError('invalid_management_key')
            import bcrypt
            hashed = bcrypt.hashpw(management_key.encode('ascii'), bcrypt.gensalt(rounds=10))
            management.append('  secret-key: ' + json.dumps(hashed.decode('ascii')))
        management.append('  disable-control-panel: true')
        auth_dir = self.config_path.parent / 'auth'
        os.makedirs(auth_dir, exist_ok=True, mode=0o700)
        lines = ['config-version: 8', 'server:', '  host: 127.0.0.1', f'  port: {port}',
                 '  trusted-proxies: []', '  discovery:', '    enabled: false', *management,
                 'access:', '  api-keys:', *('    - ' + json.dumps(key) for key in keys),
                 'oauth:', '  auth-dir: ' + json.dumps(str(auth_dir)),
                 'routing:', '  strategy: round-robin', '  retry:', '    request-retry: 3',
                 'observability:', '  logs:', '    request-log: false']
        self._publish_new(('\n'.join(lines) + '\n').encode('utf-8'))

    def snapshot(self) -> Path:
        snap_path = self.config_path.with_suffix('.snapshot')
        self._atomic(snap_path, self._read_bytes())
        return snap_path

    def rollback(self, snap_path: Path):
        raw = self._read_bytes(snap_path)
        if not raw:
            raise ValueError('Missing snapshot')
        self._write_in_place(raw)

    def _journal(self, phase, revision):
        self._atomic(self.config_path.with_suffix('.journal'),
                     json.dumps({'phase': phase, 'revision': revision}).encode())

    def _unfinished(self):
        """True when a journal exists and is not terminal (or is unreadable)."""
        journal = self.config_path.with_suffix('.journal')
        if not (journal.exists() or journal.is_symlink()):
            return False
        try:
            return json.loads(self._read_bytes(journal))['phase'] not in TERMINAL_PHASES
        except Exception:
            return True

    def _discard_candidate(self):
        try:
            self.candidate_path.unlink(missing_ok=True)
        except OSError:
            pass  # A stale candidate never authorizes anything; recovery ignores it.

    def transact(self, expected_revision, requests, *, expected_generation, owner,
                 restart_consent=False, reviewed_candidate_revision=None):
        """Apply ONE native-prepared candidate under the owner's mutation gate.

        ``requests`` are ordered native config requests (Go prepare wire). The
        owner prepares them with the pinned CPA against ``expected_revision``;
        ``reviewed_candidate_revision`` pins the exact bytes a human reviewed.
        A running child takes hot candidates through native live
        compare-and-commit. ``restart_required`` candidates (restart consent
        required) and stopped children are written here in place after the
        child is reaped, then the child restarts. Pending OAuth sessions are
        cancelled before they are invalidated, never rolled back. Every write
        is journaled first; an unfinished journal blocks writes and starts
        until explicit recovery.
        """
        with owner.mutation_gate:
            prepared = {}

            def result(code, rollback=None, detail=None):
                try:
                    revision = self.compute_digest()
                except Exception:
                    revision = ''
                value = dict(ok=code == 'OK', code=code, revision=revision,
                             generation=owner.generation, rollback=rollback)
                value.update(prepared)
                if detail:
                    value['detail'] = detail
                return value

            def locked():
                return owner.policy_locked or owner.mode != 'managed'

            def lock_code():
                return 'READ_ONLY' if owner.mode != 'managed' else 'POLICY_LOCKED'

            if owner.mode != 'managed':
                return result('READ_ONLY')
            if owner.policy_locked:
                return result('POLICY_LOCKED')
            if type(expected_generation) is not int or expected_generation != owner.generation:
                return result('STALE_GENERATION')
            try:
                if (not isinstance(expected_revision, str) or not REVISION.match(expected_revision) or
                        expected_revision != self.compute_digest()):
                    return result('STALE_REVISION')
                if self._unfinished():
                    return result('RECOVERY_REQUIRED')
            except Exception:
                return result('UNAVAILABLE')
            try:
                outcome = owner.prepare_config(expected_revision, requests)
            except Exception:
                outcome = {'ok': False, 'code': 'UNAVAILABLE'}
            if not outcome.get('ok'):
                return result(outcome.get('code') or 'UNAVAILABLE', detail=outcome.get('detail'))
            candidate, target, effects = (outcome['candidate'], outcome['candidate_revision'],
                                          list(outcome['effects']))
            prepared.update(base_revision=outcome['base_revision'], candidate_revision=target,
                            effects=effects, changed_paths=list(outcome['changed_paths']))
            if outcome['base_revision'] != expected_revision:
                return result('STALE_REVISION')
            if reviewed_candidate_revision is not None and target != reviewed_candidate_revision:
                return result('REVIEW_STALE')
            if hashlib.sha256(candidate).hexdigest() != target:
                return result('UNAVAILABLE')
            if target == expected_revision or 'no_change' in effects:
                return result('OK')
            was_running = owner.running
            live = was_running and 'restart_required' not in effects
            if was_running and not live and restart_consent is not True:
                return result('CONSENT_REQUIRED')
            wrote = applied = False
            snap = None
            try:
                owner.assert_owned()
                snap = self.snapshot()
                if hashlib.sha256(self._read_bytes(snap)).hexdigest() != expected_revision:
                    return result('STALE_REVISION')
                self._journal('prepared', expected_revision)
                if live:
                    if locked():
                        self._journal('aborted', expected_revision)
                        return result(lock_code())
                    if 'pending_oauth_invalidated' in effects:
                        owner.cancel_pending_oauth('config_transaction')
                    self._atomic(self.candidate_path, candidate)
                    # Write-ahead marker: a lost reply now requires explicit recovery.
                    self._journal('applying', expected_revision)
                    if locked():
                        self._journal('aborted', expected_revision)
                        self._discard_candidate()
                        return result(lock_code())
                    wrote = True
                    committed = owner.commit_live(expected_revision, target, candidate)
                    if committed == 'OUTCOME_UNKNOWN' and self.compute_digest() != target:
                        # The native writer may still publish; never guess or resubmit.
                        return result('APPLY_FAILED', 'outcome_unknown')
                    if committed not in ('OK', 'OUTCOME_UNKNOWN'):
                        wrote = False
                        self._journal('aborted', expected_revision)
                        self._discard_candidate()
                        return result('STALE_REVISION' if committed == 'STALE_REVISION' else
                                      'UNAVAILABLE' if committed == 'UNAVAILABLE' else 'APPLY_FAILED',
                                      'not_attempted')
                    applied = True
                    self._journal('applied', target)
                else:
                    if was_running and not owner.stop_owned_and_reap():
                        self._journal('aborted', expected_revision)
                        return result('STOP_FAILED')
                    self._journal('stopped', expected_revision)
                    # Stop may block while host policy is revoked. Never continue a
                    # previously authorized write or restart under revoked authority.
                    if locked():
                        self._journal('aborted', expected_revision)
                        return result(lock_code())
                    if self.compute_digest() != expected_revision:
                        self._journal('aborted', expected_revision)
                        return result('STALE_REVISION')
                    self._atomic(self.candidate_path, candidate)
                    self._journal('applying', expected_revision)
                    if locked():
                        self._journal('aborted', expected_revision)
                        self._discard_candidate()
                        return result(lock_code())
                    wrote = True
                    self._write_in_place(candidate)
                    if self.compute_digest() != target:
                        raise ValueError('write_mismatch')
                    applied = True
                    self._journal('applied', target)
                    if locked():
                        raise PermissionError('policy_locked')
                    if was_running and not owner.start_owned_and_poll_readiness():
                        raise RuntimeError('restart')
                if locked():
                    raise PermissionError('policy_locked')
                self._journal('committed', target)
                self._discard_candidate()
                if locked():
                    raise PermissionError('policy_locked')
                owner.config_committed(expected_revision, target, effects)
                return result('OK')
            except Exception:
                code = ('POLICY_LOCKED' if locked() else 'RESTART_FAILED' if applied
                        else 'APPLY_FAILED')
                if not wrote:
                    return result(code, 'not_attempted')
                try:
                    if not owner.stop_owned_and_reap():
                        return result(code, 'stop_failed')
                    # Never overwrite bytes we cannot identify: a torn write, a
                    # native rewrite or an external editor all require explicit
                    # recovery against the observed current revision.
                    if self.compute_digest() not in (target, expected_revision):
                        return result(code, 'drift_detected')
                    if snap is None:
                        return result(code, 'restore_failed')
                    self.rollback(snap)
                    self._journal('restored', expected_revision)
                except Exception:
                    return result(code, 'restore_failed')
                try:
                    # Restoring acknowledged bytes is safe recovery, not authority
                    # to resume inference after explicit revocation.
                    if locked():
                        self._journal('recovered', expected_revision)
                        self._discard_candidate()
                        return result(code, 'restored_stopped')
                    recovered = not was_running or owner.start_owned_and_poll_readiness()
                    if locked():
                        stopped = owner.stop_owned_and_reap()
                        self._journal('recovered' if stopped else 'recovery_failed', expected_revision)
                        return result('POLICY_LOCKED', 'restored_stopped' if stopped else 'stop_failed')
                    self._journal('recovered' if recovered else 'recovery_failed', expected_revision)
                    if recovered:
                        self._discard_candidate()
                    return result(code, 'recovered' if recovered else 'restart_failed')
                except Exception:
                    return result(code, 'restart_failed')

    def recover(self, *, expected_revision, snapshot_revision, expected_generation,
                owner, native_consent=False):
        """Explicit restore to a selected snapshot; leave stopped for new consent.

        Caller is the authenticated owner under its mutation gate. Never infer
        which bytes an interrupted write published. Require the human to
        acknowledge both the CURRENT revision and the snapshot digest.
        """
        with owner.mutation_gate:
            def result(code):
                return {'ok': code == 'OK', 'code': code,
                        'generation': owner.generation}

            def check():
                if owner.mode != 'managed':
                    return 'READ_ONLY'
                if owner.policy_locked:
                    return 'POLICY_LOCKED'
                if type(expected_generation) is not int or expected_generation != owner.generation:
                    return 'STALE_GENERATION'
                if native_consent is not True:
                    return 'CONSENT_REQUIRED'
                if not expected_revision or self.compute_digest() != expected_revision:
                    return 'STALE_REVISION'
                raw = self._read_bytes(self.config_path.with_suffix('.snapshot'))
                if not raw or hashlib.sha256(raw).hexdigest() != snapshot_revision:
                    return 'STALE_SNAPSHOT'
                return None

            try:
                code = check()
                if code:
                    return result(code)
                journal = self.config_path.with_suffix('.journal')
                phase = json.loads(self._read_bytes(journal))['phase']
                if phase in TERMINAL_PHASES:
                    return result('NO_RECOVERY_REQUIRED')
                if phase not in UNFINISHED_PHASES:
                    return result('RECOVERY_FAILED')
                owner.assert_owned()
                if not owner.stop_owned_and_reap():
                    return result('STOP_FAILED')
                code = check()
                if code:
                    return result(code)
                # Capture the verified bytes rather than reopening after the
                # journal write; never restore OAuth or other sibling data.
                snapshot = self._read_bytes(self.config_path.with_suffix('.snapshot'))
                if hashlib.sha256(snapshot).hexdigest() != snapshot_revision:
                    return result('STALE_SNAPSHOT')
                self._journal('recovering', expected_revision)
                code = check()
                if code:
                    return result(code)
                self._write_in_place(snapshot)
                # Restoration is a safe-stop action, not permission to claim
                # success after explicit revocation during blocking disk I/O.
                if owner.policy_locked or owner.mode != 'managed':
                    return result('POLICY_LOCKED')
                self._journal('recovered', snapshot_revision)
                if owner.policy_locked or owner.mode != 'managed':
                    self._journal('recovery_failed', snapshot_revision)
                    return result('POLICY_LOCKED')
                self._discard_candidate()
                return result('OK')
            except Exception:
                return result('RECOVERY_FAILED')
