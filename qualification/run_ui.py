#!/usr/bin/env python3
"""Run one generated-data UI scenario in its prepared network-none container."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=('desktop', 'http', 'http-remap', 'https', 'https-bare'), required=True)
    parser.add_argument('--consent', action='store_true')
    args = parser.parse_args()
    if (not args.consent or not args.root.is_absolute() or args.root.exists() or args.root.is_symlink()
            or {entry.name for entry in Path('/sys/class/net').iterdir()} != {'lo'}):
        parser.error('Explicit consent, a fresh absolute root and a network-none namespace are required')
    q = [sys.executable, '/opt/q/scripts/qualification.py']
    result = 1
    prepared = False
    try:
        subprocess.run(q + ['prepare', '--root', str(args.root)], check=True)
        prepared = True
        subprocess.run(q + ['permit', '--root', str(args.root), '--hours', '2'], check=True)
        subprocess.run([sys.executable, '/opt/q/qualification/desktop_prepare.py',
                        '--root', str(args.root), '--consent'], check=True)
        environment = dict(os.environ, CPA_UI_ROOT=str(args.root))
        environment.pop('HERMES_UI_DASHBOARD_MODE', None)
        desktop = args.mode == 'desktop'
        if not desktop:
            environment['HERMES_UI_DASHBOARD_MODE'] = args.mode
        spec = 'hermes-cpa-parity.spec.ts' if desktop else 'hermes-cpa-dashboard-parity.spec.ts'
        command = ['node', '/opt/q/host/node_modules/@playwright/test/cli.js', 'test',
                   'e2e/' + spec, '--retries=0', '--workers=1', '--reporter=list',
                   '--output=' + str(args.root / 'ui-results')]
        if desktop:
            command = ['xvfb-run', '-a'] + command
        result = subprocess.run(command, cwd='/opt/q/host/apps/desktop', env=environment, timeout=1800).returncode
    finally:
        if prepared:
            cleanup = subprocess.run(q + ['quiesce', '--root', str(args.root), '--consent'], timeout=120)
            print(f'UI exit: {result}; cleanup exit: {cleanup.returncode}', flush=True)
            if cleanup.returncode:
                result = 1
    return result


if __name__ == '__main__':
    sys.exit(main())
