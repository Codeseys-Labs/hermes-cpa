"""Explicit native broker-provider enrollment; never copies owner credentials."""
import json
from pathlib import Path
import shlex
import sys
import time

import httpx

from .subscription_client import grant_token, provider_runtime


def enroll(opts):
    from hermes_cli.config import atomic_config_replace, get_config_path, read_user_config_raw
    from hermes_constants import get_hermes_home

    if not opts.consent:
        print('Provider enrollment requires --consent; no settings changed.')
        return 1
    from .host_compat import require_supported_host
    try:
        require_supported_host()
    except RuntimeError as exc:
        print(str(exc))
        return 1
    try:
        home = get_hermes_home()
        control = Path(opts.control)
        if not control.is_absolute() or control.name != 'supervisor.sock':
            raise ValueError('invalid_control')
        runtime = provider_runtime(control, home, opts.base_url)
        path = get_config_path()
        before = path.read_bytes() if path.exists() else None
        config = read_user_config_raw()
        providers = config.setdefault('providers', {})
        if not isinstance(providers, dict):
            raise ValueError('invalid_providers')
        source = Path(__file__).resolve().parents[1]
        command = shlex.join(['env', 'PYTHONPATH=' + str(source), sys.executable,
                             '-m', 'hermes_cpa.subscription_client', '--control', str(control),
                             '--home', str(home), '--enrolled'])
        existing = providers.get('cpa')
        if existing is not None and (not isinstance(existing, dict) or
                existing.get('key_cmd') != command or
                existing.get('base_url') != opts.base_url or
                any(key in existing for key in ('api_key', 'key_env', 'api_key_env')) or
                existing.get('enabled') is False):
            raise ValueError('provider_conflict')
        # Strict exact endpoint, no fallback origin or implicit model invocation.
        started = time.monotonic()
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
            with client.stream('GET', runtime['base_url'] + '/models', headers={
                    'Authorization': 'Bearer ' + grant_token(control, home)}) as response:
                if response.status_code != 200:
                    raise ValueError('discovery_unavailable')
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > 1024 * 1024 or time.monotonic() - started > 10:
                        raise ValueError('discovery_limit')
        data = json.loads(raw)
        rows = data.get('data')
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError('invalid_models')
        models = {}
        for row in rows:
            model = row.get('id') if isinstance(row, dict) else None
            if (not isinstance(model, str) or not 1 <= len(model) <= 512 or
                    any(ord(c) < 32 for c in model)):
                raise ValueError('invalid_model')
            models[model] = {}
        # Preserve all unrelated settings and refuse drift while discovery ran.
        if (path.read_bytes() if path.exists() else None) != before:
            raise ValueError('config_changed')
        entry = dict(existing or {})
        entry.update(base_url=opts.base_url, key_cmd=command, models=models,
                     models_discovered=True, discover_models=True)
        providers['cpa'] = entry
        # Establish the permanent fail-closed boundary BEFORE publishing config.
        # A crash after config commit must never leave legacy minting available
        # when the entry is later removed. This is ordered publication, not a
        # two-file transaction: failure after the marker can require recovery.
        from .process_owner import atomic_json, private_directory
        private_directory(home / 'cpa')
        atomic_json(home / 'cpa/provider-enrollment.json', {'control': str(control)})
        # Marker I/O can block; do not discard an edit made during that window.
        # This optimistic guard is not CAS against arbitrary external writers.
        if (path.read_bytes() if path.exists() else None) != before:
            raise ValueError('config_changed')
        # Explicit full-state write permits removal of stale discovered models.
        atomic_config_replace(path, config)
    except Exception:
        print('Provider enrollment refused; verify broker availability and provider conflicts. No credential fallback.')
        return 1
    print(f'CPA provider ready: {len(models)} discovered models. Select custom:cpa; default model unchanged.')
    return 0


def retire(opts):
    """Keep the enrollment tombstone so removing config cannot restore legacy grants."""
    from hermes_cli.config import atomic_config_replace, get_config_path, read_user_config_raw
    from hermes_constants import get_hermes_home
    if not opts.consent:
        print('Provider retirement requires --consent; no settings changed.')
        return 1
    try:
        if not (get_hermes_home() / 'cpa/provider-enrollment.json').is_file():
            raise ValueError('not_enrolled')
        config = read_user_config_raw()
        providers = config.get('providers', {})
        if opts.subcommand == 'provider-remove':
            providers.pop('cpa', None)
        elif 'cpa' in providers:
            providers['cpa']['enabled'] = False
        atomic_config_replace(get_config_path(), config)
    except Exception:
        print('Provider retirement refused; no credential fallback.')
        return 1
    print('CPA provider retired. Owner and independent CPA process unchanged.')
    return 0
