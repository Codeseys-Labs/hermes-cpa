#!/usr/bin/env python3
"""Prepare an explicitly permitted network-none root for real Desktop/Dashboard UI proof.

Besides the generated provider and API key, this installs the generated native resource
plugin (qualification/native-resource-fixture) through CPA's own plugin store, before any
application starts: a loopback registry serves the reproducibly built archive, the pinned
patched CPA owner downloads and installs it, and the installed bytes are compared with the
build. The plugin is then registered natively (one menu entry) and has never been invoked.
Nothing is placed in the plugin directory by this script.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import secrets
import sys

from console_client import ConsoleClient, q
from provider_smoke import fresh_root, free_ports, loopback_namespace, native, require


def fixture_modules():
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'native-resource-fixture'))
    import fixture_common
    import fixture_install
    return fixture_common, fixture_install


def install_native_fixture(console, root, modules, manifest):
    """Real plugin-store install of the first fixture build; returns its version."""
    common, install = modules
    version = common.VARIANTS[0][0]
    log_path = install.effect_log_path(root)
    with install.PluginRegistry(manifest) as registry:
        entry = install.install_fixture(console, registry, log_path, version)
        require(registry.artifact_requests == 1, 'plugin_store_not_exercised')
    installed = Path(entry['path'])
    installed = installed if installed.is_absolute() else root / '.hermes/cpa/instances/default' / installed
    require(installed.resolve().is_relative_to(root.resolve()) and
            common.sha256(installed.read_bytes()) == manifest['variants'][version]['sha256'],
            'installed_plugin_mismatch')
    require(install.read_log(log_path) == [], 'fixture_invoked_during_preparation')
    return version


def prepare(root, host, report, modules, manifest):
    cpa_port, broker_port = free_ports()
    report['stage'] = 'native_setup'
    native(host, 'setup', '--archive', str(root / q.ARCHIVE_NAME), '--consent',
           '--isolated-authorization', str(root / q.PERMIT), '--no-enroll',
           '--port', str(cpa_port), '--broker-port', str(broker_port))
    report['stage'] = 'console_start'
    with ConsoleClient(root, host) as console:
        report['stage'] = 'provider_config'
        console.config('api-keys/openai-compatibility', [{'name': 'qualification',
            'base-url': 'http://127.0.0.1:9/v1',
            'keys': [{'api-key': 'generated-' + secrets.token_urlsafe(32)}],
            'models': [{'name': 'qualification-model', 'alias': 'qualification-model'}]}])
        report['stage'] = 'native_fixture_install'
        version = install_native_fixture(console, root, modules, manifest)
        report['stage'] = 'provider_enroll'
        native(host, 'provider-enroll', '--base-url', f'http://127.0.0.1:{broker_port}/v1',
               '--control', str(root / '.hermes/cpa/run/subscription/supervisor.sock'), '--consent')
    report['stage'] = 'host_config'
    from hermes_cli.config import atomic_config_replace, read_user_config_raw
    config = read_user_config_raw()
    require('cpa' in config.get('providers', {}), 'registered_provider_missing')
    config['model'] = {'default': 'qualification-model', 'provider': 'custom:cpa',
                       'base_url': f'http://127.0.0.1:{broker_port}/v1', 'context_length': 64000}
    config['model_catalog'] = {'enabled': False}
    config['models_dev'] = {'url': 'http://127.0.0.1:9/metadata-unavailable'}
    config['auxiliary'] = {'title_generation': {'enabled': False}}
    atomic_config_replace(root / '.hermes/config.yaml', config)
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--host', type=Path, default=q.ROOT / 'host')
    parser.add_argument('--consent', action='store_true')
    args = parser.parse_args()
    report = {'ok': False, 'stage': 'init'}
    try:
        require(args.consent, 'consent_required')
        loopback_namespace()
        report['stage'] = 'fixture_artifacts'
        modules = fixture_modules()
        manifest = modules[0].load_manifest()  # verifies the build and the source digest; changes nothing
        report['stage'] = 'root'
        root, _ = q.load_root(args.root)
        host = q.checked_path(args.host)
        env = q.runtime_environment(root, host)
        os.environ.clear()
        os.environ.update(env)
        sys.path.insert(0, str(host))
        with q.root_lock(root):
            fresh_root(root)
            q.atomic_file(root / '.desktop-smoke-used', b'generated-ui-qualification\n')
            with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
                version = prepare(root, host, report, modules, manifest)
        report.update(ok=True, stage='complete', generated_provider='custom:cpa',
                      real_inference_requested=False, native_fixture_installed=True,
                      native_fixture_version=version, native_fixture_invoked=False)
    except BaseException:
        report['failure'] = 'desktop_fixture_preparation_failed'
    print(json.dumps(report, sort_keys=True))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
