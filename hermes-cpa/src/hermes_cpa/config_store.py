import hashlib
import json
import os
import io
import stat
from ruamel.yaml.tokens import AliasToken, AnchorToken
from .contracts import validate_changes
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from ruamel.yaml import YAML

class ConfigStore:
    def __init__(self, config_path: Path):
        self.config_path = config_path
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
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Configuration too large')
        return raw

    def compute_digest(self) -> str:
        raw = self._read_bytes()
        return hashlib.sha256(raw).hexdigest() if raw else ''

    def read_config(self) -> Dict[str, Any]:
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

    def create_initial_config(self, port: int, management_key: str, inference_key: str):
        import bcrypt
        hashed_mgmt = bcrypt.hashpw(management_key.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")
        auth_dir = str(self.config_path.parent / "auth")
        os.makedirs(auth_dir, exist_ok=True, mode=0o700)
        content = (
            f"config-version: 8\n"
            f"server:\n"
            f"  host: 127.0.0.1\n"
            f"  port: {port}\n"
            f"  trusted-proxies: []\n"
            f"  discovery:\n"
            f"    enabled: false\n"
            f"management:\n"
            f"  allow-remote: false\n"
            f"  secret-key: \"{hashed_mgmt}\"\n"
            f"  disable-control-panel: true\n"
            f"access:\n"
            f"  api-keys:\n"
            f"    - \"{inference_key}\"\n"
            f"oauth:\n"
            f"  auth-dir: \"{auth_dir}\"\n"
            f"routing:\n"
            f"  strategy: round-robin\n"
            f"  retry:\n"
            f"    request-retry: 3\n"
            f"observability:\n"
            f"  logs:\n"
            f"    request-log: false\n"
        )
        self._atomic(self.config_path, content.encode('utf-8'))

    def snapshot(self) -> Path:
        snap_path = self.config_path.with_suffix('.snapshot')
        self._atomic(snap_path, self._read_bytes())
        return snap_path

    def rollback(self, snap_path: Path):
        raw = self._read_bytes(snap_path)
        if not raw:
            raise ValueError('Missing snapshot')
        self._atomic(self.config_path, raw)

    def _journal(self, phase, revision):
        self._atomic(self.config_path.with_suffix('.journal'),
                     json.dumps({'phase': phase, 'revision': revision}).encode())

    def transact(self, expected_revision, changes, *, expected_generation, owner,
                 restart_consent=False):
        """Only the supervisor invokes this; its gate serializes all mutations.

        An unfinished journal blocks new writes until explicit recovery. Neither
        the journal nor snapshots include or restore the independent OAuth store.
        """
        with owner.mutation_gate:
            def result(code, rollback=None):
                try:
                    revision = self.compute_digest()
                except Exception:
                    revision = ''
                return dict(ok=code == 'OK', code=code, revision=revision,
                            generation=owner.generation, rollback=rollback)
            if owner.mode != 'managed':
                return result('READ_ONLY')
            if owner.policy_locked:
                return result('POLICY_LOCKED')
            if type(expected_generation) is not int or expected_generation != owner.generation:
                return result('STALE_GENERATION')
            try:
                if not expected_revision or expected_revision != self.compute_digest():
                    return result('STALE_REVISION')
                validate_changes(changes)
                self.read_config()
            except Exception:
                return result('INVALID_INPUT')
            was_running = owner.running
            if was_running and restart_consent is not True:
                return result('CONSENT_REQUIRED')
            journal = self.config_path.with_suffix('.journal')
            if journal.exists():
                try:
                    phase = json.loads(self._read_bytes(journal))['phase']
                    if phase not in ('committed', 'recovered', 'aborted'):
                        return result('RECOVERY_REQUIRED')
                except Exception:
                    return result('RECOVERY_REQUIRED')
            wrote = False
            snap = None
            new_revision = None
            try:
                owner.assert_owned()
                snap = self.snapshot()
                if hashlib.sha256(self._read_bytes(snap)).hexdigest() != expected_revision:
                    return result('STALE_REVISION')
                self._journal('prepared', expected_revision)
                if was_running and not owner.stop_owned_and_reap():
                    self._journal('aborted', expected_revision)
                    return result('STOP_FAILED')
                self._journal('stopped', expected_revision)
                # Stop may block while host policy is revoked. Never continue a
                # previously authorized write or restart under revoked authority.
                if owner.policy_locked or owner.mode != 'managed':
                    self._journal('aborted', expected_revision)
                    return result('POLICY_LOCKED' if owner.policy_locked else 'READ_ONLY')
                if self.compute_digest() != expected_revision:
                    self._journal('aborted', expected_revision)
                    return result('STALE_REVISION')
                # Write-ahead marker: a crash now requires explicit reconciliation.
                self._journal('applying', expected_revision)
                if owner.policy_locked or owner.mode != 'managed':
                    self._journal('aborted', expected_revision)
                    return result('POLICY_LOCKED')
                wrote = True
                ok, new_revision, code = self.apply_safe_changes(expected_revision, changes)
                if not ok:
                    wrote = False
                    self._journal('aborted', expected_revision)
                    return result(code)
                self._journal('applied', new_revision)
                if owner.policy_locked or owner.mode != 'managed':
                    raise PermissionError('policy_locked')
                if was_running and not owner.start_owned_and_poll_readiness():
                    raise RuntimeError('restart')
                if owner.policy_locked or owner.mode != 'managed':
                    raise PermissionError('policy_locked')
                self._journal('committed', new_revision)
                if owner.policy_locked or owner.mode != 'managed':
                    raise PermissionError('policy_locked')
                return result('OK')
            except Exception:
                code = ('POLICY_LOCKED' if owner.policy_locked or owner.mode != 'managed'
                        else 'RESTART_FAILED' if new_revision else 'APPLY_FAILED')
                if not wrote:
                    return result(code, 'not_attempted')
                try:
                    if not owner.stop_owned_and_reap():
                        return result(code, 'stop_failed')
                    # Do not silently overwrite a concurrent external edit.
                    current = self.compute_digest()
                    # An atomic replacement can succeed before its directory
                    # fsync raises. Without an acknowledged new digest we cannot
                    # distinguish that write from an external writer: preserve
                    # changed bytes and require explicit recovery, never guess.
                    if current != (new_revision or expected_revision):
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
                    if owner.policy_locked or owner.mode != 'managed':
                        self._journal('recovered', expected_revision)
                        return result(code, 'restored_stopped')
                    recovered = not was_running or owner.start_owned_and_poll_readiness()
                    if owner.policy_locked or owner.mode != 'managed':
                        stopped = owner.stop_owned_and_reap()
                        self._journal('recovered' if stopped else 'recovery_failed', expected_revision)
                        return result('POLICY_LOCKED', 'restored_stopped' if stopped else 'stop_failed')
                    self._journal('recovered' if recovered else 'recovery_failed', expected_revision)
                    return result(code, 'recovered' if recovered else 'restart_failed')
                except Exception:
                    return result(code, 'restart_failed')

    def recover(self, *, expected_revision, snapshot_revision, expected_generation,
                owner, native_consent=False):
        """Explicit restore to a selected snapshot; leave stopped for new consent.

        Caller is the authenticated owner under its mutation gate. Never infer
        which bytes an interrupted atomic replace published. Require the human
        to acknowledge both the CURRENT revision and the snapshot digest.
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
                if phase in ('committed', 'recovered', 'aborted'):
                    return result('NO_RECOVERY_REQUIRED')
                if phase not in ('prepared', 'stopped', 'applying', 'applied',
                                 'restored', 'recovery_failed', 'recovering'):
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
                self._atomic(self.config_path, snapshot)
                # Restoration is a safe-stop action, not permission to claim
                # success after explicit revocation during blocking disk I/O.
                if owner.policy_locked or owner.mode != 'managed':
                    return result('POLICY_LOCKED')
                self._journal('recovered', snapshot_revision)
                if owner.policy_locked or owner.mode != 'managed':
                    self._journal('recovery_failed', snapshot_revision)
                    return result('POLICY_LOCKED')
                return result('OK')
            except Exception:
                return result('RECOVERY_FAILED')

    def apply_safe_changes(self, expected_revision: str, changes: Dict[str, Any]) -> Tuple[bool, str, str]:
        current_rev = self.compute_digest()
        if not isinstance(expected_revision, str) or not expected_revision or current_rev != expected_revision:
            return False, current_rev, "STALE_REVISION"
        if not isinstance(changes, dict) or not changes or set(changes) - {"strategy", "request_retry", "request_log"}:
            return False, current_rev, "INVALID_INPUT"

        raw = self.read_config()
        if not raw:
            return False, current_rev, "UNCONFIGURED"

        # Validate allowlisted paths only
        if "strategy" in changes:
            val = changes["strategy"]
            if val not in ["round-robin", "fill-first"]:
                return False, current_rev, "INVALID_STRATEGY"
            raw.setdefault("routing", {})["strategy"] = val

        if "request_retry" in changes:
            val = changes["request_retry"]
            if not isinstance(val, int) or isinstance(val, bool) or val < 0 or val > 10:
                return False, current_rev, "INVALID_RETRY"
            raw.setdefault("routing", {}).setdefault("retry", {})["request-retry"] = val

        if "request_log" in changes:
            val = changes["request_log"]
            if not isinstance(val, bool):
                return False, current_rev, "INVALID_LOG"
            raw.setdefault("observability", {}).setdefault("logs", {})["request-log"] = val

        output = io.StringIO()
        self.yaml.dump(raw, output)
        if self.compute_digest() != expected_revision:
            return False, self.compute_digest(), 'STALE_REVISION'
        self._atomic(self.config_path, output.getvalue().encode('utf-8'))
        return True, self.compute_digest(), 'OK'
