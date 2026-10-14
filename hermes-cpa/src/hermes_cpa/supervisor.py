"""Single Linux owner. All mutation paths share mutation_gate; no auto-restart.

The owned CPA child serves private owner-only Unix channels (control and
inference) beside its user-configured public listener. Readiness, config
transactions, the inference broker and the console service reach the child
only through those channels, verified against the exact launched process.
Public listener, TLS, management and access keys are user configuration and
confer no owner authority.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import secrets
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import parse_qsl

from .settings import CpaSettings
from .contracts import LifecycleState, validate_changes
from .owner_transport import (CONSOLE_FRAME_SECONDS, CONSOLE_IPC_LIMIT, ORDINARY_REQUEST_LIMIT,
    PREPARE_RESPONSE_LIMIT, REVISION, ControlChannel, InferenceChannel, TransportError,
    decode_prepare, encode_prepare, frame_length, read_exact, read_line)
from .process_owner import (require_platform, process_identity, private_directory,
    private_open, atomic_json, socket_address, peer_pid, signal_owned)

# The reduced legacy config.apply wire, expressed as native v8 config requests.
LEGACY_CONFIG_ROUTES = {'strategy': '/v8/management/config/routing/strategy',
                        'request_retry': '/v8/management/config/routing/retry/request-retry',
                        'request_log': '/v8/management/config/observability/logs/request-log'}
NATIVE_REFUSALS = frozenset({'INVALID_REQUEST', 'INVALID_CONFIG', 'NOT_FOUND', 'READ_ONLY_FIELD'})
CONSOLE_JOBS = 4
# Receipts of accepted console work and scoped grant cleanup: no native call,
# original scope only, so they stay reachable after revocation or shutdown latch.
CONSOLE_RECEIPT_OPS = frozenset({'result', 'authority_terminal', 'resource_close'})
IPC_HANDLERS = 8
PASSIVE_READ_LIMIT = 1024 * 1024
_RESULT_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')


def _stamp(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class InstallPolicy:
    """Profile enablement and installed-package authority, never credentials.

    Two tiers share one sampler:

    * capture()/matches() pin the full config.yaml and plugin.yaml metadata for
      ONE accepted operation. Any rewrite revokes that work, including an exact
      byte restore after disable/re-enable.
    * __call__() is the long-lived service tier. Home, package directory and
      manifest identities stay strict, so remove/reinstall revokes. A
      config.yaml rewrite keeps the owner only while the host's durable
      per-plugin policy epoch is readable and unchanged AND this plugin's policy
      projection (enabled/disabled membership, entry flags, settings except the
      autostart opt-in) is unchanged. With no readable epoch any rewrite revokes.

    Unsupported: a raw external writer that bypasses Hermes's policy writers and
    restores the exact projection between two samples looks like an unrelated
    edit. Revocation is latched; only a new owner samples a new policy.
    """
    def __init__(self, home, package_root, plugin_key='hermes-cpa'):
        self.home, self.package_root, self.plugin_key = Path(home), Path(package_root), plugin_key
        self._pinned = None
        self._last = None
        self._revoked = False
        self._gate = threading.RLock()

    def _identities(self):
        config, manifest = self.home / 'config.yaml', self.package_root / 'plugin.yaml'
        for item in (config, manifest):
            if not item.is_file() or item.is_symlink():
                return None
        home, package = self.home.stat(), self.package_root.stat()
        # Directories pin identity only: unrelated files beneath them are not policy.
        return ((home.st_dev, home.st_ino), (package.st_dev, package.st_ino),
                _stamp(manifest.stat()), _stamp(config.stat()))

    def _epoch(self):
        try:
            from hermes_cli.plugin_policy_epoch import read_policy_epoch
            value = read_policy_epoch(self.home, self.plugin_key)
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    def _projection(self):
        from ruamel.yaml import YAML
        path = self.home / 'config.yaml'
        if path.stat().st_size > 1024 * 1024:
            raise RuntimeError('policy_unavailable')
        data = YAML(typ='safe').load(path.read_text())
        plugins = data.get('plugins') if isinstance(data, dict) else None
        plugins = {} if plugins is None else plugins
        if not isinstance(plugins, dict):
            raise RuntimeError('policy_unavailable')
        enabled, disabled = plugins.get('enabled', []), plugins.get('disabled', [])
        entries = plugins.get('entries') or {}
        if not isinstance(enabled, list) or not isinstance(disabled, list) or not isinstance(entries, dict):
            raise RuntimeError('policy_unavailable')
        entry = entries.get(self.plugin_key) or {}
        settings = (entry.get('settings') or {}) if isinstance(entry, dict) else None
        if not isinstance(settings, dict):
            raise RuntimeError('policy_unavailable')
        key = self.plugin_key
        allowed = (key in enabled and key not in disabled and entry.get('enabled') is not False and
                   settings.get('mode', 'external') == 'managed')
        projection = json.dumps([key in enabled, key in disabled,
                                 {k: v for k, v in entry.items() if k != 'settings'},
                                 {k: v for k, v in settings.items() if k != 'autostart'}],
                                sort_keys=True, default=str)
        return allowed, projection

    def _sample(self):
        for _ in range(3):
            before = self._identities()
            if before is None:
                return False, None, None, None
            epoch = self._epoch()
            allowed, projection = self._projection()
            # Never pin a different file or epoch than the one just parsed.
            if self._identities() == before and self._epoch() == epoch:
                return allowed, before, epoch, projection
        raise RuntimeError('policy_unavailable')

    def _check(self):
        if self._revoked:
            return False
        allowed, incarnation, epoch, projection = self._sample()
        if allowed and self._pinned is not None:
            pinned, pinned_epoch, pinned_projection = self._pinned
            allowed = (incarnation[:3] == pinned[:3] and epoch == pinned_epoch and
                       (incarnation[3] == pinned[3] or
                        (epoch is not None and projection == pinned_projection)))
        if not allowed:
            self._revoked = True
            return False
        self._pinned = (incarnation, epoch, projection)
        self._last = incarnation
        return True

    def __call__(self):
        with self._gate:
            return self._check()

    def capture(self):
        """Exact incarnation authorizing one accepted operation, or None."""
        with self._gate:
            return self._last if self._check() else None

    def matches(self, captured):
        """Service still authorized AND nothing rewritten since capture."""
        with self._gate:
            return captured is not None and self._check() and self._last == captured


class CpaSupervisor:
    """One owner per home, enforced by the instance lifetime lock.

    lifetime='persistent' (explicit setup/start/bootstrap) runs until explicit
    stop, unload under revoked policy, or policy revocation. lifetime='plugin'
    (remembered autostart) additionally exits once no native registration has
    held a lifetime lease for lease_grace seconds. Neither mode restarts CPA.

    ``console`` is the console service factory ``(owner, control) -> service``;
    ``broker`` is the inference broker factory ``(owner) -> context manager``.
    """
    def __init__(self, settings, *, mode=None, probe=None, policy=None,
                 readiness_timeout=30., stop_grace=10.,
                 watchdog_interval=5., policy_failure_timeout=30., command=None,
                 prepare_command=None, compatibility_check=None, lifetime='persistent',
                 lease_grace=10., broker=None, console=None, authorization=None):
        self.settings = settings
        self.mode = mode or getattr(settings, 'mode', 'external')
        self.policy = policy or (lambda: False)
        # Dependency injection for harmless generated children only.
        self.command = command
        self.prepare_command = prepare_command
        self.compatibility_check = compatibility_check
        self.authorization = authorization
        self.readiness_timeout, self.stop_grace = readiness_timeout, stop_grace
        self.watchdog_interval, self.policy_failure_timeout = watchdog_interval, policy_failure_timeout
        self.child_process = None
        self.child_identity = None
        # Private owner channels accept only the live child launched here.
        self.control = ControlChannel(settings.private_dir, self.private_peer)
        self.inference = InferenceChannel(settings.private_dir, self.private_peer)
        self.probe = probe or self.control.probe
        self.lock_file = None
        self.server_sock = None
        self.socket_inode = None
        self.running = False
        self.generation = secrets.randbits(52) + 1
        self.state = LifecycleState.STOPPED
        self.sanitized_reason = ''
        self.last_transition = time.time()
        self.inference_configured = None
        self.mutation_gate = threading.RLock()
        self._policy_failure_since = None
        self._events = deque(maxlen=256)
        self._seq = 0
        self._operations = OrderedDict()
        self._ledger_gate = threading.Lock()
        self._worker = None
        import queue
        self._queue = queue.Queue(maxsize=100)
        self._shutdown = False
        self._cleanup_gate = threading.Lock()
        self._exit_deadline = None
        self._operation_policy = threading.local()
        self._ipc_slots = threading.BoundedSemaphore(IPC_HANDLERS)
        if lifetime not in ('persistent', 'plugin'):
            raise ValueError('invalid_lifetime')
        self.lifetime, self.lease_grace = lifetime, lease_grace
        self._lease_fd = None
        self._lease_idle_since = None
        # Factory for the default-owner inference broker, entered by run_ipc.
        self.broker = broker
        self._broker = None
        self.broker_state = 'disabled' if broker is None else 'stopped'
        # One console service per owner; it never gets a second acceptance ledger.
        self.console = console(self, self.control) if console is not None else None

    def _authorization_current(self):
        return self.authorization is None or self.authorization.current()

    def _authorized_timeout(self, timeout):
        return timeout if self.authorization is None else min(timeout, self.authorization.remaining())

    def authorized_timeout(self, timeout):
        """Bound owner work by the captured candidate authorization deadline."""
        return self._authorized_timeout(timeout)

    def _capture_policy(self):
        """Pin an accepted operation to the exact policy incarnation, if supported.

        Returns None when the policy denies or cannot be read now.
        """
        if not self._authorization_current():
            return None
        capture = getattr(self.policy, 'capture', None)
        if capture is None:
            return True  # Injected test policies have no incarnation to pin.
        try:
            captured = capture()
            return captured if self._authorization_current() else None
        except Exception:
            return None

    def _operation_allowed(self, captured=None):
        if not self._authorization_current():
            return False
        if captured is None:
            captured = getattr(self._operation_policy, 'captured', None)
        if captured is not None and captured is not True and not self.policy.matches(captured):
            return False  # Any config/manifest rewrite revokes accepted work.
        request = getattr(self._operation_policy, 'request', None)
        if request is None or '_host_grant' not in request:
            return True  # Native CLI path retains its independent install policy.
        from .operation_guard import revalidate_grant
        intent = {key: value for key, value in request.items() if key != '_host_grant'}
        return revalidate_grant(request['_host_grant'], intent)

    def operation_current(self):
        """Owner authority for console work right now; call at linearization points.

        Always checks the shutdown latch, authorization deadline and service
        policy. Inside an accepted job (bound by the ledger worker) it also
        checks the policy incarnation captured at acceptance and any grant.
        """
        return not self._shutdown and self._policy_ok()

    def _transition(self, state, reason=''):
        self.state, self.sanitized_reason = state, reason
        self.last_transition = time.time()
        self._seq += 1
        self._events.append({'seq': self._seq, 'time': self.last_transition,
                             'generation': self.generation, 'type': 'lifecycle',
                             'summary_code': reason or state.value})

    def _console_hook(self, name, *args):
        """Best-effort service notification; a failing hook never blocks the owner."""
        hook = getattr(self.console, name, None) if self.console is not None else None
        if hook is None:
            return
        try:
            hook(*args)
        except Exception:
            pass

    def acquire_lock(self):
        require_platform()
        import fcntl
        if self.lock_file is not None:
            return True
        for directory in (self.settings.cpa_dir, self.settings.run_dir):
            private_directory(directory)
        fd = private_open(self.settings.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.lock_file = os.fdopen(fd, 'r+')
        return True

    def assert_ownership(self):
        if self.mode != 'managed' or self.lock_file is None:
            raise RuntimeError('unowned')
        if self.child_process and self.child_process.poll() is None:
            if self.child_identity != process_identity(self.child_process.pid):
                raise RuntimeError('ownership_lost')

    def private_peer(self):
        """Identity of the live child launched here: the only acceptable private peer."""
        child, identity = self.child_process, self.child_identity
        if child is None or identity is None or child.poll() is not None:
            return None
        return identity if process_identity(child.pid) == identity else None

    def _reset_private_channels(self):
        """Caller holds the instance lock: stale socket paths are ours to remove.

        The child refuses to start over an existing path and never unlinks one
        it did not create; a foreign listener left there is never adopted.
        """
        private_directory(self.settings.private_dir)
        for path in (self.settings.control_socket_path, self.settings.inference_socket_path):
            if path.is_symlink() or (path.exists() and not path.is_socket()):
                raise RuntimeError('unsafe_socket')
            path.unlink(missing_ok=True)

    def _private_arguments(self):
        return ['--hermes-control-socket', str(self.settings.control_socket_path),
                '--hermes-inference-socket', str(self.settings.inference_socket_path),
                '--hermes-owner-pid', str(os.getpid())]

    def _policy_ok(self, captured=None):
        if not self._authorization_current():
            self._policy_failure_since = None  # Expiry never receives transient-policy grace.
            return False
        try:
            allowed = self.policy() is True and self._operation_allowed(captured)
            self._policy_failure_since = None
            return allowed and self._authorization_current()
        except Exception:
            if not self._authorization_current():
                self._policy_failure_since = None
            elif self._policy_failure_since is None:
                self._policy_failure_since = time.monotonic()
            return False

    def watchdog(self):
        if self._policy_ok():
            return True
        expired = not self._authorization_current()
        if (not expired and self._policy_failure_since is not None and
                time.monotonic() - self._policy_failure_since < self.policy_failure_timeout):
            self.sanitized_reason = 'policy_unavailable'
            return False
        # Latch shutdown before waiting for the mutation gate. A queued start
        # must not revive the child after this owner has decided to exit.
        self._shutdown = True
        with self.mutation_gate:
            stopped = self.stop_child()
            if stopped:
                self._transition(LifecycleState.STOPPED,
                                 'authorization_expired' if expired else 'policy_disabled')
            self.running = False
        return False

    def _launch_child(self, command):
        """Keep the creating thread alive for the child's parent-death guard.

        Linux PR_SET_PDEATHSIG follows the thread that calls Popen, not the
        supervisor process as a whole. Console jobs and IPC handlers are
        transient, so they must not directly create a long-lived CPA child.
        """
        launched = threading.Event()
        outcome = {}

        def launch():
            try:
                child = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name('process_owner.py')),
                     str(os.getpid()), *command], cwd=self.settings.instances_dir,
                    env={'HOME': str(self.settings.instances_dir), 'PATH': '/usr/bin:/bin'},
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                outcome['child'] = child
            except Exception as exc:
                outcome['error'] = exc
            finally:
                launched.set()
            if 'child' in outcome:
                # No mutation gate or operation authority is held here. Popen's
                # wait lock also permits the owner to stop/reap this same child.
                outcome['child'].wait()

        threading.Thread(target=launch, daemon=True, name='hermes-cpa-child-parent').start()
        launched.wait()
        if 'error' in outcome:
            raise outcome['error']
        return outcome['child']

    def start_child(self, *, _transaction=False):
        with self.mutation_gate:
            try:
                self.assert_ownership()
            except RuntimeError:
                self._transition(LifecycleState.OWNERSHIP_LOST, 'unowned')
                return False
            if self._shutdown or not self._policy_ok():
                self._transition(LifecycleState.FAILED, 'policy_locked')
                return False
            if self.child_process and self.child_process.poll() is None:
                return self.state == LifecycleState.READY
            journal = self.settings.config_path.with_suffix('.journal')
            if not _transaction and (journal.exists() or journal.is_symlink()):
                try:
                    fd = private_open(journal)
                    with os.fdopen(fd) as stream:
                        phase = json.loads(stream.read(4096))['phase']
                    if phase not in ('committed', 'recovered', 'aborted'):
                        raise ValueError('recovery_required')
                except Exception:
                    self._transition(LifecycleState.FAILED, 'recovery_required')
                    return False
            try:
                if self.command is None and (self.compatibility_check is None or
                                             not self.compatibility_check()):
                    raise RuntimeError('incompatible')
                command = (self.command or [str(self.settings.binary_path), '-config',
                                            str(self.settings.config_path), '-local-model'])
                command = [*command, *self._private_arguments()]
                self._reset_private_channels()
                if self._shutdown or not self._policy_ok():
                    raise RuntimeError('policy_locked')
                self._transition(LifecycleState.STARTING)
                # The user-configured public listener is CPA's own business; no
                # TCP port grants readiness or adoption authority here.
                self.child_process = self._launch_child(command)
                self.child_identity = process_identity(self.child_process.pid)
            except Exception:
                self._transition(LifecycleState.FAILED, 'launch_failed')
                return False
            deadline = time.monotonic() + self._authorized_timeout(self.readiness_timeout)
            reason = 'readiness_timeout'
            while time.monotonic() < deadline:
                if self.child_process.poll() is not None:
                    reason = 'child_exited'
                    break
                if self._shutdown or not self._policy_ok():
                    reason = 'policy_locked'
                    break
                try:
                    result = self.probe(timeout=self._authorized_timeout(
                        min(1., max(.001, deadline - time.monotonic()))))
                    if (time.monotonic() < deadline and not self._shutdown and
                            self._policy_ok() and
                            getattr(result, 'management_ready', False) is True and
                            getattr(result, 'compatible', False) is True and
                            self.child_process.poll() is None):
                        self.inference_configured = result.inference_configured
                        self._transition(LifecycleState.READY)
                        return True
                except Exception:
                    pass  # Never put upstream errors in diagnostics.
                time.sleep(min(.05, max(0., deadline - time.monotonic())))
            if not self._authorization_current():
                reason = 'authorization_expired'
            self.stop_child(grace_period=min(self.stop_grace, .5))
            self._transition(LifecycleState.FAILED, reason)
            return False

    def stop_child(self, grace_period=None):
        with self.mutation_gate:
            if self.mode != 'managed':
                return False
            child = self.child_process
            if child is None or child.poll() is not None:
                self._transition(LifecycleState.STOPPED)
                return True
            if self.lock_file is None or self.child_identity != process_identity(child.pid):
                self._transition(LifecycleState.OWNERSHIP_LOST, 'ownership_lost')
                return False
            # In-memory OAuth sessions die with the child: cancel the recorded
            # pending ones natively while the child can still answer.
            self._console_hook('cancel_pending_oauth', 'child_stopping')
            if not signal_owned(child, self.child_identity, signal.SIGTERM):
                self._transition(LifecycleState.OWNERSHIP_LOST, 'ownership_lost')
                return False
            self._transition(LifecycleState.STOPPING)
            try:
                child.wait(timeout=self.stop_grace if grace_period is None else grace_period)
            except subprocess.TimeoutExpired:
                if not signal_owned(child, self.child_identity, signal.SIGKILL):
                    self._transition(LifecycleState.OWNERSHIP_LOST, 'ownership_lost')
                    return False
                child.wait(timeout=2)
            self.inference_configured = None
            self._transition(LifecycleState.STOPPED)
            return True

    def check_health(self):
        """Refresh readiness without restarting; never hold up a mutation in progress."""
        if not self._authorization_current():
            self.watchdog()
            return
        if not self.mutation_gate.acquire(blocking=False):
            return
        try:
            if self.state not in (LifecycleState.READY, LifecycleState.DEGRADED):
                return
            self.status()
            child, probe = self.child_process, self.probe
            if child is None or probe is None:
                return
            if self.state == LifecycleState.FAILED:
                return
            try:
                self.assert_ownership()
            except RuntimeError:
                self._transition(LifecycleState.OWNERSHIP_LOST, 'ownership_lost')
                return
            timeout = self._authorized_timeout(1.)
            deadline = time.monotonic() + timeout
            healthy = False
            result = None
            try:
                result = probe(timeout=timeout)
                healthy = (time.monotonic() < deadline and not self._shutdown and
                           self._policy_ok() and result.management_ready is True and
                           result.compatible is True and child.poll() is None)
            except Exception:
                pass
            if child.poll() is not None:
                self._transition(LifecycleState.FAILED, 'child_exited')
            elif healthy and result is not None:
                self.inference_configured = result.inference_configured
                if self.state != LifecycleState.READY:
                    self._transition(LifecycleState.READY)
            elif self.state != LifecycleState.DEGRADED:
                self.inference_configured = None
                self._transition(LifecycleState.DEGRADED, 'management_unavailable')
        finally:
            self.mutation_gate.release()

    def status(self):
        if (self.child_process and self.child_process.poll() is not None and
                self.state in (LifecycleState.READY, LifecycleState.STARTING, LifecycleState.DEGRADED)):
            self._transition(LifecycleState.FAILED, 'child_exited')
        authorized = self._authorization_current()
        return {'state': self.state.value, 'generation': self.generation,
                'pid': self.child_process.pid if self.child_process and self.child_process.poll() is None else None,
                'owner': process_identity(os.getpid()), 'last_transition': self.last_transition,
                'reason': self.sanitized_reason if authorized else 'authorization_expired',
                'management_ready': authorized and self.state == LifecycleState.READY,
                'inference_configured': self.inference_configured if authorized else None,
                'lifetime': self.lifetime, 'broker': self.broker_state}

    def prepare_config(self, expected_revision, requests, timeout=10.):
        """Native canonical candidate for ordered config requests; never writes.

        A live owned child prepares over the private control channel. A stopped
        instance uses the verified binary's offline mode, so first
        configuration and repairing a config that fails to start take the
        same native path. A live but unverifiable child prepares nothing.
        """
        try:
            payload = encode_prepare(expected_revision, requests)
        except TransportError as exc:
            return {'ok': False, 'code': exc.code}
        timeout = self._authorized_timeout(timeout)
        if timeout <= 0 or self.mode != 'managed':
            return {'ok': False, 'code': 'UNAVAILABLE'}
        if self.private_peer() is not None:
            try:
                status, body = self.control.prepare(payload, timeout=timeout)
            except TransportError:
                return {'ok': False, 'code': 'UNAVAILABLE'}
            return decode_prepare(status, body)
        child = self.child_process
        if child is not None and child.poll() is None:
            return {'ok': False, 'code': 'UNAVAILABLE'}
        return self._prepare_offline(payload, timeout)

    def _prepare_offline(self, payload, timeout):
        command = self.prepare_command
        if command is None:
            try:
                if self.compatibility_check is None or not self.compatibility_check():
                    return {'ok': False, 'code': 'UNAVAILABLE'}
            except Exception:
                return {'ok': False, 'code': 'UNAVAILABLE'}
            command = [str(self.settings.binary_path), '-config', str(self.settings.config_path),
                       '--hermes-prepare-config']
        try:
            completed = subprocess.run(
                command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=timeout, cwd=self.settings.instances_dir, close_fds=True,
                env={'HOME': str(self.settings.instances_dir), 'PATH': '/usr/bin:/bin'})
        except (OSError, subprocess.SubprocessError):
            return {'ok': False, 'code': 'UNAVAILABLE'}
        status = {0: 200, 1: 500, 2: 400, 3: 409}.get(completed.returncode)
        if status is None or len(completed.stdout) > PREPARE_RESPONSE_LIMIT:
            return {'ok': False, 'code': 'UNAVAILABLE'}
        return decode_prepare(status, completed.stdout)

    def _commit_live(self, expected_revision, candidate_revision, candidate):
        """Native compare-and-commit inside the running child.

        Returns OK, STALE_REVISION, RESTART_REQUIRED, REFUSED (nothing written),
        UNAVAILABLE (nothing sent) or OUTCOME_UNKNOWN (never resubmitted).
        """
        try:
            status, body = self.control.commit(expected_revision, candidate_revision, candidate,
                                               timeout=self._authorized_timeout(10.))
        except TransportError as exc:
            return 'UNAVAILABLE' if exc.code == 'UNAVAILABLE' else 'OUTCOME_UNKNOWN'
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            data = None
        data = data if isinstance(data, dict) else {}
        if status == 200:
            return 'OK' if data.get('revision') == candidate_revision else 'OUTCOME_UNKNOWN'
        if status == 409 and data.get('code') in ('STALE_REVISION', 'RESTART_REQUIRED'):
            return data['code']
        if status in (400, 422):
            return 'REFUSED'
        # A native write failure may already have truncated the document.
        return 'OUTCOME_UNKNOWN'

    def transaction_owner(self):
        supervisor = self
        # One view is one accepted operation: reuse the submit-time capture when
        # dispatched by the worker, otherwise pin the incarnation authorized now.
        captured = getattr(self._operation_policy, 'captured', None) or self._capture_policy()

        class OwnerView:
            mutation_gate = supervisor.mutation_gate
            @property
            def mode(self):
                return supervisor.mode

            @property
            def generation(self):
                return supervisor.generation

            @property
            def running(self):
                return bool(supervisor.child_process and supervisor.child_process.poll() is None)

            @property
            def policy_locked(self):
                return (captured is None or supervisor._shutdown or
                        not supervisor._policy_ok(captured))

            def assert_owned(self):
                supervisor.assert_ownership()

            def stop_owned_and_reap(self):
                return supervisor.stop_child()

            def start_owned_and_poll_readiness(self):
                return supervisor.start_child(_transaction=True)

            def prepare_config(self, expected_revision, requests):
                return supervisor.prepare_config(expected_revision, requests)

            def commit_live(self, expected_revision, candidate_revision, candidate):
                return supervisor._commit_live(expected_revision, candidate_revision, candidate)

            def cancel_pending_oauth(self, reason):
                supervisor._console_hook('cancel_pending_oauth', reason)

            def config_committed(self, base_revision, revision, effects):
                supervisor._console_hook('config_committed', base_revision, revision, effects)
        return OwnerView()

    def upgrade_owned(self, installer, archive_path, *, expected_generation,
                      expected_revision, native_consent=False):
        """Internal native seam only; no additional IPC or REST action."""
        with self.mutation_gate:
            return installer.upgrade(archive_path, self.transaction_owner(),
                expected_generation=expected_generation, expected_revision=expected_revision,
                native_consent=native_consent)

    def prepare_removal(self, installer, *, expected_generation,
                        expected_revision, native_consent=False):
        """Retained-data preparation, not host package deletion."""
        with self.mutation_gate:
            result = installer.remove(self.transaction_owner(),
                expected_generation=expected_generation, expected_revision=expected_revision,
                native_consent=native_consent)
            if result.get('ok'):
                self._shutdown = True
                self._exit_deadline = time.monotonic() + .5
            return result

    def apply_transaction(self, request):
        """Reduced legacy ``config.apply``: three settings as native config requests.

        Validation and canonical bytes come from the native preparer; this only
        names the v8 config routes for the legacy wire's fixed fields.
        """
        with self.mutation_gate:
            self.assert_ownership()
            if not self._policy_ok():
                return {'ok': False, 'code': 'POLICY_LOCKED'}
            if request.get('expected_generation') != self.generation:
                return {'ok': False, 'code': 'STALE_GENERATION'}
            if not request.get('expected_revision'):
                return {'ok': False, 'code': 'STALE_REVISION'}
            if self.child_process and self.child_process.poll() is None and request.get('restart_consent') is not True:
                return {'ok': False, 'code': 'CONSENT_REQUIRED'}
            try:
                changes = validate_changes(request.get('changes'))
            except ValueError:
                return {'ok': False, 'code': 'INVALID_INPUT'}
            requests = [{'method': 'PUT', 'path': LEGACY_CONFIG_ROUTES[key],
                         'content_type': 'application/json',
                         'body_base64': base64.b64encode(json.dumps(value).encode()).decode()}
                        for key, value in sorted(changes.items())]
            from .config_store import ConfigStore
            result = ConfigStore(self.settings.config_path).transact(
                request['expected_revision'], requests,
                expected_generation=request['expected_generation'],
                owner=self.transaction_owner(), restart_consent=request.get('restart_consent') is True)
            code = 'INVALID_INPUT' if result['code'] in NATIVE_REFUSALS else result['code']
            return {'ok': result['ok'], 'code': code, 'revision': result['revision'],
                    'generation': result['generation'], 'rollback': result['rollback']}

    def config_transaction(self, expected_revision, requests, *, reviewed_candidate_revision,
                           restart_consent=False):
        """Full native config transaction for ONE reviewed console save.

        Only inside an accepted ``console.execute`` job: authority is the
        policy incarnation and grant captured at acceptance.
        """
        request = getattr(self._operation_policy, 'request', None)
        if not isinstance(request, dict) or request.get('action') != 'console.execute':
            return {'ok': False, 'code': 'POLICY_LOCKED'}
        if not isinstance(reviewed_candidate_revision, str) or not REVISION.match(reviewed_candidate_revision):
            return {'ok': False, 'code': 'REVIEW_STALE'}
        with self.mutation_gate:
            try:
                self.assert_ownership()
            except RuntimeError:
                return {'ok': False, 'code': 'UNAVAILABLE'}
            if self._shutdown or not self._policy_ok():
                return {'ok': False, 'code': 'POLICY_LOCKED'}
            from .config_store import ConfigStore
            return ConfigStore(self.settings.config_path).transact(
                expected_revision, requests, expected_generation=request.get('expected_generation'),
                owner=self.transaction_owner(), restart_consent=restart_consent is True,
                reviewed_candidate_revision=reviewed_candidate_revision)

    def submit(self, args):
        action = args.get('action')
        request_id = args.get('request_id')
        actions = ('start', 'stop', 'restart', 'config.apply') + (
            ('console.execute',) if self.console is not None else ())
        if action not in actions or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            return {'ok': False, 'code': 'INVALID_INPUT'}
        if self._shutdown or not self._policy_ok():
            return {'ok': False, 'code': 'POLICY_LOCKED'}
        if '_host_grant' in args:
            from .operation_guard import revalidate_grant
            intent = {key: value for key, value in args.items() if key != '_host_grant'}
            if not revalidate_grant(args['_host_grant'], intent, timeout=self._authorized_timeout(1.)):
                return {'ok': False, 'code': 'POLICY_LOCKED'}
        # Accepted-operation authority: any later config/manifest rewrite
        # revokes this request even if the long-lived service stays authorized.
        captured = self._capture_policy()
        if captured is None:
            return {'ok': False, 'code': 'POLICY_LOCKED'}
        try:
            body = json.dumps(args, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError):
            return {'ok': False, 'code': 'INVALID_INPUT'}
        # Idempotency needs only a digest; request contents stay out of the ledger.
        fingerprint = hashlib.sha256(body.encode()).hexdigest()
        with self._ledger_gate:
            for key, value in list(self._operations.items()):
                if value['result'].get('state') not in ('pending', 'running') and time.time() - value['created'] > 86400:
                    del self._operations[key]
            old = self._operations.get(request_id)
            if old:
                return dict(old['result']) if old['fingerprint'] == fingerprint else {'ok': False, 'code': 'REQUEST_CONFLICT'}
            if type(args.get('expected_generation')) is not int or args['expected_generation'] != self.generation:
                return {'ok': False, 'code': 'STALE_GENERATION'}
            if action == 'console.execute' and sum(
                    1 for value in self._operations.values()
                    if value['result']['action'] == 'console.execute' and
                    value['result']['state'] in ('pending', 'running')) >= CONSOLE_JOBS:
                return {'ok': False, 'code': 'BUSY'}
            if len(self._operations) >= 100:
                done = next((key for key, value in self._operations.items() if value['result']['state'] not in ('pending', 'running')), None)
                if done is None:
                    return {'ok': False, 'code': 'BUSY'}
                del self._operations[done]
            result = {'ok': True, 'operation_id': request_id, 'action': action, 'state': 'pending',
                      'created_at': time.time(), 'generation': self.generation}
            self._operations[request_id] = {'fingerprint': fingerprint, 'grant': args.get('_host_grant'),
                                            'created': time.time(), 'result': result, 'policy': captured}
            self._queue.put_nowait(dict(args))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, daemon=True)
                self._worker.start()
            return dict(result)

    def _finish(self, request_id, result):
        with self._ledger_gate:
            record = self._operations[request_id]['result']
            state = 'succeeded' if result.get('ok') else 'failed'
            if record['action'] == 'console.execute' and result.get('code') in ('OUTCOME_UNKNOWN', 'RECONCILE_REQUIRED'):
                state = 'unknown'
            record.update(result, state=state, completed_at=time.time())

    def _work(self):
        import queue
        while True:
            try:
                args = self._queue.get(timeout=.2)
            except queue.Empty:
                if self._shutdown:
                    return
                continue
            request_id, action = args['request_id'], args['action']
            with self._ledger_gate:
                self._operations[request_id]['result']['state'] = 'running'
                captured = self._operations[request_id].get('policy')
            if action == 'console.execute':
                # Long native/network work must not hold up lifecycle operations.
                try:
                    threading.Thread(target=self._console_job, args=(args, captured),
                                     daemon=True, name='hermes-cpa-console-job').start()
                except Exception:
                    self._console_hook('job_rejected', args, 'UNAVAILABLE')
                    self._finish(request_id, {'ok': False, 'code': 'UNAVAILABLE'})
                self._queue.task_done()
                continue
            try:
                with self.mutation_gate:
                    self._operation_policy.request = args
                    self._operation_policy.captured = captured
                    if self._shutdown or not self._policy_ok():
                        result = {'ok': False, 'code': 'POLICY_LOCKED'}
                    elif args.get('expected_generation') != self.generation:
                        result = {'ok': False, 'code': 'STALE_GENERATION'}
                    elif action == 'config.apply':
                        result = self.apply_transaction(args)
                    else:
                        ok = self.start_child() if action == 'start' else self.stop_child()
                        if action == 'restart' and ok:
                            ok = self.start_child()
                        result = {'ok': ok, 'code': 'OK' if ok else 'UNAVAILABLE'}
                        if action == 'stop' and ok:
                            self._shutdown = True
                            self._exit_deadline = time.monotonic() + .5
                    if result.get('ok') and not self._policy_ok():
                        stopped = self.stop_child()
                        result = {'ok': False, 'code': 'POLICY_LOCKED',
                                  'rollback': 'stopped' if stopped else 'stop_failed'}
            except Exception:
                result = {'ok': False, 'code': 'UNAVAILABLE'}
            finally:
                self._operation_policy.request = None
                self._operation_policy.captured = None
            self._finish(request_id, result)
            self._queue.task_done()

    def _console_job(self, args, captured):
        """One accepted console job; only a safe code reaches the generic ledger."""
        self._operation_policy.request = args
        self._operation_policy.captured = captured
        dispatched = False
        try:
            rejected = ('POLICY_LOCKED' if self._shutdown or not self._policy_ok() else
                        'STALE_GENERATION' if args.get('expected_generation') != self.generation
                        else None)
            if rejected:
                # Let the service release reservations and record its private failure.
                self._console_hook('job_rejected', args, rejected)
                result = {'ok': False, 'code': rejected}
            else:
                dispatched = True
                raw = self.console.execute_job(args)
                ok = isinstance(raw, dict) and raw.get('ok') is True
                code = raw.get('code') if isinstance(raw, dict) else None
                result = {'ok': ok,
                          'code': code if isinstance(code, str) and _RESULT_CODE.match(code)
                          else 'OK' if ok else 'OUTCOME_UNKNOWN',
                          'result_ready': isinstance(raw, dict) and raw.get('result_ready') is True}
        except Exception:
            code = 'OUTCOME_UNKNOWN' if dispatched else 'UNAVAILABLE'
            self._console_hook('job_rejected', args, code)
            result = {'ok': False, 'code': code}
        finally:
            self._operation_policy.request = None
            self._operation_policy.captured = None
        self._finish(args['request_id'], result)

    def _passive_read(self, args):
        """Fixed reduced-slice reads over the private control channel; no key."""
        from .cpa_adapter import passive_paths
        path = args.get('path')
        if not isinstance(path, str) or path not in passive_paths():
            return {'ok': False, 'code': 'INVALID_INPUT'}
        if self._shutdown or not self._policy_ok():
            return {'ok': False, 'code': 'POLICY_LOCKED'}
        route, _, query = path.partition('?')
        try:
            response = self.control.request('GET', route, query=parse_qsl(query) or None,
                                            headers={'Accept': 'application/json'},
                                            timeout=self._authorized_timeout(2.),
                                            max_response=PASSIVE_READ_LIMIT)
        except TransportError:
            return {'ok': False, 'code': 'UNAVAILABLE'}
        data = None
        if response.status == 200:
            try:
                data = json.loads(response.body)
            except ValueError:
                data = None
        return {'status': response.status, 'data': data,
                'version': response.headers.get('x-cpa-version', '')}

    def _dispatch_console(self, op, args):
        console = self.console
        if console is None:
            return {'ok': False, 'code': 'UNAVAILABLE'}
        # Receipts for already accepted work outlive revocation: the service binds
        # them to the original scope and withholds secret data without authority.
        if op not in CONSOLE_RECEIPT_OPS and (self._shutdown or not self._policy_ok()):
            return {'ok': False, 'code': 'POLICY_LOCKED'}
        try:
            reply = console.dispatch(op, args)
        except Exception:
            return {'ok': False, 'code': 'UNAVAILABLE'}
        return reply if isinstance(reply, dict) else {'ok': False, 'code': 'UNAVAILABLE'}

    def dispatch(self, req):
        cmd, args = req.get('cmd'), req.get('args', {})
        if not isinstance(args, dict):
            return {'ok': False, 'code': 'INVALID_INPUT'}
        if isinstance(cmd, str) and cmd.startswith('console.'):
            return self._dispatch_console(cmd[len('console.'):], args)
        if cmd == 'recover':
            # Native same-user private IPC only; deliberately absent from guest
            # bridge and HTTP action schemas. Recovery never starts inference.
            from .config_store import ConfigStore
            if set(args) != {'expected_revision', 'snapshot_revision', 'expected_generation', 'native_consent'}:
                return {'ok': False, 'code': 'CONSENT_REQUIRED' if args.get('native_consent') is not True else 'INVALID_INPUT'}
            return ConfigStore(self.settings.config_path).recover(
                **args, owner=self.transaction_owner())
        if cmd == 'submit':
            return self.submit(args)
        if cmd == 'status':
            return self.status()
        if cmd == 'read':
            return self._passive_read(args)
        if cmd == 'events':
            cursor = args.get('cursor', 0)
            if type(cursor) is not int or cursor < 0:
                return {'ok': False, 'code': 'INVALID_INPUT'}
            events = tuple(self._events)  # Atomic snapshot; transitions run on other threads.
            reset = bool(events and cursor < events[0]['seq'] - 1)
            return {'events': [e for e in events if e['seq'] > cursor][:100], 'reset': reset}
        if cmd == 'operation_terminal':
            # Private backend receipt: bind retirement to the exact grant and
            # current owner generation, never a renderer's terminal assertion.
            with self._ledger_gate:
                record = self._operations.get(args.get('operation_id'))
                if record is None or record['result']['state'] not in ('succeeded', 'failed'):
                    return {'terminal': False}
                return {'terminal': record['grant'] == args.get('grant') and
                        args.get('generation') == self.generation}
        if cmd == 'operation':
            with self._ledger_gate:
                record = self._operations.get(args.get('operation_id'))
                return dict(record['result']) if record else {'ok': False, 'code': 'RECONCILE_REQUIRED'}
        if cmd == 'shutdown':
            # Acknowledge the latch, not cleanup. run_ipc stops/reaps with
            # its bounded cleanup grace after sending this reply.
            # Do not block the IPC reply behind an in-flight mutation gate.
            self._shutdown = True
            self.running = False
            return {'ok': True, 'state': 'pending'}
        return {'ok': False, 'code': 'INVALID_INPUT'}

    def _start_broker(self):
        """Enter the default-owner inference broker; a conflict is reported, never adopted."""
        if self.broker is None or not self._authorization_current():
            return
        try:
            broker = self.broker(self)
            broker.__enter__()
        except Exception:
            self.broker_state = 'unavailable'
            return
        self._broker, self.broker_state = broker, 'ready'

    def _lease_expired(self):
        """Plugin lifetime ends once no registration held a lease for lease_grace."""
        if self.lifetime != 'plugin':
            return False
        import fcntl
        if self._lease_fd is None:
            self._lease_fd = private_open(self.settings.lease_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(self._lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lease_idle_since = None
            return False
        now = time.monotonic()
        if self._lease_idle_since is None:
            self._lease_idle_since = now
        if now - self._lease_idle_since < self.lease_grace:
            fcntl.flock(self._lease_fd, fcntl.LOCK_UN)  # Admit a returning registration.
            return False
        # Keep LOCK_EX until exit: a new registration waits for a fresh owner
        # instead of attaching to this one after it decided to stop.
        return True

    def _serve_connection(self, conn):
        """One owner IPC exchange on its own thread; the accept loop keeps policing.

        Ordinary commands are one JSON line (≤64 KiB). Console commands are
        length-framed: the header line bounds the body before allocation.
        """
        try:
            with conn:
                framed = False
                try:
                    peer_pid(conn)
                    deadline = time.monotonic() + self._authorized_timeout(1.)
                    line, rest = read_line(conn, ORDINARY_REQUEST_LIMIT, deadline)
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise ValueError('invalid_input')
                    cmd = req.get('cmd')
                    if isinstance(cmd, str) and cmd.startswith('console.') and 'length' in req:
                        _, length = frame_length(line)
                        framed = True
                        deadline = time.monotonic() + self._authorized_timeout(CONSOLE_FRAME_SECONDS)
                        req = {'cmd': cmd, 'args': json.loads(read_exact(conn, length, deadline, rest))}
                    elif rest:
                        raise ValueError('invalid_input')
                    resp = self.dispatch(req)
                    if framed:
                        body = json.dumps(resp, separators=(',', ':')).encode()
                        if len(body) > CONSOLE_IPC_LIMIT:
                            body = b'{"ok":false,"code":"UNAVAILABLE"}'
                        conn.settimeout(max(.001, self._authorized_timeout(CONSOLE_FRAME_SECONDS)))
                        conn.sendall(json.dumps({'length': len(body)}).encode() + b'\n' + body)
                    else:
                        conn.settimeout(1.)
                        conn.sendall(json.dumps(resp).encode() + b'\n')
                except (ValueError, OSError, RuntimeError, TypeError):
                    try:
                        conn.settimeout(.1)
                        conn.sendall(b'{"ok":false,"code":"UNAVAILABLE"}\n')
                    except OSError:
                        pass
        finally:
            self._ipc_slots.release()

    def run_ipc(self):
        if self.lock_file is None:
            raise RuntimeError('unowned')
        path = self.settings.socket_path
        if path.is_symlink() or (path.exists() and not path.is_socket()):
            raise RuntimeError('unsafe_socket')
        path.unlink(missing_ok=True)  # Lifetime lock, not stale PID, authorizes socket replacement.
        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with socket_address(self.settings.run_dir) as address:
            self.server_sock.bind(address)
        os.chmod(path, 0o600)
        self.socket_inode = path.stat().st_ino
        atomic_json(self.settings.identity_path, {'owner': process_identity(os.getpid()), 'generation': self.generation})
        self.server_sock.listen(16)
        self.server_sock.settimeout(min(.2, self.watchdog_interval))
        self.running = True
        self._start_broker()
        last_watchdog = 0.
        while self.running:
            if not self._authorization_current():
                self.watchdog()
                break
            if self._exit_deadline is not None and time.monotonic() >= self._exit_deadline:
                break
            try:
                expired = self._lease_expired()
            except (OSError, RuntimeError):
                expired = False  # Unverifiable leases never stop an owner by guess.
            if expired:
                self._shutdown = True
                break
            if time.monotonic() - last_watchdog >= self.watchdog_interval:
                self.watchdog()
                if not self._shutdown:
                    self.check_health()
                last_watchdog = time.monotonic()
            if not self.running:
                break
            self.status()
            try:
                conn, _ = self.server_sock.accept()
            except socket.timeout:
                continue
            if not self._ipc_slots.acquire(blocking=False):
                with conn:
                    try:
                        conn.settimeout(.1)
                        conn.sendall(b'{"ok":false,"code":"BUSY"}\n')
                    except OSError:
                        pass
                continue
            try:
                threading.Thread(target=self._serve_connection, args=(conn,), daemon=True,
                                 name='hermes-cpa-ipc').start()
            except Exception:
                self._ipc_slots.release()
                conn.close()
        if self._shutdown:
            self.cleanup()

    def cleanup(self):
        # run_ipc and native teardown can converge here after the latch reply.
        # Serialize the exact socket/lock release; do not infer stop from reply.
        with self._cleanup_gate:
            self._shutdown = True
            self.running = False
            if self.lock_file is not None:
                broker, self._broker = self._broker, None
                if broker is not None:
                    # Stop admissions before the upstream child disappears.
                    try:
                        broker.__exit__(None, None, None)
                    except Exception:
                        pass
                    self.broker_state = 'stopped'
                stopped = self.stop_child(grace_period=min(self.stop_grace, 2.))
                self._console_hook('close')
                if stopped:
                    # A SIGKILLed child cannot unlink its sockets; the reaped
                    # child's paths are still ours under the instance lock.
                    for private in (self.settings.control_socket_path,
                                    self.settings.inference_socket_path):
                        try:
                            if private.is_socket() and not private.is_symlink():
                                private.unlink()
                        except OSError:
                            pass
                if self.server_sock:
                    self.server_sock.close()
                path = self.settings.socket_path
                if self.socket_inode and path.exists() and not path.is_symlink() and path.stat().st_ino == self.socket_inode:
                    path.unlink()
                self.lock_file.close()
                self.lock_file = None
            # Release the closing-owner exclusive lease last, after the instance lock.
            if self._lease_fd is not None:
                os.close(self._lease_fd)
                self._lease_fd = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--package-root', required=True)
    parser.add_argument('--isolated-authorization')
    parser.add_argument('--lifetime', choices=('persistent', 'plugin'), default='persistent')
    args = parser.parse_args()
    settings = CpaSettings(Path(args.home))
    # Wiring requires the backend's scoped dependency factory; absence fails closed.
    from .controller import build_owner
    owner = build_owner(settings, Path(args.package_root),
                        isolated_authorization=args.isolated_authorization,
                        lifetime=args.lifetime)
    if not owner.acquire_lock():
        return 1
    def stop(signum, frame):
        owner.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        owner.run_ipc()
    finally:
        owner.cleanup()
    return 0


if __name__ == '__main__':
    sys.exit(main())
