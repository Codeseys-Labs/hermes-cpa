#!/usr/bin/env python3
"""Disposable Linux qualification, never release approval or a live-profile installer.

Native use: prepare --root DIR --source PACKAGE --archive ARCHIVE; permit --root
DIR --hours 4; serve --root DIR --host PATCHED_HOST. Use the host's Python 3.14
venv, with its frozen dependencies and the plugin's declared bcrypt dependency.
All runtime commands require a root made by prepare. Only permit grants or renews
candidate authorization. Refresh requires --consent and a stopped CPA owner;
quiesce stops that owner without signalling a PID read from disk. Restart the
backend after refresh: smoke refuses until serve runs on the current package.
Smoke serializes on its own lock and reuses one stable hosted client through a
private selection cache; it never rotates client IDs.
"""
import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from cpa_runtime_common import load_runtime_manifest_module

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / 'host-integration/provenance.json'
PATCH = ROOT / 'host-integration/patches/0001-scoped-cpa-host-contract.patch'
BASE = '0ed432d1871a9a9b169f5a778f6f9444fc13e0c1'
RUNTIME = load_runtime_manifest_module()
ARCHIVE_NAME = RUNTIME.ARCHIVE_NAME
MARKER = '.qualification.json'
TOKEN = 'session-token'
PERMIT = 'candidate-permit.json'
BACKEND = '.qualification-backend.json'
BACKEND_LOCK = '.qualification-backend.lock'
SELECTION = '.qualification-selection.json'
SMOKE_LOCK = '.qualification-smoke.lock'
PLUGIN_API = '/api/plugins/hermes-cpa'
# The compose healthcheck timeout is 45 s; lock wait plus every request stays below it.
SMOKE_LOCK_SECONDS = 15
SMOKE_DEADLINE_SECONDS = 40
HTTP_TIMEOUT_SECONDS = 8
BACKEND_KEYS = frozenset({'version', 'purpose', 'root', 'root_id', 'uid', 'generation', 'pid',
                          'start', 'boot', 'port', 'package_sha256'})
SELECTION_KEYS = frozenset({'version', 'purpose', 'root', 'root_id', 'uid', 'client',
                            'generation', 'scope_id', 'epoch'})
# Mirrors the repository ignore rules and .dockerignore: generated qualification
# state, common credential files/directories, runtime state, caches and
# archives never enter a root. Name-based only; file contents are not scanned.
EXCLUDED_NAMES = frozenset({
    '.git', '.verification', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
    '.ruff_cache', '.mypy_cache', 'dist', 'dist-hermes', 'dist-reference', 'coverage',
    'test-results', 'playwright-report', '.hermes', 'user-data', 'test-home', 'runtime',
    'evidence', 'tests', 'uv.lock', '.ds_store', 'session-token', 'candidate-permit.json',
    'credentials', 'credentials.json', 'auth.json', 'token.json', '.netrc', '.npmrc', '.pypirc',
    '.git-credentials', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', '.ssh', '.aws', '.gnupg',
    '.docker', '.kube', 'secrets'})
EXCLUDED_PREFIXES = ('.env', '.qualification')
EXCLUDED_SUFFIXES = ('.pyc', '.pyo', '.pem', '.key', '.secret', '.sqlite', '.sqlite3', '.db',
                     '.db-wal', '.db-shm', '.sock', '.pid', '.log', '.tar.gz', '.tgz', '.zip',
                     '.p12', '.pfx', '.jks', '.keystore')


class Refused(ValueError):
    pass


def digest(content):
    return hashlib.sha256(content).hexdigest()


def checked_path(value):
    path = Path(os.path.abspath(value))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise Refused('symlink_path')
    return path


def read_file(path, *, private=False, limit=100 * 1024 * 1024):
    path = checked_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or (private and info.st_mode & 0o077)):
            raise Refused('unsafe_file')
        result = stream.read(limit + 1)
    if len(result) > limit:
        raise Refused('file_too_large')
    return result


