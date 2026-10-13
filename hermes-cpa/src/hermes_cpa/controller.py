"""Request-scoped facade. Construction and reads never launch an owner."""
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .settings import CpaSettings
from .contracts import LifecycleState, StatusData, ConfigProjection
from .config_store import ConfigStore
from .process_owner import (private_directory, private_open, socket_address,
                                      peer_pid, process_identity, require_platform)


from .cpa_adapter import CpaAdapter


class DeadlineAdapter(CpaAdapter):
    """Bound HTTP header/body wall time; trusted secret resolver must be bounded."""
    def _request(self, path, timeout=5.):
        request_deadline = getattr(self, 'request_deadline', None)
        if request_deadline is not None:
            timeout = min(timeout, request_deadline - time.monotonic())
            if timeout <= 0:
                return 0, None, None
        import http.client
        import threading
        import urllib.request
        from .cpa_adapter import _NoRedirect
        deadline = time.monotonic() + timeout
        timers = []

        class Connection(http.client.HTTPConnection):
            def connect(connection):
                connection.timeout = max(.001, deadline - time.monotonic())
                super().connect()
                sock = connection.sock
                def expire():
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                timer = threading.Timer(max(0., deadline - time.monotonic()), expire)
                timer.daemon = True
                timers.append(timer)
                timer.start()

        class Handler(urllib.request.HTTPHandler):
            def http_open(handler, req):
                return handler.do_open(Connection, req)

        # Preserve backend authentication/projection; do not share mutable openers.
        adapter = CpaAdapter(self.base_url, secret=self._secret)
        adapter._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(), Handler())
        try:
            return adapter._request(path, timeout=timeout)
        finally:
            for timer in timers:
                timer.cancel()
                timer.join()

    def probe(self, timeout=1.):
        adapter = CpaAdapter(self.base_url, secret=self._secret)
        adapter._request = lambda path, timeout=timeout: self._request(path, timeout=timeout)
        return adapter.probe()


def config_intent(request):
    """Stable exact native intent; response-loss retries reuse the same ID."""
    import hashlib
    body = dict(request, action='config.apply')
    body['request_id'] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


