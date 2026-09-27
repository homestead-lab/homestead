"""Activity finishes before a freeze; a published fence cannot be raced."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_fence as F
import homestead_shared as S
from homestead_storage_journal import Held


@unittest.skipUnless(F.fcntl is not None, "Linux shared locking")
class BarrierTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.closed = False
        self.barrier = F.WriteBarrier(self.directory, self.check)

    def check(self):
        if self.closed: raise Held("published")

    def test_activity_stays_concurrent_but_freeze_waits_for_it(self):
        other = F.WriteBarrier(self.directory, self.check)
        with self.barrier.activity():
            with other.activity(): pass
            with self.assertRaisesRegex(Held, "not finished"):
                with other.freeze(timeout=.05): self.fail("drained a live writer")
        with other.freeze():
            with self.assertRaisesRegex(Held, "finishing current work"):
                with self.barrier.activity(): self.fail("new activity entered a freeze")
        with self.barrier.activity(): pass  # Refused attempts did not leak locks.

    def test_freeze_cannot_upgrade_its_own_active_request(self):
        with self.barrier.activity():
            with self.assertRaisesRegex(Held, "outside an active"):
                with self.barrier.freeze(): self.fail("unsafe lock upgrade")

    def test_publication_during_open_is_rechecked_after_lock_acquisition(self):
        original = self.barrier._open
        def opened():
            fd = original(); self.closed = True; return fd
        with mock.patch.object(self.barrier, "_open", side_effect=opened):
            with self.assertRaisesRegex(Held, "published"):
                with self.barrier.activity(): self.fail("passed a stale allow check")
        self.closed = False
        with self.barrier.freeze(timeout=.05): pass

    def test_publication_remains_held_after_exclusive_lock_is_released(self):
        with self.barrier.freeze():
            with self.barrier.activity(): pass  # Final pre-publication journal writes.
            self.closed = True
        with self.assertRaisesRegex(Held, "published"):
            with F.WriteBarrier(self.directory, self.check).activity(): self.fail("source resumed")

    def test_shared_lock_holds_activity_until_its_outermost_exit(self):
        other = F.WriteBarrier(self.directory, self.check)
        lock = S.SharedLock("fixture", strict=True, directory=lambda: self.directory)
        with mock.patch.object(S, "WRITE_SCOPE", lambda _: self.barrier.activity()):
            with lock:
                with lock:
                    with self.assertRaises(Held):
                        with other.freeze(timeout=.05): self.fail("nested writer not drained")
                with self.assertRaises(Held):
                    with other.freeze(timeout=.05): self.fail("outer writer not drained")
            with other.freeze(timeout=.05): pass

    def test_exception_releases_activity_and_strict_lock(self):
        lock = S.SharedLock("fixture", strict=True, directory=lambda: self.directory)
        with mock.patch.object(S, "WRITE_SCOPE", lambda _: self.barrier.activity()):
            with mock.patch.object(lock, "_strict_acquire", side_effect=OSError("no lock")):
                with self.assertRaises(OSError):
                    with lock: self.fail("acquired broken lock")
            with self.barrier.freeze(timeout=.05): pass
            with lock: pass

    def test_lock_path_must_not_be_a_symlink_or_hardlink(self):
        target = Path(self.directory, "target"); target.touch()
        path = Path(self.directory, ".self-data-access-v1.lock")
        path.symlink_to(target)
        with self.assertRaises((Held, OSError)):
            with self.barrier.activity(): self.fail("followed symlink")
        path.unlink(); os.link(target, path)
        with self.assertRaises(Held):
            with self.barrier.activity(): self.fail("accepted ambiguous hardlink")

    def test_other_process_is_really_drained_not_just_this_threads_lock(self):
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from homestead_self_data_fence import WriteBarrier
with WriteBarrier(sys.argv[2], lambda: None).activity():
    print('active', flush=True)
    sys.stdin.readline()
"""
        proc = subprocess.Popen([sys.executable, "-u", "-c", script, str(Path(F.__file__).parent), self.directory],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual("active", proc.stdout.readline().strip())
            with self.assertRaisesRegex(Held, "not finished"):
                with self.barrier.freeze(timeout=.05): self.fail("missed another process")
        finally:
            try:
                _, error = proc.communicate("finish\n", timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.communicate(); self.fail("fixture process did not finish")
        self.assertEqual(0, proc.returncode, error)
        with self.barrier.freeze(timeout=.05): pass

    def test_publication_checks_jobs_after_drain_and_keeps_the_source_closed(self):
        import homestead_self_data_setup as setup
        from test_self_data_coordinator import Cluster
        cluster = Cluster(published=False)
        barrier = F.WriteBarrier(self.directory, lambda: (_ for _ in ()).throw(Held("published"))
            if F.read_marker(self.directory) else None)
        other = F.WriteBarrier(self.directory, lambda: None)
        checked = mock.Mock(return_value=True)
        with other.activity():
            with self.assertRaises(Held):
                setup.publish_after_drain(self.directory, cluster.anchor, barrier, checked, timeout=.05)
        checked.assert_not_called()
        self.assertNotIn("pointer_receipt", cluster.anchor.state)
        with self.assertRaises(Held):
            setup.publish_after_drain(self.directory, cluster.anchor, barrier, lambda: False)
        self.assertIsNone(F.read_marker(self.directory))
        setup.publish_after_drain(self.directory, cluster.anchor, barrier, checked)
        checked.assert_called_once()
        self.assertIn("pointer_receipt", cluster.anchor.state)
        with self.assertRaisesRegex(Held, "published"):
            with barrier.activity(): self.fail("source started another action")
        self.assertEqual("quiesce", cluster.step()["phase"])


class WriterHooksTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def test_direct_writers_refuse_before_creating_files(self):
        import homestead_alerts as alerts
        import homestead_push as push
        import homestead_icons as icons
        import homestead_move_engine as moves
        import homestead_console as console
        import homestead_updates as updates
        def refused(_): raise Held("frozen")
        with mock.patch.object(S, "WRITE_SCOPE", side_effect=refused), \
             mock.patch.object(alerts, "DATA_DIR", self.directory), mock.patch.object(push, "DATA_DIR", self.directory), \
             mock.patch.object(moves, "DATA_DIR", self.directory), mock.patch.object(updates, "DATA_DIR", self.directory):
            for action in (lambda: alerts._save({}), lambda: push._write([]), lambda: moves._write([]),
                           lambda: icons.store(b"icon", self.directory, "image/png"),
                           lambda: console.audit(self.directory, {}), lambda: updates._history({})):
                with self.subTest(action=action), self.assertRaises(Held): action()
        self.assertEqual([], list(Path(self.directory).iterdir()))

    def test_json_secret_permissions_and_durable_write_are_preserved(self):
        path = Path(self.directory, "private.json")
        S.write_json(str(path), {"fixture": True}, mode=0o600, durable=True)
        self.assertIn("true", path.read_text())
        if os.name == "posix": self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_scope_refusal_does_not_poison_shared_lock(self):
        lock = S.SharedLock("fixture", strict=True, directory=lambda: self.directory)
        with mock.patch.object(S, "WRITE_SCOPE", side_effect=Held("frozen")):
            with self.assertRaises(Held):
                with lock: self.fail("lock file written while frozen")
        self.assertFalse(Path(self.directory, ".locks").exists())
        with lock: pass

    def test_http_scope_wraps_entire_handler_and_destination_boot_bypasses_it(self):
        import server
        from contextlib import contextmanager
        steps = []
        @contextmanager
        def activity():
            steps.append("enter")
            try: yield
            finally: steps.append("exit")
        @server.self_data_request
        def handler(_): steps.append("handler")
        request = mock.Mock(path="/api/settings")
        with mock.patch.object(server, "self_data_activity", activity), mock.patch.object(server, "_self_data_boot_pending", False):
            handler(request)
        self.assertEqual(["enter", "handler", "exit"], steps)
        steps.clear()
        with mock.patch.object(server, "self_data_activity", side_effect=AssertionError("read-only boot wrote lock")), \
             mock.patch.object(server, "_self_data_boot_pending", True): handler(request)
        self.assertEqual(["handler"], steps)

    def test_saved_handoff_status_remains_read_only_and_authenticated_during_source_freeze(self):
        import server
        request = object.__new__(server.H)
        request.path = "/api/self/data/handoff/" + "a" * 24
        request.command, request.headers = "GET", {}
        request._who = mock.Mock(return_value=None); request._send = mock.Mock()
        with mock.patch.object(server, "self_data_activity", side_effect=AssertionError("status tried to write a lock")), \
             mock.patch.object(server, "_self_data_boot_pending", False), \
             mock.patch.object(server.CFACCESS, "enabled", return_value=False):
            request.do_GET()
        self.assertEqual(401, request._send.call_args.args[0])
        request._who.assert_called_once()


if __name__ == "__main__": unittest.main()