def private_dir(path):
    path = checked_path(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused('unsafe_directory')
    return path


def owned_dir(path):
    # Plugin-created intermediate directories follow the umask; the 0700 root
    # already bars other users, so only ownership and type are required here.
    path = checked_path(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise Refused('unsafe_directory')
    return path


def private_lock(path):
    """Open or lazily create a 0600 lock file; existing roots may predate it."""
    private_dir(path.parent)
    path = checked_path(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_nlink != 1 or info.st_mode & 0o077):
        os.close(fd)
        raise Refused('unsafe_lock')
    return fd


@contextmanager
def bounded_lock(path, wait, busy):
    import fcntl
    fd = private_lock(path)
    try:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise Refused(busy) from None
                time.sleep(.05)
        yield
    finally:
        os.close(fd)


def strict_json(data, code):
    try:
        return json.loads(data)
    except ValueError:
        raise Refused(code) from None


def hex_value(value, length):
    return isinstance(value, str) and re.fullmatch(f'[a-f0-9]{{{length}}}', value) is not None


def boot_id():
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except UnicodeDecodeError:
        raise Refused('malformed_proc_identity') from None
    if not re.fullmatch(r'[0-9a-f-]{36}', value):
        raise Refused('malformed_proc_identity')
    return value


def process_identity(pid):
    """Return (state, start) from /proc, or None when the PID does not exist."""
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except UnicodeDecodeError:
        raise Refused('malformed_proc_identity') from None
    _, close, tail = text.rpartition(')')
    fields = tail.split()
    if not close or len(fields) < 20 or not fields[19].isdigit():
        raise Refused('malformed_proc_identity')
    return fields[0], fields[19]


def same_process(pid, start, boot):
    identity = process_identity(pid)
    return identity is not None and identity[0] != 'Z' and identity[1] == start and boot_id() == boot


def atomic_file(path, data, *, replace=False):
    private_dir(path.parent)
    if path.exists() or path.is_symlink():
        read_file(path, private=True)
        if not replace:
            raise Refused('existing_file')
    fd, name = tempfile.mkstemp(prefix='.qualification-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(name, path)
        else:
            os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def linux_root(value):
    if sys.platform != 'linux' or os.getuid() == 0:
        raise Refused('nonroot_linux_required')
    import pwd
    root = checked_path(value)
    homes = {Path.home(), Path(pwd.getpwuid(os.getuid()).pw_dir)}
    protected = {h / '.hermes' for h in homes}
    if os.environ.get('HERMES_HOME'):
        protected.add(Path(os.environ['HERMES_HOME']).absolute())
    if root in homes or root == Path('/'):
        raise Refused('user_home_refused')
    for live in protected:
        live = live.resolve()
        if root == live or root.is_relative_to(live) or live.is_relative_to(root):
            raise Refused('live_home_refused')
    # Linux permission/locking semantics are mandatory; never place tokens on
    # Windows drvfs, SMB, 9p or NTFS, even through a nonstandard mount point.
    mounts = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), left.split()[4])
        if root.is_relative_to(mount):
            mounts.append((len(mount), right.split()[0]))
    if not mounts or max(mounts)[1] in {'9p', 'drvfs', 'cifs', 'smb3', 'ntfs', 'ntfs3', 'fuseblk'}:
        raise Refused('linux_private_filesystem_required')
    return root


def package_path(root):
    return root / '.hermes/plugins/hermes-cpa'


def runtime_artifact(package):
    try:
        manifest = RUNTIME.parse_runtime_manifest(read_file(package / 'cpa-runtime.json', limit=65536))
    except ValueError:
        raise Refused('invalid_runtime_manifest') from None
    if manifest.artifact is None:
        raise Refused('runtime_unpinned')
    return manifest.artifact


def candidate(package):
    manifest = json.loads(read_file(package / 'compatibility.json', limit=65536))
    if (not isinstance(manifest, dict) or
            not str(manifest.get('candidate_status', '')).startswith('unaccepted') or
            manifest.get('adr_status') != 'Proposed' or
            'tested_minimum_hermes' not in manifest or manifest['tested_minimum_hermes'] is not None):
        raise Refused('candidate_flags_changed')
    pairs = manifest.get('supported_pairs', [])
    if not isinstance(pairs, list) or any(not isinstance(pair, dict) for pair in pairs):
        raise Refused('invalid_manifest')
    pair = next((p for p in pairs if p.get('cpa_version') == '8.0.21' and
                 p.get('console_version') == '1.25.6' and p.get('platform') == 'linux_amd64'), None)
    artifact = runtime_artifact(package)
    if (pair is None or pair.get('integration') != RUNTIME.INTEGRATION or
            pair.get('archive_sha256') != artifact.archive_sha256 or
            pair.get('binary_sha256') != artifact.binary_sha256):
        raise Refused('unsupported_pair')
    asset = read_file(package / 'assets/cpamc.html', limit=8 * 1024 * 1024)
    metadata = json.loads(read_file(package / 'assets/cpamc.manifest.json', limit=1024 * 1024))
    if (not isinstance(metadata, dict) or digest(asset) != pair.get('console_sha256') or
            metadata.get('sha256') != digest(asset) or metadata.get('version') != '1.25.6' or
            metadata.get('cpa_version') != '8.0.21'):
        raise Refused('console_mismatch')
    notices = manifest.get('notice_sha256', {})
    if not isinstance(notices, dict) or set(notices) != {'LICENSE', 'THIRD_PARTY_NOTICES.md'}:
        raise Refused('notice_missing')
    for name, expected in notices.items():
        if digest(read_file(package / name, limit=1024 * 1024)) != expected:
            raise Refused('notice_mismatch')
    for name in ('plugin.yaml', 'pyproject.toml', 'dashboard/plugin_api.py', 'src/hermes_cpa/__init__.py'):
        read_file(package / name, limit=1024 * 1024)
    return digest(read_file(package / 'compatibility.json', limit=65536))


def excluded(name):
    lowered = name.lower()
    return (lowered in EXCLUDED_NAMES or lowered.startswith(EXCLUDED_PREFIXES) or
            lowered.endswith(EXCLUDED_SUFFIXES))


def walk_error(error):
    raise error  # An unreadable subtree must never be silently omitted.


def package_files(source):
    """Yield (relative, path, data) in sorted order for every packaged file."""
    source = checked_path(source)
    info = source.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise Refused('unsafe_package')
    for directory, dirs, files in os.walk(source, onerror=walk_error, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not excluded(name))
        for name in dirs:
            path = checked_path(Path(directory) / name)
            if path.stat().st_uid != os.getuid():
                raise Refused('unsafe_package')
        for name in sorted(files):
            if excluded(name):
                continue
            path = Path(directory) / name
            yield path.relative_to(source), path, read_file(path)


def inventory_digest(files):
    return digest(json_bytes({str(relative): digest(data) for relative, _, data in files}))


def copy_package(source, target):
    candidate(source)
    files = list(package_files(source))  # Refuse unsafe trees before any target write.
    target.mkdir(mode=0o700)
    for relative, path, data in files:
        destination = target / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_file(destination, data)
        if path.stat().st_mode & stat.S_IXUSR:
            destination.chmod(0o700)
    candidate(target)
    return inventory_digest(files)


def load_root(value):
    root = linux_root(value)
    private_dir(root)
    record = strict_json(read_file(root / MARKER, private=True, limit=16384), 'unmarked_root')
    if (not isinstance(record, dict) or record.get('version') != 1 or
            record.get('purpose') != 'hermes-cpa-qualification' or
            record.get('root') != str(root) or record.get('uid') != os.getuid() or
            not hex_value(record.get('id'), 32) or not hex_value(record.get('package_sha256'), 64)):
        raise Refused('unmarked_root')
    private_dir(root / '.hermes')
    private_dir(root / '.hermes/plugins')
    private_dir(package_path(root))
    candidate(package_path(root))
    # Same-uid integrity, not a sandbox: recompute the inventory prepare or
    # refresh recorded and refuse drift instead of re-attesting or adopting it.
    if inventory_digest(package_files(package_path(root))) != record['package_sha256']:
        raise Refused('package_tampered')
    token = read_file(root / TOKEN, private=True, limit=128).decode().strip()
    if not re.fullmatch(r'[a-f0-9]{64}', token):
        raise Refused('invalid_session_token')
    return root, record


@contextmanager
def root_lock(root):
    import fcntl
    path = root / '.qualification.lock'
    read_file(path, private=True, limit=0)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('qualification_busy') from None
        yield
    finally:
        os.close(fd)


def prepare(args, *, session_token=None):
    root = linux_root(args.root)
    if root.exists():
        raise Refused('existing_root_refused')
    if session_token is not None and not re.fullmatch(rb'[a-f0-9]{64}\n?', session_token):
        raise Refused('invalid_session_token')
    parent = checked_path(root.parent)
    info = parent.stat()
    if info.st_uid != os.getuid() and not info.st_mode & stat.S_ISVTX:
        raise Refused('unowned_parent')
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        raise Refused('writable_parent')
    source = checked_path(args.source)
    candidate(source)
    list(package_files(source))
    archive = read_file(args.archive)
    artifact = runtime_artifact(source)
    if len(archive) != artifact.archive_size or digest(archive) != artifact.archive_sha256:
        raise Refused('archive_mismatch')
    root.mkdir(mode=0o700)
    # A failed prepare deliberately leaves an unmarked root. Never delete or
    # adopt a directory another process might now be using.
    home = root / '.hermes'
    home.mkdir(mode=0o700)
    (home / 'plugins').mkdir(mode=0o700)
    tree_sha = copy_package(source, package_path(root))
    atomic_file(home / 'config.yaml', b'plugins:\n  isolation: host\n  enabled: [hermes-cpa]\n'
                b'  entries:\n    hermes-cpa:\n      settings: {mode: external}\n')
    token = session_token.strip() if session_token is not None else secrets.token_hex(32).encode()
    atomic_file(root / TOKEN, token + b'\n')
    atomic_file(root / ARCHIVE_NAME, archive)
    atomic_file(root / '.qualification.lock', b'')
    record = dict(version=1, purpose='hermes-cpa-qualification', root=str(root),
                  uid=os.getuid(), id=secrets.token_hex(16), package_sha256=tree_sha)
    atomic_file(root / MARKER, json_bytes(record))
    return {'root_id': record['id'], 'prepared': True, 'permit_created': False}


def permit(args):
    if not math.isfinite(args.hours) or not 0 < args.hours <= 24:
        raise Refused('permit_hours_must_be_positive_and_at_most_24')
    root, _ = load_root(args.root)
    with root_lock(root):
        expires = time.time() + args.hours * 3600
        record = dict(version=1, purpose='isolated-candidate-test', home=str(root / '.hermes'),
                      package_root=str(package_path(root)),
                      manifest_sha256=candidate(package_path(root)), expires_at=expires)
        atomic_file(root / PERMIT, json_bytes(record), replace=True)
    return {'permit_expires_at': expires}


@contextmanager
def stopped_owner(root):
    import fcntl
    cpa = root / '.hermes/cpa'
    if not (cpa.exists() or cpa.is_symlink()):
        yield  # No CPA runtime was ever created in this root.
        return
    owned_dir(cpa)
    run = cpa / 'run'
    try:
        run.mkdir(mode=0o700)
    except FileExistsError:
        pass
    private_dir(run)
    # Prepared or failed-start roots may lack instance.lock. Creating and
    # holding it makes an owner starting meanwhile lose the race instead.
    fd = private_lock(run / 'instance.lock')
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('owner_running_quiesce_first') from None
        # A leftover identity belonging to a still-live owner is not adopted.
        identity = run / 'identity.json'
        if identity.exists() or identity.is_symlink():
            value = strict_json(read_file(identity, private=True, limit=16384), 'invalid_owner_identity')
            owner = value.get('owner') if isinstance(value, dict) else None
            if (not isinstance(owner, dict) or type(owner.get('pid')) is not int or owner['pid'] <= 0 or
                    not isinstance(owner.get('start'), str) or not isinstance(owner.get('boot'), str)):
                raise Refused('invalid_owner_identity')
            if same_process(owner['pid'], owner['start'], owner['boot']):
                raise Refused('owner_running_quiesce_first')
        yield
    finally:
        os.close(fd)


def refresh(args):
    if not args.consent:
        raise Refused('refresh_consent_required')
    root, record = load_root(args.root)
    source = checked_path(args.source)
    package = package_path(root)
    if source == package or source.is_relative_to(root) or root.is_relative_to(source):
        raise Refused('refresh_source_must_be_external')
    candidate(source)
    list(package_files(source))
    with root_lock(root), stopped_owner(root):
        # Staging must stay outside plugin discovery while the backend runs.
        stage = root / ('.qualification-stage-' + secrets.token_hex(8))
        backup = root / ('.qualification-old-' + secrets.token_hex(8))
        try:
            tree_sha = copy_package(source, stage)
            # Even an unchanged manifest never extends or reuses an update's
            # authorization. A new explicit permit is required after refresh.
            # The running backend's record is invalidated too, even for an
            # identical package, so smoke fails until serve starts again.
            for invalidated in (root / PERMIT, root / BACKEND):
                if invalidated.exists() or invalidated.is_symlink():
                    read_file(invalidated, private=True, limit=16384)
                    invalidated.unlink()
            package.rename(backup)
            try:
                stage.rename(package)
            except BaseException:
                backup.rename(package)
                raise
            record['package_sha256'] = tree_sha
            atomic_file(root / MARKER, json_bytes(record), replace=True)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        # Only our renamed package is removed. Refuse symlinks/special files
        # before recursively removing it; never remove arbitrary user roots.
        list(package_files(backup))
        shutil.rmtree(backup)
    return {'refreshed': True, 'permit_created': False, 'restart_backend_required': True}


def host_manifest(host, phase):
    host = checked_path(host)
    manifest = json.loads(PROVENANCE.read_bytes())
    if manifest.get('hermes_base_commit') != BASE or digest(PATCH.read_bytes()) != manifest['patch_sha256']:
        raise Refused('host_provenance_mismatch')
    for row in manifest['files']:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise Refused('unsafe_provenance_path')
        target = checked_path(host / relative)
        expected = row[phase + '_sha256']
        if expected is None:
            if target.exists():
                raise Refused('host_file_already_exists')
        elif not target.is_file() or digest(target.read_bytes()) != expected:
            raise Refused('host_' + phase + '_hash_mismatch')
    return manifest


def build_host(args):
    host = checked_path(args.host)
    manifest = host_manifest(host, 'before')
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env['GIT_CEILING_DIRECTORIES'] = str(host.parent)
    if (host / '.git').exists():
        revision = subprocess.check_output(
            ['git', '-C', str(host), 'rev-parse', 'HEAD'], env=env, text=True).strip()
        if revision != BASE:
            raise Refused('host_revision_mismatch')
    # git apply also works on a source archive without .git. Disable ambient
    # Git repository selection so a surrounding checkout cannot redirect it.
    for command in (['--check'], []):
        subprocess.run(['git', 'apply', *command, str(PATCH)], cwd=host, env=env, check=True)
    host_manifest(host, 'after')
    return {'host_base': BASE, 'patch_sha256': manifest['patch_sha256'], 'files_verified': len(manifest['files'])}


def runtime_environment(root, host):
    host_manifest(host, 'after')
    # No ambient credentials, proxy settings, PYTHONPATH, user site, live HOME,
    # envfile or alternate host workspace reaches the backend or CLI.
    return {'HOME': str(root), 'HERMES_HOME': str(root / '.hermes'),
            'PATH': os.pathsep.join((str(Path(sys.executable).parent), '/usr/local/bin', '/usr/bin', '/bin')),
            'PYTHONPATH': str(host), 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'HERMES_SERVE_HEADLESS': '1', 'LANG': 'C.UTF-8', 'UV_OFFLINE': '1'}


def run_cpa(args):
    root, _ = load_root(args.root)
    host = checked_path(args.host)
    argv = list(args.arguments)
    if argv[:1] == ['--']:
        argv.pop(0)
    if not argv:
        raise Refused('cpa_command_required')
    with root_lock(root):
        return subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'cpa', *argv],
                              cwd=host, env=runtime_environment(root, host)).returncode


