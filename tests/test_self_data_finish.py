import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_anchor as A
import homestead_self_data_fence as F
import homestead_self_data_finish as FIN
import homestead_self_data_worker as W
import homestead_shared as SHARED
from homestead_storage_journal import Held
from test_self_data_coordinator import Cluster
import test_self_data_execute as execute_fixture


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = execute_fixture.ExecutionTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.fixture.test_signed_setup_reaches_independent_worker_only_after_drain()
        self.c, self.directory = self.fixture.cluster, self.fixture.temp.name
        self.anchor = self.c.fresh().load(operation=self.fixture.approved["scope"].operation, uid=self.c.objects[self.c.anchor.path]["metadata"]["uid"])
        self.marker = A.pointer("lab", self.anchor.state, self.anchor.handle()["uid"])
        SHARED.write_json(os.path.join(self.directory, F.MARKER), self.marker, durable=True)
        pod = self.c.objects["/api/v1/namespaces/lab/pods/new-0"]
        pod["metadata"]["ownerReferences"][0]["name"] = "hs-rs"
        pod["spec"]["containers"][0]["volumeMounts"] = [{"name": "data", "mountPath": self.directory}]
        self.fence = F.Fence(self.c.read, "lab", "homestead", "new-0", "homestead", self.directory)

    def finish(self, send=None):
        return FIN.finish(self.fence, self.c.read, send or self.c.send)

    def test_cleanup_only_deletes_receipted_helpers_never_data_and_survives_restart(self):
        self.assertTrue(self.finish()["done"])
        self.assertIsNone(F.read_marker(self.directory))
        self.assertNotIn(self.anchor.path, self.c.objects)
        for name in ("source", "target"):
            self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/" + name, self.c.objects)
        for method, path, body in self.c.sent:
            self.assertFalse(method == "DELETE" and "persistentvolume" in path)
        restarted = F.Fence(self.c.read, "lab", "homestead", "new-0", "homestead", self.directory)
        self.assertTrue(restarted.inspect()["writable"])
        before = len(self.c.sent)
        self.assertTrue(FIN.finish(restarted, self.c.read, self.c.send)["done"])
        self.assertEqual(before, len(self.c.sent))

    def test_copied_completion_certificate_never_releases_an_old_source(self):
        self.finish()
        self.c.objects["/api/v1/namespaces/lab/pods/new-0"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "source"
        restarted = F.Fence(self.c.read, "lab", "homestead", "new-0", "homestead", self.directory)
        with self.assertRaisesRegex(Held, "original data volume"): restarted.inspect()

    def test_new_move_marker_revokes_cached_completion(self):
        self.finish(); self.fence.inspect()
        marker = {**self.marker, "operation": "f" * 24}
        SHARED.write_json(os.path.join(self.directory, F.MARKER), marker, durable=True)
        with self.assertRaises(Held): self.fence.require_write()

    def test_lost_delete_reply_observed_without_repeating_the_request(self):
        self.c.lost = lambda method, path, body: method == "DELETE"
        with self.assertRaises(TimeoutError): self.finish()
        first = [p for m, p, _ in self.c.sent if m == "DELETE"][-1]
        self.c.lost = None
        self.assertTrue(self.finish()["done"])
        self.assertEqual(1, sum(m == "DELETE" and p == first for m, p, _ in self.c.sent))

    def test_request_lost_before_delete_is_not_replayed(self):
        self.c.reject = lambda method, path, body: method == "DELETE"
        with self.assertRaises(TimeoutError): self.finish()
        count = len(self.c.sent); self.c.reject = None
        with self.assertRaisesRegex(Held, "Nothing was retried"): self.finish()
        self.assertEqual(count, len(self.c.sent))
        self.assertTrue(self.fence.inspect()["writable"])

    def test_replaced_helper_is_retained(self):
        target = self.anchor.state["setup"]["resources"][7]["target"]["path"]
        self.c.objects[target]["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(Held, "replacement"): self.finish()
        self.assertIn(target, self.c.objects)

    def test_certificate_cannot_claim_deletion_of_different_uid(self):
        self.finish()
        path = Path(self.directory, FIN.FILE)
        value = json.loads(path.read_text())
        next(iter(value["cleanup"].values()))["uid"] = "wrong"
        SHARED.write_json(str(path), value)
        with self.assertRaises(Held): FIN.read(self.directory, "lab", "homestead")


class PreparationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.c = Cluster(published=False)
        self.fence = F.Fence(self.c.read, "lab", "homestead", "old-0", "homestead", self.temp.name)
        pod = self.c.objects["/api/v1/namespaces/lab/pods/old-0"]
        pod["metadata"]["ownerReferences"][0]["name"] = "hs-rs"
        pod["spec"]["containers"] = [{"name": "homestead", "volumeMounts": [{"name": "data", "mountPath": self.temp.name}]}]

    def test_unacknowledged_marker_serves_read_only_then_explicit_abort_restores_source(self):
        pointer = A.pointer("lab", self.c.anchor.state, self.c.handle["uid"])
        SHARED.write_json(os.path.join(self.temp.name, F.MARKER), pointer)
        self.assertEqual("recovery", self.fence.inspect()["mode"])
        with self.assertRaises(Held): self.fence.require_write()
        self.c.fresh().load(**self.c.handle).abort_setup()
        self.assertTrue(self.fence.inspect()["writable"])
        self.assertTrue(FIN.finish(self.fence, self.c.read, self.c.send)["done"])
        restarted = F.Fence(self.c.read, "lab", "homestead", "old-0", "homestead", self.temp.name)
        self.assertTrue(restarted.inspect()["writable"])
        self.assertEqual(2, self.c.objects[self.c.dep_path]["spec"]["replicas"])

    def test_missing_local_marker_cannot_automatically_resume_writes(self):
        self.assertFalse(self.fence.inspect()["writable"])
        with self.assertRaises(Held): self.fence.require_write()
        self.c.fresh().load(**self.c.handle).abort_setup()
        self.assertTrue(FIN.finish(self.fence, self.c.read, self.c.send)["done"])
        self.assertTrue(self.fence.inspect()["writable"])

    def test_abort_permanently_disarms_fresh_and_stale_publishers(self):
        stale = self.c.fresh().load(**self.c.handle)
        self.c.fresh().load(**self.c.handle).abort_setup()
        for publisher in (stale, self.c.fresh().load(**self.c.handle)):
            with self.assertRaises(Exception): publisher.pointer_published(A.pointer_digest("lab", publisher.state, self.c.handle["uid"]))
        self.assertEqual("cancelled", W.progress(self.c.fresh().load(**self.c.handle), 1000)["status"])

    def test_published_operation_cannot_be_abandoned_even_before_first_stop(self):
        self.c.anchor.pointer_published(A.pointer_digest("lab", self.c.anchor.state, self.c.handle["uid"]))
        with self.assertRaises(Held): self.fence.recovery()
        with self.assertRaises(Held): self.c.fresh().load(**self.c.handle).abort_setup()

    def test_replacement_claim_or_foreign_controller_cannot_authorize_recovery(self):
        self.c.objects["/api/v1/namespaces/lab/persistentvolumeclaims/source"]["metadata"]["uid"] = "replacement"
        with self.assertRaises(Held): self.fence.inspect()


if __name__ == "__main__": unittest.main()
