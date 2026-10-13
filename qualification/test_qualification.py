"""Linux CLI/filesystem tests; no Docker, providers or public archive downloads.

Run with the qualification interpreter:
  python -m unittest discover -s qualification -p 'test_qualification.py' -v
The archive fixture tests filesystem policy, not CPA artifact qualification.
Smoke tests use a loopback HTTP double of the patched host selection contract;
real pinned-archive/hosted-backend checks use qualification.py smoke in exec.
"""
import contextlib
import fcntl
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/qualification.py'
SPEC = importlib.util.spec_from_file_location('qualification', SCRIPT)
q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(q)
# Runs the real CLI in a separate process with the generated fixture pin.
WRAPPER = ('import importlib.util, sys; s = importlib.util.spec_from_file_location("q", sys.argv[1]); '
           'q = importlib.util.module_from_spec(s); s.loader.exec_module(q); q.ARCHIVE_SHA = sys.argv[2]; '
           'raise SystemExit(q.main(sys.argv[3:]))')


class Exec(Exception):
    """Stands in for a successful execve; this process plays the backend."""


class FakeHost:
    """Loopback HTTP double of the patched host selection contract.

    Mirrors SelectionAuthority.select/resolve: a per-process epoch counter, a
    256-client cap, CAS on the caller's stated epoch (default 0), idempotent
    reselection, and an empty authority after each backend restart.
    """

    def __init__(self, token):
        self.token = token
        self.lock = threading.Lock()
        self.posts = []
        self.restart()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.handle(self, 'GET')

            def do_POST(self):
                owner.handle(self, 'POST')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def restart(self):
        with self.lock:
            self.selections, self.epoch = {}, 0

    def select(self, client, expected):
        with self.lock:
            if client not in self.selections and len(self.selections) >= 256:
                raise ValueError('BUSY')
            previous = self.selections.get(client)
            if str(previous[1] if previous else 0) != str(expected):
                raise ValueError('STALE_SCOPE')
            if previous is None:
                self.epoch += 1
                self.selections[client] = (secrets.token_hex(16), self.epoch)
            return self.selections[client]

    def resolve(self, client, scope, epoch):
        with self.lock:
            selected = self.selections.get(client)
            if selected is None or selected[0] != scope or str(selected[1]) != str(epoch):
                raise ValueError('STALE_SCOPE')

    def handle(self, request, method):
        request.rfile.read(int(request.headers.get('Content-Length') or 0))
        if request.headers.get('X-Hermes-Session-Token') != self.token:
            return self.reply(request, 401, {'detail': 'Unauthorized'})
        client = request.headers.get('X-Hermes-Plugin-Client', '')
        try:
            if not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', client):
                raise ValueError('STALE_SCOPE')
            if method == 'POST' and request.path == q.PLUGIN_API + '/_host/selection':
                expected = request.headers.get('X-Hermes-Plugin-Epoch', '0')
                with self.lock:
                    self.posts.append((client, expected))
                scope, epoch = self.select(client, expected)
                return self.reply(request, 200, {'scope_id': scope, 'epoch': epoch})
            if method == 'GET' and request.path in (q.PLUGIN_API + '/status', q.PLUGIN_API + '/metrics'):
                self.resolve(client, request.headers.get('X-Hermes-Plugin-Scope'),
                             request.headers.get('X-Hermes-Plugin-Epoch'))
                data = {'mode': 'external', 'lifecycle': 'stopped'} if request.path.endswith('/status') else {}
                return self.reply(request, 200, {'data': data})
            return self.reply(request, 404, {'detail': 'Not Found'})
        except ValueError as exc:
            return self.reply(request, 409, {'detail': str(exc)})

    @staticmethod
    def reply(request, status, body):
        data = json.dumps(body).encode()
        request.send_response(status)
        request.send_header('Content-Type', 'application/json')
        request.send_header('Content-Length', str(len(data)))
        request.end_headers()
        request.wfile.write(data)


def open_fds(path):
    found = []
    for name in os.listdir('/proc/self/fd'):
        try:
            if os.readlink(f'/proc/self/fd/{name}') == str(path):
                found.append(int(name))
        except OSError:
            pass
    return found


