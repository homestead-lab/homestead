"""A command in a pod, over a real socket.

A local server answers the exec upgrade (101) and speaks the Kubernetes
v4.channel.k8s.io protocol: each websocket frame starts with its channel
(0 stdin, 1 stdout, 2 stderr, 3 the run's status as JSON), the server's
frames unmasked, the client's masked. Only TLS is left out: the context
hands back the plain socket.
"""
import hashlib
import json
import os
import subprocess
import tempfile
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
import homestead_hostrun as HOSTRUN
import homestead_pod_exec as POD_EXEC
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
        self.enterContext(mock.patch.multiple(POD_EXEC, API=api, TOKEN="token", CTX=PlainContext()))
        return server

    def run_exec(self, **kwargs):
        start = time.monotonic()
        result = POD_EXEC.run("lab", "helper", ["sh", "-c", "true"], **kwargs)
        return result, time.monotonic() - start

    def assert_times_out(self, took_less_than, **kwargs):
        start = time.monotonic()
        with self.assertRaises(POD_EXEC.ExecTimeout) as caught:
            POD_EXEC.run("lab", "helper", ["sh", "-c", "true"], **kwargs)
        self.assertLess(time.monotonic() - start, took_less_than)
        self.assertIn("did not finish within", str(caught.exception))
        self.assertIn("not known", str(caught.exception))
        return caught.exception

    # What does not change: a command that finishes.

    def test_stdout_and_stderr_of_a_command_that_finishes(self):
        def script(conn):
            send(conn, 1, b"hello\n")
            send(conn, 2, b"a warning\n")
            status(conn, SUCCESS)

        server = self.serve(script)
        result, _ = self.run_exec()
        out, err = result
        self.assertEqual((b"hello\n", "a warning"), (out, err))
        self.assertEqual(0, result.exit_code)
        self.assertFalse(result.cut)
        self.assertIn("GET /api/v1/namespaces/lab/pods/helper/exec?container=files&stdout=true&stderr=true"
                      "&command=sh&command=-c&command=true HTTP/1.1", server.requests[0])
        self.assertIn("Sec-WebSocket-Protocol: v4.channel.k8s.io", server.requests[0])

    def test_a_nonzero_exit_returns_its_code_and_is_not_an_error(self):
        def script(conn):
            send(conn, 1, b"some\n")
            status(conn, EXIT_3)

        self.serve(script)
        result, _ = self.run_exec()
        self.assertEqual((b"some\n", ""), tuple(result))
        self.assertEqual(3, result.exit_code)

    def test_a_status_without_an_exit_code_reads_as_none(self):
        def script(conn):
            status(conn, {"metadata": {}, "status": "Failure", "message": "executable file not found"})

        self.serve(script)
        result, _ = self.run_exec()
        self.assertIsNone(result.exit_code)

    # The deadline is for the whole run, and a timeout is an error.

    def test_a_command_that_trickles_is_stopped_at_its_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def script(conn):
            for _ in range(50):          # at most 5 s: a run with no deadline ends, and fails below
                if release.is_set():
                    return
                send(conn, 1, b".")
                time.sleep(0.1)
            status(conn, SUCCESS)

        self.serve(script)
        error = self.assert_times_out(2, timeout=0.6)
        self.assertTrue(error.stdout and set(error.stdout) == {ord(".")}, error.stdout)

    def test_a_command_that_goes_quiet_raises_with_what_it_had_printed(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def script(conn):
            send(conn, 1, b"partial")
            send(conn, 2, b"half a warning")
            release.wait(5)

        self.serve(script)
        error = self.assert_times_out(2, timeout=0.5)
        self.assertEqual((b"partial", "half a warning"), (error.stdout, error.stderr))
        self.assertEqual(0.5, error.timeout)

    def test_stdin_that_cannot_be_sent_in_time_raises(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def script(conn):
            release.wait(5)   # never reads: the send buffers fill

        self.serve(script)
        self.assert_times_out(3, timeout=0.5, stdin=b"x" * (64 * 1024 * 1024))

    def test_a_connection_that_closes_mid_frame_returns_what_came_before_with_no_exit_code(self):
        def script(conn):
            send(conn, 1, b"before")
            conn.sendall(bytes([0x82, 100, 1]) + b"cut short")

        self.serve(script)
        result, _ = self.run_exec()
        self.assertEqual((b"before", ""), tuple(result))
        self.assertIsNone(result.exit_code)

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
        result, _ = self.run_exec(stdin=stdin)
        self.assertEqual(hashlib.sha256(stdin).hexdigest().encode(), result.stdout)
        self.assertEqual(0, result.exit_code)
        self.assertIn("&stdin=true", server.requests[0])

    def test_output_past_four_mebibytes_is_cut_with_a_marker(self):
        piece = b"x" * (512 * 1024)

        def script(conn):
            for _ in range(10):
                send(conn, 1, piece)
                send(conn, 2, piece)
            status(conn, SUCCESS)

        self.serve(script)
        result, _ = self.run_exec()
        self.assertEqual(4 * 1024 * 1024 + len(POD_EXEC.CUT), len(result.stdout))
        self.assertTrue(result.stdout.endswith(POD_EXEC.CUT))
        self.assertTrue(result.stderr.endswith(POD_EXEC.CUT.decode().strip()))
        self.assertTrue(result.cut)
        self.assertEqual(0, result.exit_code)

    def test_an_upgrade_that_never_comes_is_a_timeout_and_its_socket_is_closed(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        accepted = []

        def accept():
            conn, _ = listener.accept()
            accepted.append(conn)
            conn.recv(65536)        # reads the request, never answers it

        threading.Thread(target=accept, daemon=True).start()
        api = urllib.parse.urlparse(f"https://127.0.0.1:{listener.getsockname()[1]}")
        self.enterContext(mock.patch.multiple(POD_EXEC, API=api, TOKEN="token", CTX=PlainContext()))
        self.assert_times_out(3, timeout=0.5)
        accepted[0].settimeout(2)
        self.assertEqual(b"", accepted[0].recv(1), "the client closed its side")

    def test_a_run_that_said_how_it_ended_is_not_a_timeout_at_the_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def script(conn):
            got = 0
            while got < 5:
                got += len(read_frame(conn, require_mask=True)[2]) - 1
            send(conn, 1, b"read it")
            status(conn, EXIT_3)
            release.wait(5)          # v4 with stdin: the server may not close

        self.serve(script)
        result, took = self.run_exec(stdin=b"hello", timeout=0.5)
        self.assertEqual((b"read it", 3), (result.stdout, result.exit_code))
        self.assertLess(took, 2)

    # The callers.

    def test_the_file_browser_runs_its_commands_through_it(self):
        def script(conn):
            send(conn, 1, b"listed\n")
            status(conn, SUCCESS)

        server = self.serve(script)
        out, err = FILES._sh("lab", "helper", "ls")
        self.assertEqual((b"listed\n", ""), (out, err))
        self.assertIn("command=sh&command=-c&command=ls", server.requests[0])

    def test_a_host_script_that_times_out_says_so_and_its_helper_is_removed(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def script(conn):
            send(conn, 1, b"STEP=half\n")
            release.wait(5)

        self.serve(script)
        sent = []
        running = {"status": {"phase": "Running"}}
        with mock.patch.object(HOSTRUN, "ksend", side_effect=lambda *a, **k: sent.append(a[:2]) or {}), \
                mock.patch.object(HOSTRUN, "kget", return_value=running), \
                mock.patch.object(HOSTRUN, "exec_in", lambda *a, **k: POD_EXEC.run(*a, **k)), \
                mock.patch.object(HOSTRUN, "_clear_ended"):
            with self.assertRaises(POD_EXEC.ExecTimeout) as caught:
                HOSTRUN.run("node-a", "echo hi", timeout=0.5)
        self.assertIn("node-a", str(caught.exception))
        self.assertIn("stays changed", str(caught.exception))
        self.assertEqual("STEP=half\n", caught.exception.stdout)
        self.assertEqual("DELETE", sent[-1][0])

    def test_a_host_script_returns_its_exit_code(self):
        def script(conn):
            send(conn, 1, b"done\n")
            status(conn, EXIT_3)

        self.serve(script)
        running = {"status": {"phase": "Running"}}
        with mock.patch.object(HOSTRUN, "ksend", return_value={}), \
                mock.patch.object(HOSTRUN, "kget", return_value=running), \
                mock.patch.object(HOSTRUN, "exec_in", lambda *a, **k: POD_EXEC.run(*a, **k)), \
                mock.patch.object(HOSTRUN, "_clear_ended"):
            result = HOSTRUN.run("node-a", "exit 3", timeout=5)
        out, err = result
        self.assertEqual(("done\n", ""), (out, err))
        self.assertEqual(3, result.exit_code)


class RunningServer(ExecServer):
    """Runs the command it is asked for, here, as the pod would: the file
    browser's own scripts against a real folder."""

    def __init__(self):
        super().__init__(self._run)

    def _run(self, conn):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.requests[-1].split(" ")[1]).query)
        process = subprocess.Popen(query["command"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while True:
            chunk = process.stdout.read1(256 * 1024)
            if not chunk:
                break
            send(conn, 1, chunk)
        err = process.stderr.read()
        if err:
            send(conn, 2, err)
        code = process.wait()
        status(conn, SUCCESS if code == 0 else {"status": "Failure", "reason": "NonZeroExitCode",
                                                 "details": {"causes": [{"reason": "ExitCode", "message": str(code)}]}})


class BigFolders(unittest.TestCase):
    """A folder of 60k entries, with names long enough that a full listing
    would pass the 4 MiB cut, lists its first 500 - folders first - and says
    it is truncated."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.big = os.path.join(cls.tmp.name, "big")
        os.makedirs(cls.big)
        for index in range(3):
            os.mkdir(os.path.join(cls.big, f"zz-folder-{index}"))
        for index in range(60000):
            open(os.path.join(cls.big, f"file-{index:05d}-" + "n" * 90), "w").close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        server = RunningServer()
        self.addCleanup(server.close)
        api = urllib.parse.urlparse(f"https://127.0.0.1:{server.port}")
        self.enterContext(mock.patch.multiple(POD_EXEC, API=api, TOKEN="token", CTX=PlainContext()))
        self.enterContext(mock.patch.object(FILES, "MOUNT", self.tmp.name))
        self.enterContext(mock.patch.object(FILES, "_session", return_value=("lab", "helper")))
        self.enterContext(mock.patch.object(FILES, "open_session", return_value={"namespace": "lab", "pod": "helper"}))

    def check(self, listing):
        self.assertTrue(listing["truncated"])
        self.assertEqual(FILES.MAX_LIST, len(listing["entries"]))
        self.assertEqual(["zz-folder-0", "zz-folder-1", "zz-folder-2"], [e["name"] for e in listing["entries"][:3]])

    def test_the_file_manager_lists_the_first_entries_of_a_huge_folder(self):
        start = time.monotonic()
        self.check(FILES.list_entries("lab", "data", "big"))
        self.assertLess(time.monotonic() - start, 30)

    def test_the_file_list_lists_the_first_entries_of_a_huge_folder(self):
        self.check(FILES.list_files("lab", "data", "big"))

    def test_an_answer_cut_at_the_cap_is_refused_in_words(self):
        cut = POD_EXEC.Output(b"half", "", 0, True)
        with mock.patch.object(FILES, "_sh", return_value=cut):
            with self.assertRaisesRegex(ValueError, "4 MiB"):
                FILES._run("lab", "data", "true")

class CutAnswers(unittest.TestCase):
    """A reader whose JSON answer went past the cap says so, rather than
    failing to parse half of it."""

    def test_a_snapshot_walk_cut_at_the_cap_is_refused_in_words(self):
        import homestead_snapshot_restore as RESTORE
        cut = POD_EXEC.Output(b'{"entries": [{"path": "a"', "", 0, True)
        with mock.patch.object(RESTORE.POD_EXEC, "run", return_value=cut):
            with self.assertRaisesRegex(ValueError, "4 MiB"):
                RESTORE._walk("lab", "session", "", ["a"])

    def test_a_snapshot_read_cut_at_the_cap_is_refused_in_words(self):
        import homestead_snapshot_files as SNAPSHOT
        cut = POD_EXEC.Output(b'{"entries": [', "", 0, True)
        with mock.patch.object(SNAPSHOT, "_verified"), mock.patch.object(SNAPSHOT.POD_EXEC, "run", return_value=cut):
            with self.assertRaisesRegex(ValueError, "4 MiB"):
                SNAPSHOT.read("lab", "copy", "list", "folder")

if __name__ == "__main__":
    unittest.main()
