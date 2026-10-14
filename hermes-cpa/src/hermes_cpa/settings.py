import os
from pathlib import Path
from typing import Optional, Tuple, Protocol
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestScope:
    """Created only by an authenticated host integration, never from guest fields."""
    scope_id: str
    connection_id: str
    selection_epoch: int
    home: Path
    mode: str = 'external'
    policy_locked: bool = True

    def settings(self):
        if not self.home.is_absolute():
            raise ValueError('Explicit absolute scope required')
        return CpaSettings(self.home)


class SecretResolver(Protocol):
    def resolve_management(self, scope: RequestScope) -> str:
        """Backend-only scoped vault boundary; raise on absent/locked secret."""
        ...


MANAGEMENT_SECRET_ENV = "HERMES_CPA_MANAGEMENT_KEY"
INFERENCE_SECRET_ENV = "HERMES_CPA_INFERENCE_KEY"

def resolve_hermes_home(profile_override: Optional[str] = None) -> Path:
    if profile_override:
        return Path(os.environ.get("HOME", "/tmp")) / ".hermes" / "profiles" / profile_override
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        pass  # The pip package can also run outside a Hermes installation.
    else:
        return get_hermes_home()
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home)
    return Path(os.environ.get("HOME", "/tmp")) / ".hermes"

class CpaSettings:
    def __init__(self, hermes_home: Optional[Path] = None):
        self.home = hermes_home or resolve_hermes_home()
        self.cpa_dir = self.home / "cpa"
        self.instances_dir = self.cpa_dir / "instances" / "default"
        # The owner-channel integration build, never the stock 8.0.21 release path.
        self.bin_dir = self.cpa_dir / "bin" / "8.0.21-hermes-owner-v1"
        self.run_dir = self.cpa_dir / "run"
        self.logs_dir = self.cpa_dir / "logs"

    def ensure_directories(self):
        for directory in [self.instances_dir, self.bin_dir, self.run_dir, self.logs_dir]:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    @property
    def config_path(self) -> Path:
        return self.instances_dir / "config.yaml"

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "supervisor.sock"

    @property
    def private_dir(self) -> Path:
        """Owner-only directory for the CPA child's private Unix channels."""
        return self.run_dir / "private"

    @property
    def control_socket_path(self) -> Path:
        return self.private_dir / "control.sock"

    @property
    def inference_socket_path(self) -> Path:
        return self.private_dir / "inference.sock"

    @property
    def lock_path(self) -> Path:
        return self.run_dir / "instance.lock"

    @property
    def lease_path(self) -> Path:
        return self.run_dir / "lifetime.lock"

    @property
    def binary_path(self) -> Path:
        return self.bin_dir / "cli-proxy-api"

    @property
    def identity_path(self) -> Path:
        return self.run_dir / "identity.json"

    @property
    def journal_path(self) -> Path:
        return self.instances_dir / "recovery_journal.json"
