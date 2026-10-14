"""Real native installation of the generated resource plugin through CPA's plugin store.

A loopback registry (127.0.0.1, ephemeral port) serves the generated archives; the
pinned CPA owner fetches and installs them through its ordinary plugin-store API.
Nothing is placed in the plugin directory by this module. Only meant for a
generated, explicitly permitted, network-none qualification root.

`console` is qualification/console_client.ConsoleClient (duck-typed), so a parent
UI preparation script can reuse install_fixture() unchanged:

    sys.path.insert(0, '/opt/q/qualification/native-resource-fixture')
    import fixture_install as fx
    fixture = fx.load_manifest()
    with fx.PluginRegistry(fixture) as registry, ConsoleClient(root, host) as console:
        fx.install_fixture(console, registry, fx.effect_log_path(root))
"""
import hashlib
import http.server
import json
from pathlib import Path
import threading
import time

from fixture_common import (ARTIFACT_DIR, BASE, EFFECT_LOG, ENTRY, MENU, PLUGIN_ID, VARIANTS,  # noqa: F401
                            FixtureError, archive_name, load_manifest, registry_document, sha256)

PREFIX = '/v8/management'


def effect_log_path(root):
    """Where the plugin appends its JSONL effect log inside an owned root."""
    return Path(root) / '.hermes/cpa/instances/default' / EFFECT_LOG


def source_id(url):
    """CPA's plugin store source identifier (internal/pluginstore.SourceID)."""
    return 'source-' + hashlib.sha256(url.strip().encode()).hexdigest()[:12]


class PluginRegistry:
    """Loopback plugin store registry and artifact server; never forwards anything."""

    def __init__(self, fixture):
        self.variants = fixture['variants']
        self.advertised = VARIANTS[0][0]
        self.lock = threading.Lock()
        self.registry_requests = 0
        self.artifact_requests = 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, body, content_type):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                with owner.lock:
                    version = owner.advertised
                if self.path == '/registry.json':
                    with owner.lock:
                        owner.registry_requests += 1
                    document = registry_document(
                        version, owner.artifact_url(version), owner.variants[version]['archive'])
                    return self.reply(200, json.dumps(document).encode(), 'application/json')
                for known, variant in owner.variants.items():
                    if self.path == '/artifacts/' + archive_name(known):
                        with owner.lock:
                            owner.artifact_requests += 1
                        return self.reply(200, variant['archive'], 'application/zip')
                self.reply(404, b'{"error":"not_found"}', 'application/json')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.url = self.base + '/registry.json'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def artifact_url(self, version):
        return self.base + '/artifacts/' + archive_name(version)

    @property
    def source_id(self):
        return source_id(self.url)

    def advertise(self, version):
        if version not in self.variants:
            raise FixtureError('fixture_version_unknown')
        with self.lock:
            self.advertised = version

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def read_log(path):
    """Parsed effect log events (oldest first); missing file means no events."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return []
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            raise FixtureError('effect_log_corrupt') from None
        if not isinstance(event, dict):
            raise FixtureError('effect_log_corrupt')
        events.append(event)
    return events


def effects(path, generation=None):
    return [event for event in read_log(path) if event.get('event') == 'effect' and
            (generation is None or event.get('generation') == generation)]


def _native(console, method, path, value=None, **kwargs):
    response = console.request(method, PREFIX + path, value, **kwargs)
    return response['status'], (response['body'].get('value') if response['body'].get('kind') == 'json' else None)


def plugin_entry(console):
    status, listing = _native(console, 'GET', '/plugins')
    if status != 200 or not isinstance(listing, dict):
        raise FixtureError('plugin_list_failed')
    return next((p for p in listing.get('plugins', []) if p.get('id') == PLUGIN_ID), None), listing


def wait_registered(console, version, timeout=60.):
    """Poll the native plugin list until the exact installed version is live."""
    deadline = time.monotonic() + timeout
    while True:
        entry, _ = plugin_entry(console)
        menus = (entry or {}).get('menus') or []
        if (entry and entry.get('registered') and entry.get('effective_enabled') and
                (entry.get('metadata') or {}).get('version') == version and
                len(menus) == 1 and menus[0].get('path') == ENTRY and menus[0].get('menu') == MENU):
            return entry
        if time.monotonic() >= deadline:
            raise FixtureError('plugin_not_registered')
        time.sleep(.25)


def install_version(console, registry, version):
    """One real plugin-store install of VERSION from the loopback registry."""
    registry.advertise(version)
    status, result = _native(console, 'POST', '/plugins/store/' + PLUGIN_ID + '/install',
                             query={'source': registry.source_id})
    if status != 200:
        error = result.get('error') if isinstance(result, dict) else None
        known = {'invalid_request', 'plugin_directory_invalid', 'plugin_store_source_invalid',
                 'plugin_manifest_invalid', 'plugin_update_requires_restart', 'plugin_install_failed',
                 'plugin_manifest_failed', 'config_unavailable', 'config_update_failed', 'config_save_failed',
                 'plugin_store_registry_failed', 'plugin_not_found', 'plugin_store_source_not_found',
                 'plugin_store_source_required'}
        raise FixtureError(error if isinstance(error, str) and error in known else 'plugin_install_http_failed')
    if (not isinstance(result, dict) or result.get('status') != 'installed' or
            result.get('id') != PLUGIN_ID or result.get('version') != version):
        raise FixtureError('plugin_install_response_invalid')
    return result


def install_fixture(console, registry, log_path, version=VARIANTS[0][0]):
    """Enable plugins, register the loopback store, install and verify; return the entry.

    Every step is an ordinary native console transaction or management request,
    reviewed and approved through the real owner console service.
    """
    console.config('plugins/enabled', True)
    console.config('plugins/store-sources', [registry.url])
    console.config('plugins/store-auth', [{'match': registry.base + '/',
        'apply-to': ['registry', 'artifact'], 'type': 'none', 'allow-insecure': True}])
    install_version(console, registry, version)
    wait_registered(console, version)
    console.config('plugins/configs/' + PLUGIN_ID + '/effect-log', str(log_path))
    return wait_registered(console, version)
