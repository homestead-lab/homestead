"""An Image cache pre-pull job, followed on the shared Kubernetes fake.

A pull DaemonSet gets no Progressing or Available conditions, so a pull that
can never succeed (a typo in the name, a private registry without
credentials) has to be read from its pods: every pod still waiting failing
its pull for PULL_STUCK_FOR ends the job, with the error, and stops the
pull."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_image_cache as IMAGES
import homestead_imports as imports
import homestead_kube as K
import homestead_names as NAMES
import homestead_operations as operations
from fakes.kube import Kube

NAME = "homestead-pull-frigate-0a1b2c3d"
IMAGE = "ghcr.example.com/x/frigate:0.18.0"
DS_PATH = f"/apis/apps/v1/namespaces/lab/daemonsets/{NAME}"


def daemonset(desired, ready):
    return {"apiVersion": "apps/v1", "kind": "DaemonSet",
            "metadata": {"name": NAME, "namespace": "lab",
                         "labels": {"homestead.io/task": "prepull", "homestead.io/pull-by": "image-cache"}},
            "spec": {"selector": {"matchLabels": {"app": NAME}},
                     "template": {"metadata": {"labels": {"app": NAME}},
                                  "spec": {"initContainers": [{"name": "pull", "image": IMAGE}],
                                           "containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.9"}]}}},
            "status": {"desiredNumberScheduled": desired, "numberReady": ready,
                       "numberUnavailable": desired - ready}}


def pod(node, waiting=None, message="", ready=False):
    """One pull pod on node: ready, or its pull container waiting for a reason."""
    init = {"name": "pull", "ready": ready,
            "state": {"terminated": {"exitCode": 0}} if ready else
                     {"waiting": {"reason": waiting or "PodInitializing", "message": message}}}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": f"{NAME}-{node}", "namespace": "lab", "labels": {"app": NAME}},
            "spec": {"nodeName": node},
            "status": {"phase": "Running" if ready else "Pending",
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "initContainerStatuses": [init]}}


NOT_FOUND = 'failed to resolve reference "ghcr.example.com/x/frigate:0.18.0": not found'


class PrepullJobTests(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.now = [1_000_000.0]
        self.enterContext(mock.patch.object(IMAGES, "_clock", lambda: self.now[0]))
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        for module, name, value in ((operations, "kget", K.kget), (operations, "DATA_DIR", tmp),
                                    (imports, "kget", K.kget), (imports, "ksend", K.ksend),
                                    (NAMES, "kget", K.kget)):
            self.enterContext(mock.patch.object(module, name, value))
        self.enterContext(mock.patch.dict(operations.RESOLVERS, {"image-pull": operations._prepull}))

    def start(self, *objects):
        for obj in objects:
            self.kube.add(obj)
        return operations.start("image-pull", f"Pull {IMAGE}", {"kind": "Image", "name": IMAGE, "namespace": "lab"},
                                "/image-cache", {"namespace": "lab", "name": NAME})

    def step(self, after=0):
        self.now[0] += after
        return operations.list_operations()[0]

    def test_a_pull_that_fails_on_every_node_ends_the_job_failed_and_stops_the_pull(self):
        self.start(daemonset(2, 0), pod("node-a", "ErrImagePull", NOT_FOUND), pod("node-b", "ImagePullBackOff",
                                                                                 f'Back-off pulling image "{IMAGE}"'))
        first = self.step()
        self.assertEqual("running", first["status"], "a pull is retried for a while: a registry can be down briefly")
        self.assertIn("ErrImagePull", first["message"], "the error is shown while it is retried")
        self.assertIsNotNone(self.kube.object(DS_PATH))

        self.assertEqual("running", self.step(IMAGES.PULL_STUCK_FOR - 10)["status"])
        failed = self.step(20)
        self.assertEqual("failed", failed["status"])
        self.assertIn(IMAGE, failed["message"])
        self.assertIn("not found", failed["message"], "the registry's own words, not only the kubelet's reason")
        self.assertIsNone(self.kube.object(DS_PATH), "a failed pull's DaemonSet is removed, not left retrying")

    def test_a_pull_failing_on_one_node_while_the_others_finished_fails_and_says_where(self):
        self.start(daemonset(2, 1), pod("node-a", ready=True), pod("node-b", "ErrImagePull", NOT_FOUND))
        self.step()
        failed = self.step(IMAGES.PULL_STUCK_FOR + 1)
        self.assertEqual("failed", failed["status"])
        self.assertIn("node-b", failed["message"])
        self.assertIn("1/2", failed["message"])

    def test_a_pull_that_recovers_starts_its_wait_again(self):
        self.start(daemonset(1, 0), pod("node-a", "ImagePullBackOff", "Back-off pulling image"))
        self.step()
        self.kube.add(pod("node-a"))                    # pulling again, the registry answered
        self.assertEqual("running", self.step(IMAGES.PULL_STUCK_FOR - 10)["status"])
        self.kube.add(pod("node-a", "ErrImagePull", NOT_FOUND))
        self.assertEqual("running", self.step(20)["status"], "the wait starts again from the new error")
        self.assertEqual("failed", self.step(IMAGES.PULL_STUCK_FOR + 1)["status"])

    def test_a_slow_pull_is_never_failed_for_taking_long(self):
        self.start(daemonset(2, 1), pod("node-a", ready=True), pod("node-b"))
        self.step()
        self.assertEqual("running", self.step(10 * IMAGES.PULL_STUCK_FOR)["status"])
        self.assertIsNotNone(self.kube.object(DS_PATH))

    def test_a_pod_not_made_yet_can_still_pull(self):
        self.start(daemonset(2, 0), pod("node-a", "ErrImagePull", NOT_FOUND))
        self.step()
        self.assertEqual("running", self.step(IMAGES.PULL_STUCK_FOR + 1)["status"])

    def test_a_finished_pull_still_succeeds(self):
        self.start(daemonset(2, 2), pod("node-a", ready=True), pod("node-b", ready=True))
        done = self.step()
        self.assertEqual("succeeded", done["status"])
        self.assertEqual("Image cached on 2/2 nodes", done["message"])


if __name__ == "__main__":
    unittest.main()
