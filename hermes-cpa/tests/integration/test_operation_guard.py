"""Actual socket checks including an independent owner-side process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from hermes_cpa.operation_guard import OperationGuardServer, revalidate_grant
from test_lifecycle_safety import owner, ready


def test_guard_process_intent_revocation_and_disconnect(tmp_path):
    server = OperationGuardServer(tmp_path / 'guard')
    allowed = [True]
    intent = {'action': 'start', 'request_id': 'op', 'expected_generation': 1}
    grant = server.grant(intent, lambda: allowed[0])
    try:
        code = ('import json,sys; from hermes_cpa.operation_guard import revalidate_grant; '
                'g,i=json.load(sys.stdin); assert revalidate_grant(g,i)')
        subprocess.run([sys.executable, '-c', code], input=json.dumps([grant, intent]),
            text=True, timeout=5, check=True,
            env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(Path(__file__).parents[2] / 'src')})
        assert not revalidate_grant(grant, dict(intent, action='stop'))
        assert revalidate_grant(grant, intent)
        allowed[0] = False
        assert not revalidate_grant(grant, intent)
        allowed[0] = True
        assert not revalidate_grant(grant, intent)
    finally:
        server.close()
    assert not revalidate_grant(grant, intent)


def test_owner_worker_revalidates_grant_after_acceptance(owner, tmp_path):
    server = OperationGuardServer(tmp_path / 'guard')
    allowed = [True]
    owner.probe = ready
    intent = {'action': 'start', 'request_id': 'accepted', 'expected_generation': owner.generation}
    grant = server.grant(intent, lambda: allowed[0])
    try:
        # Queue behind the real gate, then revoke before execution.
        with owner.mutation_gate:
            result = owner.submit(dict(intent, _host_grant=grant))
            assert result['ok']
            allowed[0] = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = owner.dispatch({'cmd': 'operation', 'args': {'operation_id': 'accepted'}})
            if result['state'] == 'failed':
                break
            time.sleep(.02)
        assert result['code'] == 'POLICY_LOCKED', result
        assert owner.child_process is None
    finally:
        server.close()


def test_close_revokes_inflight_revalidation_and_rejects_retry(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import pytest

    server = OperationGuardServer(tmp_path / 'closing')
    entered, release = threading.Event(), threading.Event()
    intent = {'request_id': 'closing'}
    def policy():
        entered.set()
        assert release.wait(3)
        return True
    grant = server.grant(intent, policy, authority_key='same')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            checking = pool.submit(revalidate_grant, grant, intent)
            assert entered.wait(3)
            closing = pool.submit(server.close)
            assert server.closed.wait(3)
            with pytest.raises(RuntimeError, match='BUSY'):
                server.grant(intent, policy, authority_key='same')
            release.set()
            assert checking.result(timeout=4) is False
            closing.result(timeout=4)
        assert not server.path.exists()
        assert not server.records
    finally:
        release.set()
        server.close()


def test_registered_unload_closes_only_original_profile_guard(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cpa import register, host_binding
    from hermes_cpa.controller import CpaController
    from hermes_cpa.settings import CpaSettings

    home_a, home_b = tmp_path / 'a', tmp_path / 'b'
    first = OperationGuardServer(home_a / 'guard')
    second = OperationGuardServer(home_b / 'guard')
    first_grant = first.grant({'id': 'a'}, lambda: True)
    second_grant = second.grant({'id': 'b'}, lambda: True)
    monkeypatch.setattr(host_binding, '_guards', {str(home_a): first, str(home_b): second})
    controller = CpaController(CpaSettings(home_a), mode='external')
    monkeypatch.setenv('HERMES_HOME', str(home_a))
    monkeypatch.setattr('hermes_cpa.entrypoint.controller_from_profile', lambda *args: controller)
    callbacks = []
    try:
        register(SimpleNamespace(on_unload=callbacks.append, register_cli_command=lambda **kwargs: None))
        monkeypatch.setenv('HERMES_HOME', str(home_b))
        for callback in reversed(callbacks):
            callback()
        assert not revalidate_grant(first_grant, {'id': 'a'})
        assert not first.path.exists()
        assert str(home_a) not in host_binding._guards
        assert revalidate_grant(second_grant, {'id': 'b'})
        assert host_binding._guards[str(home_b)] is second
    finally:
        first.close()
        second.close()


def test_retention_preserves_pending_retry_and_revokes_evicted_grant(tmp_path):
    server = OperationGuardServer(tmp_path / 'retention')
    completed = [False]
    retired = []
    grants = []
    try:
        for index in range(100):
            intent = {'request_id': str(index)}
            grants.append(server.grant(intent, lambda: True,
                terminal=lambda: completed[0],
                retire=lambda: retired.append(True) or True))
        server.reclaim_terminal()
        assert len(server.records) == 100 and not retired
        completed[0] = True
        server.reclaim_terminal(preserve_digest=grants[0]['digest'])
        assert revalidate_grant(grants[0], {'request_id': '0'})
        assert len(server.records) == 100 and not retired
        server.reclaim_terminal()
        assert not revalidate_grant(grants[0], {'request_id': '0'})
        assert revalidate_grant(grants[1], {'request_id': '1'})
        assert len(server.records) == 99 and len(retired) == 1
        server.grant({'request_id': '101'}, lambda: True)
        assert len(server.records) == 100
    finally:
        server.close()
