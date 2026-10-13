from hermes_cpa.telemetry import TelemetryCollector

def test_telemetry_normalization():
    raw = {
        "anthropic": {
            "key1": {"success": 10, "failed": 2},
            "key2": {"success": 5, "failed": 1}
        },
        "openai": {
            "key3": {"success": 20, "failed": 0}
        }
    }
    norm = TelemetryCollector.normalize_usage(raw)
    assert norm["available"] is True
    assert norm["total_success"] == 35
    assert norm["total_failed"] == 3
    assert norm["providers"]["anthropic"]["success"] == 15
