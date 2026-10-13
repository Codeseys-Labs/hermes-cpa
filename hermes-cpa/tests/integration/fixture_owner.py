"""Bounded test-only owner. Never imported by the product launch path."""
import argparse
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

from hermes_cpa.settings import CpaSettings
from hermes_cpa.supervisor import CpaSupervisor, InstallPolicy

parser = argparse.ArgumentParser()
parser.add_argument('home')
parser.add_argument('package')
parser.add_argument('port', type=int)
args = parser.parse_args()
settings = CpaSettings(Path(args.home))
owner = CpaSupervisor(settings, mode='managed', port=args.port,
    probe=lambda timeout: SimpleNamespace(management_ready=True, compatible=True,
                                         inference_configured=False),
    policy=InstallPolicy(settings.home, Path(args.package)),
    command=[sys.executable, '-c', 'import time; time.sleep(15)'],
    readiness_timeout=.3, stop_grace=.2, watchdog_interval=.05, policy_failure_timeout=.2)
if not owner.acquire_lock():
    raise SystemExit(1)
signal.signal(signal.SIGTERM, lambda *_: setattr(owner, 'running', False))
# Hard ceiling even if a failed test forgets cleanup; not a recurring service.
signal.signal(signal.SIGALRM, lambda *_: setattr(owner, 'running', False))
signal.alarm(12)
try:
    owner.run_ipc()
finally:
    owner.cleanup()