def quiesce(args):
    if not args.consent:
        raise Refused('quiesce_consent_required')
    root, _ = load_root(args.root)
    host = checked_path(args.host)
    env = runtime_environment(root, host)
    env['PYTHONPATH'] = os.pathsep.join((str(package_path(root) / 'src'), str(host)))
    # This uses the existing credentialled owner IPC, never kill(pid). It does
    # not load/register the plugin, so it cannot trigger remembered autostart.
    code = ('from pathlib import Path; import os; '
            'from hermes_cpa.controller import controller_from_profile; '
            'from hermes_cpa.settings import CpaSettings; '
            'c=controller_from_profile(CpaSettings(Path(os.environ["HERMES_HOME"]))); '
            'c.unload()')
    with root_lock(root):
        subprocess.run([sys.executable, '-c', code], cwd=root, env=env, check=True)
        deadline = time.monotonic() + 10
        while True:
            try:
                with stopped_owner(root):
                    break
            except Refused:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)
    return {'owner_stopped': True}


def serve(args):
    import fcntl
    if args.prepare_if_missing and not checked_path(args.root).exists():
        prepare(args)
    root, record = load_root(args.root)
    host = checked_path(args.host)
    env = runtime_environment(root, host)
    # Re-exec the same interpreter with a clean environment. This discards
    # ambient PYTHONPATH and user-site imports without dropping the host venv.
    # Token goes only into the process environment, never argv or stdout.
    env['HERMES_DASHBOARD_SESSION_TOKEN'] = read_file(
        root / TOKEN, private=True, limit=128).decode().strip()
    code = ('from pathlib import Path; import sys; '
            'from hermes_cli import web_server; '
            'expected=Path(sys.argv[1])/\"hermes_cli/web_server.py\"; '
            'actual=Path(web_server.__file__).resolve(); '
            'assert actual == expected, \"ambiguous_host_import\"; '
            'web_server.start_server(host=\"127.0.0.1\", port=int(sys.argv[2]), '
            'open_browser=False, headless=True, isolated=True)')
    # One backend per root: the exec'd backend inherits this flock and holds it
    # for its whole life. A second serve on any port is refused.
    fd = private_lock(root / BACKEND_LOCK)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('backend_already_running') from None
        pid = os.getpid()  # execve keeps the PID and kernel start time.
        identity = process_identity(pid)
        if identity is None:
            raise Refused('malformed_proc_identity')
        # Smoke compares this package digest with the marker, so a refresh
        # keeps health failing until a backend starts on the current package.
        atomic_file(root / BACKEND, json_bytes(dict(
            version=1, purpose='hermes-cpa-qualification-backend', root=str(root),
            root_id=record['id'], uid=os.getuid(), generation=secrets.token_hex(16), pid=pid,
            start=identity[1], boot=boot_id(), port=args.port,
            package_sha256=record['package_sha256'])), replace=True)
        os.set_inheritable(fd, True)
        os.chdir(root)
        os.execve(sys.executable, [sys.executable, '-s', '-c', code, str(host), str(args.port)], env)
    finally:
        os.close(fd)  # Reached only when exec did not happen.


