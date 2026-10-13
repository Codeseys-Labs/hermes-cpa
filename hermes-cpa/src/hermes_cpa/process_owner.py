"""Linux identity, private filesystem and child parent-death primitives.

Never signal a PID recovered from a file. Only a live, unreaped Popen handle
and its captured kernel start identity confer termination authority.
"""
import ctypes
import json
import os
import platform
import signal
import socket
import stat
import sys
from contextlib import contextmanager
from pathlib import Path


def require_platform():
    if sys.platform != 'linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('unsupported_platform')


def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start': fields[19],
                'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError):
        return None


def check_process_alive(pid):
    identity = process_identity(pid)
    if not identity:
        return False
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


def private_directory(path):
    path = Path(path).absolute()
    # Reject symlinks anywhere, not only at the leaf; never chmod an outsider.
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise RuntimeError('unsafe_path')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.stat()
    if st.st_uid != os.getuid() or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError('unsafe_path')
    os.chmod(path, 0o700)


def private_open(path, flags=os.O_RDONLY):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise RuntimeError('unsafe_file')
    if st.st_mode & 0o077:
        os.close(fd)
        raise RuntimeError('unsafe_permissions')
    return fd


def acquire_lease(run_dir, *, timeout=0.):
    """Hold one shared plugin-lifetime lease; return its fd or None.

    One fd per registration, so independent registrations in a process are
    counted separately. The kernel drops it when the holder closes or dies;
    no PID file is ever consulted. A closing owner holds the lock exclusively
    until it has released its instance lock, so a later holder never attaches
    to an owner that already decided to exit.
    """
    import fcntl
    import time
    private_directory(run_dir)
    fd = private_open(Path(run_dir) / 'lifetime.lock', os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(.05)
        except OSError:
            os.close(fd)
            raise


def atomic_json(path, value):
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.identity-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def socket_address(run_dir):
    """Linux pathname sockets via an open directory handle avoid sun_path limits.

    Both ends resolve their own fd to the same private inode. No global /tmp
    socket, hashed alias, abstract socket or weakened directory permissions.
    """
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise RuntimeError('unsafe_directory')
        yield f'/proc/self/fd/{fd}/supervisor.sock'
    finally:
        os.close(fd)


def peer_pid(conn):
    import struct
    pid, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid():
        raise RuntimeError('unowned_peer')
    return pid


def owns_loopback_listener(pid, port):
    """Require the expected literal loopback listening inode in the owned PID."""
    try:
        inodes = set()
        for fd in Path(f'/proc/{pid}/fd').iterdir():
            target = os.readlink(fd)
            if target.startswith('socket:['):
                inodes.add(target[8:-1])
        expected = f'0100007F:{port:04X}'
        for line in Path(f'/proc/{pid}/net/tcp').read_text().splitlines()[1:]:
            fields = line.split()
            if fields[1] == expected and fields[3] == '0A' and fields[9] in inodes:
                return True
    except (OSError, IndexError):
        pass
    return False


def signal_owned(child, identity, sig):
    # An unreaped Popen child cannot have its PID recycled. Do not killpg: a
    # process group number alone is not authority over every group member.
    if child is None or child.poll() is not None:
        return True
    if identity != process_identity(child.pid):
        return False
    child.send_signal(sig)
    return True


def child_exec(parent_pid, argv):
    require_platform()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise RuntimeError('parent_death_unavailable')
    # Parent can die between fork/exec and prctl. Close that race explicitly.
    if os.getppid() != parent_pid:
        os._exit(125)
    os.setpgrp()
    os.execv(argv[0], argv)


if __name__ == '__main__':
    child_exec(int(sys.argv[1]), sys.argv[2:])
