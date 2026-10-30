import json
import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_imports as imports
import homestead_image_cache as IMAGES
from fakes.platform import plain_platform


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        plain_platform(self)
        self.active = "sha256:" + "a" * 64
        self.rollback = "sha256:" + "b" * 64
        self.unused = "sha256:" + "c" * 64
        self.nodes = [{"metadata": {"name": "node1"}, "status": {"images": [
            {"names": ["docker.io/library/nginx:1.27.0",
                       "docker.io/library/nginx@" + self.active], "sizeBytes": 100 * 1024**2},
            {"names": ["docker.io/library/nginx@" + self.rollback], "sizeBytes": 99 * 1024**2},
            {"names": ["example.test/unused@" + self.unused], "sizeBytes": 25 * 1024**2},
            {"names": ["docker.io/library/redis:7.2"], "sizeBytes": 40 * 1024**2},
            {"names": ["registry.k8s.io/pause@sha256:" + "d" * 64], "sizeBytes": 1 * 1024**2},
        ]}}]
        self.sent = []
        pods = [{
            "metadata": {"name": "web-abc", "namespace": "lab", "labels": {"app": "web"}},
            "spec": {"containers": [{"name": "nginx", "image": "nginx:1.27.0"},
                                      {"name": "redis", "image": "redis:7.2"}]},
            "status": {"containerStatuses": [
                {"name": "nginx", "imageID": "docker-pullable://docker.io/library/nginx@" + self.active},
                {"name": "redis", "imageID": ""},
            ]},
        }, {
            "metadata": {"name": "finished-job", "namespace": "lab"},
            "spec": {"containers": [{"name": "job", "image": "example.test/unused@" + self.unused}]},
            "status": {"phase": "Succeeded", "containerStatuses": [{
                "name": "job", "imageID": "example.test/unused@" + self.unused}]},
        }]
        deployments = [{
            "metadata": {"name": "web", "namespace": "lab", "annotations": {
                "homestead.io/update-previous": json.dumps({
                    "images": {"nginx": "docker.io/library/nginx@" + self.rollback},
                    "at": "2026-09-19T18:00:00Z",
                })}},
            # Running: its status says its pod (web-abc) is there.
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "nginx", "image": "nginx:1.27.0"}, {"name": "redis", "image": "redis:7.2"}]}}},
            "status": {"replicas": 1, "updatedReplicas": 1},
        }]

        def get(path):
            if path == "/api/v1/nodes":
                return {"items": self.nodes}
            if path == "/api/v1/pods":
                return {"items": pods}
            if path == "/apis/apps/v1/deployments":
                return {"items": deployments}
            if path in ("/apis/apps/v1/statefulsets", "/apis/batch/v1/cronjobs",
                        "/apis/apps/v1/daemonsets", "/apis/batch/v1/jobs",
                        "/apis/kubevirt.io/v1/virtualmachines",
                        "/apis/kubevirt.io/v1/virtualmachineinstancereplicasets",
                        "/apis/pool.kubevirt.io/v1alpha1/virtualmachinepools"):
                return {"items": []}
            # No KubeVirt add-ons, Harvester or CDI here: discovery says so.
            if path == "/apis":
                return {"groups": [{"name": "apps"}, {"name": "batch"}, {"name": "kubevirt.io"}]}
            if path.count("/") == 2 and path.startswith("/apis/"):
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            raise AssertionError(path)

        # Scans saved to disk belong to the tests that ask for it.
        self.addCleanup(setattr, imports, "SCAN_DIR", imports.SCAN_DIR)
        imports.SCAN_DIR = ""
        IMAGES._SCANS.clear()
        imports.kget = get
        imports.NAMES.bind(get)
        def send(method, path, body=None, **kwargs):
            if method == "DELETE":
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            self.sent.append((method, path, body))
            return body
        imports.ksend = send

    def test_active_and_rollback_digests_are_protected(self):
        report = IMAGES.image_cache()
        by_digest = {row["digest"]: row for row in report["images"] if row["digest"]}
        self.assertTrue(by_digest[self.active]["protected"])
        self.assertEqual("active", by_digest[self.active]["retained_by"][0]["reason"])
        self.assertTrue(by_digest[self.rollback]["protected"])
        self.assertEqual("rollback", by_digest[self.rollback]["retained_by"][0]["reason"])
        self.assertFalse(by_digest[self.unused]["protected"])
        self.assertEqual(3, report["protected"])

    def test_active_tag_without_runtime_digest_matches_canonical_alias(self):
        report = IMAGES.image_cache()
        redis = next(row for row in report["images"] if row["name"] == "docker.io/library/redis:7.2")
        self.assertTrue(redis["protected"])
        self.assertEqual("redis", redis["retained_by"][0]["container"])

    def test_cleanup_revalidates_protection_and_builds_scoped_pod(self):
        with self.assertRaisesRegex(PermissionError, "protected by active"):
            IMAGES.cleanup_image(self.active, ["node1"])
        result = IMAGES.cleanup_image(self.unused, ["node1"])
        self.assertEqual(["node1"], result["nodes"])
        pod = self.sent[-1][2]
        self.assertEqual("node1", pod["spec"]["nodeName"])
        container = pod["spec"]["containers"][0]
        script = container["command"][-1]
        self.assertIn(f"rmi 'example.test/unused@{self.unused}'", script)
        self.assertIn("--state exited", script, "only exited containers are cleared, never a running one")
        self.assertEqual("FallbackToLogsOnError", container["terminationMessagePolicy"])
        self.assertRegex(pod["metadata"]["name"], r"^homestead-image-clean-[0-9a-f]{10}-[0-9a-f]+$",
                         "each attempt has a name of its own")
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(["ALL"], container["securityContext"]["capabilities"]["drop"])

    def test_platform_images_cannot_be_cleaned(self):
        with self.assertRaisesRegex(PermissionError, "platform images"):
            IMAGES.cleanup_image("sha256:" + "d" * 64, ["node1"])


