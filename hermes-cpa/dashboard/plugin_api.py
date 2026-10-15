"""Request-scoped REST seam. Host bindings are explicit and fail closed.

The host installs app.state.cpa_host, whose authenticate(request) verifies its
normal credential and returns a binding. binding.guard() holds the connection
selection lock; binding.scope is RequestScope and binding.controller is the
lifecycle-owned facade. Nothing here derives identity from client paths/env.
"""
import hashlib
import atexit
import json
import sys
import threading
import time
from pathlib import Path

# Hermes loads this file directly from an installed standalone package. Resolve
# only our adjacent packaged source; never rely on the launching shell's PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from contextlib import asynccontextmanager
from hermes_cpa.contracts import ConfigApplyRequest, OperationRequest


@asynccontextmanager
async def lifespan(app):
    # Track only homes authenticated by this app's request binding, never the
    # process environment (a hosted backend may serve several profiles).
    app.state.cpa_guard_homes = set()
    from hermes_cpa.host_binding import close_profile_guard
    try:
        yield
    finally:
        for home in app.state.cpa_guard_homes:
            close_profile_guard(home)


_guard_lock = threading.Lock()


def _close_guards(homes):
    from hermes_cpa.host_binding import close_profile_guard
    for home in list(homes):
        close_profile_guard(home)


def guard_homes(request):
    """Homes this app authenticated.

    A hosted backend hot-mounts plugin routes after startup, so the router
    lifespan never runs. Create the set on first use and close the same guards
    at process exit, exactly as the lifespan would.
    """
    state = request.app.state
    with _guard_lock:
        homes = getattr(state, 'cpa_guard_homes', None)
        if homes is None:
            homes = state.cpa_guard_homes = set()
            atexit.register(_close_guards, homes)
        return homes


router = APIRouter(lifespan=lifespan)
ASSETS = Path(__file__).resolve().parent.parent / 'assets'


class BoundaryError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


ERRORS = {'UNAUTHORIZED', 'UNAVAILABLE', 'STALE_SCOPE', 'STALE_GENERATION',
          'STALE_REVISION', 'INVALID_INPUT', 'READ_ONLY', 'POLICY_LOCKED',
          'CONSENT_REQUIRED', 'RECONCILE_REQUIRED', 'BUSY', 'CONFLICT',
          'LEASE_REQUIRED', 'LEASE_EXPIRED', 'REVIEW_REQUIRED', 'REVIEW_STALE',
          'RESOURCE_CONFLICT', 'REQUEST_CONFLICT', 'OUTCOME_UNKNOWN',
          'ROUTE_NOT_ALLOWED', 'PAYLOAD_TOO_LARGE', 'RESPONSE_TOO_LARGE',
          'BATCH_SPLIT_REQUIRED', 'OAUTH_STATE_REQUIRED', 'OAUTH_STATE_MISMATCH',
          'OAUTH_PROVIDER_MISMATCH', 'INVALID_REQUEST', 'INVALID_CONFIG',
          'NOT_FOUND', 'READ_ONLY_FIELD', 'READ_FAILED', 'RECOVERY_REQUIRED',
          'RESOURCE_GONE', 'RESOURCE_REVOKED', 'RESOURCE_PATH_DENIED', 'REDIRECT_DENIED', 'INVALID_RESPONSE'}


def error(code, status, effect=None):
    if code not in ERRORS:
        code = 'UNAVAILABLE'
    details = {'code': code, 'message': code.replace('_', ' ').capitalize(), 'retryable': False}
    if isinstance(effect, dict):
        for key in ('effect_invoked', 'response_withheld'):
            if type(effect.get(key)) is bool:
                details[key] = effect[key]
    return JSONResponse({'api_version': 1, 'error': details},
        status_code=status, headers={'Cache-Control': 'no-store'})


def plain(value):
    return value.model_dump(mode='json') if hasattr(value, 'model_dump') else value


