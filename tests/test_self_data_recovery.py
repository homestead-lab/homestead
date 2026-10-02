import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_anchor as A
import homestead_self_data_recovery as R
import homestead_self_data_worker as W
import homestead_self_data_fence as F
import homestead_self_data_finish as FIN
import homestead_shared as SHARED
from homestead_storage_journal import Held, shape
from test_self_data_coordinator import Cluster, obj


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.c = Cluster()
        self.c.copying(); self.c.finish_copy()
        self.anchor().report("coordinator-uid", 1000, "held", "Transient observation needs review")

    def anchor(self): return self.c.fresh().load(**self.c.handle)

    def review(self, action="return-original"):
        return R.review(self.anchor(), self.c.read, lambda _: self.c.logs_text, action, clock=lambda: 1000)

    def confirm(self, action="return-original", fingerprint=None):
        return R.confirm(self.anchor(), self.c.read, lambda _: self.c.logs_text, action,
                         fingerprint or self.review(action)["fingerprint"], clock=lambda: 1000)

    def runner(self):
        return W.Runner(self.c.read, self.c.send, lambda _: self.c.logs_text, self.c.admit,
                        namespace="lab", deployment="homestead", operation=self.c.handle["operation"],
                        anchor_uid=self.c.handle["uid"], worker_uid="coordinator-uid", clock=lambda: 1000)

    def restored(self):
        self.confirm(); runner = self.runner()
        runner.tick()  # Normal Job deletion; completed Pod still retains mounts.
        before = len(self.c.sent); runner.tick()
        self.assertEqual(0, self.c.objects[self.c.dep_path]["spec"]["replicas"])
        self.assertNotIn("recover-start", [e["step"] for e in self.anchor().item()["ref"]["storage_writes"]])
        self.c.objects.pop("/api/v1/namespaces/lab/pods/copy-pod")
        runner.tick(); self.c.settle_start()
        for i in range(2):
            self.c.objects[f"/api/v1/namespaces/lab/pods/new-{i}"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "source"
        return runner

    def test_preview_writes_nothing_and_confirmation_rejects_changed_facts(self):
        before = copy.deepcopy(self.c.objects), len(self.c.sent)
        proposed = self.review()
        self.assertEqual(before, (self.c.objects, len(self.c.sent)))
        stranger = obj("Pod", "consumer", {"volumes": [{"persistentVolumeClaim": {"claimName": "source"}}]})
        self.c.put("/api/v1/namespaces/lab/pods/consumer", stranger)
        with self.assertRaises(Held): self.confirm(fingerprint=proposed["fingerprint"])
        self.assertEqual("held", self.anchor().state["runtime"]["state"])

    def test_confirmation_binds_anchor_version_even_if_resource_shapes_are_equal(self):
        proposed = self.review()
        anchor = self.anchor(); anchor.report("coordinator-uid", 1001, "held", "Different review")
        with self.assertRaisesRegex(Held, "facts changed"):
            self.confirm(fingerprint=proposed["fingerprint"])

    def test_fresh_lease_renewal_does_not_prevent_confirmation(self):
        proposal = self.review()
        lease = self.c.objects["/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/node1"]
        lease["spec"]["renewTime"] = "1970-01-01T00:16:39Z"
        self.confirm(fingerprint=proposal["fingerprint"])

    def test_existing_recovery_receipt_cannot_clear_a_subsequent_hold(self):
        self.confirm("resume"); runner = self.runner(); runner.tick()
        pv = self.c.objects["/api/v1/persistentvolumes/new-pv"]
        pv["metadata"]["annotations"] = {"operator/hold": "true"}
        self.assertEqual("held", runner.tick()["status"])
        # Even an external runtime-only edit cannot recycle the old approval.
        anchor = self.anchor(); anchor.state["runtime"]["state"] = "running"
        self.c.objects[anchor.path]["data"]["state.json"] = A._validate(anchor.state, "lab")
        self.assertEqual("held", runner.tick()["status"])
        self.assertEqual("held", self.anchor().state["runtime"]["state"])

    def test_resume_consumes_copy_evidence_once_and_clears_live_worker_latch(self):
        runner = self.runner(); self.assertEqual("held", runner.tick()["status"])
        self.confirm("resume")
        self.assertEqual("verify", runner.tick()["phase"])
        self.assertIsNone(runner.hold)
        self.assertEqual("verified", self.anchor().state["copy_receipt"]["state"])
        self.assertEqual(1, sum(method == "POST" and path.endswith("/jobs") for method, path, _ in self.c.sent))

    def test_original_recovery_waits_for_mount_release_and_all_replica_readiness(self):
        runner = self.restored()
        p = self.c.objects["/api/v1/namespaces/lab/pods/new-1"]
        p["status"]["conditions"] = []
        self.assertNotEqual("cancelled", runner.tick()["status"])
        p["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
        self.assertEqual("cancelled", runner.tick()["status"])
        self.assertEqual("done", self.anchor().state["phase"])
        self.assertTrue(self.anchor().item()["ref"]["retain_resources"])
        self.assertFalse(any(e["step"] in ("switch", "start") for e in self.anchor().item()["ref"]["storage_writes"]))
        self.assertTrue(all("persistentvolume" not in path for method, path, _ in self.c.sent if method == "DELETE"))

    def test_restored_source_stays_read_only_until_verified_then_cleanup_survives_restart(self):
        runner = self.restored()
        with tempfile.TemporaryDirectory() as directory:
            pointer = A.pointer("lab", self.anchor().state, self.c.handle["uid"])
            SHARED.write_json(os.path.join(directory, F.MARKER), pointer, durable=True)
            for i in range(2):
                p = self.c.objects[f"/api/v1/namespaces/lab/pods/new-{i}"]
                p["metadata"]["ownerReferences"][0]["name"] = "hs-rs"
                p["spec"]["containers"][0]["volumeMounts"] = [{"name": "data", "mountPath": directory}]
            fence = F.Fence(self.c.read, "lab", "homestead", "new-0", "homestead", directory)
            self.assertFalse(fence.inspect()["writable"])
            with self.assertRaises(Held): fence.require_write()
            runner.tick(); self.assertTrue(fence.inspect()["writable"])
            self.assertTrue(FIN.finish(fence, self.c.read, self.c.send)["done"])
            restarted = F.Fence(self.c.read, "lab", "homestead", "new-0", "homestead", directory)
            self.assertTrue(restarted.inspect()["writable"])
            for name in ("source", "target"):
                self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/" + name, self.c.objects)
            self.c.objects["/api/v1/namespaces/lab/pods/new-0"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "target"
            with self.assertRaises(Held): restarted.inspect()

    def test_unknown_write_or_replaced_pv_or_host_blocks_both_actions(self):
        original = copy.deepcopy(self.c.objects)
        for change in (lambda: self.c.objects["/api/v1/persistentvolumes/old-pv"]["metadata"].update(uid="replacement"),
                       lambda: self.c.objects["/api/v1/nodes/node1"]["status"]["nodeInfo"].update(bootID="new-boot"),
                       lambda: self.c.objects["/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/node1"]["spec"].update(renewTime="1970-01-01T00:00:00Z")):
            self.c.objects = copy.deepcopy(original); change()
            for action in ("resume", "return-original"):
                with self.assertRaises(Held): self.review(action)
        self.c.objects = original
        state = self.anchor().state; state["journal"]["ref"]["storage_writes"][-1]["state"] = "uncertain"
        self.c.objects[self.c.anchor.path]["data"]["state.json"] = A._validate(state, "lab")
        for action in ("resume", "return-original"):
            with self.assertRaises(Held): self.review(action)

    def test_return_original_is_refused_once_cutover_was_requested(self):
        self.confirm("resume"); self.c.step(); self.c.step()
        self.c.objects.pop("/api/v1/namespaces/lab/pods/copy-pod"); self.c.step(); self.c.step()
        self.anchor().report("coordinator-uid", 1000, "held", "Startup needs review")
        with self.assertRaises(Held): self.review()

    def test_lost_recovery_restart_reply_holds_without_replaying(self):
        self.confirm(); runner = self.runner(); runner.tick()
        self.c.objects.pop("/api/v1/namespaces/lab/pods/copy-pod")
        self.c.lost = lambda method, path, body: path == self.c.dep_path
        self.assertEqual("held", runner.tick()["status"])
        self.c.lost = None
        before = len(self.c.sent); self.runner().tick()
        with self.assertRaises(Held): self.review()
        self.assertEqual(before, len(self.c.sent))

    def test_longhorn_diagnostic_is_observational_but_storage_and_other_annotations_are_pinned(self):
        pv = self.c.objects["/api/v1/persistentvolumes/new-pv"]
        pinned = shape(pv)
        pv["metadata"]["annotations"] = {"longhorn.io/volume-scheduling-error": "replica scheduling failed"}
        self.assertEqual(pinned, shape(pv)); self.review()
        for change in (lambda p: p["spec"]["claimRef"].update(uid="replacement"),
                       lambda p: p["metadata"]["annotations"].update({"operator/hold": "true"})):
            changed = copy.deepcopy(pv); change(changed)
            self.assertNotEqual(pinned, shape(changed))
        pv["metadata"]["annotations"]["operator/hold"] = "true"
        with self.assertRaisesRegex(Held, "PersistentVolume new-pv: specification or metadata changed"):
            self.review()

    def test_scoped_production_setup_capacity_cleanup_and_job_receipt(self):
        import test_self_data_execute as fixture
        import homestead_self_data_execute as E
        import homestead_operations as OPS
        f = fixture.ExecutionTests(); f.setUp(); self.addCleanup(f.doCleanups)
        with mock.patch.object(E.P, "publish_pointer", side_effect=f.publish): handle = f.task().run()
        c = f.cluster
        anchor = lambda: A.Anchor(f.read, f.send, "lab", "homestead").load(**handle)
        uid = anchor().state["plan"]["worker"]["uid"]
        runner = W.Runner(f.read, f.send, lambda _: c.logs_text, namespace="lab", deployment="homestead",
                          operation=handle["operation"], anchor_uid=handle["uid"], worker_uid=uid,
                          require_setup_receipts=True, clock=lambda: 1000)
        runner.tick(); c.settle_stop(); f.f.f.pods = []
        runner.tick(); runner.tick(); c.finish_copy()
        anchor().report(uid, 1000, "held", "Review original-volume recovery")
        runner.tick()
        proposal = R.review(anchor(), f.read, lambda _: c.logs_text, "return-original", clock=lambda: 1000)
        self.assertIn("restart", proposal)
        R.confirm(anchor(), f.read, lambda _: c.logs_text, "return-original", proposal["fingerprint"], clock=lambda: 1000)
        self.assertNotEqual("held", runner.tick()["status"])
        c.objects.pop("/api/v1/namespaces/lab/pods/copy-pod")
        result = runner.tick(); self.assertNotEqual("held", result["status"], result)
        c.settle_start()
        for i in range(2):
            p = c.objects[f"/api/v1/namespaces/lab/pods/new-{i}"]
            p["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "source"
            p["metadata"]["ownerReferences"][0]["name"] = "hs-rs"
            p["spec"]["containers"][0]["volumeMounts"] = [{"name": "data", "mountPath": f.temp.name}]
        self.assertEqual("cancelled", runner.tick()["status"])
        SHARED.write_json(os.path.join(f.temp.name, F.MARKER), A.pointer("lab", anchor().state, handle["uid"]), durable=True)
        fence = F.Fence(f.read, "lab", "homestead", "new-0", "homestead", f.temp.name)
        self.assertTrue(FIN.finish(fence, f.read, f.send)["done"])
        self.assertTrue(E.reconcile_completed(OPS, f.temp.name, "lab", "homestead"))
        job = next(i for i in OPS._read() if i["id"] == f.job["id"])
        self.assertEqual("cancelled", job["status"])
        for method, path, _ in c.sent:
            if method == "DELETE": self.assertNotIn("persistentvolume", path)


if __name__ == "__main__": unittest.main()