def http_request(port, path, headers, deadline, method='GET'):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Refused('smoke_deadline_exceeded')
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=min(HTTP_TIMEOUT_SECONDS, remaining))
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        data = b''
        while chunk := response.read(65536):
            data += chunk
            if len(data) > 1024 * 1024:
                raise Refused('smoke_response_too_large')
            if time.monotonic() >= deadline:
                raise Refused('smoke_deadline_exceeded')
        return response.status, strict_json(data, 'backend_http_malformed') if data else None
    except http.client.HTTPException:
        raise Refused('backend_http_malformed') from None
    finally:
        connection.close()


def backend_state(root, record, port):
    path = root / BACKEND
    if not (path.exists() or path.is_symlink()):
        raise Refused('backend_restart_required')
    value = strict_json(read_file(path, private=True, limit=4096), 'invalid_backend_record')
    if (not isinstance(value, dict) or set(value) != BACKEND_KEYS or value['version'] != 1 or
            value['purpose'] != 'hermes-cpa-qualification-backend' or value['root'] != str(root) or
            value['root_id'] != record['id'] or value['uid'] != os.getuid() or
            not hex_value(value['generation'], 32) or not hex_value(value['package_sha256'], 64) or
            type(value['pid']) is not int or value['pid'] <= 0 or type(value['port']) is not int or
            not isinstance(value['start'], str) or not isinstance(value['boot'], str)):
        raise Refused('invalid_backend_record')
    if value['package_sha256'] != record['package_sha256']:
        raise Refused('backend_restart_required')
    if not same_process(value['pid'], value['start'], value['boot']):
        raise Refused('backend_not_running')
    if value['port'] != port:
        raise Refused('backend_port_mismatch')
    return value


