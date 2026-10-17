"""Owner-derived session approval for fixed built-in quota/profile reads.

A request qualifies only when its URL, method, header names and values and parsed body are exactly one of the templates below
for a current built-in provider (Codex, Claude, Antigravity) and the selected
credential, read from trusted native inventory, belongs to that provider and
account. Anything else is not matched here and keeps the ordinary exact review;
nothing is rewritten into a template.

The matched request still gets its own owner prepare/execute review, host grant
and egress policy. Only the human prompt is shared, per provider credential.
"""
import hashlib
import json
import re
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from .console_protocol import PREFIX, encoded

API_CALL_PATH = PREFIX + '/requests/api-call'
SCOPE_EFFECT = 'quota_reads_until_lock'
MAX_GRANTS_PER_LEASE = 32

_AUTH_KEYS = ('auth_index', 'authIndex', 'AuthIndex')
_BODY_KEYS = frozenset(_AUTH_KEYS) | {'method', 'url', 'header', 'data'}
_TOKEN = 'Bearer $TOKEN$'
_JSON = 'application/json'
_PROJECT = re.compile(r'[a-z][a-z0-9-]{3,62}')
_ACCOUNT_PARAM = re.compile(r"[A-Za-z0-9%._~!*'()-]{1,768}")
_PRINTABLE = re.compile(r'[\x20-\x7e]{1,256}')

DISPLAY = {'codex': 'Codex', 'claude': 'Claude', 'antigravity': 'Antigravity'}


def _exact(expected):
    return lambda value: value == expected


# User-Agent strings pinned from the console's own quota constants (the console asset is itself pinned).
_CODEX_AGENT = 'codex-tui/0.149.1 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.149.1)'
_CLAUDE_AGENT = 'claude-cli/2.1.280 (external, cli)'
_ANTIGRAVITY_AGENT = 'antigravity/cli/1.0.13 (aidev_client; os_type=darwin; arch=arm64)'
# Native row fields that identify one stored credential across token refreshes. modtime, updated_at and size
# are deliberately absent: a refresh during an approved read rewrites them and would void its own approval.
REVISION_FIELDS = ('created_at', 'path', 'source')


# Header name (lower case) -> value predicate. These are the exact header sets
# the console's own quota readers send.
_CODEX_HEADERS = {'authorization': _exact(_TOKEN), 'content-type': _exact(_JSON),
                  'user-agent': _exact(_CODEX_AGENT)}
_CODEX_CREDITS_HEADERS = dict(_CODEX_HEADERS, **{
    'accept': _exact(_JSON), 'openai-beta': _exact('codex-1'), 'originator': _exact('Codex Desktop')})
_CLAUDE_HEADERS = {'authorization': _exact(_TOKEN), 'content-type': _exact(_JSON),
                   'user-agent': _exact(_CLAUDE_AGENT), 'anthropic-beta': _exact('oauth-2025-04-20')}
_ANTIGRAVITY_HEADERS = {'authorization': _exact(_TOKEN), 'content-type': _exact(_JSON),
                        'user-agent': _exact(_ANTIGRAVITY_AGENT)}


@dataclass(frozen=True)
class Template:
    family: str
    provider: str
    method: str
    host: str
    path: str
    query: str | None  # None: no query; 'account': account_id=<account>; else the literal query
    headers: dict
    body: str | None  # None: no body; 'ide': fixed loadCodeAssist; 'project': {"project": id}
    description: str


TEMPLATES = (
    Template('codex.usage', 'codex', 'GET', 'chatgpt.com', '/backend-api/wham/usage', None,
             _CODEX_HEADERS, None, 'usage windows'),
    Template('codex.subscription', 'codex', 'GET', 'chatgpt.com', '/backend-api/subscriptions', 'account',
             _CODEX_HEADERS, None, 'subscription term'),
    Template('codex.reset_credits', 'codex', 'GET', 'chatgpt.com', '/backend-api/wham/rate-limit-reset-credits',
             None, _CODEX_CREDITS_HEADERS, None, 'reset-credit balance (read only)'),
    Template('claude.usage', 'claude', 'GET', 'api.anthropic.com', '/api/oauth/usage', None,
             _CLAUDE_HEADERS, None, 'usage windows'),
    Template('claude.grants', 'claude', 'GET', 'api.anthropic.com', '/api/oauth/usage',
             'cedar_ember=1&skip_spend=1', _CLAUDE_HEADERS, None, 'reset-grant status (read only)'),
    Template('claude.profile', 'claude', 'GET', 'api.anthropic.com', '/api/oauth/profile', None,
             _CLAUDE_HEADERS, None, 'profile'),
    Template('antigravity.subscription', 'antigravity', 'POST', 'daily-cloudcode-pa.googleapis.com',
             '/v1internal:loadCodeAssist', None, _ANTIGRAVITY_HEADERS, 'ide', 'subscription tier'),
) + tuple(
    Template('antigravity.quota', 'antigravity', 'POST', host, '/v1internal:retrieveUserQuotaSummary', None,
             _ANTIGRAVITY_HEADERS, 'project', 'quota summary (read only)')
    for host in ('daily-cloudcode-pa.googleapis.com', 'daily-cloudcode-pa.sandbox.googleapis.com',
                 'cloudcode-pa.googleapis.com'))


