"""Backend-only bounded passive CPA reads. Never return upstream config or headers.

External mode reaches CPA over literal-loopback HTTP with the profile's
management key. Managed mode replaces ``_request`` with the verified owner's
private control channel (controller.OwnerReadAdapter); it holds no key.
"""
import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProbeResult:
    management_ready: bool = False
    inference_configured: bool | None = None
    compatible: bool = False
    reason: str = 'UNAVAILABLE'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def passive_paths():
    """Fixed passive management reads for the reduced projection slice."""
    from .management_features import FEATURE_PATHS
    return ('/v8/management/config', '/v8/management/observability/usage/api-keys',
            *(value for value in FEATURE_PATHS.values() if value is not None))


class CpaAdapter:
    def __init__(self, base_url='http://127.0.0.1:8318', management_key='', *, secret=None):
        parsed = urlsplit(base_url)
        try:
            valid = (parsed.scheme == 'http' and ipaddress.ip_address(parsed.hostname).is_loopback
                     and parsed.port is not None and not parsed.username and not parsed.password
                     and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError('Literal loopback HTTP origin required')
        self.base_url = base_url.rstrip('/')
        # Callable resolves on each request; do not cache vault material.
        self._secret = secret if secret is not None else lambda: management_key
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_scope(cls, base_url, scope, resolver):
        return cls(base_url, secret=lambda: resolver.resolve_management(scope))

    def _request(self, path, timeout=5.0):
        if path not in passive_paths():
            return 0, None, None
        try:
            key = self._secret()
            if not isinstance(key, str) or not key or '\n' in key or '\r' in key:
                return 0, None, None
            req = urllib.request.Request(self.base_url + path, headers={
                'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
            with self._opener.open(req, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    return 0, None, None
                version = response.headers.get('X-CPA-VERSION', '')
                return response.status, json.loads(raw), version
        except Exception:
            # No exception messages, response bodies or header values escape.
            return 0, None, None

    def probe(self):
        status, data, version = self._request('/v8/management/config')
        if status != 200 or not isinstance(data, dict):
            return ProbeResult()
        if version not in ('8.0.21', 'v8.0.21') or type(data.get('config-version')) is not int or data['config-version'] != 8:
            return ProbeResult(reason='INCOMPATIBLE')
        # Config cannot prove OAuth/provider readiness. Access keys are not providers.
        return ProbeResult(True, None, True, 'OK')

    def probe_config(self):
        """Compatibility shim deliberately excludes raw config and headers."""
        result = self.probe()
        return result.management_ready, None, {}

    def probe_usage(self):
        from .telemetry import TelemetryCollector
        status, data, version = self._request('/v8/management/observability/usage/api-keys')
        if status != 200 or version not in ('8.0.21', 'v8.0.21'):
            return False, TelemetryCollector.normalize_usage(None)
        normalized = TelemetryCollector.normalize_usage(data)
        return normalized['available'], normalized
