import copy
import os
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


if __name__ == "__main__":
    unittest.main()
