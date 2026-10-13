"""Private, bounded revalidation bridge from owner to accepted host capability.

Only backend code creates grants. A grant binds the exact supervisor intent,
original request scope and authority process identity; it is never UI data.
Authority loss denies continuation rather than adopting a new host context.
"""
import hashlib
import json
import os
import secrets
import socket
import threading
import time

from .process_owner import private_directory, process_identity, peer_pid, socket_address


def intent_digest(intent):
    return hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


class OperationGuardServer:
    def __init__(self, directory):
        self.directory = directory
        private_directory(directory)
        self.path = directory / 'supervisor.sock'
        self.records = {}
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with socket_address(directory) as address:
            self.socket.bind(address)
        self.path.chmod(0o600)
        self.socket.listen(8)
        self.socket.settimeout(.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def reclaim_terminal(self, *, preserve_digest=None, timeout=None):
        """Make one admission slot, never evict uncertain or in-flight work."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.lock:
            if len(self.records) < 100 or any(
                    record[0] == preserve_digest for record in self.records.values()):
                return
            for token, record in list(self.records.items()):
                if record[4] is None or record[5] is None:
                    continue
                try:
                    if deadline is None:
                        terminal, retired = record[4](), None
                        if terminal is True:
                            retired = record[5]()
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return
                        terminal, retired = record[4](timeout=remaining), None
                        remaining = deadline - time.monotonic()
                        if terminal is True and remaining > 0:
                            retired = record[5](timeout=remaining)
                    if terminal is not True or retired is not True:
                        return
                except Exception:
                    return
                record[2] = True
                del self.records[token]
                return

    def grant(self, intent, revalidate, *, authority_key=None, terminal=None, retire=None):
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError('BUSY')
            digest = intent_digest(intent)
            if authority_key is not None:
                for token, record in self.records.items():
                    if record[3] == authority_key and record[0] == digest:
                        return {'directory': str(self.directory), 'authority': process_identity(os.getpid()),
                                'token': token, 'digest': digest}
            if self.closed.is_set() or len(self.records) >= 100:
                raise RuntimeError('BUSY')
            token = secrets.token_hex(32)
            self.records[token] = [digest, revalidate, False, authority_key, terminal, retire]
            return {'directory': str(self.directory), 'authority': process_identity(os.getpid()),
                    'token': token, 'digest': digest}

    def _serve(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(.5)
                try:
                    peer_pid(connection)
                    raw = bytearray()
                    deadline = time.monotonic() + .5
                    while not raw.endswith(b'\n'):
                        connection.settimeout(max(.001, deadline - time.monotonic()))
                        chunk = connection.recv(4097 - len(raw))
                        if not chunk or len(raw) + len(chunk) > 4096 or time.monotonic() >= deadline:
                            raise ValueError('invalid')
                        raw.extend(chunk)
                    request = json.loads(raw)
                    with self.lock:
                        record = self.records.get(request.get('token'))
                    allowed = False
                    if record is not None and not record[2] and request.get('digest') == record[0]:
                        # The host's private RPC already has its own two-second
                        # bound. Never accept an arbitrary renderer callback here.
                        try:
                            allowed = record[1]() is True
                        except Exception:
                            allowed = False
                        with self.lock:
                            allowed = (allowed and not self.closed.is_set() and
                                       self.records.get(request.get('token')) is record and not record[2])
                            if not allowed:
                                record[2] = True
                    connection.sendall(b'true\n' if allowed else b'false\n')
                except (OSError, ValueError, TypeError):
                    continue

    def close(self):
        self.closed.set()
        self.socket.close()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError('authority_shutdown_timeout')
        self.path.unlink(missing_ok=True)
        with self.lock:
            self.records.clear()


def revalidate_grant(grant, intent):
    """Owner checks a backend grant; total transport deadline is three seconds."""
    from pathlib import Path
    try:
        if not isinstance(grant, dict) or grant.get('digest') != intent_digest(intent):
            return False
        identity = grant['authority']
        if process_identity(identity['pid']) != identity:
            return False
        directory = Path(grant['directory'])
        if not directory.is_absolute() or directory.is_symlink():
            return False
        deadline = time.monotonic() + 3
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            with socket_address(directory) as address:
                connection.connect(address)
            if peer_pid(connection) != identity['pid'] or process_identity(identity['pid']) != identity:
                return False
            connection.sendall(json.dumps({'token': grant['token'], 'digest': grant['digest']}).encode() + b'\n')
            result = bytearray()
            while not result.endswith(b'\n'):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                connection.settimeout(remaining)
                chunk = connection.recv(7 - len(result))
                if not chunk or len(result) + len(chunk) > 6:
                    return False
                result.extend(chunk)
            return result == b'true\n'
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False
