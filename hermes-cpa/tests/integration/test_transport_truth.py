"""Missing control transport is not proof an owned child has stopped."""
from hermes_cpa.controller import CpaController
from test_detached_owner import detached


def test_unreachable_control_does_not_report_stopped(detached, monkeypatch):
    controller, _, pids = detached
    assert controller.start()
    pids.append(controller.snapshot()['pid'])
    observer = CpaController(controller.settings, mode='managed')
    monkeypatch.setattr(observer, '_call_supervisor', lambda *a, **k: None)
    snapshot = observer.snapshot()
    assert snapshot['state'] == 'failed'
    assert snapshot['reason'] == 'supervisor_unavailable'
    assert observer.get_status().supported_operations == ['status']
    assert controller.snapshot()['pid'] == pids[0]
