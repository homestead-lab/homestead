"""Moving an app to another host lands it there, or says why not.

Frigate was moved to k3s-3 twice and restarted on k3s-1 both times: the move
only preferred k3s-3, and k3s-1 already held its 2.2 GB image. On the shared
fake API (tests/fakes/kube.py)."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_host_move as MOVE
import homestead_image_cache as IMAGES
import homestead_kube as K
import homestead_operations as OPS
import homestead_workloads as W
from fakes.kube import Kube

IMAGE = "ghcr.io/blakeblackshear/frigate:stable"
DIGEST = "ghcr.io/blakeblackshear/frigate@sha256:" + "f" * 64


def deployment(image=IMAGE):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": "lab", "name": "frigate", "resourceVersion": "7"},
            "spec": {"template": {"spec": {"containers": [{"name": "frigate", "image": image}]}}}}


def node(held):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "k3s-3"}, "status": {"images": [{"names": held}]}}


def pull_daemonset(ready):
    return {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"namespace": "lab", "name": "homestead-pull-frigate"},
            "status": {"desiredNumberScheduled": 1, "numberReady": ready}}


class HostMoveTests(unittest.TestCase):
    def setUp(self, held=()):
        self.kube = self.enterContext(Kube(node(list(held)), deployment()))
        # The shared fake runs the real write guards; the storage guard takes the job
        # store's lock, which lives under its DATA_DIR.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.started = []
        for patch in (mock.patch.object(W, "kget", K.kget, create=True), mock.patch.object(W, "ksend", K.ksend, create=True),
                      mock.patch.object(MOVE.OPS, "start", side_effect=lambda kind, title, res, href, ref, *a, **k:
                                        self.started.append({"kind": kind, "title": title, "ref": ref}) or {"id": "op"}),
                      mock.patch.object(IMAGES, "prepull", side_effect=lambda image, nodes, **kw: {"daemonset": "homestead-pull-frigate"}),
                      mock.patch.object(IMAGES, "stop_prepull", return_value={}),
                      mock.patch.object(IMAGES, "I", mock.Mock(NS="lab"))):
            patch.start()
            self.addCleanup(patch.stop)
        self.applied = []

    def move(self, mode="preferred"):
        capacity = {"move": {"node": "k3s-3", "mode": mode}}
        MOVE.start({}, deployment(), capacity, lambda: self.applied.append(True))
        return {"ref": self.started[-1]["ref"]}

    def test_an_image_the_host_lacks_is_pulled_there_before_the_app_moves(self):
        item = self.move()
        IMAGES.prepull.assert_called_once_with(IMAGE, ["k3s-3"], own=True)
        self.assertEqual([], self.applied, "the app keeps running where it is while its image is pulled")
        self.kube.add(pull_daemonset(0))
        self.assertEqual("running", MOVE.status(item)[0])
        self.assertEqual([], self.kube.writes)
        self.kube.add(pull_daemonset(1))
        state, _, message = MOVE.status(item)
        self.assertEqual(("running", "PUT"), (state, self.kube.writes[0][0]))
        self.assertEqual("7", self.kube.writes[0][2]["metadata"]["resourceVersion"], "the reviewed manifest, as reviewed")
        self.assertNotIn("proposed", item["ref"])

    def test_a_host_that_has_the_image_moves_at_once(self):
        self.setUp(held=[IMAGE])
        self.move()
        IMAGES.prepull.assert_not_called()
        self.assertEqual([True], self.applied)
        self.setUp(held=[DIGEST])
        MOVE.start({}, deployment(DIGEST), {"move": {"node": "k3s-3", "mode": "preferred"}}, lambda: self.applied.append(True))
        IMAGES.prepull.assert_not_called()

    def test_an_app_edited_while_its_image_was_pulled_is_not_moved(self):
        item = self.move()
        def conflict(*a, **k):
            raise urllib.error.HTTPError("", 409, "conflict", {}, None)
        with mock.patch.object(W, "ksend", side_effect=conflict, create=True):
            state, _, message = MOVE.status(item)
        self.assertEqual("failed", state)
        self.assertIn("nothing was moved", message)

    def test_a_move_that_lands_elsewhere_fails_and_says_where(self):
        self.setUp(held=[IMAGE])
        item = self.move()
        with mock.patch.object(MOVE.OPS, "_deployment", return_value=("succeeded", 100, "1/1 replicas ready")), \
                mock.patch.object(MOVE.OPS, "deployment_progress", return_value={"pods": [{"node": "k3s-1", "phase": "Running"}]}):
            state, _, message = MOVE.status(item)
        self.assertEqual("failed", state)
        self.assertIn("running on k3s-1, not k3s-3", message)
        with mock.patch.object(MOVE.OPS, "_deployment", return_value=("succeeded", 100, "1/1 replicas ready")), \
                mock.patch.object(MOVE.OPS, "deployment_progress", return_value={"pods": [{"node": "k3s-3", "phase": "Running"}]}):
            self.assertEqual(("succeeded", 100, "frigate is running on k3s-3"), MOVE.status(item))


if __name__ == "__main__":
    unittest.main()
