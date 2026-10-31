"""A capacity review is not voided by a controller's status write (#826's twin).

Review tokens are signed over what the review read. They used to pin each
object's resourceVersion, which every status write moves: a Deployment mid
rollout, a VM or its instance as KubeVirt reports on it, a launcher pod.
Confirming then said "review again" although nothing reviewed had changed.
They now pin the uid and everything but status and the server's bookkeeping,
so a change to the spec, labels or annotations still needs a fresh review."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_capacity_review as review
import homestead_deploy_server as DEPLOY_SERVER
from fakes.kube import Kube
import test_vm_power_admission as power

DEPLOYMENT = "/apis/apps/v1/namespaces/lab/deployments/app"


def deployment():
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "app", "namespace": "lab"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "app", "image": "example.com/app:1"}]}}}}


class RolloutReviewTests(unittest.TestCase):
    def setUp(self):
        self.kube = Kube(deployment())
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        patch = mock.patch.object(review, "_key", return_value=b"review-tests")
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = {"target_mode": "existing", "target_workload": "app", "namespace": "lab"}
        self.token = review.issue(self.cfg, DEPLOY_SERVER.rollout_review_context(K.kget(DEPLOYMENT)))

    def still_valid(self):
        return review.valid({**self.cfg, "capacity_token": self.token},
                            DEPLOY_SERVER.rollout_review_context(K.kget(DEPLOYMENT)))

    def test_a_status_write_between_preview_and_confirm_keeps_the_review(self):
        K.ksend("PUT", DEPLOYMENT + "/status", {"status": {"observedGeneration": 1, "readyReplicas": 0}})
        self.assertTrue(self.still_valid())

    def test_a_spec_change_between_preview_and_confirm_needs_a_fresh_review(self):
        changed = K.kget(DEPLOYMENT)
        changed["spec"]["replicas"] = 2
        K.ksend("PUT", DEPLOYMENT, changed)
        self.assertFalse(self.still_valid())

    def test_an_annotation_change_still_needs_a_fresh_review(self):
        changed = K.kget(DEPLOYMENT)
        changed["metadata"]["annotations"] = {"example.com/note": "changed"}
        K.ksend("PUT", DEPLOYMENT, changed)
        self.assertFalse(self.still_valid())

    def test_a_replacement_with_the_same_spec_needs_a_fresh_review(self):
        K.ksend("DELETE", DEPLOYMENT)
        K.ksend("POST", "/apis/apps/v1/namespaces/lab/deployments", deployment())
        self.assertFalse(self.still_valid())


class VMPowerStatusWriteTests(unittest.TestCase):
    read = power.VMPowerAdmissionTests.read
    running = power.VMPowerAdmissionTests.running
    setUp = power.VMPowerAdmissionTests.setUp
    call = power.VMPowerAdmissionTests.call
    reviewed = power.VMPowerAdmissionTests.reviewed

    @staticmethod
    def status_write(obj, **status):
        """What virt-controller, virt-handler or the kubelet does: only status and the version move."""
        obj["status"] = {**(obj.get("status") or {}), **status}
        obj["metadata"]["resourceVersion"] += "1"

    def test_start_survives_a_status_write_on_the_vm(self):
        signed = self.reviewed()
        self.status_write(self.vm, printableStatus="Stopped", conditions=[{"type": "Ready", "status": "False"}])
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(200, result[0], result)
        writes.assert_called_once()

    def test_restart_survives_status_writes_on_the_instance_and_its_launcher(self):
        vmi, pod = self.running()
        self.vm["spec"]["runStrategy"] = "Manual"
        self.body["action"] = "restart"
        signed = self.reviewed()
        self.status_write(vmi, guestOSInfo={"name": "Example OS"})
        self.status_write(pod, conditions=[{"type": "Ready", "status": "True"}])
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(200, result[0], result)
        writes.assert_called_once()

    def test_a_vm_spec_change_still_needs_a_fresh_review(self):
        signed = self.reviewed()
        self.vm["spec"]["template"]["spec"]["domain"]["memory"]["guest"] = "6Gi"
        self.vm["metadata"]["resourceVersion"] += "1"
        result, writes = self.call("/api/vm/power", signed)
        self.assertEqual(409, result[0], result)
        writes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
