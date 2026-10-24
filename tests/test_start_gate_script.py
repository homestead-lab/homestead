"""#746: the start gate's script, run under /bin/sh against ports on this
machine. A fake nc stands in for busybox's (`nc -z -w 2 host port`)."""
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_start_order as START

FAKE_NC = f"""#!{sys.executable}
import socket, sys
host, port = sys.argv[-2], int(sys.argv[-1])
try:
    socket.create_connection((host, port), timeout=2).close(); sys.exit(0)
except OSError:
    sys.exit(1)
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class GateScriptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        nc = Path(self.dir.name, "nc")
        nc.write_text(FAKE_NC)
        nc.chmod(nc.stat().st_mode | stat.S_IEXEC)
        self.message = Path(self.dir.name, "message")

    def listen(self, port):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(8)
        self.addCleanup(server.close)
        return server

    def run_gate(self, port, timeout, on_timeout):
        env = {"PATH": f"{self.dir.name}:{os.environ.get('PATH', '/usr/bin:/bin')}",
               "HS_AFTER": f"127.0.0.1:{port}", "HS_TIMEOUT": str(timeout), "HS_ON_TIMEOUT": on_timeout,
               "HS_MESSAGE": str(self.message)}
        started = time.monotonic()
        done = subprocess.run(["/bin/sh", "-c", START.SCRIPT], env=env, capture_output=True, text=True, timeout=20)
        return done, time.monotonic() - started

    def test_ready_dependency_exits_at_once(self):
        port = free_port()
        self.listen(port)
        done, took = self.run_gate(port, 300, "start")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertLess(took, 2)
        self.assertRegex(done.stdout.strip(), r"\Aready after [01] s\Z", "whole seconds: it may cross one")
        self.assertFalse(self.message.exists())

    def test_start_anyway_after_timeout(self):
        port = free_port()
        done, took = self.run_gate(port, 3, "start")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertGreater(took, 2, "date +%s counts whole seconds, so 3 s may be a little over 2")
        self.assertEqual(f"timeout 127.0.0.1:{port}", self.message.read_text().strip())
        self.assertRegex(done.stdout, rf"waiting for 127\.0\.0\.1:{port} \([01] s of 3 s\)")
        self.assertIn("starting anyway", done.stdout)

    def test_keep_waiting_until_it_comes_up(self):
        port = free_port()
        timer = threading.Timer(5, self.listen, (port,))
        timer.start()
        self.addCleanup(timer.cancel)
        done, took = self.run_gate(port, 3, "wait")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertGreater(took, 5)
        self.assertIn("ready after", done.stdout)
        self.assertFalse(self.message.exists())


if __name__ == "__main__":
    unittest.main()
