import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import harvui_updates as updates


DEPLOYMENT = {
    "metadata": {"name": "demo", "namespace": "lab", "generation": 2, "annotations": {}},
    "spec": {
        "replicas": 1,
        "selector": {"matchLabels": {"app": "demo"}},
        "template": {
            "metadata": {"labels": {"app": "demo"}},
            "spec": {"containers": [{"name": "demo", "image": "nginx:1.27.0"}]},
        },
    },
    "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1,
               "readyReplicas": 1, "availableReplicas": 1, "unavailableReplicas": 0},
}


class ImageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.dep = copy.deepcopy(DEPLOYMENT)
        self.sent = []
        self.tmp = tempfile.TemporaryDirectory()

        def get(path):
            if path.endswith("/deployments/demo"):
                return self.dep
            if path == "/api/v1/pods":
                return {"items": []}
            if "/serviceaccounts/" in path:
                return {"imagePullSecrets": []}
            raise AssertionError(path)

        def send(method, path, body, **kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            self.dep = copy.deepcopy(body)
            return self.dep

        updates.bind(get, send, "lab", self.tmp.name)
        updates._CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parses_docker_and_private_registry_references(self):
        docker = updates.parse_image("nginx:1.27")
        self.assertEqual("docker.io/library/nginx", docker["base"])
        self.assertEqual("registry-1.docker.io", docker["endpoint"])
        private = updates.parse_image("ghcr.io/example/app:v2.1.0")
        self.assertEqual("ghcr.io", private["registry"])
        self.assertEqual("example/app", private["repo"])

    def test_semver_stays_in_current_major(self):
        tags = ["1.9.1", "1.10.0", "2.0.0", "edge", "1.10.0-rc1"]
        self.assertEqual("1.10.0", updates.newer_semver("1.9.0", tags))
        self.assertIsNone(updates.newer_semver("latest", tags))

    def test_multiarch_child_digest_is_not_a_false_update(self):
        child = "sha256:" + "a" * 64
        pod = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
               "status": {"containerStatuses": [{"name": "demo", "imageID": "repo@" + child}]}}
        original_tags, original_manifest = updates.registry_tags, updates.manifest_info
        original_creds = updates._secret_credentials
        try:
            updates.registry_tags = lambda *args, **kwargs: ["1.27.0"]
            updates.manifest_info = lambda *args, **kwargs: {
                "digest": "sha256:" + "b" * 64, "children": [child]}
            updates._secret_credentials = lambda *args: {}
            result = updates._check_deployment(self.dep, [pod])
            self.assertFalse(result["available"])
        finally:
            updates.registry_tags, updates.manifest_info = original_tags, original_manifest
            updates._secret_credentials = original_creds

    def test_stopped_workload_is_not_reported_as_a_failed_registry_check(self):
        """An import lands scaled to zero, so there is no digest to compare."""
        original_tags, original_manifest = updates.registry_tags, updates.manifest_info
        original_creds = updates._secret_credentials
        try:
            updates.registry_tags = lambda *args, **kwargs: ["1.27.0"]
            updates.manifest_info = lambda *args, **kwargs: {
                "digest": "sha256:" + "b" * 64, "children": []}
            updates._secret_credentials = lambda *args: {}

            stopped = copy.deepcopy(self.dep)
            stopped["spec"]["replicas"] = 0
            self.assertEqual("", updates._check_deployment(stopped, [])["images"][0]["error"])

            running = {"metadata": {"namespace": "lab", "labels": {"app": "demo"}},
                       "status": {"containerStatuses": [{"name": "demo", "imageID": ""}]}}
            item = updates._check_deployment(self.dep, [running])["images"][0]
            self.assertEqual("running image digest is not available yet", item["error"])
        finally:
            updates.registry_tags, updates.manifest_info = original_tags, original_manifest
            updates._secret_credentials = original_creds

    def test_scan_uses_shared_system_namespace_filter(self):
        user = copy.deepcopy(self.dep)
        system = copy.deepcopy(self.dep)
        system["metadata"].update({"namespace": "cattle-fleet-local-system", "name": "fleet"})

        def get(path):
            if path == "/apis/apps/v1/deployments":
                return {"items": [system, user]}
            if path == "/api/v1/pods":
                return {"items": []}
            raise AssertionError(path)

        original = updates._check_deployment
        try:
            updates.bind(get, lambda *args, **kwargs: None, "lab", self.tmp.name,
                         {"cattle-fleet-local-system"})
            updates._check_deployment = lambda dep, pods, force=False: {
                "ns": dep["metadata"]["namespace"], "name": dep["metadata"]["name"],
                "images": [], "available": False, "can_rollback": False,
                "last_action": "",
            }
            report = updates.scan()
        finally:
            updates._check_deployment = original

        self.assertEqual([("lab", "demo")],
                         [(x["ns"], x["name"]) for x in report["workloads"]])

    def test_apply_pins_digest_and_records_exact_rollback(self):
        digest = "sha256:" + "c" * 64
        original = updates._check_deployment
        try:
            updates._check_deployment = lambda *args, **kwargs: {
                "images": [{"container": "demo", "available": True,
                            "source": "docker.io/library/nginx:1.27.0",
                            "current_digest": "sha256:" + "d" * 64,
                            "candidate": "docker.io/library/nginx:1.28.0",
                            "remote_digest": digest}]}
            result = updates.apply_update("lab", "demo")
        finally:
            updates._check_deployment = original
        image = self.dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual("docker.io/library/nginx@" + digest, image)
        previous = json.loads(self.dep["metadata"]["annotations"][updates.PREVIOUS])
        self.assertEqual("docker.io/library/nginx@sha256:" + "d" * 64,
                         previous["images"]["demo"])
        self.assertEqual("ready", result["phase"])

        updates.rollback("lab", "demo")
        restored = self.dep["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual("docker.io/library/nginx@sha256:" + "d" * 64, restored)

    def test_an_init_container_on_the_same_image_moves_with_it(self):
        """Homestead's own data-permissions init container is built from the
        Homestead image: leaving it behind pins it to the release the workload
        was installed with, however many times the app is updated."""
        spec = self.dep["spec"]["template"]["spec"]
        spec["containers"] = [{"name": "homestead",
                               "image": "ghcr.io/wjcloudy/homestead:2.8.2",
                               "env": [{"name": "PORT", "value": "8080"},
                                       {"name": "HOMESTEAD_VERSION", "value": "2.8.2"}]}]
        spec["initContainers"] = [{"name": "data-permissions",
                                   "image": "ghcr.io/wjcloudy/homestead:2.8.2"},
                                  {"name": "wait", "image": "busybox:1.36"}]
        new_digest = "sha256:" + "e" * 64
        old_digest = "sha256:" + "f" * 64
        original = updates._check_deployment
        try:
            updates._check_deployment = lambda *args, **kwargs: {
                "images": [{"container": "homestead", "available": True,
                            "source": "ghcr.io/wjcloudy/homestead:2.8.2",
                            "current_digest": old_digest,
                            "candidate": "ghcr.io/wjcloudy/homestead:2.8.37",
                            "remote_digest": new_digest}]}
            updates.apply_update("lab", "demo")
        finally:
            updates._check_deployment = original

        spec = self.dep["spec"]["template"]["spec"]
        wanted = "ghcr.io/wjcloudy/homestead@" + new_digest
        self.assertEqual(wanted, spec["containers"][0]["image"])
        self.assertEqual(wanted, spec["initContainers"][0]["image"],
                         "the init container is the same release as the app")
        self.assertEqual("busybox:1.36", spec["initContainers"][1]["image"],
                         "an unrelated init container is left alone")
        self.assertEqual([{"name": "PORT", "value": "8080"}], spec["containers"][0]["env"],
                         "a pinned version env var would now be a lie")

        updates.rollback("lab", "demo")
        spec = self.dep["spec"]["template"]["spec"]
        was = "ghcr.io/wjcloudy/homestead@" + old_digest
        self.assertEqual(was, spec["containers"][0]["image"])
        self.assertEqual("ghcr.io/wjcloudy/homestead:2.8.2", spec["initContainers"][0]["image"],
                         "rollback takes the init container back with it")
        self.assertEqual("busybox:1.36", spec["initContainers"][1]["image"])

    def test_progress_waits_for_surge_replacement_to_be_ready(self):
        self.dep["status"].update({"replicas": 2, "updatedReplicas": 1,
                                   "readyReplicas": 1, "availableReplicas": 1,
                                   "unavailableReplicas": 1})
        result = updates.progress("lab", "demo", self.dep)
        self.assertEqual("progressing", result["phase"])

        self.dep["status"].update({"replicas": 1, "updatedReplicas": 1,
                                   "readyReplicas": 1, "availableReplicas": 1,
                                   "unavailableReplicas": 0})
        result = updates.progress("lab", "demo", self.dep)
        self.assertEqual("ready", result["phase"])


if __name__ == "__main__":
    unittest.main()
