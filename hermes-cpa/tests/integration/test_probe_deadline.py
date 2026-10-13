"""Real trickling loopback peers, generated payload only; no live CPA."""
import socket
import threading
import time
from hermes_cpa.controller import DeadlineAdapter


def test_trickling_headers_and_body_have_total_deadline():
    for headers_first in (False, True):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            server.settimeout(2)
            finished = threading.Event()
            def serve():
                try:
                    conn, _ = server.accept()
                    with conn:
                        conn.settimeout(1)
                        conn.recv(4096)
                        if headers_first:
                            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nX-CPA-VERSION: 8.0.21\r\n\r\n')
                        for _ in range(60):
                            if finished.wait(.02):
                                break
                            conn.sendall(b'x')
                except OSError:
                    pass
            thread = threading.Thread(target=serve)
            thread.start()
            try:
                adapter = DeadlineAdapter(f'http://127.0.0.1:{server.getsockname()[1]}', 'generated-only')
                started = time.monotonic()
                result = adapter.probe(timeout=.15)
                elapsed = time.monotonic() - started
                assert not result.management_ready
                assert elapsed < .5, elapsed
            finally:
                finished.set()
                thread.join(timeout=2)
                assert not thread.is_alive()
