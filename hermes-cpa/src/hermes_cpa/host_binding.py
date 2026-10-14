"""Bindings to authenticated private hosted-ASGI request capabilities."""
import atexit
import hashlib
import secrets
import threading
import time
from pathlib import Path

from .controller import CONSOLE_RPC_SECONDS, controller_from_profile
from .settings import CpaSettings, RequestScope


def read_controller(request, *, console_deadline=None):
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
    now = time.monotonic()
    deadline = now + 4. if console_deadline is None else min(now + CONSOLE_RPC_SECONDS, console_deadline)
    controller = controller_from_profile(CpaSettings(path), scope=scope)
    # Ordinary reads keep their two-second exchange cap. Console exchanges
    # must outlive the owner's 3.5-second dispatch, but not host containment.
    if console_deadline is None:
        controller.timeout = 2.
    else:
        controller.timeout = min(controller.timeout, CONSOLE_RPC_SECONDS)
        if controller.request_deadline is not None:
            deadline = min(deadline, controller.request_deadline)
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


def console_scope(request, controller, document_id, binding=None):
    """Build console authority only from the authenticated host binding."""
    import uuid
    from .console_protocol import ConsoleError
    if not isinstance(document_id, str):
        raise ConsoleError('STALE_SCOPE')
    try:
        if str(uuid.UUID(document_id)) != document_id:
            raise ValueError()
    except ValueError:
        raise ConsoleError('STALE_SCOPE') from None
    context = getattr(request.state, 'hermes_context', None)
    scope = controller.scope
    if isinstance(context, dict):
        principal = context.get('principal')
    else:
        principal = getattr(binding, 'principal', None)
    if not isinstance(principal, str) or not principal or scope is None:
        raise ConsoleError('STALE_SCOPE')
    return {'principal': principal, 'home': str(controller.settings.home),
            'scope_id': scope.scope_id, 'connection_id': scope.connection_id,
            'selection_epoch': scope.selection_epoch, 'document_id': document_id}


def console_call(request, controller, op, payload, binding=None):
    """Pin host acceptance for unlock/execute; other calls use the scoped lease.

    The host acceptance map and supervisor ledger stay authoritative. A lost
    acceptance or execute response is reconciled, never silently resubmitted.
    """
    from .console_protocol import ConsoleError, identifier
    from .console_service import execute_intent, unlock_intent
    from .operation_guard import OperationGuardServer, intent_digest
    from .supervisor import InstallPolicy
    if controller.mode != 'managed':
        raise ConsoleError('READ_ONLY')
    args = dict(payload, _scope=console_scope(request, controller, payload.get('document_id'), binding))
    operations = getattr(request.state, 'hermes_operations', None)
    if operations is None and binding is not None:
        operations = getattr(binding, 'operations', None)
    # Even direct desktop bindings must provide the host's accepted-operation
    # capability. Selection checking at one instant is not async authority.
    if operations is None:
        raise ConsoleError('UNAVAILABLE')
    if op not in ('unlock', 'execute'):
        try:
            return controller.console(op, args)
        except Exception:
            if op != 'resource':
                raise
            # The call was delegated. An exception cannot prove that the
            # resource handler did not run, and this endpoint never retries.
            return {'ok': False, 'code': 'OUTCOME_UNKNOWN',
                    'effect_invoked': True, 'response_withheld': False}
    identifier(args.get('request_id'))
    if op == 'unlock':
        if args.get('consent') is not True:
            raise ConsoleError('CONSENT_REQUIRED')
        if args.get('expected_generation') != controller.generation:
            raise ConsoleError('STALE_GENERATION')
        intent = unlock_intent(args)
    else:
        if not isinstance(args.get('lease_id'), str) or not isinstance(args.get('review_id'), str):
            raise ConsoleError('INVALID_INPUT')
        intent = execute_intent(args, controller.generation)
    policy = InstallPolicy(controller.settings.home, controller.package_root)
    captured = policy.capture()
    if captured is None:
        raise ConsoleError('POLICY_LOCKED')
    revision = controller.revision
    if not revision:
        raise ConsoleError('STALE_REVISION')
    with _guard_lock:
        server = _guards.get(str(controller.settings.home))
        if server is not None:
            server.reclaim_terminal(preserve_digest=intent_digest(intent), timeout=controller.remaining())
    try:
        accepted = operations.accept(intent['request_id'], revision, timeout=controller.remaining())
    except TimeoutError:
        raise ConsoleError('RECONCILE_REQUIRED') from None

    def current(timeout=2.):
        return operations.revalidate(accepted, timeout=timeout) and policy.matches(captured)

    if controller.remaining() <= 0:
        raise ConsoleError('RECONCILE_REQUIRED')
    try:
        if not current(controller.remaining()):
            raise ConsoleError('STALE_SCOPE')
    except TimeoutError:
        raise ConsoleError('RECONCILE_REQUIRED') from None
    with _guard_lock:
        home = str(controller.settings.home)
        server = _guards.get(home)
        if server is None:
            if len(_guards) >= 16:
                raise ConsoleError('BUSY')
            server = OperationGuardServer(controller.settings.run_dir / ('authority-' + secrets.token_hex(8)))
            _guards[home] = server
            atexit.register(server.close)

        def terminal(*, timeout=2.):
            receipt = controller_from_profile(CpaSettings(controller.settings.home))
            receipt.timeout = min(2., timeout)
            if op == 'unlock':
                response = receipt.console('authority_terminal', {
                    'request_id': intent['request_id'], '_scope': args['_scope']})
            else:
                response = receipt._call_supervisor('operation_terminal', {
                    'operation_id': intent['request_id'], 'generation': intent['expected_generation'],
                    'grant': grant})
            return bool(response and response.get('terminal') is True)

        grant = server.grant(intent, current, authority_key=accepted['capability'],
                             terminal=terminal,
                             retire=lambda timeout=2.: operations.retire(accepted, timeout=timeout))
    if controller.remaining() <= 0:
        raise ConsoleError('RECONCILE_REQUIRED')
    args['_host_grant'] = grant
    return controller.console(op, args)
