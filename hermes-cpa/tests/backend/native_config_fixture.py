"""Generated stand-in for the pinned CPA's native config prepare/commit wire.

Only the agreed envelope is modeled (the integration build owns real ConfigV8
semantics): full YAML replacement, a JSON value PUT at a v8 path, DELETE of a
path, a native-looking refusal for a negative request-retry, and the agreed
effect vocabulary for the server/oauth/access/management subtrees.
"""
import base64
import hashlib
import io
import json

from ruamel.yaml import YAML

PREFIX = '/v8/management/config'
RESTART = (('server', 'host'), ('server', 'port'), ('server', 'tls'), ('oauth', 'auth-dir'))


def _yaml():
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _plain(value):
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _leaves(value, prefix=()):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _leaves(child, prefix + (key,))
    else:
        yield prefix, value


def _get(tree, parts):
    for part in parts:
        if not isinstance(tree, dict) or part not in tree:
            return None
        tree = tree[part]
    return tree


def _refuse(status, code, error, index=None):
    body = {'code': code, 'error': error, 'status': status}
    if index is not None:
        body['request_index'] = index
    return status, json.dumps(body).encode()


def prepare(base, payload):
    """(status, body) exactly as the Go prepare endpoint/offline mode reply."""
    request = json.loads(payload)
    base_revision = hashlib.sha256(base).hexdigest()
    if request.get('expected_revision') != base_revision:
        return 409, json.dumps({'code': 'STALE_REVISION', 'error': 'stale_revision',
                                'base_revision': base_revision}).encode()
    yaml = _yaml()
    doc = yaml.load(base) if base else {}
    before = _plain(doc)
    for index, item in enumerate(request['requests']):
        body = base64.b64decode(item.get('body_base64', ''))
        if item['path'] == PREFIX + '.yaml':
            doc = yaml.load(body)
            if not isinstance(doc, dict):
                return _refuse(400, 'INVALID_CONFIG', 'config_must_be_object', index)
            continue
        parts = [part for part in item['path'][len(PREFIX):].split('/') if part]
        if not parts:
            return _refuse(400, 'INVALID_REQUEST', 'unsupported_path', index)
        parent = doc
        for part in parts[:-1]:
            parent = parent.setdefault(part, {})
        if item['method'] == 'DELETE':
            if parts[-1] not in parent:
                return _refuse(404, 'NOT_FOUND', 'not_found', index)
            del parent[parts[-1]]
            continue
        try:
            value = json.loads(body)
        except ValueError:
            return _refuse(400, 'INVALID_CONFIG', 'invalid_json', index)
        if parts[-1] == 'request-retry' and (type(value) is not int or value < 0):
            return _refuse(422, 'INVALID_CONFIG', 'invalid_config', index)
        parent[parts[-1]] = value
    out = io.StringIO()
    yaml.dump(doc, out)
    after = _plain(doc)
    # A semantic no-op keeps the stored canonical bytes, as native preparation does.
    candidate = base if after == before and base else out.getvalue().encode()
    old, new = dict(_leaves(before)), dict(_leaves(after))
    changed = sorted('/'.join(path) for path in set(old) | set(new) if old.get(path) != new.get(path))
    effects = set()
    if candidate == base:
        effects.add('no_change')
    for path in RESTART:
        if _get(before, path) != _get(after, path):
            effects.update({'restart_required', 'pending_oauth_invalidated'})
            effects.add('auth_dir_changed' if path[0] == 'oauth' else 'public_listener_changed')
    for section, effect in (('access', 'access_keys_changed'),
                            ('management', 'management_access_changed')):
        if _get(before, (section,)) != _get(after, (section,)):
            effects.add(effect)
    return 200, json.dumps({'base_revision': base_revision,
                            'candidate_revision': hashlib.sha256(candidate).hexdigest(),
                            'candidate_base64': base64.b64encode(candidate).decode(),
                            'effects': sorted(effects), 'changed_paths': changed}).encode()


def commit(current, payload):
    """(status, body, bytes_to_persist_or_None) for the Go live commit endpoint."""
    request = json.loads(payload)
    candidate = base64.b64decode(request['candidate_base64'])
    if hashlib.sha256(current).hexdigest() != request['expected_revision']:
        return 409, json.dumps({'code': 'STALE_REVISION', 'error': 'stale_revision'}).encode(), None
    if hashlib.sha256(candidate).hexdigest() != request['candidate_revision']:
        return 400, json.dumps({'code': 'CANDIDATE_MISMATCH', 'error': 'candidate_revision_mismatch'}).encode(), None
    status, body = prepare(current, json.dumps({
        'expected_revision': request['expected_revision'],
        'requests': [{'method': 'PUT', 'path': PREFIX + '.yaml',
                      'body_base64': request['candidate_base64']}]}).encode())
    if status == 200 and 'restart_required' in json.loads(body)['effects']:
        return 409, json.dumps({'code': 'RESTART_REQUIRED', 'error': 'restart_required',
                                'effects': json.loads(body)['effects']}).encode(), None
    return 200, json.dumps({'status': 'ok', 'revision': request['candidate_revision']}).encode(), candidate


def write_in_place(path, raw):
    """Native WriteConfig: truncate and rewrite the same inode."""
    with open(path, 'r+b') as stream:
        stream.truncate(0)
        stream.write(raw)
        stream.flush()