def valid_epoch(value):
    return type(value) is int and 0 < value <= 2 ** 53 - 1


def read_selection(root, record, client):
    path = root / SELECTION
    if not (path.exists() or path.is_symlink()):
        return None
    try:
        value = json.loads(read_file(path, private=True, limit=4096))
    except (Refused, OSError, ValueError):
        raise Refused('selection_cache_unsafe') from None
    if (not isinstance(value, dict) or set(value) != SELECTION_KEYS or value['version'] != 1 or
            value['purpose'] != 'hermes-cpa-qualification-selection' or value['root'] != str(root) or
            value['root_id'] != record['id'] or value['uid'] != os.getuid() or
            value['client'] != client or not hex_value(value['generation'], 32) or
            not hex_value(value['scope_id'], 32) or not valid_epoch(value['epoch'])):
        raise Refused('selection_cache_unsafe')
    return value


def stale_scope(status, body):
    return status == 409 and isinstance(body, dict) and body.get('detail') == 'STALE_SCOPE'


def scoped(headers, selection):
    return {**headers, 'X-Hermes-Plugin-Scope': selection['scope_id'],
            'X-Hermes-Plugin-Epoch': str(selection['epoch'])}


def hosted_selection(root, record, backend, port, token, deadline):
    """Return (scoped headers, status body, reused) for this root's one client.

    The host refuses a select unless the caller states the current epoch, and
    that epoch only comes from the server. A backend start (new generation) is
    the sole authority to assert epoch 0; otherwise only the cached epoch is
    used. At most three requests; client IDs are never rotated.
    """
    client = 'qualification-' + record['id']
    headers = {'X-Hermes-Session-Token': token, 'X-Hermes-Plugin-Client': client}
    cache = read_selection(root, record, client)
    expected = 0
    if cache is not None and cache['generation'] == backend['generation']:
        status, state = http_request(port, PLUGIN_API + '/status', scoped(headers, cache), deadline)
        if status == 200:
            return scoped(headers, cache), state, True
        if not stale_scope(status, state):
            raise Refused('hosted_status_failed')
        expected = cache['epoch']
    status, selected = http_request(port, PLUGIN_API + '/_host/selection',
                                    {**headers, 'X-Hermes-Plugin-Epoch': str(expected)}, deadline, 'POST')
    if stale_scope(status, selected):
        # The server holds a selection this root no longer remembers.
        raise Refused('selection_cache_lost_restart_backend')
    if (status != 200 or not isinstance(selected, dict) or set(selected) != {'scope_id', 'epoch'} or
            not hex_value(selected['scope_id'], 32) or not valid_epoch(selected['epoch'])):
        raise Refused('hosted_selection_failed')
    if cache is None or (cache['generation'], cache['scope_id'], cache['epoch']) != (
            backend['generation'], selected['scope_id'], selected['epoch']):
        # Persist exactly what the server returned before any further request.
        atomic_file(root / SELECTION, json_bytes(dict(
            version=1, purpose='hermes-cpa-qualification-selection', root=str(root),
            root_id=record['id'], uid=os.getuid(), client=client, generation=backend['generation'],
            scope_id=selected['scope_id'], epoch=selected['epoch'])), replace=True)
    status, state = http_request(port, PLUGIN_API + '/status', scoped(headers, selected), deadline)
    if status != 200:
        raise Refused('hosted_status_failed')
    return scoped(headers, selected), state, False