@dataclass(frozen=True)
class Candidate:
    """A syntactic template match; credential facts are still unverified."""
    template: Template
    auth_index: str
    account: str | None  # decoded account_id query value
    account_header: str | None
    project: str | None


@dataclass(frozen=True)
class QuotaRead:
    provider: str
    family: str
    auth_index: str
    identity: str
    policy: dict
    summary: str

    @property
    def key(self):
        return self.provider + '\0' + self.auth_index

    def scope(self):
        return {'key': self.key, 'provider': self.provider, 'family': self.family,
                'auth_index': self.auth_index, 'identity': self.identity}


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate key')
    return dict(pairs)


def _headers(value, spec):
    if not isinstance(value, dict):
        return None
    lowered = {}
    for name, text in value.items():
        if not isinstance(name, str) or not isinstance(text, str) or name.lower() in lowered:
            return None
        lowered[name.lower()] = text
    extra = set(lowered) - set(spec) - {'chatgpt-account-id'}
    if extra or any(name not in lowered or not spec[name](lowered[name]) for name in spec):
        return None
    return lowered


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip() and \
        _PRINTABLE.fullmatch(value) is not None and not any(c.isspace() for c in value)


def match(request):
    """Return the Candidate for a request that is exactly a fixed template, else None."""
    if request.method != 'POST' or request.path != API_CALL_PATH or request.wire.get('query'):
        return None
    if request.wire.get('expected_revision') is not None or request.wire['body'].get('kind') != 'json':
        return None
    value = request.wire['body'].get('value')
    if not isinstance(value, dict) or set(value) - _BODY_KEYS:
        return None
    indices = [value[k] for k in _AUTH_KEYS if k in value]
    method, url = value.get('method'), value.get('url')
    if len(indices) != 1 or not _identifier(indices[0]) or not isinstance(method, str) or \
            not isinstance(url, str) or len(url) > 2048:
        return None
    data = value.get('data', '')
    if not isinstance(data, str):
        return None
    try:
        target = urlsplit(url)
        _ = target.port
    except ValueError:
        return None
    for template in TEMPLATES:
        if method != template.method or target.scheme != 'https' or target.netloc != template.host or \
                target.path != template.path or target.fragment or target.username or target.password:
            continue
        account = None
        base = 'https://' + template.host + template.path
        if template.query is None:
            if url != base:
                continue
        elif template.query == 'account':
            raw = url[len(base + '?account_id='):]
            if not url.startswith(base + '?account_id=') or _ACCOUNT_PARAM.fullmatch(raw) is None:
                continue
            try:
                account = unquote(raw, errors='strict')
            except UnicodeError:
                continue
        elif url != base + '?' + template.query:
            continue
        headers = _headers(value.get('header'), template.headers)
        if headers is None:
            continue
        project = None
        if template.body is None:
            if data != '':
                continue
        else:
            try:
                parsed = json.loads(data, object_pairs_hook=_no_duplicates)
            except ValueError:
                continue
            if template.body == 'ide':
                if parsed != {'metadata': {'ideType': 'ANTIGRAVITY'}}:
                    continue
            else:
                project = parsed.get('project') if isinstance(parsed, dict) and set(parsed) == {'project'} else None
                if not isinstance(project, str) or _PROJECT.fullmatch(project) is None:
                    continue
        return Candidate(template, indices[0], account, headers.get('chatgpt-account-id'), project)
    return None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= 512 else None


