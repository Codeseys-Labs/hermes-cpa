import time
import psutil

_PROVIDERS = ('gemini', 'gemini-interactions', 'vertex', 'codex', 'claude', 'xai', 'meta', 'other')
_MAX_SAFE_INTEGER = 2 ** 53 - 1
_MAX_USAGE_ENTRIES = 1000
_BUCKET_COUNT = 20
_BUCKET_SECONDS = 600


def _valid_counts(success, failed):
    return (type(success) is int and type(failed) is int
            and 0 <= success <= _MAX_SAFE_INTEGER and 0 <= failed <= _MAX_SAFE_INTEGER
            and success + failed <= _MAX_SAFE_INTEGER)



class TelemetryCollector:
    @staticmethod
    def get_process_metrics(pid):
        result = {'available': False, 'timestamp': time.time(), 'rss_bytes': None,
                  'cpu_percent': None, 'units': {'rss_bytes': 'bytes', 'cpu_percent': 'percent'},
                  'reason': 'no_process'}
        if not pid:
            return result
        try:
            proc = psutil.Process(pid)
            result.update(available=True, rss_bytes=proc.memory_info().rss, reason=None)
            # A new psutil.Process has no prior sample; its first 0 is not a reading.
            return result
        except Exception:
            result['reason'] = 'process_unavailable'
            return result

    @staticmethod
    def normalize_usage(raw_usage):
        """Project bounded API-key entry counts, never upstream names or timestamps."""
        result = {'available': False, 'timestamp': time.time(), 'unit': 'requests',
                  'scope': 'api_key_credentials', 'credential_entries': None,
                  'total_success': None, 'total_failed': None, 'providers': [],
                  'window': {'available': False, 'bucket_seconds': _BUCKET_SECONDS, 'buckets': []}}
        if not isinstance(raw_usage, dict) or len(raw_usage) > _MAX_USAGE_ENTRIES:
            return result
        credential_entries = 0
        for provider, items in raw_usage.items():
            if not isinstance(provider, str) or not isinstance(items, dict):
                return result
            credential_entries += len(items)
            if credential_entries > _MAX_USAGE_ENTRIES:
                return result

        providers = {}
        success = failed = 0
        window_available = credential_entries > 0
        window_buckets = [{'success': 0, 'failed': 0} for _ in range(_BUCKET_COUNT)]
        window_requests = 0
        for provider, items in raw_usage.items():
            # Compatibility names may be arbitrary. Only canonical labels leave here.
            key = provider if provider in _PROVIDERS else 'other'
            for credential, value in items.items():
                if not isinstance(credential, str) or not isinstance(value, dict):
                    return result
                s, f = value.get('success'), value.get('failed')
                if not _valid_counts(s, f):
                    return result
                success += s
                failed += f
                if success + failed > _MAX_SAFE_INTEGER:
                    return result
                group = providers.get(key)
                if group is None:
                    group = {
                        'provider': key, 'credential_entries': 0, 'success': 0, 'failed': 0,
                        'buckets': [{'success': 0, 'failed': 0} for _ in range(_BUCKET_COUNT)]}
                    providers[key] = group
                group['credential_entries'] += 1
                group['success'] += s
                group['failed'] += f

                if not window_available:
                    continue
                recent = value.get('recent_requests')
                if not isinstance(recent, list) or len(recent) != _BUCKET_COUNT:
                    window_available = False
                    continue
                # The pinned upstream endpoint aligns all twenty buckets by index.
                # One bad entry invalidates every window, but not lifetime counts.
                for index, bucket in enumerate(recent):
                    if not isinstance(bucket, dict):
                        window_available = False
                        break
                    s, f = bucket.get('success'), bucket.get('failed')
                    if not _valid_counts(s, f):
                        window_available = False
                        break
                    window_requests += s + f
                    if window_requests > _MAX_SAFE_INTEGER:
                        window_available = False
                        break
                    group['buckets'][index]['success'] += s
                    group['buckets'][index]['failed'] += f
                    window_buckets[index]['success'] += s
                    window_buckets[index]['failed'] += f

        ordered = [providers[key] for key in _PROVIDERS if key in providers]
        if not window_available:
            for group in ordered:
                group['buckets'] = []
        result.update(available=True, credential_entries=credential_entries,
                      total_success=success, total_failed=failed, providers=ordered,
                      window={'available': window_available, 'bucket_seconds': _BUCKET_SECONDS,
                              'buckets': window_buckets if window_available else []})
        return result