if __name__ == "__main__":
    unittest.main()


class PrepullTests(unittest.TestCase):
    """A pre-pull is a DaemonSet, which means it needs stopping, not deleting."""

    def setUp(self):
        self.sent = []
        self.daemonsets = []
        self.nodes = [
            self._node("node1"),
            self._node("node2", ready=False),
            self._node("node3", cordoned=True),
        ]

        def get(path):
            if path == "/api/v1/nodes":
                return {"items": self.nodes}
            if "/daemonsets?" in path:
                return {"items": self.daemonsets}
            if "/daemonsets/" in path:
                name = path.rsplit("/", 1)[-1].split("?")[0]
                found = next((d for d in self.daemonsets
                              if d["metadata"]["name"] == name), None)
                if not found:
                    raise urllib.error.HTTPError(path, 404, "missing", {}, None)
                return found
            raise AssertionError(path)

        imports.kget = get
        imports.NAMES.bind(get)
        self.posted = []

        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path))
            if method == "POST":
                self.posted.append(body)
            return body or {}

        imports.ksend = send
        self.sleep, imports.time.sleep = imports.time.sleep, lambda _: None

    def tearDown(self):
        imports.time.sleep = self.sleep

    @staticmethod
    def _node(name, ready=True, cordoned=False):
        return {"metadata": {"name": name},
                "spec": {"unschedulable": cordoned} if cordoned else {},
                "status": {"conditions": [{"type": "Ready",
                                           "status": "True" if ready else "False"}]}}

    def _pull(self, name="homestead-pull-frigate", desired=1, ready=1):
        return {"metadata": {"name": name, "labels": {"homestead.io/task": "prepull"}},
                "spec": {"template": {"spec": {"initContainers": [
                    {"name": "pull", "image": "frigate:0.18.0"}]}}},
                "status": {"desiredNumberScheduled": desired, "numberReady": ready}}

    def test_a_cordoned_or_unready_node_is_left_out(self):
        result = IMAGES.prepull("ghcr.io/x/frigate:0.18.0")

        self.assertEqual(["node1"], result["nodes"])
        self.assertEqual(["node2", "node3"], result["skipped"])
        # Pinned by affinity, because a DaemonSet cannot refuse the tolerations
        # its controller adds for unschedulable, not-ready and unreachable.
        terms = (self.posted[0]["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]
                 ["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"])
        self.assertEqual(["node1"], terms[0]["matchExpressions"][0]["values"])

    def test_asking_for_only_an_unhealthy_node_is_refused(self):
        with self.assertRaisesRegex(ValueError, "node2 is not ready"):
            IMAGES.prepull("ghcr.io/x/frigate:0.18.0", ["node2"])
        self.assertNotIn(("POST", "/apis/apps/v1/namespaces/lab/daemonsets"), self.sent)

    def test_a_finished_pull_is_cleared_away_rather_than_left_running(self):
        self.daemonsets = [self._pull(desired=2, ready=2)]

        status = IMAGES.prepull_status()

        self.assertEqual([], status["pulls"])
        self.assertEqual(["homestead-pull-frigate"], [row["name"] for row in status["finished"]])
        self.assertIn(("DELETE", "/apis/apps/v1/namespaces/lab/daemonsets/"
                       "homestead-pull-frigate?propagationPolicy=Background"), self.sent)

    def test_a_pull_still_running_is_reported_and_kept(self):
        self.daemonsets = [self._pull(desired=3, ready=1)]

        status = IMAGES.prepull_status()

        self.assertEqual([{"name": "homestead-pull-frigate", "image": "frigate:0.18.0",
                           "desired": 3, "ready": 1, "complete": False}], status["pulls"])
        self.assertEqual([], [x for x in self.sent if x[0] == "DELETE"])

    def test_only_a_pre_pull_can_be_stopped_this_way(self):
        for name in ("", "longhorn-manager", "../../kube-system/x", "homestead-import-frigate"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "not an image pre-pull"):
                    IMAGES.stop_prepull(name)


    def test_two_jobs_pulling_the_same_image_each_get_their_own_pull(self):
        """One job's cancel, or the end of its pulling stage, deletes its own
        DaemonSet only, and a second job's pull does not replace the first."""
        first = IMAGES.prepull("ghcr.io/x/frigate:0.18.0", ["node1"], own=True)["daemonset"]
        self.sent.clear()
        second = IMAGES.prepull("ghcr.io/x/frigate:0.18.0", ["node1"], own=True)["daemonset"]
        self.assertNotEqual(first, second)
        for name in (first, second):
            self.assertRegex(name, r"^homestead-pull-frigate-[0-9a-f]{8}$")
        self.assertEqual([], [path for method, path in self.sent
                              if method == "DELETE" and path.rsplit("/", 1)[-1].split("?")[0] == first])
        apps = [body["spec"]["template"]["metadata"]["labels"]["app"] for body in self.posted]
        self.assertEqual([first, second], apps)
        self.assertEqual([first, second], [body["spec"]["selector"]["matchLabels"]["app"] for body in self.posted])
        self.sent.clear()
        IMAGES.stop_prepull(second)
        self.assertEqual([("DELETE", f"/apis/apps/v1/namespaces/lab/daemonsets/{second}?propagationPolicy=Background")],
                         self.sent)

    def test_a_job_pull_name_stays_a_valid_label_for_a_long_image_name(self):
        name = IMAGES.prepull("ghcr.io/x/" + "a" * 80 + ":1", ["node1"], own=True)["daemonset"]
        self.assertLessEqual(len(name), 63)

    def test_a_pull_without_a_job_keeps_its_one_name_per_image(self):
        self.assertEqual("homestead-pull-frigate", IMAGES.prepull("ghcr.io/x/frigate:0.18.0")["daemonset"])

    def test_an_old_per_image_pull_from_a_job_can_still_be_stopped_and_swept(self):
        IMAGES.stop_prepull("homestead-pull-frigate")
        self.assertIn(("DELETE", "/apis/apps/v1/namespaces/lab/daemonsets/"
                       "homestead-pull-frigate?propagationPolicy=Background"), self.sent)
        self.daemonsets = [self._pull("homestead-pull-frigate"), self._pull("homestead-pull-frigate-0a1b2c3d")]
        self.assertEqual(["homestead-pull-frigate", "homestead-pull-frigate-0a1b2c3d"],
                         [row["name"] for row in IMAGES.prepull_status()["finished"]])

    def test_a_pull_started_from_image_cache_is_its_own_job(self):
        from types import SimpleNamespace
        from unittest import mock
        started = []
        with mock.patch.object(imports.OPS, "start", side_effect=lambda *a: started.append(a) or {"id": "op"}):
            first = imports._prepull(SimpleNamespace(body={"image": "ghcr.io/x/frigate:0.18.0", "nodes": ["node1"]}))
            second = imports._prepull(SimpleNamespace(body={"image": "ghcr.io/x/frigate:0.18.0", "nodes": ["node1"]}))
        self.assertNotEqual(first["daemonset"], second["daemonset"])
        self.assertEqual([first["daemonset"], second["daemonset"]], [a[4]["name"] for a in started])
