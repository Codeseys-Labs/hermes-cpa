"""Shared native registration for directory and pip-package discovery."""
import logging
from pathlib import Path

from .cli import build_parser, main as cli_main
from .controller import controller_from_profile
from .lifetime import RegistrationLifetime
from .settings import CpaSettings


def register(ctx):
    """Bind callbacks to this registration's home, never the launch environment.

    External/default profiles stay inert. A managed profile with remembered
    autostart holds a lifetime lease for this registration and requests startup
    after a short debounce, or immediately on the first agent session.
    """
    home = CpaSettings().home
    package_root = Path(__file__).resolve().parents[2]
    active = True
    lifetime = RegistrationLifetime(home, package_root)
    lifetime.begin()

    def handle(args):
        if not active:
            raise RuntimeError('CPA registration unloaded')
        # A `hermes cpa ...` process is a command, not a Hermes service lifetime.
        lifetime.release()
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(home)
        try:
            return cli_main(args)
        finally:
            reset_hermes_home_override(token)

    def session_started(**_kwargs):
        if active:
            lifetime.await_ready()

    def unload():
        nonlocal active
        active = False
        from .host_binding import close_profile_guard
        close_profile_guard(home)
        # Release only this registration's lease. Services other native or hosted
        # lifetimes still hold keep running; a plugin-lifetime owner idles out.
        lifetime.release()
        if not lifetime.policy_revoked():
            return
        # Disable, removal or mode change revokes the profile for every lifetime:
        # contact only the verified existing owner, never launch on unload.
        controller = controller_from_profile(CpaSettings(home), package_root)
        # Unload can run again in an isolated child after parent revocation.
        # A verified but non-serving owner socket must not hold remount behind
        # the controller's 35s native operation deadline.
        controller.timeout = 2.
        result = controller.unload()
        if result.get('state') == 'pending' or not result.get('ok'):
            logging.getLogger(__name__).warning(
                'CPA unload cleanup pending; shutdown acknowledgement is not a reap receipt')

    ctx.on_unload(unload)
    ctx.register_cli_command(
        name='cpa', help='Manage native CPA lifecycle and configuration',
        setup_fn=build_parser, handler_fn=handle,
    )
    register_hook = getattr(ctx, 'register_hook', None)
    if callable(register_hook):
        register_hook('on_session_start', session_started)
