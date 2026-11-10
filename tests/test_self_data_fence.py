import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_fence as F
import homestead_shared as SHARED
from homestead_storage_journal import Held
from test_self_data_coordinator import Cluster


class FenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.c = Cluster()
        self.marker = {"protocol": 1, "namespace": "lab", "deployment": "homestead",
            "operation": self.c.handle["operation"], "anchor_uid": self.c.handle["uid"],
            "source": "source", "source_uid": "source-uid", "destination": "target", "destination_uid": "target-uid"}
        self.fence = F.Fence(self.c.read, "lab", "homestead", "new-0", "homestead", self.directory)

    def save_marker(self, value=None):
        SHARED.write_json(os.path.join(self.directory, F.MARKER), self.marker if value is None else value, durable=True)

    def starting(self):
        self.c.switching(); self.c.step(); self.c.settle_stop(); self.c.step(); self.c.settle_start()
        self.save_marker()
        pod = self.c.objects["/api/v1/namespaces/lab/pods/new-0"]
        pod["metadata"]["ownerReferences"][0]["name"] = "hs-rs"
        pod["spec"]["containers"][0]["volumeMounts"] = [{"name": "data", "mountPath": self.directory}]

    def test_normal_boot_without_marker_or_anchor_remains_writable(self):
        del self.c.objects[self.c.anchor.path]
        self.assertEqual({"mode": "normal", "writable": True}, self.fence.inspect())
        self.fence.read = lambda _: self.fail("normal mutations should not poll Kubernetes")
        self.fence.require_write()

    def test_marker_disappearing_during_open_is_not_treated_as_no_marker(self):
        self.save_marker()
        with mock.patch.object(F.os, "open", side_effect=FileNotFoundError()):
            with self.assertRaises(Held): F.read_marker(self.directory)

    def test_marker_replaced_during_read_is_not_accepted(self):
        self.save_marker()
        lstat = F.os.lstat
        count = 0
        def replaced(path):
            nonlocal count
            count += 1
            result = lstat(path)
            if count == 2:
                return type("Replaced", (), {"st_dev": result.st_dev, "st_ino": result.st_ino + 1,
                    "st_size": result.st_size, "st_mtime_ns": result.st_mtime_ns})()
            return result
        with mock.patch.object(F.os, "lstat", side_effect=replaced):
            with self.assertRaisesRegex(Held, "changed while reading"): F.read_marker(self.directory)

    def test_published_but_unconfirmed_pointer_does_not_authorize_startup(self):
        self.c = Cluster(published=False)
        self.fence.read = self.c.read
        self.save_marker()
        with self.assertRaises(Held): self.fence.inspect()

    def test_unknown_same_name_anchor_never_authorizes_missing_pointer(self):
        with self.assertRaisesRegex(Held, "handed over"):
            self.fence.inspect()
        self.assertFalse(self.fence.checked)

    def test_starting_destination_can_serve_read_only_but_cannot_resume_jobs(self):
        self.starting()
        self.assertEqual("start", self.fence.inspect()["mode"])
        with self.assertRaisesRegex(Held, "background jobs are held"):
            self.fence.require_write()
        self.assertEqual("done", self.c.step()["phase"])
        self.fence.require_write()
        self.assertTrue(self.fence.inspect()["writable"])

    def test_worker_hold_blocks_destination_even_after_phase_completion(self):
        self.starting(); self.c.step()
        control = self.c.fresh().load(**self.c.handle)
        control.report("coordinator-uid", 1000, "held", "Restart verification needs review")
        with self.assertRaisesRegex(Held, "recovery review"):
            self.fence.require_write()

    def test_original_volume_never_resumes_after_cutover_or_completion(self):
        self.starting()
        for completed in (False, True):
            if completed:
                # Completion is authoritative but does not authorize an old pod.
                self.c.objects["/api/v1/namespaces/lab/pods/new-0"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "target"
                self.c.step()
            self.c.objects["/api/v1/namespaces/lab/pods/new-0"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "source"
            with self.assertRaisesRegex(Held, "original data volume"):
                self.fence.inspect()

    def test_marker_appearing_after_normal_boot_blocks_mutations(self):
        anchor = self.c.objects.pop(self.c.anchor.path)
        self.fence.inspect()
        self.c.objects[self.c.anchor.path] = anchor
        self.save_marker()
        with self.assertRaisesRegex(Held, "held while"):
            self.fence.require_write()

    def test_once_observed_missing_marker_cannot_look_like_fresh_install(self):
        self.starting(); self.fence.inspect()
        os.unlink(os.path.join(self.directory, F.MARKER))
        del self.c.objects[self.c.anchor.path]
        with self.assertRaisesRegex(Held, "disappeared"):
            self.fence.require_write()

    def test_missing_or_replaced_control_record_blocks(self):
        self.starting()
        original = self.c.objects[self.c.anchor.path]
        del self.c.objects[self.c.anchor.path]
        with self.assertRaises(Held): self.fence.inspect()
        self.c.objects[self.c.anchor.path] = copy.deepcopy(original)
        self.c.objects[self.c.anchor.path]["metadata"]["uid"] = "replacement"
        with self.assertRaises(Held): self.fence.inspect()

    def test_normal_boot_api_failure_is_unknown_not_absence(self):
        self.fence.read = lambda p: (_ for _ in ()).throw(urllib.error.HTTPError(p, 503, "private error", {}, None))
        with self.assertRaises(Held) as caught: self.fence.inspect()
        self.assertNotIn("private", str(caught.exception))

    def test_malformed_and_foreign_markers_do_not_authorize_start(self):
        for update in ({"protocol": 2}, {"namespace": "elsewhere"}, {"password": "private"},
                       {"source_uid": "target-uid"}):
            self.save_marker({**self.marker, **update})
            with self.assertRaises(Held): self.fence.inspect()

    def test_replaced_destination_or_rebound_volume_blocks(self):
        self.starting()
        original = copy.deepcopy(self.c.objects)
        for path, mutate in (
                ("/api/v1/namespaces/lab/persistentvolumeclaims/target", lambda o: o["metadata"].update(uid="replacement")),
                ("/api/v1/persistentvolumes/new-pv", lambda o: o["spec"]["claimRef"].update(uid="other"))):
            self.c.objects = copy.deepcopy(original); mutate(self.c.objects[path])
            with self.assertRaises(Held): self.fence.inspect()

    def test_pod_owner_mount_or_subpath_changes_block(self):
        self.starting()
        original = copy.deepcopy(self.c.objects)
        for change in (lambda p: p["metadata"]["ownerReferences"][0].update(uid="foreign"),
                       lambda p: p["spec"]["containers"][0]["volumeMounts"][0].update(subPath="old"),
                       lambda p: p["spec"]["containers"][0]["volumeMounts"][0].update(readOnly=True)):
            self.c.objects = copy.deepcopy(original)
            change(self.c.objects["/api/v1/namespaces/lab/pods/new-0"])
            with self.assertRaises(Held): self.fence.inspect()

    def test_later_image_update_is_allowed_only_after_completion(self):
        self.starting()
        dep = self.c.objects[self.c.dep_path]
        original = copy.deepcopy(dep)
        dep["spec"]["template"]["spec"]["containers"][0]["image"] = "later-release"
        with self.assertRaises(Held): self.fence.inspect()
        self.c.objects[self.c.dep_path] = original
        self.c.step()
        self.c.objects[self.c.dep_path]["spec"]["template"]["spec"]["containers"][0]["image"] = "later-release"
        self.fence.require_write()


# Another replica, as a separate process with its own locks: it enters an
# activity (described when kind is given) and waits for a line on stdin.
# "crash" ends it inside the activity with no clean-up; "norecord" makes
# its record write fail.
HOLDER = """
import os, sys
sys.path.insert(0, sys.argv[1])
import homestead_self_data_fence as F
from contextlib import nullcontext
directory, kind, user, host, how = sys.argv[2:7]
if how == "norecord":
    os.rename = lambda *_: (_ for _ in ()).throw(OSError("read-only"))
describe = getattr(F, "described", None)  # absent before #1305: the plain activity
with describe(kind, user=user or None, host=host or None) if kind and describe else nullcontext():
    with F.WriteBarrier(directory, lambda: None).activity():
        print("active", flush=True)
        if how == "crash":
            os._exit(1)
        sys.stdin.readline()
"""


@unittest.skipUnless(F.fcntl is not None, "Linux shared locking")
class HolderRecordTests(unittest.TestCase):
    """#1305: a data move held back by open activity names what holds it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.barrier = F.WriteBarrier(self.directory, lambda: None)

    def holder(self, kind="", user="", host="", how=""):
        proc = subprocess.Popen([sys.executable, "-u", "-c", HOLDER, str(Path(F.__file__).parent), self.directory,
                                 kind, user, host, how],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def finish():
            try:
                proc.communicate("done\n", timeout=10)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                proc.kill()
                proc.communicate()
        self.addCleanup(finish)
        self.assertEqual("active", proc.stdout.readline().strip())
        if how == "crash":
            proc.wait(timeout=10)
        return proc

    def refusal(self):
        with self.assertRaises(Held) as caught:
            with self.barrier.freeze(timeout=.05): self.fail("froze past an open activity")
        return str(caught.exception)

    def records(self):
        return sorted(name for name in os.listdir(self.directory) if name.startswith(".self-data-holder-v1-"))

    def test_a_freeze_names_the_terminal_that_holds_it(self):
        self.holder("terminal", "alice", "node-a")
        message = self.refusal()
        self.assertTrue(message.startswith("Current Homestead activity has not finished. "), message)
        self.assertRegex(message, r"A terminal on host node-a, opened by alice at \d{4}-\d\d-\d\d \d\d:\d\d UTC, is still open\.")
        self.assertIn("Close the open sessions, then start the move again.", message)
        self.assertTrue(message.endswith("No copy or shutdown was authorized"), message)

    def test_other_activity_is_counted_not_named(self):
        self.holder()
        self.holder("vm-console", "bob")
        message = self.refusal()
        self.assertRegex(message, r"A VM console, opened by bob at [^,]+ UTC, is still open\.")
        self.assertIn("1 other Homestead task is still running.", message)

    def test_a_crashed_holders_record_is_not_named_and_is_removed(self):
        self.holder("terminal", "alice", "node-a", how="crash")
        self.assertEqual(1, len(self.records()), "a crash leaves its record behind")
        self.holder()
        message = self.refusal()
        self.assertNotIn("terminal", message.lower())
        self.assertIn("1 other Homestead task is still running.", message)
        self.assertEqual(1, len(self.records()), "only the running holder's record is left")

    def test_a_record_alone_never_refuses(self):
        self.holder("terminal", "alice", "node-a", how="crash")
        with self.barrier.freeze(timeout=.05):
            pass

    def test_with_no_record_to_read_the_refusal_is_the_plain_sentence(self):
        self.holder("terminal", "alice", "node-a", how="norecord")
        self.assertEqual([], self.records())
        self.assertEqual("Current Homestead activity has not finished. No copy or shutdown was authorized",
                         self.refusal())

    def test_a_record_holds_only_kind_user_host_and_since_and_goes_at_the_end(self):
        with F.described("terminal", user="alice", host="node-a"):
            with self.barrier.activity():
                [name] = self.records()
                self.assertTrue(name.endswith(".json"), "written to a temporary name, then renamed")
                record = json.loads(Path(self.directory, name).read_text())
                self.assertEqual({"kind", "user", "host", "since"}, set(record))
                self.assertEqual(("terminal", "alice", "node-a"), (record["kind"], record["user"], record["host"]))
                self.assertEqual([record], self.barrier.holders(), "its own process's record is named too")
        self.assertEqual([], self.records())

    def test_a_record_goes_when_the_work_inside_fails(self):
        with self.assertRaises(RuntimeError):
            with self.barrier.activity():
                raise RuntimeError("the work failed")
        self.assertEqual([], self.records())
        with self.barrier.freeze(timeout=.05):
            pass

    def test_a_record_that_cannot_be_read_names_nothing(self):
        bad = Path(self.directory, ".self-data-holder-v1-" + "x" * 24 + ".json")
        for text in ("not json", json.dumps({"kind": "terminal", "user": "alice", "host": "node-a", "since": 1, "extra": 1}),
                     json.dumps({"kind": "rm -rf", "user": None, "host": None, "since": 1})):
            with self.subTest(text):
                bad.write_text(text)
                with open(bad) as held:
                    F.fcntl.flock(held, F.fcntl.LOCK_EX)   # its holder still runs
                    self.assertEqual([], self.barrier.holders())

    def test_a_bad_host_or_user_is_left_out_not_the_record(self):
        with F.described("terminal", user="alice\nbob", host="node a; rm"):
            with self.barrier.activity():
                [record] = self.barrier.holders()
        self.assertEqual(("terminal", None, None), (record["kind"], record["user"], record["host"]))

    def test_a_terminal_request_is_described_by_its_host_and_its_signed_in_person(self):
        import homestead_self_data_server as SD
        seen = {}

        def method(_handler):
            seen["records"] = self.barrier.holders()

        handler = mock.Mock(path="/api/node/shell?node=node-a", command="GET")
        handler._cookies.return_value = {SD.AUTH.COOKIE: "a-token"}
        with mock.patch.object(SD, "self_data_activity", self.barrier.activity, create=True), \
                mock.patch.object(SD.AUTH, "verify_token", lambda token: {"user": "alice"} if token == "a-token" else None):
            SD.request_scope(method, lambda: False)(handler)
            self.assertEqual([("terminal", "alice", "node-a")], [(r["kind"], r["user"], r["host"]) for r in seen["records"]])
            handler.path, handler.command = "/api/apps/restart", "POST"
            SD.request_scope(method, lambda: False)(handler)
            self.assertEqual([("work", None, None)], [(r["kind"], r["user"], r["host"]) for r in seen["records"]])
        self.assertEqual([], self.records())


if __name__ == "__main__":
    unittest.main()
