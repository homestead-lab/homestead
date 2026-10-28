"""Looking at the image cache or the imports writes nothing (#622). The scans,
the clean-up of finished pulls and the passwords finished copies borrowed are
the leader's, once a minute."""
import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_imports as IMPORTS
import homestead_image_cache as IMAGES
import homestead_container_import as CONTAINER_IMPORT
import homestead_names as NAMES
from fakes.kube import Kube

TASK = NAMES.key("task")
LISTING = {"images": [{"repoTags": ["example.test/app:1"], "repoDigests": [], "size": "10"}]}


def node(name="node1"):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name},
            "status": {"conditions": [{"type": "Ready", "status": "True"}], "images": []}}


def finished_scan():
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": "homestead-image-scan-x", "namespace": "lab", "labels": {TASK: "image-scan"},
                         "annotations": {NAMES.key("cache-node"): "node1"}},
            "spec": {"nodeName": "node1", "containers": [{"name": "scan", "image": "scan"}]},
            "status": {"phase": "Succeeded"}}


def finished_pull():
    return {"apiVersion": "apps/v1", "kind": "DaemonSet",
            "metadata": {"name": "homestead-pull-frigate", "namespace": "lab", "labels": {TASK: "prepull"}},
            "spec": {"template": {"spec": {"initContainers": [{"name": "pull", "image": "frigate:0.18.0"}],
                                           "containers": [{"name": "pause", "image": "pause"}]}}},
            "status": {"desiredNumberScheduled": 1, "numberReady": 1}}


def finished_copy():
    """An import into another namespace that has finished, and the source
    password it borrowed."""
    return [{"apiVersion": "batch/v1", "kind": "Job",
             "metadata": {"name": "homestead-import-b", "namespace": "media", "labels": {TASK: "import"}},
             "status": {"succeeded": 1}},
            {"apiVersion": "v1", "kind": "Secret",
             "metadata": {"name": "homestead-import-b-source", "namespace": "media"}}]


