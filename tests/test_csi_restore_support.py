"""Snapshot prerequisites are checked before migration can stop its source."""
import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_csi_restore as CSI
from test_addons import unpack


def deployment(image, namespace="kube-system", ready=True):
    return {"metadata": {"namespace": namespace, "generation": 2},
            "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
            "status": {"availableReplicas": int(ready), "observedGeneration": 2}}


class SnapshotSupportTests(unittest.TestCase):
    def setUp(self):
        self.objects = {"/apis/apps/v1/deployments": {"items": []},
                        "/api/v1/nodes": {"items": [{"status": {"nodeInfo": {"kubeletVersion": "v1.33.4+k3s1"}}}]}}
        def read(path):
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            return self.objects[path]
        self.sent = []
        self.read = read
        self.bind_patch = patch.multiple(CSI, kget=read, ksend=lambda *args, **kwargs: self.sent.append(args))
        self.bind_patch.start()
        self.addCleanup(self.bind_patch.stop)
        platform_patch = patch.object(CSI.ADDONS, "platform", return_value={"helm_controller": True})
        self.platform = platform_patch.start()
        self.addCleanup(platform_patch.stop)

    def ready(self):
        self.objects[CSI.API] = {"resources": [{"name": name} for name in CSI.CRDS]}
        self.objects["/apis/apps/v1/deployments"]["items"] = [
            deployment("registry.k8s.io/sig-storage/snapshot-controller:v8.2.0"),
            deployment("registry.k8s.io/sig-storage/csi-snapshotter:v8.2.0", "longhorn-system")]

    def test_existing_healthy_platform_controller_is_used_unchanged(self):
        self.ready()
        original = copy.deepcopy(self.objects)
        with patch.object(CSI.ADDONS, "_post_chart") as install:
            self.assertTrue(CSI.ensure_support()["ready"])
            install.assert_not_called()
        self.assertEqual(original, self.objects)

    def test_controller_starting_or_longhorn_snapshotter_unavailable_is_waited_out(self):
        self.ready()
        for index in (0, 1):
            deployments = self.objects["/apis/apps/v1/deployments"]["items"]
            deployments[index]["status"]["availableReplicas"] = 0
            with patch.object(CSI.ADDONS, "_post_chart") as install:
                self.assertFalse(CSI.ensure_support()["ready"])
                install.assert_not_called()
            deployments[index]["status"]["availableReplicas"] = 1

    def test_mirrored_and_digest_pinned_controllers_are_recognised_without_replacement(self):
        self.ready()
        deployments = self.objects["/apis/apps/v1/deployments"]["items"]
        deployments[0]["spec"]["template"]["spec"]["containers"][0]["image"] = "example.com/mirrored-sig-storage-snapshot-controller@sha256:" + "a" * 64
        deployments[1]["spec"]["template"]["spec"]["containers"][0]["image"] = "example.com/csi-snapshotter@sha256:" + "b" * 64
        with patch.object(CSI.ADDONS, "_post_chart") as install:
            self.assertTrue(CSI.ensure_support()["ready"])
            install.assert_not_called()

    def test_missing_support_is_reported_read_only_and_installed_as_pinned_chart(self):
        self.assertTrue(CSI.support()["can_install"])
        self.assertEqual([], self.sent)
        urls = []
        def fetch(url):
            urls.append(url)
            if "/crd/" in url:
                return "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: example.snapshot.storage.k8s.io\n", url
            if "rbac-" in url:
                return "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: snapshot-controller\n  namespace: kube-system\n", url
            return "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: snapshot-controller\nspec:\n  image: registry.k8s.io/sig-storage/snapshot-controller:v8.2.0\n", url
        with patch.object(CSI.ADDONS, "_helmcharts", return_value={}), \
             patch.object(CSI.ADDONS, "fetch", side_effect=fetch), \
             patch.object(CSI.ADDONS, "_post_chart") as post:
            result = CSI.ensure_support()
        self.assertFalse(result["ready"])
        self.assertEqual(5, len(urls))
        self.assertTrue(all("/v8.6.0/" in url for url in urls))
        name, spec = post.call_args.args
        self.assertEqual(CSI.CONTROLLER, name)
        archive = unpack(spec["chartContent"])
        templates = archive[f"{name}/templates/release.yaml"]
        self.assertIn("name: homestead-snapshot-controller", templates)
        self.assertIn("registry.k8s.io/sig-storage/snapshot-controller:v8.6.0", templates)
        self.assertNotIn("sig-storage/homestead", templates)
        self.assertIn(f"{name}/crds/crds.yaml", archive)

    def test_existing_crds_are_not_fetched_or_claimed_by_the_chart(self):
        for resource in CSI.CRDS:
            self.objects[f"/apis/apiextensions.k8s.io/v1/customresourcedefinitions/{resource}.snapshot.storage.k8s.io"] = {}
        manifest = "kind: Deployment\napiVersion: apps/v1\nmetadata:\n  name: snapshot-controller\n"
        with patch.object(CSI.ADDONS, "_helmcharts", return_value={}), \
             patch.object(CSI.ADDONS, "fetch", return_value=(manifest, "")) as fetch, \
             patch.object(CSI.ADDONS, "_post_chart") as post:
            CSI.ensure_support()
        self.assertEqual(2, fetch.call_count)
        self.assertFalse(any("/crds/" in name for name in unpack(post.call_args.args[1]["chartContent"])))

    def test_pending_install_is_not_dispatched_twice(self):
        with patch.object(CSI.ADDONS, "_helmcharts", return_value={CSI.CONTROLLER: {}}), \
             patch.object(CSI.ADDONS, "_post_chart") as post:
            self.assertFalse(CSI.ensure_support()["ready"])
            post.assert_not_called()

    def test_platform_managed_or_older_cluster_is_not_modified(self):
        self.platform.return_value = {"harvester": True, "helm_controller": True}
        with self.assertRaisesRegex(ValueError, "cluster platform"):
            CSI.ensure_support()
        self.platform.return_value = {"helm_controller": True}
        self.objects["/api/v1/nodes"]["items"][0]["status"]["nodeInfo"]["kubeletVersion"] = "v1.24.9"
        with self.assertRaisesRegex(ValueError, "1.25"):
            CSI.ensure_support()
        self.assertEqual([], self.sent)

    def test_inventory_failure_never_triggers_install(self):
        self.objects["/apis/apps/v1/deployments"]["metadata"] = {"continue": "next-page"}
        with self.assertRaisesRegex(ValueError, "incomplete"):
            CSI.ensure_support()
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
