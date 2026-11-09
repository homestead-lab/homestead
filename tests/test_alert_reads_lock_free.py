"""Alert reads take no lock, and keep working while Homestead's data is moved (#906).

Writers replace alerts.json whole (write_json: a temporary file, fsync, then a
rename over the old one), so a reader sees the old file or the new one and
needs no lock. Writers keep the alerts lock and the data-move barrier. A plain
GET is not data activity any more, but each write it causes still is.

The other side of each test is a real process with real files and real flock:
the data volume is a shared NFS volume, where flock is a lock call to the
server and a second replica is a second process on another node.
"""
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

SERVER_DIR = os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, SERVER_DIR)
import homestead_alerts as A
import homestead_self_data_fence as F
import homestead_shared as S
from homestead_storage_journal import Held

FREEZE = """
import sys
sys.path.insert(0, sys.argv[1])
from homestead_self_data_fence import WriteBarrier
with WriteBarrier(sys.argv[2], lambda: None).freeze(timeout=5):
    print('frozen', flush=True)
    sys.stdin.readline()
"""

FREEZE_TRY = """
import sys
sys.path.insert(0, sys.argv[1])
from homestead_self_data_fence import WriteBarrier
from homestead_storage_journal import Held
try:
    with WriteBarrier(sys.argv[2], lambda: None).freeze(timeout=float(sys.argv[3])):
        print('froze', flush=True)
except Held as error:
    print('held: ' + str(error), flush=True)
"""

HOLD_ALERTS_LOCK = """
import sys
sys.path.insert(0, sys.argv[1])
import homestead_shared as S
S.bind(sys.argv[2])
with S.SharedLock("alerts"):
    print('held', flush=True)
    sys.stdin.readline()
"""

WRITE_ALERTS = """
import sys
sys.path.insert(0, sys.argv[1])
import homestead_shared as S, homestead_alerts as A
S.bind(sys.argv[2]); A.bind(sys.argv[2])
print('writing', flush=True)
for n in range(int(sys.argv[3])):
    A.note({"key": f"event:{n}", "category": "health", "title": f"Event {n}", "body": "x" * 2000, "event": True})
print('done', flush=True)
"""


def seed(directory):
    """Alert state as a writer leaves it: one open alert, three log lines."""
    row = {"key": "disk:node-a", "category": "health", "severity": "degraded", "title": "Disk",
           "body": "A disk is failing", "since": 1, "announced": 1}
    log = [{"id": n, "at": n, "phase": "raised", "key": f"event:{n}", "category": "health",
            "severity": "info", "title": f"Event {n}", "body": "", "event": True} for n in (1, 2, 3)]
    S.write_json(os.path.join(directory, "alerts.json"),
                 {"seq": 3, "active": {row["key"]: row}, "log": log, "acknowledgements": {}})
    return log


