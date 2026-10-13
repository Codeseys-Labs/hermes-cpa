"""Explicit native onboarding and consented managed setup.

Called by a trusted native backend, never by the untrusted console. Every refusal
that needs no write runs first: consent, supported host, dependencies, package
resources, release gate, profile policy, port availability, then the pinned
archive and executable digests. Only then are the separate management and
inference keys written. Mode is published last; a partial preparation stays
external and stopped. A second attempt refuses existing config/keys instead of
rotating them.
"""
import argparse
import fcntl
import importlib
import os
import secrets
import socket
import uuid
from pathlib import Path

from .config_store import ConfigStore
from .controller import CpaController, controller_from_profile, is_default_home
from .installer import CpaInstaller, MAX_ARCHIVE, bounded_file, download_pinned_archive
from .process_owner import private_directory, private_open, require_platform
from .settings import MANAGEMENT_SECRET_ENV, INFERENCE_SECRET_ENV

DEPENDENCIES = ('bcrypt', 'ruamel.yaml', 'httpx', 'psutil', 'pydantic')
# The unified Git directory install carries these; a wheel does not.
PACKAGE_RESOURCES = ('plugin.yaml', 'compatibility.json', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
                     'assets/cpamc.html', 'assets/cpamc.manifest.json')
UNCHANGED = ' Nothing was changed.'


class SetupRefused(ValueError):
    """Actionable refusal. str() is the stable code; message never holds secrets."""
    def __init__(self, code, message):
        super().__init__(code)
        self.code, self.message = code, message


def _release_refusal(exc):
    code = str(exc)
    if code == 'unaccepted_release':
        return SetupRefused(code, 'Packaged release metadata is not accepted. Generated-data '
                            'validation needs an explicit --isolated-authorization permit.' + UNCHANGED)
    if code == 'isolated_authorization_invalid':
        return SetupRefused(code, 'The --isolated-authorization permit is invalid, expired or bound '
                            'to another home, package or manifest.' + UNCHANGED)
    if code == 'unsupported_platform':
        return SetupRefused(code, 'Managed CPA requires Linux x86_64.' + UNCHANGED)
    return SetupRefused('package_verification_failed', f'Packaged release metadata failed '
                        f'verification ({code}); reinstall the plugin.' + UNCHANGED)


def _require_dependencies():
    missing = []
    for name in DEPENDENCIES:
        try:
            module = importlib.import_module(name)
            if name == 'bcrypt':
                # Native extension must load and hash before keys exist.
                module.hashpw(b'preflight', module.gensalt(rounds=4))
        except Exception:
            missing.append(name)
    if missing:
        raise SetupRefused('missing_dependencies', 'Missing Python dependencies: ' +
                           ', '.join(missing) + ". Install the plugin's pyproject dependencies "
                           "into the Hermes environment." + UNCHANGED)


def _require_package(package_root):
    root = Path(package_root)
    if any(not (root / name).is_file() for name in PACKAGE_RESOURCES):
        raise SetupRefused('package_incomplete', 'Managed setup requires the unified Git directory '
                           'install (plugin.yaml, compatibility.json, console assets, notices). Wheel '
                           'installs support inert registration and status only.' + UNCHANGED)


