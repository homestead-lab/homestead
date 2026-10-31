"""A host move that pulls its image first survives the app's status updates.

The move keeps the reviewed manifest for up to PULL_WAIT while the image is
pulled onto the new host. A status write in that time (a pod restarting, a
readiness flap) moved the resourceVersion, and the move failed with "Review
the move again". It now sends the reviewed manifest again on the new version
when only status moved; an edit since the review still stops the move."""
import copy
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_capacity_review as REVIEW
import homestead_host_move as MOVE
import homestead_image_cache as IMAGES
import homestead_workloads as W
from fakes.kube import Kube

APP = "/apis/apps/v1/namespaces/lab/deployments/app"
PULL = "/apis/apps/v1/namespaces/lab/daemonsets/homestead-pull-app"
IMAGE = "example.com/app:1"


def deployment():
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "app", "namespace": "lab"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "app", "image": IMAGE}]}}}}


class HostMoveStatusWriteTests(unittest.TestCase):
    def setUp(self):
        node = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node-a"}, "status": {"images": []}}
        pull = {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": "homestead-pull-app", "namespace": "lab"},
                "spec": {}, "status": {"desiredNumberScheduled": 1, "numberReady": 1}}
        self.kube = Kube(deployment(), node, pull)
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        self.started = []
        for patch in (mock.patch.object(W, "kget", K.kget, create=True), mock.patch.object(W, "ksend", K.ksend, create=True),
                      mock.patch.object(MOVE.OPS, "start", side_effect=lambda kind, title, res, href, ref, *a, **k:
                                        self.started.append(ref) or {"id": "op"}),
                      mock.patch.object(IMAGES, "prepull", return_value={"daemonset": "homestead-pull-app"}),
                      mock.patch.object(IMAGES, "stop_prepull", return_value={}),
                      mock.patch.object(IMAGES, "I", mock.Mock(NS="lab"))):
            patch.start()
            self.addCleanup(patch.stop)

    def start(self):
        """The reviewed move, as move_to_host starts it: the image is not on node-a yet."""
        current = K.kget(APP)
        context = {"action": "host-move", "uid": current["metadata"]["uid"],
                   "resourceVersion": current["metadata"]["resourceVersion"],
                   "settled": REVIEW.review_identity(current)["settled"]}
        proposed = copy.deepcopy(current)
        proposed["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "node-a"}
        MOVE.start({}, proposed, {"move": {"node": "node-a", "mode": "pinned"}}, lambda: self.fail("moved before the pull"),
                   settled=context["settled"])
        self.assertEqual("pulling", self.started[-1]["stage"])
        return {"ref": self.started[-1]}

    def test_a_status_write_during_the_pull_does_not_stop_the_move(self):
        item = self.start()
        K.ksend("PUT", APP + "/status", {"status": {"readyReplicas": 0, "conditions": [{"type": "Available", "status": "False"}]}})
        state, _, message = MOVE.status(item)
        self.assertEqual("running", state, message)
        spec = self.kube.object(APP)["spec"]["template"]["spec"]
        self.assertEqual({"kubernetes.io/hostname": "node-a"}, spec.get("nodeSelector"))

    def test_an_edit_during_the_pull_still_stops_the_move(self):
        item = self.start()
        edited = K.kget(APP)
        edited["spec"]["template"]["spec"]["containers"][0]["image"] = "example.com/app:2"
        K.ksend("PUT", APP, edited)
        state, _, message = MOVE.status(item)
        self.assertEqual("failed", state)
        self.assertIn("nothing was moved", message)
        spec = self.kube.object(APP)["spec"]["template"]["spec"]
        self.assertEqual(("example.com/app:2", None), (spec["containers"][0]["image"], spec.get("nodeSelector")))

    def test_an_annotation_written_during_the_pull_is_not_overwritten(self):
        item = self.start()
        K.ksend("PATCH", APP, {"metadata": {"annotations": {"example.com/checked": "now"}}}, ctype="application/merge-patch+json")
        state, _, _ = MOVE.status(item)
        self.assertEqual("failed", state)
        self.assertEqual("now", self.kube.object(APP)["metadata"]["annotations"]["example.com/checked"])


if __name__ == "__main__":
    unittest.main()