class CpaController:
    def __init__(self, settings=None, *, scope=None, adapter=None, mode=None,
                 package_root=None, launch_command=None, timeout=35., isolated_authorization=None):
        self.settings = settings or CpaSettings()
        self.scope = scope
        self.mode = mode or getattr(settings, 'mode', 'external')
        self.config_store = ConfigStore(self.settings.config_path)
        self.adapter = adapter
        self.package_root = Path(package_root or Path(__file__).resolve().parents[2])
        self.launch_command = launch_command
        self.isolated_authorization = isolated_authorization
        self.timeout = timeout
        self.request_deadline: float | None = None
        self._request_snapshot: dict | None = None
        self._launched = []

    def remaining(self):
        if self.request_deadline is None:
            return self.timeout
        return max(0., min(self.timeout, self.request_deadline - time.monotonic()))

    def _call_supervisor(self, cmd, args=None):
        if self.mode != 'managed':
            return None
        timeout = self.remaining()
        if timeout <= 0:
            return None
        deadline = time.monotonic() + timeout
        try:
            fd = private_open(self.settings.identity_path)
            with os.fdopen(fd) as src:
                identity = json.load(src)
            owner = identity['owner']
            if process_identity(owner['pid']) != owner:
                return None
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(max(.001, deadline - time.monotonic()))
                with socket_address(self.settings.run_dir) as address:
                    sock.connect(address)
                if peer_pid(sock) != owner['pid'] or process_identity(owner['pid']) != owner:
                    return None
                sock.sendall(json.dumps({'cmd': cmd, 'args': args or {}}).encode() + b'\n')
                data = bytearray()
                while not data.endswith(b'\n'):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    sock.settimeout(remaining)
                    chunk = sock.recv(65536)
                    if not chunk or len(data) + len(chunk) > 1024 * 1024:
                        return None
                    data.extend(chunk)
                return json.loads(data)
        except (OSError, ValueError, KeyError, RuntimeError, TypeError):
            return None

    def ensure_supervisor(self, lifetime='persistent'):
        """Reuse the verified owner or launch one; a lock loser exits by itself.

        lifetime applies only to an owner launched here. An existing owner keeps
        its own lifetime; status reports it. Never adopts an unverified process.
        """
        if self.mode != 'managed':
            return False
        require_platform()
        if self._call_supervisor('status'):
            return True
        private_directory(self.settings.cpa_dir)
        private_directory(self.settings.run_dir)
        command = self.launch_command or [sys.executable, '-m', 'hermes_cpa.supervisor',
                    '--home', str(self.settings.home), '--package-root', str(self.package_root),
                    '--lifetime', lifetime]
        if self.isolated_authorization is not None and self.launch_command is None:
            command += ['--isolated-authorization', str(self.isolated_authorization)]
        # Carry the already-resolved backend import roots, not ambient secret
        # environment. Hosted plugin dependencies and Hermes may be separate roots.
        import_roots = [str(Path(__file__).resolve().parents[1])]
        import_roots.extend(p for p in sys.path if p and Path(p).is_absolute() and Path(p).is_dir())
        # No live secret environment inheritance, shell, boot service or gateway mutation.
        env = {'HOME': str(self.settings.home), 'HERMES_HOME': str(self.settings.home),
               'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': os.pathsep.join(dict.fromkeys(import_roots))}
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=env, start_new_session=True,
                                     close_fds=True, cwd=self.package_root)
            self._launched.append(child)
            # Reap owners we launched without tying their lifetime to this client.
            import threading
            threading.Thread(target=child.wait, daemon=True).start()
        except OSError:
            return False
        deadline = time.monotonic() + min(self.timeout, 5.)
        while time.monotonic() < deadline:
            if self._call_supervisor('status'):
                return True
            if child.poll() is not None:
                # A lock loser can exit while the winner is still binding.
                if child.returncode != 1:
                    return False
            time.sleep(.025)
        return False

    def bootstrap_owner(self):
        """Explicit native onboarding mutation, never invoked by a read.

        Host must authenticate/lock selection before invoking. Returns the new
        generation for a subsequent explicit start; does not retarget stale work.
        """
        if self.mode != 'managed':
            return {'ok': False, 'code': 'UNOWNED'}
        if not self.ensure_supervisor():
            return {'ok': False, 'code': 'UNAVAILABLE'}
        result = self.snapshot()
        if result['generation'] <= 0 or result.get('reason') == 'secret_or_policy_unavailable':
            return {'ok': False, 'code': 'UNAVAILABLE'}
        return {'ok': True, 'generation': result['generation']}

    def snapshot(self):
        if self.request_deadline is not None and self._request_snapshot is not None:
            return self._request_snapshot
        result = self._call_supervisor('status')
        if result:
            if self.request_deadline is not None:
                self._request_snapshot = result
            return result
        # Disconnection cannot establish whether an owned child is still alive.
        # Use the existing failure enum, not a fabricated stopped observation.
        return {'state': 'failed' if self.mode == 'managed' else 'external_unreachable',
                'generation': 0, 'pid': None, 'reason': 'supervisor_unavailable',
                'management_ready': False, 'inference_configured': None,
                'last_transition': time.time()}

    @property
    def generation(self):
        return self.snapshot()['generation']

    def get_status(self):
        result = self.snapshot()
        if self.mode == 'external' and self.adapter is not None:
            try:
                probe = self.adapter.probe()
                ready = (getattr(probe, 'management_ready', False) is True and
                         getattr(probe, 'compatible', False) is True)
                result.update(state='external_ready' if ready else 'external_unreachable',
                              management_ready=ready,
                              inference_configured=getattr(probe, 'inference_configured', None))
            except Exception:
                pass
        return StatusData(mode=self.mode, lifecycle=LifecycleState(result['state']), instance_id='default',
                          cpa_version='8.0.21', console_version='1.25.6',
                          management_ready=result['management_ready'],
                          inference_configured=result['inference_configured'],
                          console_compatible=False, last_transition_time=result['last_transition'],
                          sanitized_reason=result['reason'], pid=result['pid'],
                          supported_operations=['status'] if (self.mode == 'external' or
                              result['reason'] == 'supervisor_unavailable') else
                          ['start', 'stop', 'restart', 'config.read', 'config.apply'])

    def submit_operation(self, request):
        if self.mode != 'managed':
            return {'ok': False, 'code': 'UNOWNED'}
        if hasattr(request, 'model_dump'):
            request = request.model_dump()
        action = request.get('action')
        if action not in ('start', 'stop', 'restart'):
            return {'ok': False, 'code': 'INVALID_INPUT'}
        if action in ('start', 'restart') and not self.ensure_supervisor():
            return {'ok': False, 'code': 'UNAVAILABLE'}
        return self._call_supervisor('submit', request) or {'ok': False, 'code': 'UNAVAILABLE'}

    def recovery_status(self):
        """Native read-only discovery, never configuration/journal contents.

        These observations are not authority: recover rechecks both digests and
        the owner generation under the mutation gate after explicit consent.
        """
        import hashlib
        state = self.snapshot()
        report = dict(available=False, generation=state['generation'],
                      owner_available=state['generation'] > 0,
                      current_revision='', snapshot_revision='',
                      journal_phase='unavailable', recovery_required=None)
        if self.mode != 'managed':
            return report
        try:
            report['current_revision'] = self.config_store.compute_digest()
            snapshot = self.config_store._read_bytes(self.settings.config_path.with_suffix('.snapshot'))
            report['snapshot_revision'] = hashlib.sha256(snapshot).hexdigest() if snapshot else ''
            journal = self.config_store._read_bytes(self.settings.config_path.with_suffix('.journal'))
            phase = 'absent'
            if journal:
                try:
                    value = json.loads(journal)
                    phase = value.get('phase') if isinstance(value, dict) else None
                    if phase not in ('prepared', 'stopped', 'applying', 'applied',
                                     'restored', 'recovery_failed', 'recovering',
                                     'committed', 'recovered', 'aborted'):
                        phase = 'invalid'
                except (ValueError, TypeError):
                    phase = 'invalid'
            report.update(available=True, journal_phase=phase,
                          recovery_required=phase not in ('absent', 'committed', 'recovered', 'aborted'))
        except (OSError, ValueError):
            report.update(journal_phase='unavailable', recovery_required=True)
        return report

    def recover_config(self, *, expected_revision, snapshot_revision, expected_generation,
                       native_consent=False):
        if self.mode != 'managed':
            return {'ok': False, 'code': 'UNOWNED'}
        # No implicit owner spawn: a lost owner requires explicit native bootstrap.
        return self._call_supervisor('recover', dict(expected_revision=expected_revision,
            snapshot_revision=snapshot_revision, expected_generation=expected_generation,
            native_consent=native_consent)) or {'ok': False, 'code': 'RECONCILE_REQUIRED'}

    def get_operation(self, operation_id):
        return self._call_supervisor('operation', {'operation_id': operation_id}) or {'ok': False, 'code': 'RECONCILE_REQUIRED'}

    def get_events(self, cursor=None, limit=100):
        generation = self.generation
        reset = False
        try:
            if isinstance(cursor, str):
                previous, seq = cursor.split(':')
                reset = int(previous) != generation
                cursor = 0 if reset else int(seq)
            cursor = cursor or 0
        except (ValueError, TypeError):
            cursor, reset = 0, True
        result = self._call_supervisor('events', {'cursor': cursor})
        if result is None:
            if self.mode == 'managed':
                return {'ok': False, 'code': 'UNAVAILABLE'}
            result = {'events': [], 'reset': True}
        result['events'] = result['events'][:min(100, max(0, limit))]
        result['reset'] = result['reset'] or reset
        last = result['events'][-1]['seq'] if result['events'] else cursor
        result['cursor'] = f'{generation}:{last}'
        return result

    def _lifecycle(self, action):
        if self.mode != 'managed':
            return False
        if action in ('start', 'restart') and not self.ensure_supervisor():
            return False
        result = self.submit_operation({'action': action, 'request_id': str(uuid.uuid4()),
                                        'expected_generation': self.generation})
        return bool(self.wait_operation(result).get('ok'))

    def wait_operation(self, result):
        deadline = time.monotonic() + self.timeout
        while result.get('state') in ('pending', 'running') and time.monotonic() < deadline:
            time.sleep(.025)
            result = self.get_operation(result['operation_id'])
        if result.get('state') in ('pending', 'running'):
            return {'ok': False, 'code': 'TIMEOUT', 'operation_id': result['operation_id']}
        return result

    def start(self):
        return self._lifecycle('start')

    def stop(self):
        return self._lifecycle('stop')

    def restart(self):
        return self._lifecycle('restart')

    def unload(self):
        """Only a verified existing owner is contacted; never launch on unload."""
        return self._call_supervisor('shutdown') or {
            'ok': self.mode == 'external', 'code': 'RECONCILE_REQUIRED'}

    def get_config(self):
        if self.mode == 'external':
            return ConfigProjection(revision='', restart_required=False)
        if hasattr(self.config_store, 'projection'):
            return self.config_store.projection()
        cfg = self.config_store.read_config() if self.mode == 'managed' else {}
        return ConfigProjection(strategy=cfg.get('routing', {}).get('strategy', 'round-robin'),
            request_retry=cfg.get('routing', {}).get('retry', {}).get('request-retry', 3),
            request_log=cfg.get('observability', {}).get('logs', {}).get('request-log', False),
            revision=self.config_store.compute_digest() if self.mode == 'managed' else '', restart_required=True)

    @property
    def revision(self):
        return self.config_store.compute_digest() if self.mode == 'managed' else ''

    def get_feature(self, feature):
        from .management_features import FEATURE_PATHS, UNSUPPORTED, project_feature
        if feature in UNSUPPORTED:
            return project_feature(feature, None)
        if self.adapter is None or feature not in FEATURE_PATHS:
            return {'available': False}
        status, data, version = self.adapter._request(FEATURE_PATHS[feature])
        if status != 200 or version not in ('8.0.21', 'v8.0.21'):
            return {'available': False}
        return project_feature(feature, data)

    def get_metrics(self):
        from .telemetry import TelemetryCollector
        if self.adapter is not None:
            return self.adapter.probe_usage()[1]
        return TelemetryCollector.normalize_usage(None)

    def submit_config(self, request):
        if self.mode != 'managed':
            return {'ok': False, 'code': 'UNOWNED'}
        # Config wire has no request ID; derive one from revision+generation+body.
        # A retry after response loss reconciles rather than mutating twice.
        return self._call_supervisor('submit', config_intent(request)) or {'ok': False, 'code': 'UNAVAILABLE'}

    def apply_config(self, expected_revision, changes, restart_consent=False, expected_generation=None):
        if self.mode != 'managed':
            return {'ok': False, 'code': 'UNOWNED'}
        if expected_generation is None:
            return {'ok': False, 'code': 'STALE_GENERATION'}
        return self.wait_operation(self.submit_config({
            'expected_revision': expected_revision, 'changes': changes,
            'restart_consent': restart_consent, 'expected_generation': expected_generation
        }))