class Fixture(unittest.TestCase):
    def setUp(self):
        saved = (IMPORTS.kget, IMPORTS.ksend, IMPORTS.NS, IMPORTS.SCAN_DIR, IMAGES._SCAN_STARTED[0], NAMES.kget)

        def restore():
            IMPORTS.kget, IMPORTS.ksend, IMPORTS.NS, IMPORTS.SCAN_DIR, IMAGES._SCAN_STARTED[0], NAMES.kget = saved
            IMAGES._SCANS.clear()
            IMAGES._SCAN_FAILED.clear()
        self.addCleanup(restore)
        IMPORTS.kget, IMPORTS.ksend, IMPORTS.NS, IMPORTS.SCAN_DIR = K.kget, K.ksend, "lab", ""
        IMAGES._SCANS.clear()
        IMAGES._SCAN_FAILED.clear()
        IMAGES._SCAN_STARTED[0] = 0.0
        NAMES.kget = K.kget
        shim = types.ModuleType("homestead_shim")
        shim.raw_get = lambda path: json.dumps(LISTING)
        patches = [mock.patch.dict(sys.modules, {"homestead_shim": shim}),
                   mock.patch.object(IMPORTS.RUNTIME, "pod", lambda name, ns, node, *a, **k: {
                       "apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ns},
                       "spec": {"nodeName": node, "containers": [{"name": "scan", "image": "scan"}]}})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

class ViewerReadsTests(Fixture):
    def test_looking_at_the_image_cache_writes_nothing(self):
        with Kube(node(), node("node2"), finished_scan(), finished_pull()) as kube:
            report = IMAGES.image_cache()
            self.assertEqual([], kube.writes, "a viewer's read started, removed or saved something")
        # What it shows is as before: the finished scan, and the pull as finished.
        self.assertIn("example.test/app:1", {row["name"] for row in report["images"]})
        self.assertEqual(["homestead-pull-frigate"], [row["name"] for row in report["pulls_finished"]])

    def test_looking_at_the_imports_writes_nothing(self):
        with Kube(*finished_copy()) as kube:
            rows = CONTAINER_IMPORT.import_status()
            self.assertEqual([], kube.writes)
        self.assertEqual(["done"], [row["state"] for row in rows])

    def test_the_leaders_minute_does_what_looking_used_to(self):
        with Kube(node(), node("node2"), finished_scan(), finished_pull(), *finished_copy()) as kube:
            IMAGES.tick()
            deleted = {path for method, path, *_ in kube.writes if method == "DELETE"}
            started = [path for method, path, *_ in kube.writes if method == "POST"]
            self.assertIn("/api/v1/namespaces/lab/pods/homestead-image-scan-x", deleted)
            self.assertTrue(any("/daemonsets/homestead-pull-frigate" in path for path in deleted))
            self.assertIn("/api/v1/namespaces/media/secrets/homestead-import-b-source", deleted)
            # node2 has never been scanned, so a scan starts there (and on node1,
            # whose scan was only just read).
            self.assertEqual(2, len(started))
        self.assertIn("node1", IMAGES._SCANS)

    def test_a_fresh_scan_is_not_asked_again(self):
        IMAGES._SCANS["node1"] = {"at": time.time(), "images": []}
        with Kube(node()) as kube:
            IMAGES.tick()
            self.assertEqual([], [path for method, path, *_ in kube.writes if method == "POST"])


class BackgroundScanTests(Fixture):
    """Scans run on the leader whether anyone looks or not, so a host that
    cannot be scanned must not keep every host being scanned again."""

    def posts(self, kube):
        return [path for method, path, *_ in kube.writes if method == "POST"]

    def test_a_cordoned_host_does_not_keep_every_host_rescanning(self):
        cordoned = node("node2")
        cordoned["spec"] = {"unschedulable": True}
        IMAGES._SCANS["node1"] = {"at": time.time(), "images": []}
        with Kube(node(), cordoned) as kube:
            IMAGES.tick()
            self.assertEqual([], self.posts(kube), "node2 is never scanned, so it is not a reason to scan")

    def test_a_host_whose_scan_fails_is_not_asked_again_until_its_time(self):
        failed = finished_scan()
        failed["status"]["phase"] = "Failed"
        with Kube(node(), failed) as kube:
            IMAGES.tick()
            self.assertIn("/api/v1/namespaces/lab/pods/homestead-image-scan-x",
                          {path for method, path, *_ in kube.writes if method == "DELETE"})
            self.assertEqual([], self.posts(kube), "a scan that failed just now counts as this round's")

    def test_a_scan_that_fails_part_way_still_counts_as_started(self):
        def pod(name, ns, node_name, *a, **k):
            if node_name == "node2":
                raise OSError("could not build the pod")
            return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ns},
                    "spec": {"nodeName": node_name, "containers": [{"name": "scan", "image": "scan"}]}}
        with Kube(node(), node("node2")), mock.patch.object(IMPORTS.RUNTIME, "pod", pod):
            with self.assertRaises(OSError):
                IMAGES.start_image_scan()
        self.assertGreater(IMAGES._SCAN_STARTED[0], 0, "otherwise the next tick starts node1's pod again")


class LeaderLoopTests(unittest.TestCase):
    """The leader's one-minute loop runs the tick, even when the volume
    rebuild beside it fails, and not while the cluster shuts down."""

    def run_once(self, leader=True, phase=None, rebuild=None):
        import homestead_cluster_server as CLUSTER
        sleeps = iter([None])

        def sleep(_):
            next(sleeps)            # the second sleep ends the loop
        state = types.SimpleNamespace(state=lambda: {"phase": phase} if phase else {})
        tick = mock.Mock()
        with mock.patch.object(CLUSTER.time, "sleep", sleep), \
             mock.patch.object(CLUSTER.LEADER, "is_leader", lambda: leader), \
             mock.patch.object(CLUSTER, "cluster_shutdown", lambda: state), \
             mock.patch.object(CLUSTER.LHREBUILD, "tick", rebuild or mock.Mock()), \
             mock.patch.object(CLUSTER, "_cache", {}, create=True), \
             mock.patch.object(IMAGES, "tick", tick), \
             mock.patch("builtins.print"):
            with self.assertRaises(StopIteration):
                CLUSTER._detached_copies_loop()
        return tick.call_count

    def test_the_leader_runs_it_each_minute(self):
        self.assertEqual(1, self.run_once())

    def test_a_failed_rebuild_does_not_stop_it(self):
        self.assertEqual(1, self.run_once(rebuild=mock.Mock(side_effect=RuntimeError("down"))))

    def test_not_on_another_replica_or_during_a_shutdown(self):
        self.assertEqual(0, self.run_once(leader=False))
        self.assertEqual(0, self.run_once(phase="stopping"))


if __name__ == "__main__":
    unittest.main()
