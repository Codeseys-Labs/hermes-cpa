"""Captured candidate authority; renewing a permit never renews its owner."""
from dataclasses import dataclass, field
import math
import time


@dataclass(frozen=True)
class AuthorizationDeadline:
    expires_at: float
    monotonic_deadline: float = field(init=False)

    def __post_init__(self):
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at):
            raise ValueError('invalid_authorization_expiry')
        # Sample monotonic first so capture cannot add time to the permit.
        now = time.monotonic()
        remaining = self.expires_at - time.time()
        object.__setattr__(self, 'monotonic_deadline', now + max(0., remaining))

    def remaining(self) -> float:
        """Neither a wall-clock rollback nor a permit rewrite adds runtime."""
        return max(0., min(self.expires_at - time.time(),
                           self.monotonic_deadline - time.monotonic()))

    def current(self) -> bool:
        return self.remaining() > 0.