def smoke(args):
    deadline = time.monotonic() + SMOKE_DEADLINE_SECONDS
    root, record = load_root(args.root)
    token = read_file(root / TOKEN, private=True, limit=128).decode().strip()
    fingerprint = digest(token.encode())
    if args.expect_token_sha256 and args.expect_token_sha256 != fingerprint:
        raise Refused('session_token_changed')
    # Health and manual smokes share one client. Serialize them on a dedicated
    # lock; root_lock is held through cpa setup and would make health flap.
    wait = min(SMOKE_LOCK_SECONDS, deadline - time.monotonic())
    with bounded_lock(root / SMOKE_LOCK, wait, 'smoke_busy'):
        backend = backend_state(root, record, args.port)
        status, _ = http_request(args.port, PLUGIN_API + '/status', {}, deadline)
        if status != 401:
            raise Refused('unauthenticated_request_not_refused')
        headers, state, reused = hosted_selection(root, record, backend, args.port, token, deadline)
        if not isinstance(state, dict) or not isinstance(state.get('data'), dict):
            raise Refused('hosted_status_failed')
        lifecycle = state['data'].get('lifecycle')
        if args.managed and (state['data'].get('mode') != 'managed' or lifecycle != 'ready'):
            raise Refused('managed_cpa_not_ready')
        status, metrics = http_request(args.port, PLUGIN_API + '/metrics', headers, deadline)
        if status != 200 or not isinstance(metrics, dict) or not isinstance(metrics.get('data'), dict):
            raise Refused('hosted_metrics_failed')
    return {'root_id': record['id'], 'session_token_sha256': fingerprint,
            'authenticated_backend': True, 'hosted_status': lifecycle,
            'package_sha256': record['package_sha256'], 'selection_reused': reused}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build-host', help='Apply and verify the public baseline host patch')
    build.add_argument('--host', required=True, type=Path)
    for name in ('prepare', 'permit', 'serve', 'refresh', 'cpa', 'quiesce', 'smoke'):
        command = commands.add_parser(name)
        command.add_argument('--root', required=True, type=Path)
        if name in ('prepare', 'refresh', 'serve'):
            command.add_argument('--source', type=Path, default=ROOT / 'hermes-cpa')
        if name in ('prepare', 'serve'):
            command.add_argument('--archive', type=Path, default=ROOT / 'cache' / ARCHIVE_NAME)
        if name == 'prepare':
            command.add_argument('--session-token-stdin', action='store_true',
                                 help='Preserve an existing generated token from stdin in a new root; never print it')
        if name in ('serve', 'cpa', 'quiesce'):
            command.add_argument('--host', type=Path, default=ROOT / 'host')
        if name in ('serve', 'smoke'):
            command.add_argument('--port', type=int, default=9119)
        if name in ('refresh', 'quiesce'):
            command.add_argument('--consent', action='store_true')
        if name == 'serve':
            command.add_argument('--prepare-if-missing', action='store_true',
                                 help='Create a fresh marked root only; never create or renew a permit')
        if name == 'permit':
            command.add_argument('--hours', required=True, type=float)
        if name == 'cpa':
            command.add_argument('arguments', nargs=argparse.REMAINDER)
        if name == 'smoke':
            command.add_argument('--managed', action='store_true')
            command.add_argument('--expect-token-sha256')
    args = parser.parse_args(argv)
    if hasattr(args, 'port') and not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    handlers = {'build-host': build_host, 'prepare': prepare, 'permit': permit,
                'serve': serve, 'refresh': refresh, 'cpa': run_cpa,
                'quiesce': quiesce, 'smoke': smoke}
    try:
        if args.command == 'prepare' and args.session_token_stdin:
            result = prepare(args, session_token=sys.stdin.buffer.read(129))
        else:
            result = handlers[args.command](args)
    except (Refused, OSError, ValueError, KeyError, TypeError, IndexError,
            http.client.HTTPException, subprocess.SubprocessError) as exc:
        # Library errors can contain paths, response bodies or subprocess data.
        # Only our fixed refusal codes are safe to print.
        print('Qualification refused: ' + (str(exc) if isinstance(exc, Refused) else type(exc).__name__),
              file=sys.stderr)
        return 1
    if isinstance(result, dict):
        print(json.dumps(result, sort_keys=True))
        return 0
    return result


if __name__ == '__main__':
    raise SystemExit(main())
