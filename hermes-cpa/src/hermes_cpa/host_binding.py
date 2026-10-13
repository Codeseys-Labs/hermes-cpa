"""Bindings to authenticated private hosted-ASGI request capabilities."""
import atexit
import hashlib
import secrets
import threading
import time
from pathlib import Path

from .controller import controller_from_profile
from .settings import CpaSettings, RequestScope


def read_controller(request):
    context = getattr(request.state, 'hermes_context', None)
    if not isinstance(context, dict) or context.get('version') != 1:
        return None
    principal, incarnation, home = (context.get(k) for k in
                                    ('principal', 'host_incarnation', 'home'))
    if (not isinstance(principal, str) or not principal or
            not isinstance(incarnation, str) or not incarnation or
            not isinstance(home, str) or not home):
        return None
    path = Path(home)
    if not path.is_absolute() or path.is_symlink():
        return None
    selection = context.get('selection')
    if selection is not None:
        if (selection.get('home') != home or selection.get('principal') != principal or
                selection.get('connection') != incarnation or
                not isinstance(selection.get('scope_id'), str) or
                type(selection.get('epoch')) is not int):
            return None
        scope_id = selection['scope_id']
    else:
        scope_id = hashlib.sha256(('\0'.join((principal, incarnation, home))).encode()).hexdigest()
    scope = RequestScope(scope_id, incarnation, selection['epoch'] if selection else 0, path)
    deadline = time.monotonic() + 4.
    controller = controller_from_profile(CpaSettings(path), scope=scope)
    # One budget covers owner IPC, management HTTP and parent authorization,
    # leaving headroom below the child's five-second dispatch containment.
    controller.timeout = 2.
    controller.request_deadline = deadline
    if controller.adapter is not None:
        controller.adapter.request_deadline = controller.request_deadline
    return scope_id, controller


_guards = {}
_guard_lock = threading.Lock()


def close_profile_guard(home):
    """Unload only the captured profile; client exit does not stop its owner."""
    with _guard_lock:
        server = _guards.get(str(home))
        if server is not None:
            server.close()
            atexit.unregister(server.close)
            del _guards[str(home)]


def submit_accepted(request, controller, action, payload):
    """Commit immutable authority before queuing, revalidate inside the owner."""
    from .controller import config_intent
    from .operation_guard import OperationGuardServer
    from .supervisor import InstallPolicy
    operations = getattr(request.state, 'hermes_operations', None)
    context = getattr(request.state, 'hermes_context', {})
    selection = context.get('selection')
    if operations is None or not selection:
        return {'ok': False, 'code': 'UNAVAILABLE'}
    if controller.mode != 'managed':
        return {'ok': False, 'code': 'READ_ONLY'}
    if payload.expected_generation != controller.generation:
        return {'ok': False, 'code': 'STALE_GENERATION'}
    revision = controller.revision
    if not revision or (action == 'apply' and payload.expected_revision != revision):
        return {'ok': False, 'code': 'STALE_REVISION'}
    if action == 'apply' and not payload.restart_consent:
        return {'ok': False, 'code': 'CONSENT_REQUIRED'}
    policy = InstallPolicy(controller.settings.home, controller.package_root)
    # Accepted hosted work pins the full policy incarnation: ANY later config or
    # manifest rewrite revokes it, even when the long-lived service survives.
    captured = policy.capture()
    if captured is None:
        return {'ok': False, 'code': 'POLICY_LOCKED'}
    intent = config_intent(payload.model_dump()) if action == 'apply' else payload.model_dump()
    # Reclaim only an owner-confirmed terminal capability before parent
    # acceptance, otherwise the parent's bounded map rejects operation 101.
    with _guard_lock:
        server = _guards.get(str(controller.settings.home))
        if server is not None:
            from .operation_guard import intent_digest
            server.reclaim_terminal(preserve_digest=intent_digest(intent),
                                    timeout=controller.remaining())
    if controller.remaining() <= 0:
        return {'ok': False, 'code': 'UNAVAILABLE'}
    try:
        accepted = operations.accept(intent['request_id'], revision, timeout=controller.remaining())
    except TimeoutError:
        # Acceptance may have completed remotely; do not claim cancellation.
        return {'ok': False, 'code': 'RECONCILE_REQUIRED'}
    def current(timeout=2.):
        return operations.revalidate(accepted, timeout=timeout) and policy.matches(captured)
    try:
        allowed = controller.remaining() > 0 and current(controller.remaining())
    except TimeoutError:
        return {'ok': False, 'code': 'RECONCILE_REQUIRED'}
    if not allowed:
        return {'ok': False, 'code': 'RECONCILE_REQUIRED'
                if controller.remaining() <= 0 else 'POLICY_LOCKED'}
    # A server is profile-bound and process-owned; its tokens never reach REST.
    with _guard_lock:
        home = str(controller.settings.home)
        server = _guards.get(home)
        if server is None:
            if len(_guards) >= 16:
                return {'ok': False, 'code': 'BUSY'}
            server = OperationGuardServer(controller.settings.run_dir / ('authority-' + secrets.token_hex(8)))
            _guards[home] = server
            atexit.register(server.close)
        def terminal(*, timeout=2.):
            # Retirement is later work, not a continuation of this HTTP budget.
            receipt = controller_from_profile(CpaSettings(controller.settings.home))
            receipt.timeout = min(2., timeout)
            result = receipt._call_supervisor('operation_terminal', {
                'operation_id': intent['request_id'], 'generation': intent['expected_generation'],
                'grant': grant})
            return bool(result and result.get('terminal') is True)
        grant = server.grant(intent, current, authority_key=accepted['capability'],
                             terminal=terminal,
                             retire=lambda timeout=2.: operations.retire(accepted, timeout=timeout))
    if controller.remaining() <= 0:
        return {'ok': False, 'code': 'RECONCILE_REQUIRED'}
    return controller._call_supervisor('submit', dict(intent, _host_grant=grant)) or {
        'ok': False, 'code': 'RECONCILE_REQUIRED'}