def profile_options(home):
    """This plugin's nonsecret settings at an explicit home; {} when unreadable."""
    from ruamel.yaml import YAML
    try:
        path = Path(home) / 'config.yaml'
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            return {}
        data = YAML(typ='safe').load(path.read_text())
        options = data.get('plugins', {}).get('entries', {}).get('hermes-cpa', {}).get('settings', {})
        return options if isinstance(options, dict) else {}
    except Exception:
        return {}


def is_default_home(home):
    """Only the default root owns the shared inference broker."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(home).resolve() == Path(get_default_hermes_root(home=home)).resolve()
    except Exception:
        return False


def controller_from_profile(settings=None, package_root=None, *, scope=None):
    """Read only nonsecret settings at the explicit host-selected home."""
    settings = settings or CpaSettings()
    from .secret_resolver import ProfileSecretResolver, native_scope
    scope = scope or native_scope(settings)
    if scope.home != settings.home:
        raise ValueError('invalid_secret_scope')
    options = profile_options(settings.home)
    mode = options.get('mode', 'external')
    adapter = None
    # The external endpoint is unset by default; reading status never discovers
    # a service or starts one. Resolve credentials only in this pinned backend.
    origin = (f"http://127.0.0.1:{options.get('managed_port', 8318)}"
              if mode == 'managed' else options.get('external_endpoint'))
    if origin:
        try:
            adapter = DeadlineAdapter.from_scope(origin, scope, ProfileSecretResolver(settings.home))
        except (ValueError, TypeError):
            pass
    # A disposable validation permit recorded by explicit setup reaches every
    # owner launch; the installer still re-verifies its expiry and bindings.
    permit = options.get('isolated_authorization')
    return CpaController(settings, scope=scope, mode=mode if mode == 'managed' else 'external',
                         package_root=package_root, adapter=adapter,
                         isolated_authorization=permit if isinstance(permit, str) and permit else None)


def build_owner(settings, package_root, *, secret_resolver=None, adapter_factory=None,
                isolated_authorization=None, lifetime='persistent'):
    """Resolve this owner's explicit home through Hermes's supported secret API.

    The optional dependencies are for generated fixture tests. Secrets never
    enter argv, IPC, identity files or the environment of the owned CPA child.
    A default-root owner with a configured broker_port also hosts the inference
    broker; it reads the inference key per admission and never copies it.
    """
    from .supervisor import CpaSupervisor, InstallPolicy
    from .installer import CpaInstaller
    policy = InstallPolicy(settings.home, package_root)
    try:
        options = profile_options(settings.home)
        mode, port = options.get('mode', 'external'), options.get('managed_port', 8318)
        if mode != 'managed' or not policy():
            raise ValueError('policy_locked')
        from .secret_resolver import ProfileSecretResolver, native_scope
        scope = native_scope(settings)
        resolver = ProfileSecretResolver(settings.home)
        def secret():
            value = (secret_resolver(settings.home) if secret_resolver is not None
                     else resolver.resolve_management(scope))
            if not isinstance(value, str) or not value or value.startswith('op://'):
                raise RuntimeError('secret_unavailable')
            return value
        secret()  # Locked/missing credential prevents even a partial CPA launch.
        origin = f'http://127.0.0.1:{port}'
        if adapter_factory is not None:
            adapter = adapter_factory(origin, secret=secret)
        elif secret_resolver is not None:
            adapter = DeadlineAdapter(origin, secret=secret)
        else:
            adapter = DeadlineAdapter.from_scope(origin, scope, resolver)
        store = ConfigStore(settings.config_path)
        def transaction(owner, request):
            return store.transact(request['expected_revision'], request['changes'],
                expected_generation=request['expected_generation'], owner=owner.transaction_owner(),
                restart_consent=request.get('restart_consent', False))
        broker = None
        broker_port = options.get('broker_port')
        if type(broker_port) is int and is_default_home(settings.home):
            from .subscription_broker import SubscriptionBroker
            broker = lambda: SubscriptionBroker(settings.home, origin, port=broker_port)
        return CpaSupervisor(settings, mode=mode, port=port, policy=policy,
            probe=adapter.probe, transaction=transaction,
            compatibility_check=CpaInstaller(settings, package_root,
                isolated_authorization=isolated_authorization).verify_pair,
            lifetime=lifetime, broker=broker)
    except Exception:
        # Start remains unavailable; reads can report failure without exposing vault errors.
        owner = CpaSupervisor(settings, mode='managed', policy=policy, lifetime=lifetime)
        owner._transition(LifecycleState.FAILED, 'secret_or_policy_unavailable')
        return owner