def _require_free_port(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            raise SetupRefused('port_in_use', f'Port {port} is in use by another process, possibly '
                               'an independent CPA; it is never adopted. Choose another port.'
                               + UNCHANGED) from None


def _profile_state(settings):
    """Read-only policy/credential checks; caller holds the home override."""
    from agent.secret_scope import build_profile_secret_scope
    from hermes_cli.config import load_config
    cfg = load_config()
    plugins = cfg.get('plugins', {})
    entry = plugins.get('entries', {}).get('hermes-cpa', {})
    if 'hermes-cpa' not in plugins.get('enabled', []) or entry.get('enabled') is False:
        raise SetupRefused('policy_locked', 'Enable the plugin first (hermes plugins enable '
                           'hermes-cpa).' + UNCHANGED)
    if entry.get('settings', {}).get('mode', 'external') != 'external':
        raise SetupRefused('already_managed', 'This profile is already managed; use hermes cpa '
                           'status/start. Setup never rotates credentials.' + UNCHANGED)
    existing = build_profile_secret_scope(settings.home)
    if (settings.config_path.exists() or settings.config_path.is_symlink() or
            existing.get(MANAGEMENT_SECRET_ENV) or existing.get(INFERENCE_SECRET_ENV)):
        raise SetupRefused('recovery_required', 'Existing CPA config or CPA keys were found; setup '
                           'refuses to overwrite or rotate them. Run hermes cpa doctor.' + UNCHANGED)
    return cfg


def _home_scope(settings):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    home, scope = set_hermes_home_override(settings.home), set_multiplex_context(True)

    def reset():
        reset_multiplex_context(scope)
        reset_hermes_home_override(home)
    return reset


def preflight(settings, package_root, archive=None, *, consent=False, port=8318, broker_port=None,
              download=False, isolated_authorization=None):
    """Everything refusable without a write. Returns (installer, verified executable)."""
    if consent is not True:
        raise SetupRefused('consent_required', 'Setup requires explicit --consent.' + UNCHANGED)
    ports = [port] + ([broker_port] if broker_port is not None else [])
    if (any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or
            len(set(ports)) != len(ports)):
        raise SetupRefused('invalid_port', 'Ports must be distinct integers from 1024 to 65535.'
                           + UNCHANGED)
    if (archive is None) == (download is not True):
        raise SetupRefused('archive_source', 'Choose exactly one source: --archive with a local '
                           'pinned release or --download for the fixed upstream URL.' + UNCHANGED)
    from .host_compat import require_supported_host
    try:
        require_supported_host()
        require_platform()
    except RuntimeError as exc:
        if str(exc) == 'unsupported_platform':
            raise _release_refusal(exc) from None
        raise SetupRefused('unsupported_host', str(exc)) from None
    _require_dependencies()
    _require_package(package_root)
    installer = CpaInstaller(settings, package_root, isolated_authorization=isolated_authorization)
    try:
        installer.verify_release()
    except (ValueError, RuntimeError, OSError) as exc:
        raise _release_refusal(exc) from None
    reset = _home_scope(settings)
    try:
        _profile_state(settings)
    finally:
        reset()
    for value in ports:
        _require_free_port(value)
    try:
        raw = download_pinned_archive() if download else bounded_file(archive, MAX_ARCHIVE)
    except (ValueError, OSError) as exc:
        code = str(exc)
        if download:
            raise SetupRefused('download_failed', 'Download of the pinned CPA 8.0.21 release failed '
                               f'or did not match its pin ({code}); retry or pass --archive.'
                               + UNCHANGED) from None
        raise SetupRefused('archive_unreadable', 'The --archive file is missing, unsafe or too large.'
                           + UNCHANGED) from None
    try:
        content = installer.archive_binary_bytes(raw)
        installer._verify_content(content)
    except (ValueError, RuntimeError, OSError):
        raise SetupRefused('archive_mismatch', 'The archive does not match the pinned CPA 8.0.21 '
                           'linux_amd64 release.' + UNCHANGED) from None
    return installer, content


def initialize_managed(settings, package_root, archive=None, *, consent=False, port=8318,
                       isolated_authorization=None, broker_port=None, autostart=False,
                       download=False):
    """Prepare keys, executable and CPA config, then publish managed mode. No start."""
    installer, content = preflight(settings, package_root, archive, consent=consent, port=port,
                                   broker_port=broker_port, download=download,
                                   isolated_authorization=isolated_authorization)
    from hermes_cli.config import load_config, save_config, save_env_value
    from agent.secret_scope import build_profile_secret_scope
    private_directory(settings.home)
    fd = private_open(settings.home / '.cpa-onboarding.lock', os.O_CREAT | os.O_RDWR)
    reset = _home_scope(settings)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupRefused('setup_in_progress', 'Another setup for this profile is running.'
                               + UNCHANGED) from None
        cfg = _profile_state(settings)  # Recheck under the lock, before any write.
        management, inference = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        # Host writer enforces managed/locked credential policy. Readback is
        # mandatory: some blocked writers return without raising.
        for name, value in ((MANAGEMENT_SECRET_ENV, management), (INFERENCE_SECRET_ENV, inference)):
            save_env_value(name, value)
            if build_profile_secret_scope(settings.home).get(name) != value:
                raise SetupRefused('secret_store_locked', 'The Hermes credential store refused the '
                                   'generated keys; mode remains external and nothing started. '
                                   'Run hermes cpa doctor before retrying.')
        try:
            if settings.binary_path.exists():
                installer.verify_pair()
            else:
                installer.install_binary(content)
            ConfigStore(settings.config_path).create_initial_config(port, management, inference)
        except Exception:
            raise SetupRefused('incomplete', 'Keys were written but executable or CPA config '
                               'preparation failed; mode remains external and nothing started. '
                               'Run hermes cpa doctor; do not repeat setup blindly.') from None
        # Do not commit a stale settings snapshot across preparation.
        current = load_config()
        if current != cfg:
            raise SetupRefused('policy_changed', 'Hermes config changed during setup; mode remains '
                               'external and nothing started. Run hermes cpa doctor.')
        settings_map = current.setdefault('plugins', {}).setdefault('entries', {}).setdefault(
            'hermes-cpa', {}).setdefault('settings', {})
        settings_map.update(mode='managed', managed_port=port, autostart=autostart is True)
        if broker_port is not None:
            settings_map['broker_port'] = broker_port
        if isolated_authorization is not None:
            settings_map['isolated_authorization'] = str(Path(isolated_authorization).absolute())
        save_config(current)
        return CpaController(settings, mode='managed', package_root=Path(package_root),
                             isolated_authorization=isolated_authorization)
    finally:
        reset()
        os.close(fd)


def setup_managed(settings, package_root, archive=None, *, consent=False, port=8318,
                  broker_port=8319, autostart=True, download=False, enroll=True,
                  isolated_authorization=None):
    """Consented setup: prepare, start the exact-owned CPA and broker, enroll the provider.

    Returns a truthful outcome. The owner launched here is persistent: it keeps
    running after the caller exits until explicit stop, revoked policy, or an
    unload under revoked policy. Only the default root hosts the broker.
    """
    broker_port = broker_port if is_default_home(settings.home) else None
    initialize_managed(settings, package_root, archive, consent=consent, port=port,
                       broker_port=broker_port, autostart=autostart, download=download,
                       isolated_authorization=isolated_authorization)
    outcome = dict(prepared=True, owner='unavailable', lifetime=None, cpa='not_started',
                   broker='disabled' if broker_port is None else 'unavailable',
                   provider='skipped', autostart=autostart is True, port=port,
                   broker_port=broker_port)
    try:
        _launch(settings, package_root, outcome, enroll=enroll)
    except Exception:
        outcome['error'] = 'launch_failed'  # Report what was reached; never retry here.
    return outcome


def _launch(settings, package_root, outcome, *, enroll):
    controller = controller_from_profile(settings, package_root)
    if not controller.ensure_supervisor(lifetime='persistent'):
        return
    status = controller.snapshot()
    if status['generation'] <= 0 or status.get('reason') == 'secret_or_policy_unavailable':
        return
    outcome.update(owner='running', lifetime=status.get('lifetime'),
                   broker=status.get('broker', outcome['broker']))
    outcome['cpa'] = 'start_requested'
    result = controller.wait_operation(controller.submit_operation({
        'action': 'start', 'request_id': 'setup-' + uuid.uuid4().hex,
        'expected_generation': status['generation']}))
    outcome['cpa'] = 'ready' if result.get('ok') else 'start_failed'
    if outcome['cpa'] != 'ready' or outcome['broker'] != 'ready' or not enroll:
        return
    from .provider_onboarding import enroll as enroll_provider
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(settings.home)
    try:
        code = enroll_provider(argparse.Namespace(
            base_url=f"http://127.0.0.1:{outcome['broker_port']}/v1", consent=True,
            control=str(settings.run_dir / 'subscription' / 'supervisor.sock')))
    finally:
        reset_hermes_home_override(token)
    outcome['provider'] = 'enrolled' if code == 0 else 'failed'
