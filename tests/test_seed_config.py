import copy
import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import homestead_lifecycle as lifecycle


DEPLOYMENT = {
    "metadata": {"name": "frigate", "annotations": {}},
    "spec": {
        "replicas": 1,
        "template": {
            "metadata": {},
            "spec": {
                "containers": [{"name": "frigate", "image": "frigate:test"}],
                "initContainers": [{
                    "name": "seed-config",
                    "command": ["sh", "-c", "cp /src/config.yml /config/config.yml"],
                    "volumeMounts": [{"name": "cfgsrc", "mountPath": "/src"}],
                }],
                "volumes": [{"name": "cfgsrc", "configMap": {"name": "frigate-config"}}],
            },
        },
    },
}


class SeedConfigTests(unittest.TestCase):
    def setUp(self):
        self.deployment = copy.deepcopy(DEPLOYMENT)
        self.configmap = {
            "metadata": {"name": "frigate-config", "resourceVersion": "1"},
            "data": {"config.yml": "detectors:\n  ov:\n    type: openvino\n"},
        }
        self.sent = []

        def get(path):
            if path.endswith("/deployments/frigate"):
                return self.deployment
            if path.endswith("/configmaps/frigate-config"):
                return self.configmap
            raise AssertionError(path)

        def send(method, path, body, **kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            return body

        lifecycle.bind(get, send, set(), {}, lambda: [])

    def test_discovers_configmap_mounted_by_init_container(self):
        found = lifecycle.seed_configs("lab", self.deployment)
        self.assertEqual(1, len(found))
        self.assertEqual("frigate-config", found[0]["config_map"])
        self.assertEqual("config.yml", found[0]["key"])
        self.assertIn("openvino", found[0]["value"])

    def test_edit_updates_authoritative_configmap_and_rolls_workload(self):
        lifecycle.edit_workload({
            "ns": "lab",
            "name": "frigate",
            "seed_configs": [{
                "init_container": "seed-config",
                "config_map": "frigate-config",
                "key": "config.yml",
                "value": "detectors:\n  coral:\n    type: edgetpu\n",
            }],
        })
        self.assertEqual("detectors:\n  coral:\n    type: edgetpu\n", self.configmap["data"]["config.yml"])
        self.assertEqual(["PUT", "PUT"], [x[0] for x in self.sent])
        self.assertTrue(self.sent[0][1].endswith("/configmaps/frigate-config"))
        self.assertTrue(self.sent[1][1].endswith("/deployments/frigate"))

    def test_rejects_unattached_configmap(self):
        with self.assertRaisesRegex(ValueError, "not attached"):
            lifecycle.edit_workload({
                "ns": "lab",
                "name": "frigate",
                "seed_configs": [{
                    "init_container": "seed-config",
                    "config_map": "another-config",
                    "key": "config.yml",
                    "value": "unsafe",
                }],
            })

    def test_edit_updates_container_name_and_pod_hostname(self):
        lifecycle.edit_workload({
            "ns": "lab", "name": "frigate",
            "container_name": "camera-detector", "pod_hostname": "frigate-core",
        })
        saved = self.sent[-1][2]
        pod_spec = saved["spec"]["template"]["spec"]
        self.assertEqual("camera-detector", pod_spec["containers"][0]["name"])
        self.assertEqual("frigate-core", pod_spec["hostname"])

    def test_edit_rejects_invalid_or_duplicate_container_names(self):
        self.deployment["spec"]["template"]["spec"]["containers"].append(
            {"name": "sidecar", "image": "example/sidecar"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            lifecycle.edit_workload({"ns": "lab", "name": "frigate", "container_name": "sidecar"})
        with self.assertRaisesRegex(ValueError, "lowercase letters"):
            lifecycle.edit_workload({"ns": "lab", "name": "frigate", "container_name": "Bad Name"})

    def test_edit_updates_multiple_containers_and_preserves_managed_env(self):
        containers = self.deployment["spec"]["template"]["spec"]["containers"]
        containers[0]["env"] = [
            {"name": "PLAIN", "value": "old"},
            {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "camera", "key": "token"}}},
        ]
        containers.append({"name": "mqtt", "image": "mosquitto:1", "ports": [{"name": "mqtt", "containerPort": 1883}]})
        lifecycle.edit_workload({
            "ns": "lab", "name": "frigate", "containers": [
                {"original_name": "frigate", "name": "detector", "image": "frigate:2", "cpu": "250m",
                 "memory": "512Mi", "env": {"PLAIN": "new", "TOKEN": "must-not-replace"},
                 "ports": [{"name": "web", "container": 5000, "protocol": "TCP"}]},
                {"original_name": "mqtt", "name": "broker", "image": "mosquitto:2", "cpu": "20m",
                 "memory": "64Mi", "env": {"LOG_LEVEL": "info"},
                 "ports": [{"name": "mqtt", "container": 1883, "protocol": "TCP"}]},
            ],
        })
        saved = self.sent[-1][2]["spec"]["template"]["spec"]["containers"]
        self.assertEqual(["detector", "broker"], [item["name"] for item in saved])
        self.assertEqual(["frigate:2", "mosquitto:2"], [item["image"] for item in saved])
        self.assertEqual("250m", saved[0]["resources"]["requests"]["cpu"])
        self.assertEqual("512Mi", saved[0]["resources"]["requests"]["memory"])
        self.assertEqual("new", next(item["value"] for item in saved[0]["env"] if item["name"] == "PLAIN"))
        token = next(item for item in saved[0]["env"] if item["name"] == "TOKEN")
        self.assertEqual("camera", token["valueFrom"]["secretKeyRef"]["name"])
        self.assertEqual(1883, saved[1]["ports"][0]["containerPort"])

    def test_multi_container_edit_rejects_duplicate_final_names_before_save(self):
        self.deployment["spec"]["template"]["spec"]["containers"].append(
            {"name": "sidecar", "image": "example/sidecar"})
        with self.assertRaisesRegex(ValueError, "must be unique"):
            lifecycle.edit_workload({"ns": "lab", "name": "frigate", "containers": [
                {"original_name": "frigate", "name": "shared"},
                {"original_name": "sidecar", "name": "shared"},
            ]})
        self.assertEqual([], self.sent)

    def test_workload_rename_recreates_waits_retargets_hpa_and_deletes_old(self):
        old = copy.deepcopy(self.deployment)
        old["metadata"].update({"namespace": "lab", "uid": "old-uid", "resourceVersion": "8"})
        old["spec"]["selector"] = {"matchLabels": {"app": "frigate"}}
        old["spec"]["template"].setdefault("metadata", {})["labels"] = {"app": "frigate"}
        old["status"] = {"replicas": 1, "readyReplicas": 1, "availableReplicas": 1,
                         "updatedReplicas": 1, "observedGeneration": 1}
        objects = {"frigate": old}
        hpa = {"metadata": {"name": "frigate-auto", "namespace": "lab", "resourceVersion": "3"},
               "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "frigate"}}}
        sent = []

        def get(path):
            if path.endswith("/horizontalpodautoscalers"):
                return {"items": [hpa]}
            if "/deployments/" in path:
                target = path.rsplit("/", 1)[-1]
                if target not in objects:
                    raise urllib.error.HTTPError(path, 404, "not found", None, None)
                return objects[target]
            raise AssertionError(path)

        def send(method, path, body, **kwargs):
            sent.append((method, path, copy.deepcopy(body)))
            if method == "POST" and path.endswith("/deployments"):
                created = copy.deepcopy(body)
                created["metadata"]["generation"] = 1
                created["status"] = {}
                objects[created["metadata"]["name"]] = created
            elif method == "PATCH" and path.endswith("/scale"):
                target = path.split("/")[-2]
                replicas = body["spec"]["replicas"]
                objects[target]["spec"]["replicas"] = replicas
                objects[target]["status"] = {"replicas": replicas, "readyReplicas": replicas,
                                               "availableReplicas": replicas, "updatedReplicas": replicas,
                                               "observedGeneration": 1}
            elif method == "DELETE" and "/deployments/" in path:
                objects.pop(path.rsplit("/", 1)[-1], None)
            return body

        lifecycle.bind(get, send, set(), {}, lambda: [])
        result = lifecycle.edit_workload({
            "ns": "lab", "name": "frigate", "workload_name": "camera-stack",
            "container_name": "camera-detector", "pod_hostname": "camera-core",
        })

        self.assertTrue(result["renamed"])
        self.assertEqual("camera-stack", result["name"])
        self.assertNotIn("frigate", objects)
        created = objects["camera-stack"]
        self.assertNotIn("uid", created["metadata"])
        self.assertEqual("camera-stack", created["spec"]["selector"]["matchLabels"]["homestead.io/workload"])
        self.assertEqual("camera-stack", created["spec"]["template"]["metadata"]["labels"]["homestead.io/workload"])
        self.assertEqual("camera-detector", created["spec"]["template"]["spec"]["containers"][0]["name"])
        self.assertEqual("camera-core", created["spec"]["template"]["spec"]["hostname"])
        self.assertEqual("camera-stack", hpa["spec"]["scaleTargetRef"]["name"])
        self.assertEqual(["POST", "PUT", "PATCH", "PATCH", "DELETE"], [row[0] for row in sent])

    def test_failed_workload_rename_removes_replacement_and_restores_scale(self):
        old = copy.deepcopy(self.deployment)
        old["metadata"].update({"namespace": "lab", "resourceVersion": "8"})
        old["spec"]["selector"] = {"matchLabels": {"app": "frigate"}}
        old["spec"]["template"].setdefault("metadata", {})["labels"] = {"app": "frigate"}
        objects = {"frigate": old}
        sent = []

        def get(path):
            if path.endswith("/horizontalpodautoscalers"):
                return {"items": []}
            if "/deployments/" in path:
                target = path.rsplit("/", 1)[-1]
                if target not in objects:
                    raise urllib.error.HTTPError(path, 404, "not found", None, None)
                return objects[target]
            raise AssertionError(path)

        def send(method, path, body, **kwargs):
            sent.append((method, path, copy.deepcopy(body)))
            if method == "POST":
                objects[body["metadata"]["name"]] = copy.deepcopy(body)
            elif method == "PATCH" and path.endswith("/scale"):
                objects[path.split("/")[-2]]["spec"]["replicas"] = body["spec"]["replicas"]
            elif method == "DELETE":
                objects.pop(path.rsplit("/", 1)[-1], None)
            return body

        original_wait = lifecycle._wait_for_replicas
        lifecycle.bind(get, send, set(), {}, lambda: [])
        lifecycle._wait_for_replicas = lambda ns, target, desired, timeout=120: (
            (_ for _ in ()).throw(TimeoutError("replacement never became ready"))
            if target == "camera-stack" else objects[target])
        try:
            with self.assertRaisesRegex(RuntimeError, "frigate was restored"):
                lifecycle.edit_workload({
                    "ns": "lab", "name": "frigate", "workload_name": "camera-stack",
                })
        finally:
            lifecycle._wait_for_replicas = original_wait

        self.assertIn("frigate", objects)
        self.assertNotIn("camera-stack", objects)
        self.assertEqual(1, objects["frigate"]["spec"]["replicas"])
        self.assertEqual("DELETE", sent[-2][0])
        self.assertEqual(1, sent[-1][2]["spec"]["replicas"])


if __name__ == "__main__":
    unittest.main()
