"""A command in a pod, over a real socket.

A local server answers the exec upgrade (101) and speaks the Kubernetes
v4.channel.k8s.io protocol: each websocket frame starts with its channel
(0 stdin, 1 stdout, 2 stderr, 3 the run's status as JSON), the server's
frames unmasked, the client's masked. Only TLS is left out: the context
hands back the plain socket.
"""
import hashlib
import json
import socket
import sys
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_files as FILES
from homestead_console import encode_frame, read_frame

SUCCESS = {"metadata": {}, "status": "Success"}
EXIT_3 = {"metadata": {}, "status": "Failure", "message": "command terminated with non-zero exit code: exit status 3",
          "reason": "NonZeroExitCode", "details": {"causes": [{"reason": "ExitCode", "message": "3"}]}}


def send(conn, channel, data):
    conn.sendall(encode_frame(bytes([channel]) + data))


def status(conn, value):
    send(conn, 3, json.dumps(value).encode())


class ExecServer:
    """One exec at a time: answers 101, then runs script(conn)."""

    def __init__(self, script):
        self.script = script
        self.requests = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    head += chunk
                self.requests.append(head.decode("latin-1"))
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             b"Sec-WebSocket-Protocol: v4.channel.k8s.io\r\n\r\n")
                try:
                    self.script(conn)
                except OSError:
                    pass

    def close(self):
        self.listener.close()


class PlainContext:
    def wrap_socket(self, raw, server_hostname=None):
        return raw


class ExecTests(unittest.TestCase):
    def serve(self, script):
        server = ExecServer(script)
        self.addCleanup(server.close)
        api = urllib.parse.urlparse(f"https://127.0.0.1:{server.port}")
        self.enterContext(mock.patch.multiple(FILES, API=api, TOKEN="token", CTX=PlainContext()))
        return server

    def run_exec(self, **kwargs):
        start = time.monotonic()
        result = FILES._exec("lab", "helper", ["sh", "-c", "true"], **kwargs)
        return result, time.monotonic() - start

    # Today's behaviour, pinned before anything changes.

    def test_stdout_and_stderr_of_a_command_that_finishes(self):
        def script(conn):
            send(conn, 1, b"hello\n")
            send(conn, 2, b"a warning\n")
            status(conn, SUCCESS)

        server = self.serve(script)
        (out, err), _ = self.run_exec()
        self.assertEqual((b"hello\n", "a warning"), (out, err))
        self.assertIn("GET /api/v1/namespaces/lab/pods/helper/exec?container=files&stdout=true&stderr=true"
                      "&command=sh&command=-c&command=true HTTP/1.1", server.requests[0])
        self.assertIn("Sec-WebSocket-Protocol: v4.channel.k8s.io", server.requests[0])

    def test_a_nonzero_exit_reads_like_any_other_end(self):
        def script(conn):
            send(conn, 1, b"some\n")
            status(conn, EXIT_3)

        self.serve(script)
        (out, err), _ = self.run_exec()
        self.assertEqual((b"some\n", ""), (out, err))

    def test_a_command_that_trickles_is_waited_for_past_its_timeout(self):
        def script(conn):
            for _ in range(6):
                send(conn, 1, b".")
                time.sleep(0.25)
            status(conn, SUCCESS)

        self.serve(script)
        (out, _), took = self.run_exec(timeout=0.5)
        self.assertEqual(b"......", out)
        self.assertGreater(took, 1.2)

    def test_a_command_that_goes_quiet_returns_what_it_had_as_if_it_had_finished(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def script(conn):
            send(conn, 1, b"partial")
            release.wait(5)

        self.serve(script)
        (out, err), took = self.run_exec(timeout=0.5)
        self.assertEqual((b"partial", ""), (out, err))
        self.assertLess(took, 3)

    def test_a_connection_that_closes_mid_frame_returns_what_came_before(self):
        def script(conn):
            send(conn, 1, b"before")
            conn.sendall(bytes([0x82, 100, 1]) + b"cut short")

        self.serve(script)
        (out, err), _ = self.run_exec()
        self.assertEqual((b"before", ""), (out, err))

    def test_two_hundred_kilobytes_of_stdin_arrive_whole(self):
        stdin = bytes(range(256)) * 800

        def script(conn):
            got = bytearray()
            while len(got) < len(stdin):
                _, _, payload = read_frame(conn, require_mask=True)
                self.assertEqual(0, payload[0])
                got.extend(payload[1:])
            send(conn, 1, hashlib.sha256(got).hexdigest().encode())
            status(conn, SUCCESS)

        server = self.serve(script)
        (out, _), _ = self.run_exec(stdin=stdin)
        self.assertEqual(hashlib.sha256(stdin).hexdigest().encode(), out)
        self.assertIn("&stdin=true", server.requests[0])

    def test_output_is_not_limited(self):
        piece = b"x" * (512 * 1024)

        def script(conn):
            for _ in range(10):
                send(conn, 1, piece)
            status(conn, SUCCESS)

        self.serve(script)
        (out, _), _ = self.run_exec()
        self.assertEqual(10 * len(piece), len(out))


if __name__ == "__main__":
    unittest.main()
