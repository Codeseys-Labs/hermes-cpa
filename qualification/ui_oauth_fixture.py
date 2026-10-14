#!/usr/bin/env python3
"""Generated UI-only OAuth authority; line protocol never contacts a real provider."""
import argparse
import json
import os
from pathlib import Path
import sys

from console_client import ConsoleClient, q
from oauth_fixture import OAuthFixture
from provider_smoke import require


def emit(value):
    print(json.dumps(value, separators=(',', ':')), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--host', type=Path, default=q.ROOT / 'host')
    parser.add_argument('--consent', action='store_true')
    args = parser.parse_args()
    try:
        require(args.consent, 'consent_required')
        root, _ = q.load_root(args.root)
        require((root / '.desktop-smoke-used').is_file(), 'prepared_ui_root_required')
        host = q.checked_path(args.host)
        environment = q.runtime_environment(root, host)
        os.environ.clear()
        os.environ.update(environment)
        with OAuthFixture() as authority:
            with ConsoleClient(root, host) as console:
                console.config('requests/proxy-url', authority.url)
            emit({'ready': True, 'email': authority.email})
            while True:
                line = sys.stdin.readline(32769)
                if not line:
                    break
                if len(line) > 32768 or not line.endswith('\n'):
                    raise ValueError('invalid_fixture_command')
                command = json.loads(line)
                if command == {'command': 'stop'}:
                    emit({'stopped': True})
                    break
                if command == {'command': 'counts'}:
                    with authority.lock:
                        emit({'exchanges': authority.exchanges, 'refreshes': authority.refreshes})
                elif (isinstance(command, dict) and set(command) == {'command', 'url'} and
                      command['command'] == 'authorize' and isinstance(command['url'], str)):
                    emit({'callback': authority.authorize(command['url'])})
                else:
                    raise ValueError('invalid_fixture_command')
        return 0
    except BaseException:
        emit({'error': 'generated_ui_oauth_fixture_failed'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