@contextmanager
def other_process(test, script, *args, ready):
    """Run script in another process until the block ends: that is the
    other replica on the shared volume."""
    proc = subprocess.Popen([sys.executable, "-u", "-c", script, SERVER_DIR, *args],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        test.assertEqual(ready, proc.stdout.readline().strip(), proc.stderr.read() if proc.poll() is not None else "")
        yield proc
    finally:
        try:
            _, error = proc.communicate("finish\n", timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate(); test.fail("the other process did not finish")
    test.assertEqual(0, proc.returncode, error)


class Volume(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = temp.name
        for module, before in ((S, S.DIR), (A, A.DATA_DIR)):
            self.addCleanup(module.bind, before)
            module.bind(self.directory)
        # The server's write scope: every write on the volume is data activity.
        self.barrier = F.WriteBarrier(self.directory, lambda: None)
        scope = mock.patch.object(S, "WRITE_SCOPE", lambda _path: self.barrier.activity())
        scope.start(); self.addCleanup(scope.stop)
        self.log = seed(self.directory)


@unittest.skipUnless(F.fcntl is not None, "Linux shared locking")
class AlertReadsDuringAMove(Volume):
    def test_alert_reads_work_while_another_replica_freezes_for_a_data_move(self):
        with other_process(self, FREEZE, self.directory, ready="frozen"):
            self.assertEqual(3, A.log(limit=0)["latest"])
            self.assertEqual(["disk:node-a"], [row["key"] for row in A.active()])
            self.assertEqual(self.log, A.for_user(self.log, "member"))

    def test_alert_writes_still_wait_for_the_data_move(self):
        with other_process(self, FREEZE, self.directory, ready="frozen"):
            with self.assertRaisesRegex(Held, "finishing current work"):
                A.note({"key": "event:new", "category": "health", "title": "New", "event": True})
            self.assertEqual(3, A.log(limit=0)["latest"])        # nothing was written
        A.note({"key": "event:new", "category": "health", "title": "New", "event": True})
        self.assertEqual(4, A.log(limit=0)["latest"])


@unittest.skipUnless(F.fcntl is not None, "Linux shared locking")
class AlertReadsTakeNoLock(Volume):
    def test_a_reader_does_not_wait_for_another_replica_holding_the_alerts_lock(self):
        answer = {}
        reader = threading.Thread(target=lambda: answer.update(A.log(limit=0)), daemon=True)
        with other_process(self, HOLD_ALERTS_LOCK, self.directory, ready="held"):
            reader.start(); reader.join(5)
            finished = not reader.is_alive()
        reader.join(5)
        self.assertTrue(finished, "the alert read waited for the alerts lock")
        self.assertEqual(3, answer["latest"])

    def test_a_reader_never_sees_a_missing_or_partial_file_while_another_replica_writes(self):
        seen, failures = [], []
        with other_process(self, WRITE_ALERTS, self.directory, "300", ready="writing") as writer:
            while writer.poll() is None:
                latest = A.log(limit=0)["latest"]
                if latest < 3 or seen and latest < seen[-1]:
                    failures.append(latest)        # 0: the file was missing or not whole JSON
                seen.append(latest)
        self.assertEqual([], failures[:5])
        self.assertGreater(len(set(seen)), 1, "the reader never overlapped the writer")
        self.assertEqual(303, A.log(limit=0)["latest"])


class StaleHandles(Volume):
    """On the NFS share a reader on another node can hold the file a writer
    just renamed over: open or read answers ESTALE. The new file is there."""

    def estale(self, module, times, at="open"):
        real, calls = open, []
        def fake(path, *args, **kwargs):
            calls.append(path)
            if len(calls) <= times and at == "open":
                raise OSError(errno.ESTALE, "Stale file handle")
            handle = real(path, *args, **kwargs)
            if len(calls) <= times:
                handle.close()
                broken = mock.MagicMock(spec=io.TextIOWrapper)
                broken.__enter__.return_value = broken
                broken.read.side_effect = OSError(errno.ESTALE, "Stale file handle")
                return broken
            return handle
        patcher = mock.patch.object(module, "open", fake, create=True)
        patcher.start(); self.addCleanup(patcher.stop)
        return calls

    def test_an_alert_read_opens_the_file_again_once_after_estale_on_open(self):
        calls = self.estale(S, 1)
        self.assertEqual(3, A.log(limit=0)["latest"])
        self.assertEqual(2, len(calls))

    def test_an_alert_read_opens_the_file_again_once_after_estale_on_read(self):
        calls = self.estale(S, 1, at="read")
        self.assertEqual(["disk:node-a"], [row["key"] for row in A.active()])
        self.assertEqual(2, len(calls))

    def test_a_second_estale_is_not_retried_again(self):
        calls = self.estale(S, 5)
        self.assertEqual(0, A.log(limit=0)["latest"])     # read leniently, as any unreadable file
        self.assertEqual(2, len(calls))

    def test_a_strict_store_read_opens_the_file_again_once_after_estale(self):
        calls = self.estale(S, 1)
        self.assertEqual(3, S.read_json(os.path.join(self.directory, "alerts.json"), {})["seq"])
        self.assertEqual(2, len(calls))

    def test_other_read_errors_are_not_retried(self):
        calls = []
        def fake(path, *args, **kwargs):
            calls.append(path)
            raise OSError(errno.EIO, "Input/output error")
        with mock.patch.object(S, "open", fake, create=True):
            with self.assertRaises(S.Unreadable):
                S.read_json(os.path.join(self.directory, "alerts.json"), {})
        self.assertEqual(1, len(calls))


@unittest.skipUnless(F.fcntl is not None, "Linux shared locking")
class GetRequestsAndTheMoveBarrier(unittest.TestCase):
    """A plain GET is not data activity; each write it causes is."""

    def setUp(self):
        import server
        import homestead_self_data_server as ROUTES
        self.server = server
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = os.path.realpath(temp.name)
        self.barrier = F.WriteBarrier(self.directory, lambda: None)
        for patcher in (mock.patch.object(server, "_self_data_barrier", self.barrier),
                        mock.patch.object(server, "_self_data_boot_pending", False),
                        mock.patch.object(ROUTES, "DATA_DIR", self.directory),
                        mock.patch.object(S, "WRITE_SCOPE", ROUTES.self_data_file_scope)):
            patcher.start(); self.addCleanup(patcher.stop)

    def request(self, command, path="/api/alerts"):
        return mock.Mock(path=path, command=command, _send=mock.Mock(return_value="sent"))

    def test_a_get_is_not_data_activity_but_a_post_still_is(self):
        steps = []
        @contextmanager
        def activity():
            steps.append("enter")
            try: yield
            finally: steps.append("exit")
        @self.server.self_data_request
        def handler(_): steps.append("handler")
        with mock.patch.object(self.server, "self_data_activity", activity):
            handler(self.request("GET"))
            self.assertEqual(["handler"], steps)
            steps.clear()
            handler(self.request("POST"))
            self.assertEqual(["enter", "handler", "exit"], steps)

    def test_a_get_reads_while_another_replica_freezes(self):
        seed_dir = self.directory
        S.write_json(os.path.join(seed_dir, "page.json"), {"ok": 1})
        @self.server.self_data_request
        def handler(request):
            return S.read_json(os.path.join(seed_dir, "page.json"), {})
        with other_process(self, FREEZE, self.directory, ready="frozen"):
            self.assertEqual({"ok": 1}, handler(self.request("GET")))
            request = self.request("POST")
            handler(request)
            self.assertEqual(503, request._send.call_args.args[0])   # a POST is still held

    def test_a_write_a_get_causes_still_waits_for_the_data_move(self):
        target = os.path.join(self.directory, "written-by-a-get.json")
        @self.server.self_data_request
        def handler(request):
            S.write_json(target, {"written": True})
            return "wrote"
        with other_process(self, FREEZE, self.directory, ready="frozen"):
            request = self.request("GET")
            handler(request)
            status, body = request._send.call_args.args
            self.assertEqual(503, status)
            self.assertTrue(body["data_handoff"])
            self.assertIn("finishing current work", body["error"])
            self.assertFalse(os.path.exists(target))
        self.assertEqual("wrote", handler(self.request("GET")))
        self.assertTrue(os.path.exists(target))

    def test_a_get_whose_write_is_held_answers_503_from_the_handler(self):
        """The handler's own error replies must not turn the hold into a 400."""
        server = self.server
        request = object.__new__(server.H)
        request.path, request.command, request.headers = "/api/alerts", "GET", {}
        request._send = mock.Mock()
        target, ran = os.path.join(self.directory, "written-by-a-get.json"), []
        def route(*_args):
            ran.append(True)          # the GET itself ran: only its write was held
            S.write_json(target, {"written": True})
            return True
        with mock.patch.object(server.H, "_begin", lambda self: None), \
             mock.patch.object(server.H, "_fleet_target", lambda self, p: None), \
             mock.patch.object(server.H, "_guard", lambda self, p: False), \
             mock.patch.object(server.H, "_module_route", route), \
             other_process(self, FREEZE, self.directory, ready="frozen"):
            request.do_GET()
        status, body = request._send.call_args.args[:2]
        self.assertEqual([True], ran)
        self.assertEqual(503, status)
        self.assertTrue(body["data_handoff"])
        self.assertFalse(os.path.exists(target))

    def get(self, path, route=lambda *_args: False):
        """H.do_GET for path, past sign-in, with route as its module routes."""
        server = self.server
        request = object.__new__(server.H)
        request.path, request.command, request.headers = path, "GET", {}
        request._send, request.user = mock.Mock(), "admin"
        with mock.patch.object(server.H, "_begin", lambda self: None), \
             mock.patch.object(server.H, "_fleet_target", lambda self, p: None), \
             mock.patch.object(server.H, "_guard", lambda self, p: False), \
             mock.patch.object(server.H, "_module_route", route):
            request.do_GET()
        return request._send.call_args.args[:2]

    def test_only_a_data_move_hold_answers_503_from_a_get(self):
        def held(error):
            def route(*_args):
                raise error
            return route
        status, body = self.get("/api/alerts", held(Held("This storage change needs a review")))
        self.assertEqual((400, {"error": "This storage change needs a review"}), (status, body))
        status, body = self.get("/api/alerts", held(F.MoveHeld("Homestead is held while its data is being moved")))
        self.assertEqual(503, status)
        self.assertTrue(body["data_handoff"])

    def test_a_console_is_refused_at_the_door_during_a_freeze(self):
        server = self.server
        for path, opens in (("/api/console", (server.CONSOLE_PROXY, "handle")),
                            ("/api/node/shell?node=node-a", (server.NODESHELL, "open_shell")),
                            ("/api/vm/console", (server.VM_CONSOLE, "handle"))):
            with self.subTest(path), mock.patch.object(*opens) as upstream, \
                    other_process(self, FREEZE, self.directory, ready="frozen"):
                status, body = self.get(path)
                self.assertEqual(503, status)
                self.assertTrue(body["data_handoff"])
                upstream.assert_not_called()     # no upstream connection, no 101

    def freeze_from_another_process(self):
        done = subprocess.run([sys.executable, "-u", "-c", FREEZE_TRY, SERVER_DIR, self.directory, "0.3"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout.strip()

    def test_an_open_console_makes_a_freeze_wait(self):
        @self.server.self_data_request
        def console(_request):
            return self.freeze_from_another_process()
        for path in ("/api/console", "/api/node/shell", "/api/vm/console"):
            with self.subTest(path):
                self.assertIn("activity has not finished", console(self.request("GET", path)))

    def test_a_long_ordinary_get_does_not_hold_a_freeze_back(self):
        @self.server.self_data_request
        def slow(_request):
            return self.freeze_from_another_process()
        self.assertEqual("froze", slow(self.request("GET", "/api/overview")))

    def test_a_published_move_still_refuses_a_gets_write_after_the_freeze(self):
        """The long phase of a move: the marker is published and the freeze's
        lock released; the fence's check() refuses the write, the read goes on.
        The control record is the coordinator's own, published and copying,
        and the marker points at it: the refusal is the move's, not a read
        that failed."""
        from test_self_data_coordinator import Cluster
        server, cluster = self.server, Cluster()
        marker = {"protocol": 1, "namespace": "lab", "deployment": "homestead",
                  "operation": cluster.handle["operation"], "anchor_uid": cluster.handle["uid"],
                  "source": "source", "source_uid": "source-uid", "destination": "target", "destination_uid": "target-uid"}
        self.assertIn("pointer_receipt", cluster.anchor.state)
        with open(os.path.join(self.directory, F.MARKER), "w", encoding="utf-8") as handle:
            json.dump(marker, handle)
        fence = F.Fence(cluster.read, "lab", "homestead", "new-0", "homestead", self.directory)
        page, target = os.path.join(self.directory, "page.json"), os.path.join(self.directory, "written-by-a-get.json")
        with open(page, "w", encoding="utf-8") as handle:
            handle.write('{"ok": 1}')
        with mock.patch.object(server, "_self_data_fence", fence), \
             mock.patch.object(server, "_self_data_barrier", F.WriteBarrier(self.directory, server.require_self_data_write)):
            with self.assertRaisesRegex(F.MoveHeld, "being moved. The coordinator runs the move"):
                server.require_self_data_write()
            read = lambda handler, *_a: handler._send(200, S.read_json(page, {})) or True
            self.assertEqual((200, {"ok": 1}), self.get("/api/alerts", read))
            status, body = self.get("/api/alerts", lambda *_a: S.write_json(target, {"written": True}))
        self.assertEqual(503, status)
        self.assertTrue(body["data_handoff"])
        self.assertIn("being moved. The coordinator runs the move", body["error"])
        self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
