import copy
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import harvui_lifecycle as lifecycle


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


if __name__ == "__main__":
    unittest.main()