def _claims(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > 65536:
        return None
    text = value.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    segments = text.split('.')
    if len(segments) < 2:
        return None
    try:
        parsed = json.loads(urlsafe_b64decode(segments[1] + '=' * (-len(segments[1]) % 4)))
    except (ValueError, UnicodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _nested(entry, *keys):
    """Direct key, then metadata/attributes, in the order the console's own resolver uses."""
    for source in (entry, entry.get('metadata'), entry.get('attributes')):
        if isinstance(source, dict):
            for key in keys:
                if (found := _text(source.get(key))) is not None:
                    return found
    return None


def _codex_account(entry):
    if (found := _nested(entry, 'chatgpt_account_id', 'chatgptAccountId')) is not None:
        return found
    for source in (entry, entry.get('metadata'), entry.get('attributes')):
        claims = _claims(source.get('id_token')) if isinstance(source, dict) else None
        if claims and (found := _text(claims.get('chatgpt_account_id', claims.get('chatgptAccountId')))):
            return found
    return None


def _entry(files, auth_index):
    if not isinstance(files, list):
        return None
    found = [f for f in files if isinstance(f, dict) and
             str(f.get('auth_index', '')).strip() == auth_index]
    return found[0] if len(found) == 1 else None


def verify(candidate, files):
    """Bind a Candidate to the native inventory entry for its credential; None if unproven."""
    template, entry = candidate.template, _entry(files, candidate.auth_index)
    if entry is None:
        return None
    labels = {str(entry[k]).strip().lower() for k in ('provider', 'type') if k in entry}
    if template.provider not in labels or len(labels) != 1:
        return None
    account = project = None
    if template.provider == 'codex':
        account = _codex_account(entry)
        if candidate.account_header != account:
            return None
        if template.query == 'account' and (account is None or candidate.account != account):
            return None
    elif candidate.account_header is not None:
        return None
    if template.provider == 'antigravity':
        project = _nested(entry, 'project_id', 'projectId', 'gemini_virtual_project')
        # The project in the body is only a request parameter, so it must be the credential's own, proven by
        # trusted inventory; an absent or different project keeps the ordinary exact review.
        if candidate.project is not None and project != candidate.project:
            return None
    name = _text(entry.get('name')) or candidate.auth_index
    identity = hashlib.sha256(encoded({
        'provider': template.provider, 'auth_index': candidate.auth_index, 'name': entry.get('name'),
        'id': entry.get('id'), 'email': entry.get('email'), 'account': account, 'project': project,
        'revision': [entry.get(field) for field in REVISION_FIELDS],
    })).hexdigest()
    policy = {'version': 1, 'destinations': [{'scheme': 'https', 'host': template.host, 'port': 443,
              'path_prefix': template.path}], 'methods': [template.method], 'redirects': 'deny',
              'allow_token': True, 'proxy': {'mode': 'configured'}, 'auth_index': candidate.auth_index}
    return QuotaRead(template.provider, template.family, candidate.auth_index, identity, policy,
                     _summary(template, name))


def inventory_scopes(files):
    """Current credential identities eligible for consent at trusted unlock."""
    scopes = {}
    for entry in files:
        if not isinstance(entry, dict) or not _identifier(entry.get('auth_index')):
            continue
        provider = str(entry.get('provider', entry.get('type', ''))).strip().lower()
        template = next((item for item in TEMPLATES if item.provider == provider and
                         item.body != 'project'), None)
        if template is None:
            continue
        account = _codex_account(entry) if provider == 'codex' else None
        candidate = Candidate(template, entry['auth_index'], None, account, None)
        verified = verify(candidate, files)
        if verified is not None:
            scopes[verified.key] = verified.identity
            if len(scopes) == MAX_GRANTS_PER_LEASE:
                break
    return scopes


def _summary(template, name):
    provider = DISPLAY[template.provider]
    label = ''.join(c for c in name if c.isprintable())[:96]
    return (f'Read {provider} {template.description} for credential "{label}"\n'
            f'Fixed request: {template.method} https://{template.host}{template.path}; redirects are refused.\n'
            f'Approving this also permits the other fixed built-in {provider} quota/profile reads '
            f'(not other network calls) for this same credential until you Lock the console or this unlocked '
            f'session expires. It never covers other credentials or providers, credit consumption, resets, '
            f'claims, inference, proxy or Host overrides, or plugin actions; those always ask separately.\n'
            f'Effects: network_destination, {SCOPE_EFFECT}')
