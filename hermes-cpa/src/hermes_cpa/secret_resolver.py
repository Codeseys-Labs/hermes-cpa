"""Backend-only management credentials, pinned to an authenticated/native home.

Management authority is not an inference subscription. Never borrow another
profile's credential, the ambient environment, or an inference key on a miss.
"""
from pathlib import Path

from .settings import MANAGEMENT_SECRET_ENV, RequestScope


def native_scope(settings):
    """Native CLI/owner callers have an explicit home, not a renderer selection."""
    return RequestScope('native', 'native', 0, settings.home)


class ProfileSecretResolver:
    def __init__(self, home):
        self.home = Path(home)
        if not self.home.is_absolute() or self.home.is_symlink():
            raise ValueError('invalid_secret_scope')

    def resolve_management(self, scope):
        if (not isinstance(scope, RequestScope) or scope.home != self.home or
                self.home.is_symlink()):
            raise ValueError('invalid_secret_scope')
        from agent.secret_scope import build_profile_secret_scope
        # Rebuild per read so supported external/managed sources and rotation
        # remain authoritative rather than freezing a value at plugin startup.
        value = build_profile_secret_scope(self.home).get(MANAGEMENT_SECRET_ENV)
        if (not isinstance(value, str) or not value or value.startswith('op://') or
                '\r' in value or '\n' in value):
            raise RuntimeError('secret_unavailable')
        return value