@unittest.skipUnless(sys.platform == 'linux' and os.getuid() != 0, 'Nonroot Linux is the supported runtime')
class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cpa-qualification-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir(mode=0o700)
        self.archive = self.base / 'archive.tar.gz'
        self.archive.write_bytes(b'generated archive fixture, never an executable')
        self.pin = patch.object(q, 'ARCHIVE_SHA', q.digest(self.archive.read_bytes()))
        self.pin.start()
        self.addCleanup(self.pin.stop)
        self.root = self.base / 'qualification'
        self.make_package(self.source)

    def make_package(self, target):
        content = {'assets/cpamc.html': b'<html>generated qualification fixture</html>',
                   'LICENSE': b'generated license', 'THIRD_PARTY_NOTICES.md': b'generated notices',
                   'plugin.yaml': b'name: hermes-cpa\n', 'pyproject.toml': b'[project]\nname="fixture"\n',
                   'src/hermes_cpa/__init__.py': b'', 'dashboard/plugin_api.py': b'fixture = True\n'}
        for name, data in content.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(data)
        html_sha = q.digest(content['assets/cpamc.html'])
        (target / 'assets/cpamc.manifest.json').write_text(json.dumps(
            dict(sha256=html_sha, version='1.25.6', cpa_version='8.0.21')))
        manifest = dict(candidate_status='unaccepted; generated fixture', adr_status='Proposed',
                        tested_minimum_hermes=None, supported_pairs=[dict(cpa_version='8.0.21',
                        console_version='1.25.6', platform='linux_amd64', archive_sha256=q.ARCHIVE_SHA,
                        console_sha256=html_sha)], notice_sha256={name: q.digest(content[name])
                        for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md')})
        (target / 'compatibility.json').write_text(json.dumps(manifest))

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = q.main([str(arg) for arg in arguments])
        return result, out.getvalue(), err.getvalue()

    def prepare(self, root=None):
        root = root or self.root
        result = self.cli('prepare', '--root', root, '--source', self.source, '--archive', self.archive)
        self.assertEqual(result[0], 0, result[2])
        return root

    def permit(self, hours='1'):
        return self.cli('permit', '--root', self.root, '--hours', hours)

    def snapshot(self, root):
        return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in root.rglob('*') if path.is_file() and not path.is_symlink()}

    def start_backend(self, port, root=None):
        """Run the real serve path to the exec boundary; this process is the backend."""
        cwd = Path.cwd()
        try:
            with patch.object(q, 'host_manifest'), patch.object(q.os, 'execve', side_effect=Exec):
                with self.assertRaises(Exec):
                    self.cli('serve', '--root', root or self.root, '--host', self.base / 'host', '--port', port)
        finally:
            os.chdir(cwd)

    def running(self):
        self.prepare()
        host = FakeHost((self.root / q.TOKEN).read_text().strip())
        self.addCleanup(host.close)
        self.start_backend(host.port)
        return host

    def smoke(self, port, *extra):
        return self.cli('smoke', '--root', self.root, '--port', port, *extra)

    def client(self):
        return 'qualification-' + json.loads((self.root / q.MARKER).read_text())['id']

    def test_prepare_creates_private_inert_root_without_permit(self):
        result, out, err = self.cli('prepare', '--root', self.root,
                                  '--source', self.source, '--archive', self.archive)
        self.assertEqual((result, err), (0, ''))
        token = (self.root / q.TOKEN).read_text().strip()
        self.assertRegex(token, r'^[a-f0-9]{64}$')
        self.assertNotIn(token, out)
        self.assertFalse((self.root / q.PERMIT).exists())
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / q.TOKEN).stat().st_mode & 0o777, 0o600)
        self.assertIn('mode: external', (self.root / '.hermes/config.yaml').read_text())
        self.assertIn('isolation: host', (self.root / '.hermes/config.yaml').read_text())
        self.assertEqual((q.package_path(self.root) / 'assets/cpamc.html').read_bytes(),
                         (self.source / 'assets/cpamc.html').read_bytes())
        self.assertEqual((self.root / q.ARCHIVE_NAME).read_bytes(), self.archive.read_bytes())

    def test_existing_root_is_never_adopted_or_overwritten(self):
        self.root.mkdir()
        marker = self.root / 'user-file'
        marker.write_text('keep')
        before = self.snapshot(self.root)
        result = self.cli('prepare', '--root', self.root, '--source', self.source, '--archive', self.archive)
        self.assertEqual(result[0], 1)
        self.assertIn('existing_root_refused', result[2])
        self.assertEqual(before, self.snapshot(self.root))

    def test_second_prepare_does_not_rotate_token(self):
        self.prepare()
        before = self.snapshot(self.root)
        result = self.cli('prepare', '--root', self.root, '--source', self.source, '--archive', self.archive)
        self.assertEqual(result[0], 1)
        self.assertEqual(before, self.snapshot(self.root))

    def test_wrong_archive_fails_before_creating_root(self):
        self.archive.write_bytes(b'wrong')
        result = self.cli('prepare', '--root', self.root, '--source', self.source, '--archive', self.archive)
        self.assertIn('archive_mismatch', result[2])
        self.assertFalse(self.root.exists())

    def test_symlink_parent_and_source_symlink_refused_without_writes(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.base, target_is_directory=True)
        result = self.cli('prepare', '--root', alias / 'outside', '--source', self.source, '--archive', self.archive)
        self.assertIn('symlink_path', result[2])
        self.assertFalse((self.base / 'outside').exists())
        (self.source / 'linked').symlink_to(self.archive)
        result = self.cli('prepare', '--root', self.root, '--source', self.source, '--archive', self.archive)
        self.assertIn('symlink_path', result[2])
        self.assertFalse(self.root.exists())

    def test_hardlink_archive_and_unowned_file_refused(self):
        other = self.base / 'archive-alias'
        os.link(self.archive, other)
        result = self.cli('prepare', '--root', self.root, '--source', self.source, '--archive', self.archive)
        self.assertIn('unsafe_file', result[2])
        other.unlink()
        with patch.object(q.os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(q.Refused, 'unsafe_file'):
                q.read_file(self.archive)
        self.assertFalse(self.root.exists())

    def test_user_home_and_live_home_refused_in_real_cli(self):
        userhome = self.base / 'userhome'
        userhome.mkdir()
        live = self.base / 'live-profile'
        env = dict(os.environ, HOME=str(userhome), HERMES_HOME=str(live))
        for root in (userhome, userhome / '.hermes', userhome / '.hermes/new', live, live / 'new'):
            result = subprocess.run([sys.executable, str(SCRIPT), 'prepare', '--root', str(root)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertRegex(result.stderr, 'user_home_refused|live_home_refused')
        self.assertFalse(live.exists())
        self.assertEqual(list(userhome.iterdir()), [])

    def test_windows_backed_mount_refused_before_prepare_writes(self):
        mountinfo = f'40 1 0:40 / {self.base} rw - 9p drvfs rw\n'
        with patch.object(Path, 'read_text', return_value=mountinfo):
            result = self.cli('prepare', '--root', self.root,
                              '--source', self.source, '--archive', self.archive)
        self.assertIn('linux_private_filesystem_required', result[2])
        self.assertFalse(self.root.exists())

    def test_bootstrap_does_not_adopt_arbitrary_existing_root(self):
        self.root.mkdir(mode=0o700)
        (self.root / 'keep').write_text('untouched')
        before = self.snapshot(self.root)
        result = self.cli('serve', '--root', self.root, '--prepare-if-missing')
        self.assertEqual(result[0], 1)
        self.assertEqual(before, self.snapshot(self.root))

    def test_quiesce_requires_consent_before_subprocess_or_writes(self):
        self.prepare()
        before = self.snapshot(self.root)
        with patch.object(q.subprocess, 'run') as execute:
            result = self.cli('quiesce', '--root', self.root)
        self.assertIn('quiesce_consent_required', result[2])
        execute.assert_not_called()
        self.assertEqual(before, self.snapshot(self.root))

    def test_permit_only_for_marked_root_and_bounded_duration(self):
        self.root.mkdir(mode=0o700)
        self.assertEqual(self.permit()[0], 1)
        self.assertFalse((self.root / q.PERMIT).exists())
        self.root.rmdir()
        self.prepare()
        for hours in ('0', '-1', '24.001', 'nan', 'inf'):
            self.assertEqual(self.permit(hours)[0], 1)
            self.assertFalse((self.root / q.PERMIT).exists())
        before = time.time()
        result = self.permit('24')
        self.assertEqual(result[0], 0, result[2])
        permit = json.loads((self.root / q.PERMIT).read_text())
        self.assertLessEqual(permit['expires_at'], time.time() + 86400)
        self.assertGreaterEqual(permit['expires_at'], before + 86400)
        self.assertEqual(permit['home'], str(self.root / '.hermes'))
        self.assertEqual(permit['package_root'], str(q.package_path(self.root)))
        self.assertEqual(permit['manifest_sha256'], q.candidate(q.package_path(self.root)))
        self.assertEqual((self.root / q.PERMIT).stat().st_mode & 0o777, 0o600)

    def test_copied_marker_does_not_authorize_another_root(self):
        self.prepare()
        second = self.base / 'copied'
        shutil.copytree(self.root, second)
        result = self.cli('permit', '--root', second, '--hours', '1')
        self.assertIn('unmarked_root', result[2])
        self.assertFalse((second / q.PERMIT).exists())

    def test_token_symlink_and_public_permissions_fail_closed(self):
        self.prepare()
        token = self.root / q.TOKEN
        token.chmod(0o644)
        self.assertIn('unsafe_file', self.permit()[2])
        token.unlink()
        token.symlink_to(self.archive)
        self.assertIn('symlink_path', self.permit()[2])
        self.assertFalse((self.root / q.PERMIT).exists())

    def test_accepted_candidate_not_silently_qualified(self):
        path = self.source / 'compatibility.json'
        manifest = json.loads(path.read_text())
        manifest['candidate_status'] = 'accepted'
        path.write_text(json.dumps(manifest))
        result = self.cli('prepare', '--root', self.root, '--source', self.source, '--archive', self.archive)
        self.assertIn('candidate_flags_changed', result[2])
        self.assertFalse(self.root.exists())

    def test_independent_roots_have_distinct_tokens_and_bound_permits(self):
        self.prepare()
        second = self.prepare(self.base / 'second')
        self.assertEqual(self.permit()[0], 0)
        self.assertEqual(self.cli('permit', '--root', second, '--hours', '1')[0], 0)
        self.assertNotEqual((self.root / q.TOKEN).read_bytes(), (second / q.TOKEN).read_bytes())
        self.assertNotEqual(json.loads((self.root / q.MARKER).read_text())['id'],
                            json.loads((second / q.MARKER).read_text())['id'])
        self.assertNotEqual(json.loads((self.root / q.PERMIT).read_text())['home'],
                            json.loads((second / q.PERMIT).read_text())['home'])
        (self.root / '.hermes/generated-state').write_text('only first root')
        self.assertFalse((second / '.hermes/generated-state').exists())

    def test_refresh_requires_consent_and_invalidates_permit_without_rotating_token(self):
        self.prepare()
        self.permit()
        before = self.snapshot(self.root)
        args = ('refresh', '--root', self.root, '--source', self.source)
        self.assertIn('refresh_consent_required', self.cli(*args)[2])
        self.assertEqual(before, self.snapshot(self.root))
        token = (self.root / q.TOKEN).read_bytes()
        profile = (self.root / '.hermes/config.yaml').read_bytes()
        (self.source / 'src/hermes_cpa/new_module.py').write_text('new_value = 42\n')
        result = self.cli(*args, '--consent')
        self.assertEqual(result[0], 0, result[2])
        self.assertTrue((q.package_path(self.root) / 'src/hermes_cpa/new_module.py').exists())
        self.assertFalse((self.root / q.PERMIT).exists())
        self.assertEqual(token, (self.root / q.TOKEN).read_bytes())
        self.assertEqual(profile, (self.root / '.hermes/config.yaml').read_bytes())
        self.assertEqual(self.permit()[0], 0)

    def test_refresh_rejects_active_owner_lock_and_changes_nothing(self):
        self.prepare()
        self.permit()
        run = self.root / '.hermes/cpa/run'
        run.mkdir(parents=True, mode=0o700)
        lock = run / 'instance.lock'
        q.atomic_file(lock, b'')
        before = self.snapshot(self.root)
        with lock.open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli('refresh', '--root', self.root, '--source', self.source, '--consent')
        self.assertIn('owner_running_quiesce_first', result[2])
        self.assertEqual(before, self.snapshot(self.root))

    def test_failed_refresh_source_does_not_remove_permit(self):
        self.prepare()
        self.permit()
        before = self.snapshot(self.root)
        (self.source / 'assets/cpamc.html').write_text('tampered')
        result = self.cli('refresh', '--root', self.root, '--source', self.source, '--consent')
        self.assertIn('console_mismatch', result[2])
        self.assertEqual(before, self.snapshot(self.root))

    def test_refresh_removes_obsolete_files_and_preserves_candidate_flags(self):
        (self.source / 'obsolete.py').write_text('old = True\n')
        self.prepare()
        manifest = (self.source / 'compatibility.json').read_bytes()
        (self.source / 'obsolete.py').unlink()
        result = self.cli('refresh', '--root', self.root, '--source', self.source, '--consent')
        self.assertEqual(result[0], 0, result[2])
        self.assertFalse((q.package_path(self.root) / 'obsolete.py').exists())
        self.assertEqual(manifest, (q.package_path(self.root) / 'compatibility.json').read_bytes())

    def test_serve_reuses_token_and_reexecs_without_ambient_credentials(self):
        self.prepare()
        token = (self.root / q.TOKEN).read_text().strip()
        permit_before = self.permit()
        self.assertEqual(permit_before[0], 0)
        original_permit = (self.root / q.PERMIT).read_bytes()
        original_cwd = Path.cwd()
        class Reexec(Exception):
            pass
        try:
            with patch.object(q, 'host_manifest'), patch.dict(os.environ, {'OPENAI_API_KEY': 'not-inherited',
                    'PYTHONPATH': '/foreign/workspace', 'HTTPS_PROXY': 'not-inherited'}):
                for _ in range(2):
                    with patch.object(q.os, 'execve', side_effect=Reexec) as execute:
                        with self.assertRaises(Reexec):
                            self.cli('serve', '--root', self.root, '--host', self.base / 'host', '--port', '19320')
                        _, argv, env = execute.call_args.args
                        self.assertEqual(argv[-1], '19320')
                        self.assertEqual(env['HERMES_DASHBOARD_SESSION_TOKEN'], token)
                        self.assertNotIn(token, ' '.join(argv))
                        self.assertNotIn('OPENAI_API_KEY', env)
                        self.assertNotIn('HTTPS_PROXY', env)
                        self.assertEqual(env['PYTHONPATH'], str(self.base / 'host'))
                        self.assertEqual(env['HOME'], str(self.root))
                        self.assertEqual(env['HERMES_HOME'], str(self.root / '.hermes'))
        finally:
            os.chdir(original_cwd)
        self.assertEqual(original_permit, (self.root / q.PERMIT).read_bytes())
        self.assertEqual(token, (self.root / q.TOKEN).read_text().strip())

    def test_repeated_smoke_reuses_one_client_and_server_epoch(self):
        host = self.running()
        token = (self.root / q.TOKEN).read_text().strip()
        fingerprint = q.digest(token.encode())
        outputs = []
        for index in range(3):
            if index == 1:
                host.select('generated-desktop-window', 0)  # Moves the host-wide epoch counter.
            result, out, err = self.smoke(host.port, '--expect-token-sha256', fingerprint)
            self.assertEqual((result, err), (0, ''))
            outputs.append(out)
        self.assertEqual([json.loads(out)['selection_reused'] for out in outputs], [False, True, True])
        self.assertEqual(host.posts, [(self.client(), '0')])
        cache_path = self.root / q.SELECTION
        cache = json.loads(cache_path.read_text())
        self.assertEqual(cache_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((cache['scope_id'], cache['epoch']), host.selections[self.client()])
        for out in outputs:
            self.assertNotIn(token, out)
            self.assertNotIn(cache['scope_id'], out)
        self.assertIn('session_token_changed', self.smoke(host.port, '--expect-token-sha256', '0' * 64)[2])

    def test_concurrent_smoke_processes_share_one_selection(self):
        host = self.running()
        command = [sys.executable, '-c', WRAPPER, str(SCRIPT), q.ARCHIVE_SHA,
                   'smoke', '--root', str(self.root), '--port', str(host.port)]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(4)]
        results = [(*process.communicate(timeout=60), process.returncode) for process in processes]
        for out, err, code in results:
            self.assertEqual((code, err), (0, ''))
        self.assertEqual(sorted(json.loads(out)['selection_reused'] for out, _, _ in results),
                         [False, True, True, True])
        self.assertEqual(host.posts, [(self.client(), '0')])
        self.assertEqual(list(host.selections), [self.client()])
        with open(self.root / q.SMOKE_LOCK, 'r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(q, 'SMOKE_LOCK_SECONDS', .2):
                self.assertIn('smoke_busy', self.smoke(host.port)[2])
        self.assertEqual(len(host.posts), 1)

    def test_backend_restart_recovers_with_server_returned_epoch(self):
        host = self.running()
        self.assertEqual(self.smoke(host.port)[0], 0)
        host.select('generated-desktop-window', 0)
        before = json.loads((self.root / q.SELECTION).read_text())
        host.restart()  # A new backend process starts with an empty authority.
        self.start_backend(host.port)
        result, out, err = self.smoke(host.port)
        self.assertEqual((result, err), (0, ''))
        self.assertFalse(json.loads(out)['selection_reused'])
        self.assertEqual(host.posts, [(self.client(), '0'), (self.client(), '0')])
        after = json.loads((self.root / q.SELECTION).read_text())
        self.assertNotEqual(before['generation'], after['generation'])
        self.assertEqual((after['scope_id'], after['epoch']), host.selections[self.client()])
        self.assertEqual(self.smoke(host.port)[0], 0)
        self.assertEqual(len(host.posts), 2)

    def test_epoch_zero_is_never_asserted_without_backend_restart(self):
        host = self.running()
        self.assertEqual(self.smoke(host.port)[0], 0)
        cached = json.loads((self.root / q.SELECTION).read_text())
        host.restart()  # Authority lost although serve recorded no new backend.
        result = self.smoke(host.port)
        self.assertEqual(result[0], 1)
        self.assertIn('selection_cache_lost_restart_backend', result[2])
        self.assertEqual(host.posts[-1], (self.client(), str(cached['epoch'])))
        self.assertEqual(host.selections, {})

    def test_selection_cache_lost_while_backend_alive_fails_closed(self):
        host = self.running()
        self.assertEqual(self.smoke(host.port)[0], 0)
        (self.root / q.SELECTION).unlink()
        result = self.smoke(host.port)
        self.assertEqual(result[0], 1)
        self.assertIn('selection_cache_lost_restart_backend', result[2])
        self.assertEqual(list(host.selections), [self.client()])  # No rotated client ID.
        self.assertFalse((self.root / q.SELECTION).exists())
        host.restart()
        self.start_backend(host.port)
        self.assertEqual(self.smoke(host.port)[0], 0)
        self.assertEqual(list(host.selections), [self.client()])

    def test_unsafe_selection_cache_fails_closed_without_reselecting(self):
        host = self.running()
        self.assertEqual(self.smoke(host.port)[0], 0)
        cache = self.root / q.SELECTION
        original = cache.read_bytes()
        posts = len(host.posts)

        def refused():
            result = self.smoke(host.port)
            self.assertEqual(result[0], 1)
            self.assertIn('selection_cache_unsafe', result[2])

        cache.chmod(0o644)
        refused()
        cache.chmod(0o600)
        alias = self.base / 'cache-alias'
        os.link(cache, alias)
        refused()
        alias.unlink()
        moved = self.base / 'cache-target'
        cache.rename(moved)
        cache.symlink_to(moved)
        refused()
        cache.unlink()
        moved.rename(cache)
        for change in ({'epoch': True}, {'epoch': 0}, {'root_id': '0' * 32},
                       {'client': 'qualification-other-root'}, {'unexpected': 1}):
            cache.write_text(json.dumps({**json.loads(original), **change}))
            refused()
        cache.write_text('not json')
        refused()
        self.assertEqual(len(host.posts), posts)
        cache.write_bytes(original)
        result, out, _ = self.smoke(host.port)
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(out)['selection_reused'])

    def test_tampered_package_is_refused_not_reattested(self):
        self.prepare()
        marker = (self.root / q.MARKER).read_bytes()
        module = q.package_path(self.root) / 'src/hermes_cpa/__init__.py'
        module.write_bytes(b'raise SystemExit\n')
        for arguments in (('permit', '--root', self.root, '--hours', '1'),
                          ('smoke', '--root', self.root, '--port', '9'),
                          ('refresh', '--root', self.root, '--source', self.source, '--consent')):
            result = self.cli(*arguments)
            self.assertEqual(result[0], 1)
            self.assertIn('package_tampered', result[2])
        with patch.object(q, 'host_manifest'), patch.object(q.os, 'execve') as execute:
            self.assertIn('package_tampered', self.cli('serve', '--root', self.root, '--port', '9')[2])
        execute.assert_not_called()
        self.assertEqual(marker, (self.root / q.MARKER).read_bytes())
        self.assertFalse((self.root / q.PERMIT).exists())
        self.assertFalse((self.root / q.BACKEND).exists())
        module.write_bytes(b'')
        self.assertEqual(self.permit()[0], 0)
        injected = q.package_path(self.root) / 'src/hermes_cpa/injected.py'
        injected.write_text('injected = True\n')
        self.assertIn('package_tampered', self.permit()[2])

    def test_refresh_blocks_health_until_backend_restarts(self):
        host = self.running()
        self.assertEqual(self.smoke(host.port)[0], 0)
        (self.source / 'src/hermes_cpa/new_module.py').write_text('new_value = 42\n')
        result = self.cli('refresh', '--root', self.root, '--source', self.source, '--consent')
        self.assertEqual(result[0], 0, result[2])
        self.assertTrue(json.loads(result[1])['restart_backend_required'])
        posts = len(host.posts)
        for _ in range(2):
            result = self.smoke(host.port)
            self.assertEqual(result[0], 1)
            self.assertIn('backend_restart_required', result[2])
        self.assertEqual(len(host.posts), posts)
        host.restart()
        self.start_backend(host.port)
        result, out, err = self.smoke(host.port)
        self.assertEqual((result, err), (0, ''))
        current = json.loads((self.root / q.MARKER).read_text())['package_sha256']
        self.assertEqual(json.loads(out)['package_sha256'], current)
        self.assertEqual(json.loads((self.root / q.BACKEND).read_text())['package_sha256'], current)

    def test_identical_refresh_still_requires_backend_restart(self):
        host = self.running()
        self.assertEqual(self.smoke(host.port)[0], 0)
        marker = json.loads((self.root / q.MARKER).read_text())
        result = self.cli('refresh', '--root', self.root, '--source', self.source, '--consent')
        self.assertEqual(result[0], 0, result[2])
        self.assertTrue(json.loads(result[1])['restart_backend_required'])
        self.assertEqual(json.loads((self.root / q.MARKER).read_text())['package_sha256'],
                         marker['package_sha256'])  # Same package, so only the record can enforce it.
        self.assertFalse((self.root / q.BACKEND).exists())
        posts = len(host.posts)
        result = self.smoke(host.port)
        self.assertEqual(result[0], 1)
        self.assertIn('backend_restart_required', result[2])
        self.assertEqual(len(host.posts), posts)
        host.restart()
        self.start_backend(host.port)
        result, out, err = self.smoke(host.port)
        self.assertEqual((result, err), (0, ''))
        self.assertFalse(json.loads(out)['selection_reused'])

    def test_refresh_refuses_unsafe_backend_record_without_changes(self):
        self.running()
        record = self.root / q.BACKEND
        record.chmod(0o644)
        before = self.snapshot(self.root)
        (self.source / 'src/hermes_cpa/new_module.py').write_text('new_value = 42\n')
        result = self.cli('refresh', '--root', self.root, '--source', self.source, '--consent')
        self.assertEqual(result[0], 1)
        self.assertIn('unsafe_file', result[2])
        self.assertEqual(before, self.snapshot(self.root))

    def test_absent_owner_lock_is_created_and_live_owner_refused(self):
        self.prepare()
        cpa = self.root / '.hermes/cpa'
        cpa.mkdir(mode=0o755)  # A plugin-created intermediate directory.
        arguments = ('refresh', '--root', self.root, '--source', self.source, '--consent')
        result = self.cli(*arguments)
        self.assertEqual(result[0], 0, result[2])
        run = cpa / 'run'
        lock = run / 'instance.lock'
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        lock.unlink()  # Failed first start: run directory without instance.lock.
        result = self.cli(*arguments)
        self.assertEqual(result[0], 0, result[2])
        self.assertTrue(lock.exists())
        start = q.process_identity(os.getpid())[1]
        live = {'pid': os.getpid(), 'start': start, 'boot': q.boot_id()}
        q.atomic_file(run / 'identity.json', json.dumps({'owner': live}).encode())
        before = self.snapshot(self.root)
        self.assertIn('owner_running_quiesce_first', self.cli(*arguments)[2])
        self.assertEqual(before, self.snapshot(self.root))
        stale = {**live, 'start': str(int(start) + 1)}
        q.atomic_file(run / 'identity.json', json.dumps({'owner': stale}).encode(), replace=True)
        self.assertEqual(self.cli(*arguments)[0], 0)
        q.atomic_file(run / 'identity.json', json.dumps({'owner': {'pid': os.getpid()}}).encode(), replace=True)
        self.assertIn('invalid_owner_identity', self.cli(*arguments)[2])

    def test_second_backend_for_root_is_refused(self):
        self.prepare()
        lock = self.root / q.BACKEND_LOCK
        second, inherited = [], []

        def exec_backend(*_):
            inherited.extend(os.get_inheritable(fd) for fd in open_fds(lock))
            second.append(self.cli('serve', '--root', self.root, '--host', self.base / 'host', '--port', '19322'))
            raise Exec

        cwd = Path.cwd()
        try:
            with patch.object(q, 'host_manifest'), patch.object(q.os, 'execve', side_effect=exec_backend):
                with self.assertRaises(Exec):
                    self.cli('serve', '--root', self.root, '--host', self.base / 'host', '--port', '19321')
        finally:
            os.chdir(cwd)
        self.assertEqual(inherited, [True])
        self.assertEqual(second[0][0], 1)
        self.assertIn('backend_already_running', second[0][2])
        record = json.loads((self.root / q.BACKEND).read_text())
        self.assertEqual(record['port'], 19321)
        self.assertEqual((self.root / q.BACKEND).stat().st_mode & 0o777, 0o600)
        self.assertEqual(open_fds(lock), [])  # A failed exec releases the backend lock.
        self.start_backend(19323)
        self.assertNotEqual(json.loads((self.root / q.BACKEND).read_text())['generation'], record['generation'])

    def test_malformed_backend_http_is_sanitized(self):
        self.prepare()
        for response in (b'garbage\r\n\r\n',
                         b'HTTP/1.0 401 Unauthorized\r\nContent-Length: 9\r\n\r\nnot json!'):
            listener = socket.create_server(('127.0.0.1', 0))
            self.addCleanup(listener.close)

            def answer(listener=listener, response=response):
                connection, _ = listener.accept()
                with connection:
                    connection.recv(65536)
                    connection.sendall(response)

            thread = threading.Thread(target=answer, daemon=True)
            thread.start()
            port = listener.getsockname()[1]
            self.start_backend(port)
            self.assertEqual(self.smoke(port), (1, '', 'Qualification refused: backend_http_malformed\n'))
            thread.join(5)

    def test_package_excludes_credentials_and_runtime_artifacts(self):
        expected = {str(path.relative_to(self.source)) for path in self.source.rglob('*') if path.is_file()}
        planted = ('.env', '.env.local', '.envrc', 'id.pem', 'src/hermes_cpa/server.KEY', 'token.secret',
                   'state.sqlite3', 'cache.db-wal', 'owner.pid', 'debug.log', 'bundle.zip', 'release.tar.gz',
                   'user-data/profile.json', '.hermes/config.yaml', 'runtime/state.json', 'evidence/run.txt',
                   'node_modules/pkg/index.js', 'test-home/notes.txt', 'src/hermes_cpa/__pycache__/m.pyc',
                   'session-token', 'candidate-permit.json', '.qualification.json', 'dashboard/auth.json',
                   'Credentials.json', '.netrc', '.git-credentials', 'id_ed25519', '.ssh/config',
                   '.aws/credentials', 'secrets/api.txt', 'src/hermes_cpa/client.P12')
        for name in planted:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text('generated secret-like fixture')
        self.prepare()
        package = q.package_path(self.root)
        self.assertEqual({str(path.relative_to(package)) for path in package.rglob('*') if path.is_file()},
                         expected)
        self.assertEqual(self.permit()[0], 0)

    @unittest.skipUnless(shutil.which('git'), 'git is required to apply the exported patch')
    def test_build_host_works_without_git_directory_and_checks_provenance(self):
        # An archive may be nested under another project's checkout. Applying
        # its patch must not use or alter that surrounding Git work tree.
        subprocess.run(['git', 'init', str(self.base)], check=True, capture_output=True)
        host = self.base / 'host'
        host.mkdir()
        (host / 'tracked.py').write_bytes(b'before\n')
        patch_file = self.base / 'host.patch'
        patch_file.write_bytes(b'diff --git a/tracked.py b/tracked.py\n--- a/tracked.py\n'
                               b'+++ b/tracked.py\n@@ -1 +1 @@\n-before\n+after\n')
        provenance = self.base / 'provenance.json'
        manifest = dict(hermes_base_commit=q.BASE, patch_sha256=q.digest(patch_file.read_bytes()),
                        files=[dict(path='tracked.py', before_sha256=q.digest(b'before\n'),
                                    after_sha256=q.digest(b'after\n'))])
        provenance.write_text(json.dumps(manifest))
        with patch.object(q, 'PATCH', patch_file), patch.object(q, 'PROVENANCE', provenance):
            result = self.cli('build-host', '--host', host)
            self.assertEqual(result[0], 0, result[2])
            self.assertEqual((host / 'tracked.py').read_bytes(), b'after\n')
            self.assertFalse((host / '.git').exists())
            (host / 'tracked.py').write_bytes(b'changed\n')
            with self.assertRaisesRegex(q.Refused, 'host_after_hash_mismatch'):
                q.host_manifest(host, 'after')
            (host / 'tracked.py').write_bytes(b'before\n')
            patch_file.write_bytes(patch_file.read_bytes() + b'\n')
            result = self.cli('build-host', '--host', host)
            self.assertIn('host_provenance_mismatch', result[2])
            self.assertEqual((host / 'tracked.py').read_bytes(), b'before\n')


if __name__ == '__main__':
    unittest.main()
