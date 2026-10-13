import time
import psutil


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
        result = {'available': False, 'timestamp': time.time(), 'unit': 'requests',
                  'total_success': None, 'total_failed': None, 'providers': {}}
        if not isinstance(raw_usage, dict):
            return result
        providers = {}
        success = failed = 0
        # Provider names may be user-controlled compatibility names: never echo them.
        known = {'anthropic', 'openai', 'claude', 'codex', 'gemini', 'xai', 'qwen'}
        for provider, items in raw_usage.items():
            if not isinstance(items, dict):
                return result
            p_success = p_failed = 0
            for value in items.values():
                if not isinstance(value, dict):
                    return result
                s, f = value.get('success'), value.get('failed')
                if type(s) is not int or type(f) is not int or s < 0 or f < 0:
                    return result
                p_success += s
                p_failed += f
            key = provider if provider in known else 'other'
            bucket = providers.setdefault(key, {'success': 0, 'failed': 0})
            bucket['success'] += p_success
            bucket['failed'] += p_failed
            success += p_success
            failed += p_failed
        result.update(available=True, total_success=success, total_failed=failed, providers=providers)
        return result