def dispatch(request, action, payload=None):
    try:
        host = getattr(request.app.state, 'cpa_host', None)
        if host is None:
            return dispatch_hosted_read(request, action, payload)
        binding = host.authenticate(request)
        if binding is None:
            raise BoundaryError('UNAUTHORIZED', 401)
        # Host lock must include selection validation and command acceptance.
        with binding.guard():
            scope = binding.scope
            if (request.headers.get('X-CPA-Scope') != scope.scope_id or
                    request.headers.get('X-CPA-Epoch') != str(scope.selection_epoch)):
                raise BoundaryError('STALE_SCOPE')
            controller = binding.controller
            generation = controller.generation
            if payload is not None:
                if scope.mode != 'managed':
                    raise BoundaryError('READ_ONLY', 403)
                if scope.policy_locked:
                    raise BoundaryError('POLICY_LOCKED', 403)
                if payload.expected_generation != generation:
                    raise BoundaryError('STALE_GENERATION')
            if action == 'status':
                data = plain(controller.get_status())
            elif action == 'metrics':
                data = plain(controller.get_metrics())
            elif action == 'feature':
                data = controller.get_feature(request.path_params['feature'])
            elif action == 'config':
                data = plain(controller.get_config())
            elif action == 'apply':
                if payload is None or not payload.restart_consent:
                    raise BoundaryError('CONSENT_REQUIRED', 403)
                data = controller.submit_config(payload.model_dump())
            elif action == 'operation':
                if payload is None:
                    raise BoundaryError('INVALID_INPUT', 422)
                data = controller.submit_operation(payload.model_dump())
            elif action == 'query':
                data = controller.get_operation(request.path_params['operation_id'])
                if data is None:
                    raise BoundaryError('RECONCILE_REQUIRED', 404)
            elif action == 'events':
                cursor = request.query_params.get('cursor')
                if cursor is not None and (len(cursor) > 128 or not cursor.isascii()):
                    raise BoundaryError('INVALID_INPUT', 422)
                data = controller.get_events(cursor, limit=100)
            elif action == 'asset':
                raw = (ASSETS / 'cpamc.html').read_bytes()
                if len(raw) > 8 * 1024 * 1024:
                    raise BoundaryError('UNAVAILABLE', 503)
                manifest = json.loads((ASSETS / 'cpamc.manifest.json').read_bytes())
                if manifest.get('sha256') != hashlib.sha256(raw).hexdigest():
                    raise BoundaryError('UNAVAILABLE', 503)
                data = {'html': raw.decode('utf-8'), 'manifest': manifest}
            else:
                raise BoundaryError('INVALID_INPUT', 422)
            if isinstance(data, dict) and data.get('ok') is False:
                return error(data.get('code'), 409)
            binding.assert_current()
            # Facade responses are safe projections; no adapter raw JSON allowed.
            return JSONResponse({'api_version': 1, 'scope_id': scope.scope_id,
                                 'generation': controller.generation,
                                 'revision': controller.revision, 'data': plain(data)},
                                status_code=202 if action in ('apply', 'operation') else 200,
                                headers={'Cache-Control': 'no-store'})
    except BoundaryError as exc:
        return error(exc.code, exc.status)
    except Exception:
        return error('UNAVAILABLE', 503)


