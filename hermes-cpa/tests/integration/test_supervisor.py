import time
from hermes_cpa.settings import CpaSettings
from hermes_cpa.supervisor import CpaSupervisor

def test_supervisor_lock_and_lifecycle(tmp_path):
    settings = CpaSettings(tmp_path)
    sup = CpaSupervisor(settings)
    assert sup.acquire_lock() is True

    sup2 = CpaSupervisor(settings)
    assert sup2.acquire_lock() is False

    sup.cleanup()
