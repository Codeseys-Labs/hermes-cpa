"""Pip-package entrypoint; directory installs use the adjacent root wrapper.

Importing the package must stay light: worker-side helpers such as
``hermes_cpa.subscription_client`` run as the enrolled ``key_cmd`` under a bare
interpreter whose only path is this source tree, so the plugin's third-party
dependencies are not importable there.
"""


def register(ctx):
    from .entrypoint import register as _register
    return _register(ctx)