def dispatch_hosted_read(request, action, payload):
    from hermes_cpa.host_binding import read_controller
    binding = read_controller(request)
    if binding is None:
        return error('UNAVAILABLE', 503)
    if action not in ('status', 'asset', 'config', 'metrics', 'feature', 'query', 'events', 'apply', 'operation'):
        return error('UNAVAILABLE', 503)
    scope_id, controller = binding
    if payload is not None:
        guard_homes(request).add(controller.settings.home)
        from hermes_cpa.host_binding import submit_accepted
        data = submit_accepted(request, controller, action, payload)
        if data.get('ok') is False:
            return error(data.get('code'), 503 if data.get('code') == 'UNAVAILABLE' else 409)
        return JSONResponse({'api_version': 1, 'scope_id': scope_id,
                             'generation': data.get('generation', payload.expected_generation),
                             'revision': controller.revision, 'data': data},
                            status_code=202, headers={'Cache-Control': 'no-store'})
    if action == 'status':
        data = plain(controller.get_status())
    elif action == 'config':
        data = plain(controller.get_config())
    elif action == 'feature':
        data = controller.get_feature(request.path_params['feature'])
    elif action == 'metrics':
        data = plain(controller.get_metrics())
    elif action == 'query':
        data = controller.get_operation(request.path_params['operation_id'])
    elif action == 'events':
        cursor = request.query_params.get('cursor')
        if cursor is not None and (len(cursor) > 128 or not cursor.isascii()):
            return error('INVALID_INPUT', 422)
        data = controller.get_events(cursor, limit=100)
    else:
        raw = (ASSETS / 'cpamc.html').read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            return error('UNAVAILABLE', 503)
        manifest = json.loads((ASSETS / 'cpamc.manifest.json').read_bytes())
        if manifest.get('sha256') != hashlib.sha256(raw).hexdigest():
            return error('UNAVAILABLE', 503)
        data = {'html': raw.decode('utf-8'), 'manifest': manifest}
    if isinstance(data, dict) and data.get('ok') is False:
        return error(data.get('code'), 404 if action == 'query' else 409)
    generation = controller.generation
    if controller.remaining() <= 0:
        return error('UNAVAILABLE', 503)
    return JSONResponse({'api_version': 1, 'scope_id': scope_id,
                         'generation': generation,
                         'revision': controller.revision, 'data': data},
                        headers={'Cache-Control': 'no-store'})


async def mutation(request, schema, action):
    # Do not allow FastAPI's default validation errors to echo secret input.
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                return error('INVALID_INPUT', 413)
        payload = schema.model_validate_json(raw)
    except (ValidationError, ValueError):
        return error('INVALID_INPUT', 422)
    return dispatch(request, action, payload)


@router.get('/status')
def status(request: Request):
    return dispatch(request, 'status')


@router.get('/metrics')
def metrics(request: Request):
    return dispatch(request, 'metrics')


@router.get('/config')
def config(request: Request):
    return dispatch(request, 'config')


@router.put('/config')
async def apply(request: Request):
    return await mutation(request, ConfigApplyRequest, 'apply')


@router.post('/operations')
async def operation(request: Request):
    return await mutation(request, OperationRequest, 'operation')


@router.get('/operations/{operation_id}')
def query(request: Request):
    return dispatch(request, 'query')


@router.get('/events')
def events(request: Request):
    return dispatch(request, 'events')


@router.get('/features/{feature}')
def feature(request: Request):
    from hermes_cpa.management_features import FEATURE_PATHS
    if request.path_params['feature'] not in FEATURE_PATHS:
        return error('INVALID_INPUT', 422)
    return dispatch(request, 'feature')


@router.get('/console/asset')
def asset(request: Request):
    return dispatch(request, 'asset')


