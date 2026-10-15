"""Worker-side grants: never reads or returns the subscription owner's raw key."""
import json
from pathlib import Path
import socket
import time

from .cpa_adapter import CpaAdapter
from .process_owner import socket_address


def grant_token(control_path, home, *, enrolled=False):
    path = Path(control_path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            with socket_address(path.parent) as address:
                connection.connect(address)
            connection.sendall(json.dumps({'home': str(home), 'enrolled': enrolled}).encode() + b'\n')
            raw = bytearray()
            deadline = time.monotonic() + 2
            while not raw.endswith(b'\n'):
                connection.settimeout(max(.001, deadline - time.monotonic()))
                part = connection.recv(4096)
                if not part or len(raw) + len(part) > 8192 or time.monotonic() >= deadline:
                    raise ValueError('invalid_response')
                raw.extend(part)
            result = json.loads(raw)
            token = result.get('token')
            if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
                raise ValueError('invalid_grant')
            return token
    except Exception:
        raise RuntimeError('subscription_unavailable') from None


def provider_runtime(control_path, home, base_url):
    """Native SDK runtime; callable mints afresh per request, never a static copy."""
    if not base_url.endswith('/v1'):
        raise ValueError('invalid_broker_endpoint')
    CpaAdapter(base_url[:-3])
    path, pinned_home = Path(control_path), Path(home)
    return {'base_url': base_url, 'api_key': lambda: grant_token(path, pinned_home),
            'provider': 'custom', 'api_mode': 'chat_completions'}


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Mint a revocable local inference grant, not an upstream key')
    parser.add_argument('--control', required=True)
    parser.add_argument('--home')
    parser.add_argument('--enrolled', action='store_true')
    args = parser.parse_args()
    home = args.home
    if home is None:
        # Enrolled helpers always pin --home and run under a bare interpreter
        # where Hermes itself is not importable.
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    try:
        print(json.dumps({'access_token': grant_token(args.control, home, enrolled=args.enrolled),
                          'expires_in': 0}))
    except RuntimeError:
        parser.exit(1, 'Subscription broker unavailable; no credential fallback.\n')


if __name__ == '__main__':
    main()
