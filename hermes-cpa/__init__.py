"""Standalone directory entrypoint, kept in the host's profile-safe namespace.

Expose the same modules as the wheel without installing a global package alias
or adding the source tree to sys.path. Internal imports stay package-relative.
"""
from pathlib import Path

def register(ctx):
    # Delay package wiring until native registration: test collectors may import
    # a hyphenated repository's __init__.py as a non-package module.
    source = str(Path(__file__).resolve().parent / 'src' / 'hermes_cpa')
    if source not in __path__:
        __path__.append(source)
    from .entrypoint import register as register_package
    return register_package(ctx)