def console_dispatch(request, op, payload, deadline=None):
    from hermes_cpa.console_protocol import ConsoleError
    from hermes_cpa.host_binding import console_call, read_controller
    from hermes_cpa.controller import CONSOLE_RPC_SECONDS
    deadline = min(time.monotonic() + CONSOLE_RPC_SECONDS, deadline) if deadline is not None else (
        time.monotonic() + CONSOLE_RPC_SECONDS)
    data = None

    def resource_effect():
        if op != 'resource' or not isinstance(data, dict):
            return None
        if data.get('ok') is True:
            return {'effect_invoked': True, 'response_withheld': True}
        return {'effect_invoked': data.get('effect_invoked', True),
                'response_withheld': data.get('response_withheld', False)}

    try:
        host = getattr(request.app.state, 'cpa_host', None)
        if host is not None:
            binding = host.authenticate(request)
            if binding is None:
                raise BoundaryError('UNAUTHORIZED', 401)
            with binding.guard():
                scope, controller = binding.scope, binding.controller
                if (request.headers.get('X-CPA-Scope') != scope.scope_id or
                        request.headers.get('X-CPA-Epoch') != str(scope.selection_epoch)):
                    raise BoundaryError('STALE_SCOPE')
                if scope.mode != 'managed':
                    raise BoundaryError('READ_ONLY', 403)
                if scope.policy_locked and op not in ('result', 'resource_close'):
                    raise BoundaryError('POLICY_LOCKED', 403)
                guard_homes(request).add(controller.settings.home)
                data = console_call(request, controller, op, payload, binding)
                binding.assert_current()
                scope_id = scope.scope_id
        else:
            found = read_controller(request, console_deadline=deadline)
            if found is None:
                raise BoundaryError('UNAUTHORIZED', 401)
            scope_id, controller = found
            guard_homes(request).add(controller.settings.home)
            data = console_call(request, controller, op, payload)
        if data.get('ok') is False:
            return error(data.get('code'), 409, data if op == 'resource' else None)
        return JSONResponse({'api_version': 1, 'scope_id': scope_id,
                             'generation': controller.generation,
                             'revision': controller.revision, 'data': data},
                            status_code=202 if op == 'execute' else 200,
                            headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
    except (BoundaryError, ConsoleError) as exc:
        effect = resource_effect()
        code = ('OUTCOME_UNKNOWN' if effect and effect['effect_invoked'] and
                not effect['response_withheld'] else exc.code)
        return error(code, getattr(exc, 'status', 409), effect)
    except Exception:
        effect = resource_effect()
        return error('OUTCOME_UNKNOWN' if effect is not None else 'UNAVAILABLE', 503, effect)


async def console_mutation(request, op):
    from hermes_cpa.console_protocol import WIRE_LIMIT
    from starlette.concurrency import run_in_threadpool
    from hermes_cpa.controller import CONSOLE_RPC_SECONDS
    deadline = time.monotonic() + CONSOLE_RPC_SECONDS
    # Profile selects the host's authenticated context, not console authority.
    # Capability identifiers still never enter query strings or access logs.
    if (set(request.query_params) - {'profile'} or
            len(request.query_params.getlist('profile')) > 1):
        return error('INVALID_INPUT', 422)
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > WIRE_LIMIT:
                return error('PAYLOAD_TOO_LARGE', 413)
            raw.extend(chunk)
        payload = json.loads(raw)
        if (not isinstance(payload, dict) or
                any(not isinstance(k, str) or k.startswith('_') for k in payload)):
            return error('INVALID_INPUT', 422)
        if not isinstance(payload.get('document_id'), str):
            return error('STALE_SCOPE', 409)
    except (ValueError, TypeError, RecursionError):
        return error('INVALID_INPUT', 422)
    return await run_in_threadpool(console_dispatch, request, op, payload, deadline)


@router.post('/console/unlock')
async def console_unlock(request: Request):
    return await console_mutation(request, 'unlock')


@router.post('/console/lock')
async def console_lock(request: Request):
    return await console_mutation(request, 'lock')


@router.post('/console/prepare')
async def console_prepare(request: Request):
    return await console_mutation(request, 'prepare')


@router.post('/console/execute')
async def console_execute(request: Request):
    return await console_mutation(request, 'execute')


@router.post('/console/result')
async def console_result(request: Request):
    return await console_mutation(request, 'result')


@router.post('/console/browser')
async def console_browser(request: Request):
    return await console_mutation(request, 'browser')


@router.post('/console/plugin')
async def console_plugin(request: Request):
    return await console_mutation(request, 'plugin')


@router.post('/console/file')
async def console_file(request: Request):
    return await console_mutation(request, 'file')


@router.post('/console/resource')
async def console_resource(request: Request):
    return await console_mutation(request, 'resource')


@router.post('/console/resource/close')
async def console_resource_close(request: Request):
    return await console_mutation(request, 'resource_close')
