"""Request-scoped facade. Construction and reads never launch an owner.

Managed mode reaches CPA only through the verified owner's IPC; the owner
alone talks to the child's private channels. No management or inference key
is resolved for a managed profile.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .settings import CpaSettings
from .contracts import LifecycleState, StatusData, ConfigProjection
from .config_store import ConfigStore
from .owner_transport import (ORDINARY_REPLY_LIMIT, ORDINARY_REQUEST_LIMIT, frame_console,
                              frame_length, read_exact, read_line)
from .process_owner import (private_directory, private_open, socket_address,
                                      peer_pid, process_identity, require_platform)


from .cpa_adapter import CpaAdapter

CONSOLE_OP = re.compile(r'[a-z][a-z0-9_]{0,31}\Z')
# Owner console dispatch is bounded to 3.5 seconds. Leave time for its framed
# reply, while staying inside the hosted ASGI child's five-second containment.
CONSOLE_RPC_SECONDS = 4.5
# Passive replies re-encode up to 1 MiB of upstream JSON; allow escaping growth.
PASSIVE_REPLY_LIMIT = 4 * 1024 * 1024


class OwnerReadAdapter(CpaAdapter):
    """Managed passive reads: the verified owner proxies its private control channel.

    Shares CpaAdapter's fixed projections; holds no origin, key or socket.
    """
    def __init__(self, controller):
        self._controller = controller

    def _request(self, path, timeout=5.):
        result = self._controller._call_supervisor('read', {'path': path},
                                                   reply_limit=PASSIVE_REPLY_LIMIT)
        if not isinstance(result, dict) or type(result.get('status')) is not int:
            return 0, None, None
        version = result.get('version')
        return result['status'], result.get('data'), version if isinstance(version, str) else ''


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

    def _call_supervisor(self, cmd, args=None, *, reply_limit=ORDINARY_REPLY_LIMIT):
        """One verified exchange; resource failures retain dispatch uncertainty.

        Ordinary commands are one JSON line each way. ``console.*`` commands are
        length-framed (≤ CONSOLE_IPC_LIMIT encoded bytes each way).
        """
        resource = cmd == 'console.resource'
        dispatched = False

        def unanswered():
            if resource:
                return {'ok': False, 'code': 'OUTCOME_UNKNOWN' if dispatched else 'UNAVAILABLE',
                        'effect_invoked': dispatched, 'response_withheld': False}
            return None

        if self.mode != 'managed':
            return unanswered()
        timeout = self.remaining()
        if timeout <= 0:
            return unanswered()
        deadline = time.monotonic() + timeout
        try:
            framed = isinstance(cmd, str) and cmd.startswith('console.')
            if framed and self.request_deadline is not None:
                deadline = min(deadline, self.request_deadline)
            if framed:
                message = frame_console(cmd, args or {})
            else:
                message = json.dumps({'cmd': cmd, 'args': args or {}}).encode() + b'\n'
                if len(message) > ORDINARY_REQUEST_LIMIT:
                    return unanswered()
            fd = private_open(self.settings.identity_path)
            with os.fdopen(fd) as src:
                identity = json.load(src)
            owner = identity['owner']
            if process_identity(owner['pid']) != owner:
                return unanswered()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                if framed and time.monotonic() >= deadline:
                    return unanswered()
                sock.settimeout(max(.001, deadline - time.monotonic()))
                with socket_address(self.settings.run_dir) as address:
                    sock.connect(address)
                if peer_pid(sock) != owner['pid'] or process_identity(owner['pid']) != owner:
                    return unanswered()
                if framed and time.monotonic() >= deadline:
                    return unanswered()
                sock.settimeout(max(.001, deadline - time.monotonic()))
                dispatched = True  # sendall may send a partial frame before raising.
                sock.sendall(message)
                if framed:
                    line, rest = read_line(sock, ORDINARY_REQUEST_LIMIT, deadline)
                    _, length = frame_length(line)
                    return json.loads(read_exact(sock, length, deadline, rest))
                line, rest = read_line(sock, reply_limit, deadline)
                return None if rest else json.loads(line)
        except (OSError, ValueError, KeyError, RuntimeError, TypeError):
            return unanswered()

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

    def console(self, op, args):
        """The single scoped console call; the owner's service does the rest.

        ``args`` (including the authenticated backend ``_scope`` and any
        ``_host_grant``) reach ``OwnerConsoleService.dispatch(op, args)``
        verbatim. No Go transport exists in this process. A lost execute reply
        requires reconciliation; a lost effectful resource reply is unknown.
        """
        if self.mode != 'managed':
            return {'ok': False, 'code': 'UNOWNED'}
        if not isinstance(op, str) or not CONSOLE_OP.match(op) or not isinstance(args, dict):
            return {'ok': False, 'code': 'INVALID_INPUT'}
        scope = args.get('_scope')
        if not isinstance(scope, dict) or scope.get('home') != str(self.settings.home):
            return {'ok': False, 'code': 'STALE_SCOPE'}
        result = self._call_supervisor('console.' + op, args)
        if op == 'resource':
            valid = isinstance(result, dict) and (
                (result.get('ok') is True and type(result.get('status')) is int and
                 isinstance(result.get('headers'), dict) and isinstance(result.get('body_base64'), str)) or
                (result.get('ok') is False and isinstance(result.get('code'), str) and
                 type(result.get('effect_invoked')) is bool and type(result.get('response_withheld')) is bool))
            if not valid:
                return {'ok': False, 'code': 'OUTCOME_UNKNOWN',
                        'effect_invoked': True, 'response_withheld': False}
        if isinstance(result, dict):
            return result
        return {'ok': False, 'code': 'RECONCILE_REQUIRED' if op == 'execute' else 'UNAVAILABLE'}

    def recovery_status(self):
        """Native read-only discovery, never configuration/journal contents.

        These observations are not authority: recover rechecks both digests and
        the owner generation under the mutation gate after explicit consent.
        """
        import hashlib
        state = self.snapshot()
        report = dict(available=False, generation=state['generation'],
                      owner_available=state['generation'] > 0,
                      current_revision='', snapshot_revision='', candidate_revision='',
                      journal_phase='unavailable', recovery_required=None)
        if self.mode != 'managed':
            return report
        try:
            report['current_revision'] = self.config_store.compute_digest()
            snapshot = self.config_store._read_bytes(self.settings.config_path.with_suffix('.snapshot'))
            report['snapshot_revision'] = hashlib.sha256(snapshot).hexdigest() if snapshot else ''
            # Exact bytes of an interrupted write, if one was journaled.
            candidate = self.config_store._read_bytes(self.config_store.candidate_path)
            report['candidate_revision'] = hashlib.sha256(candidate).hexdigest() if candidate else ''
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
    mode = 'managed' if options.get('mode', 'external') == 'managed' else 'external'
    # A disposable validation permit recorded by explicit setup reaches every
    # owner launch; the installer still re-verifies its expiry and bindings.
    permit = options.get('isolated_authorization')
    controller = CpaController(settings, scope=scope, mode=mode, package_root=package_root,
                               isolated_authorization=permit if isinstance(permit, str) and permit else None)
    if mode == 'managed':
        # No key and no public listener: the verified owner proxies fixed reads.
        controller.adapter = OwnerReadAdapter(controller)
        return controller
    # The external endpoint is unset by default; reading status never discovers
    # a service or starts one. Resolve credentials only in this pinned backend.
    origin = options.get('external_endpoint')
    if origin:
        try:
            controller.adapter = DeadlineAdapter.from_scope(origin, scope,
                                                            ProfileSecretResolver(settings.home))
        except (ValueError, TypeError):
            pass
    return controller


def build_owner(settings, package_root, *, isolated_authorization=None, lifetime='persistent',
                console_factory=None):
    """The managed owner for an explicit home; no credential is resolved.

    The CPA child is reached only through its private owner channels, so
    nothing secret enters argv, IPC, identity files, `.env` or the child's
    environment. A default-root owner with a configured broker_port also hosts
    the inference broker on the private inference channel. ``console_factory``
    defaults to the owner console service; any failure fails closed.
    """
    from .supervisor import CpaSupervisor, InstallPolicy
    from .installer import CpaInstaller
    policy = InstallPolicy(settings.home, package_root)
    try:
        options = profile_options(settings.home)
        if options.get('mode', 'external') != 'managed' or not policy():
            raise ValueError('policy_locked')
        installer = CpaInstaller(settings, package_root,
                                 isolated_authorization=isolated_authorization)
        authorization = installer.runtime_authorization()
        if console_factory is None:
            from .console_service import OwnerConsoleService as console_factory
        broker = None
        broker_port = options.get('broker_port')
        if type(broker_port) is int and is_default_home(settings.home):
            from .subscription_broker import SubscriptionBroker
            # Enrollment binds the broker endpoint, never CPA's public listener.
            broker = lambda owner: SubscriptionBroker(settings.home, owner.inference,
                                                       port=broker_port, authorization=authorization)
        return CpaSupervisor(settings, mode='managed', policy=policy,
            compatibility_check=installer.verify_pair, lifetime=lifetime, broker=broker,
            console=console_factory, authorization=authorization)
    except Exception:
        # Start remains unavailable; reads report failure without exposing causes.
        owner = CpaSupervisor(settings, mode='managed', policy=lambda: False, lifetime=lifetime)
        owner._transition(LifecycleState.FAILED, 'secret_or_policy_unavailable')
        return owner
