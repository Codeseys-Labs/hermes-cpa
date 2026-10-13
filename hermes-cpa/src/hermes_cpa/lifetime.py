"""Remembered managed startup bound to native plugin registration lifetime.

A registration stays inert unless its profile is managed with ``autostart: true``,
the host is supported and the install policy allows it. An opted-in registration
holds one flock lease for as long as it is registered and requests startup once.
It never retries or restarts. The kernel counts leases, so concurrent native,
hosted and CLI registrations share one owner, and one registration ending never
stops services another still holds. Only a plugin-lifetime owner follows leases.
"""
import logging
import os
import threading
import uuid

from .controller import controller_from_profile, profile_options
from .process_owner import acquire_lease
from .settings import CpaSettings

log = logging.getLogger(__name__)


class RegistrationLifetime:
    # A registration that ends sooner is a command process, not a Hermes lifetime.
    DEBOUNCE = 2.
    # A closing owner keeps the lease exclusively until its instance lock is free.
    LEASE_WAIT = 15.
    READY_WAIT = 20.

    def __init__(self, home, package_root):
        self.settings = CpaSettings(home)
        self.package_root = package_root
        self.state = 'inert'
        self._gate = threading.Lock()
        self._lease = None
        self._released = threading.Event()
        self._expedite = threading.Event()
        self._done = threading.Event()
        self._done.set()

    def begin(self):
        """Decide once per registration; only opted-in managed profiles act."""
        options = profile_options(self.settings.home)
        if options.get('mode') != 'managed' or options.get('autostart') is not True:
            return self.state
        from .host_compat import require_supported_host
        try:
            require_supported_host()
        except RuntimeError as exc:
            self.state = 'unsupported_host'
            log.warning('CPA autostart skipped: %s', exc)
            return self.state
        from .supervisor import InstallPolicy
        try:
            allowed = InstallPolicy(self.settings.home, self.package_root)()
        except Exception:
            allowed = False
        if not allowed:
            self.state = 'policy_locked'
            return self.state
        self.state = 'deferred'
        self._done.clear()
        threading.Thread(target=self._run, name='hermes-cpa-autostart', daemon=True).start()
        return self.state

    def _run(self):
        try:
            lease = acquire_lease(self.settings.run_dir, timeout=self.LEASE_WAIT)
            with self._gate:
                if lease is not None and self._released.is_set():
                    os.close(lease)
                    return
                self._lease = lease
            if lease is None:
                self.state = 'lease_unavailable'
                return
            self._expedite.wait(self.DEBOUNCE)
            if self._released.is_set():
                return
            controller = controller_from_profile(self.settings, self.package_root)
            if controller.mode != 'managed':
                self.state = 'policy_locked'
                return
            # Requested process startup is not readiness; record each separately.
            self.state = 'owner_requested'
            if not controller.ensure_supervisor(lifetime='plugin'):
                self.state = 'owner_unavailable'
                return
            status = controller.snapshot()
            if status['generation'] <= 0 or status.get('reason') == 'secret_or_policy_unavailable':
                self.state = 'owner_unavailable'
                return
            if status.get('state') == 'ready':
                self.state = 'ready'
                return
            if self._released.is_set():
                return
            self.state = 'start_requested'
            # The owner serializes starts; a concurrent lifetime's start is reused.
            result = controller.wait_operation(controller.submit_operation({
                'action': 'start', 'request_id': 'autostart-' + uuid.uuid4().hex,
                'expected_generation': status['generation']}))
            self.state = 'ready' if result.get('ok') else 'start_failed'
        except Exception:
            self.state = 'failed'
        finally:
            if self.state not in ('ready', 'deferred', 'inert'):
                log.warning('CPA autostart did not reach readiness: %s; no retry', self.state)
            self._done.set()

    def await_ready(self, timeout=None):
        """Session start skips the debounce and waits, bounded, for the request."""
        self._expedite.set()
        self._done.wait(self.READY_WAIT if timeout is None else timeout)
        return self.state

    def release(self):
        """End this registration's claim. Never signals or stops the owner itself."""
        self._released.set()
        self._expedite.set()
        with self._gate:
            lease, self._lease = self._lease, None
        if lease is not None:
            os.close(lease)

    def policy_revoked(self):
        """True for disable, removal or mode change; unreadable policy is left to the owner."""
        from .supervisor import InstallPolicy
        try:
            return not InstallPolicy(self.settings.home, self.package_root)()
        except Exception:
            return False
