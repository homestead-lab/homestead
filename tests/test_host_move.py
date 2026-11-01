"""Moving an app to another host lands it there, or says why not.

Frigate was moved to k3s-3 twice and restarted on k3s-1 both times: the move
only preferred k3s-3, and k3s-1 already held its 2.2 GB image. On the shared
fake API (tests/fakes/kube.py)."""
import copy
import json
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
import homestead_place as PLACE
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
        item = self.moved_to_k3s_3()
        path = "/apis/apps/v1/namespaces/lab/deployments/frigate"
        edited = self.kube.object(path)
        edited["spec"]["template"]["spec"]["containers"][0]["image"] = DIGEST
        K.ksend("PUT", path, edited)
        state, _, message = MOVE.status(item)
        self.assertEqual("failed", state)
        self.assertIn("Homestead stopped", message)
        self.assertEqual(DIGEST, self.kube.object(path)["spec"]["template"]["spec"]["containers"][0]["image"],
                         "the edit made since the review stands")
        self.assertNotIn("proposed", item["ref"])
        self.assertNotIn("settled", item["ref"])

    def test_a_job_stuck_by_the_old_bug_fails_with_a_message(self):
        item = self.move()
        self.kube.add(pull_daemonset(1))
        item["ref"].pop("proposed")
        state, _, message = MOVE.status(item)
        self.assertEqual("failed", state)
        self.assertIn("Review the move again", message)

    def test_a_failed_put_keeps_the_reviewed_move_for_the_next_round(self):
        item = self.move()
        self.kube.add(pull_daemonset(1))
        def broken(*a, **k):
            raise urllib.error.HTTPError("", 500, "boom", {}, None)
        with mock.patch.object(W, "ksend", side_effect=broken, create=True):
            with self.assertRaises(urllib.error.HTTPError):
                MOVE.status(item)
        self.assertIn("proposed", item["ref"], "the reviewed manifest survives a failed PUT")
        item["ref"]["settled"] = "s"
        item = {"ref": json.loads(json.dumps(item["ref"]))}
        self.assertIn("settled", item["ref"])
        state, _, _ = MOVE.status(item)
        self.assertEqual(("running", "PUT"), (state, self.kube.writes[0][0]))
        self.assertNotIn("proposed", item["ref"])
        self.assertEqual("rolling", item["ref"]["stage"])

    def moved_to_k3s_3(self):
        """A reviewed manifest that really changes the app: it asks for k3s-3."""
        proposed = deployment()
        proposed["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "k3s-3"}
        MOVE.start({}, proposed, {"move": {"node": "k3s-3", "mode": "pinned"}}, lambda: self.applied.append(True))
        self.kube.add(pull_daemonset(1))
        return {"ref": self.started[-1]["ref"]}

    def test_a_put_that_landed_but_lost_its_reply_moves_on_to_rolling(self):
        # On a real cluster a PUT can commit and its reply be lost (a timeout, a
        # dropped connection, a 5xx after the write). The retry then sends the old
        # resourceVersion and gets 409, though the app is rolling to the new host.
        item = self.moved_to_k3s_3()
        path = "/apis/apps/v1/namespaces/lab/deployments/frigate"
        self.kube.fail(path, TimeoutError("timed out"), method="PUT", applied=True)
        with self.assertRaises(TimeoutError):
            MOVE.status(item)
        self.assertEqual({"kubernetes.io/hostname": "k3s-3"},
                         self.kube.object(path)["spec"]["template"]["spec"].get("nodeSelector"), "the write stands")
        item = {"ref": json.loads(json.dumps(item["ref"]))}
        state, _, message = MOVE.status(item)
        self.assertEqual("running", state, message)
        self.assertNotIn("nothing was moved", message)
        self.assertEqual("rolling", item["ref"]["stage"])
        self.assertNotIn("proposed", item["ref"])

    def test_a_lost_reply_on_a_manifest_reviewed_from_the_live_app_lands(self):
        # The real caller (homestead_workloads.move_capacity_plan) reviews a deep
        # copy of the live GET with only the placement changed, so every field the
        # API server defaulted is in it. Here the live app carries such fields.
        path = "/apis/apps/v1/namespaces/lab/deployments/frigate"
        live = deployment()
        live["spec"].update(replicas=1, revisionHistoryLimit=10, progressDeadlineSeconds=600,
                            strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}})
        live["spec"]["template"]["spec"].update(restartPolicy="Always", dnsPolicy="ClusterFirst", schedulerName="default-scheduler")
        live["spec"]["template"]["spec"]["containers"][0].update(imagePullPolicy="Always", terminationMessagePath="/dev/termination-log")
        self.kube.add(live)
        proposed = copy.deepcopy(self.kube.object(path))
        mode = PLACE.apply_node_placement(proposed, "k3s-3")
        MOVE.start({}, proposed, {"move": {"node": "k3s-3", "mode": mode}}, lambda: self.applied.append(True))
        self.kube.add(pull_daemonset(1))
        item = {"ref": json.loads(json.dumps(self.started[-1]["ref"]))}
        self.kube.fail(path, TimeoutError("timed out"), method="PUT", applied=True)
        with self.assertRaises(TimeoutError):
            MOVE.status(item)
        self.assertIn("affinity", self.kube.object(path)["spec"]["template"]["spec"], "the write stands")
        state, _, message = MOVE.status(item)
        self.assertEqual(("running", "rolling"), (state, item["ref"]["stage"]), message)

    def test_an_app_edited_another_way_after_a_lost_reply_is_not_moved(self):
        item = self.moved_to_k3s_3()
        path = "/apis/apps/v1/namespaces/lab/deployments/frigate"
        self.kube.fail(path, TimeoutError("timed out"), method="PUT", applied=True)
        with self.assertRaises(TimeoutError):
            MOVE.status(item)
        edited = self.kube.object(path)
        edited["spec"]["template"]["spec"]["containers"][0]["image"] = DIGEST
        K.ksend("PUT", path, edited)
        state, _, message = MOVE.status(item)
        self.assertEqual("failed", state)
        self.assertIn("changed while its image was being pulled", message)
        self.assertIn("check where it runs now", message, "the reviewed move may have landed before the edit")
        self.assertNotIn("nothing was moved", message)
        self.assertNotIn("proposed", item["ref"])

    APP = "/apis/apps/v1/namespaces/lab/deployments/frigate"

    def lost_reply_from_live(self):
        """A pinned move reviewed from the live app (uid and all), whose PUT
        landed and lost its reply; the job will try again next round."""
        proposed = copy.deepcopy(self.kube.object(self.APP))
        PLACE.apply_node_placement(proposed, "k3s-3", True)
        MOVE.start({}, proposed, {"move": {"node": "k3s-3", "mode": "pinned"}}, lambda: self.applied.append(True))
        self.kube.add(pull_daemonset(1))
        item = {"ref": json.loads(json.dumps(self.started[-1]["ref"]))}
        self.kube.fail(self.APP, TimeoutError("timed out"), method="PUT", applied=True)
        with self.assertRaises(TimeoutError):
            MOVE.status(item)
        return item, self.kube.object(self.APP)

    def test_a_reread_that_fails_after_a_lost_reply_tries_again_next_round(self):
        # Under load the re-read can answer 503 too: that says nothing about the app.
        item, _ = self.lost_reply_from_live()
        self.kube.fail(self.APP, 503, method="GET")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            MOVE.status(item)
        self.assertEqual(503, raised.exception.code)
        self.assertIn("proposed", item["ref"], "the job keeps the reviewed move")
        state, _, message = MOVE.status(item)
        self.assertEqual(("running", "rolling"), (state, item["ref"]["stage"]), message)

    def test_an_app_scaled_after_a_lost_reply_still_rolls(self):
        item, live = self.lost_reply_from_live()
        live["spec"]["replicas"] = 3
        K.ksend("PUT", self.APP, live)
        state, _, message = MOVE.status(item)
        self.assertEqual(("running", "rolling"), (state, item["ref"]["stage"]), message)

    def test_an_app_recreated_after_a_lost_reply_is_not_moved(self):
        item, live = self.lost_reply_from_live()
        self.kube.discard(self.APP)
        for field in ("uid", "resourceVersion", "creationTimestamp"):
            live["metadata"].pop(field)
        self.kube.add(live)
        self.assertNotEqual(item["ref"]["proposed"]["metadata"]["uid"], self.kube.object(self.APP)["metadata"]["uid"])
        state, _, message = MOVE.status(item)
        self.assertEqual("failed", state, message)
        self.assertIn("changed while its image was being pulled", message)

    def test_an_app_being_deleted_after_a_lost_reply_is_not_moved(self):
        item, live = self.lost_reply_from_live()
        live["metadata"].update(deletionTimestamp="2026-11-01T00:00:00Z", finalizers=["example.com/hold"])
        live["metadata"].pop("resourceVersion")
        self.kube.add(live)
        self.assertTrue(self.kube.object(self.APP)["metadata"].get("deletionTimestamp"))
        state, _, message = MOVE.status(item)
        self.assertEqual("failed", state, message)
        self.assertIn("changed while its image was being pulled", message)

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
