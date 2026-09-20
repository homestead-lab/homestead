import copy
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import server


class AppStoreTemplateTests(unittest.TestCase):
    def test_template_preserves_ports_environment_storage_and_devices(self):
        app = {
            "name": "Demo App", "repo": "example/demo:latest", "icon": "https://example.com/icon.png",
            "config": [
                {"@attributes": {"Name": "Web", "Target": "8080", "Type": "Port",
                                  "Mode": "udp", "Required": "true"}, "value": "18080"},
                {"@attributes": {"Name": "Timezone", "Target": "TZ", "Type": "Variable",
                                  "Default": "UTC", "Description": "Local timezone"}, "value": ""},
                {"@attributes": {"Name": "App data", "Target": "/config", "Type": "Path",
                                  "Mode": "rw", "Default": "/mnt/user/appdata/demo",
                                  "Required": "true"}, "value": "/mnt/user/appdata/demo"},
                {"@attributes": {"Name": "USB", "Target": "/dev/bus/usb", "Type": "Device"},
                 "value": "/dev/bus/usb"},
            ],
        }
        cfg = server.template_to_cfg(app)
        self.assertEqual([{"container": 8080, "host": 18080, "expose": True, "name": "p8080-udp",
                           "protocol": "UDP", "label": "Web", "description": "", "required": True}],
                         cfg["ports"])
        self.assertEqual({"TZ": "UTC"}, cfg["env"])
        self.assertEqual("demo-app-data", cfg["volumes"][0]["source"])
        self.assertTrue(cfg["volumes"][0]["create"])
        self.assertEqual("/mnt/user/appdata/demo", cfg["volumes"][0]["template_source"])
        self.assertEqual("/dev/bus/usb", cfg["template_devices"][0]["host_path"])

    def test_system_localtime_is_imported_as_read_only_host_path(self):
        cfg = server.template_to_cfg({"name": "Demo", "repo": "demo", "config": [{
            "@attributes": {"Target": "/etc/localtime", "Type": "Path", "Mode": "ro"},
            "value": "/etc/localtime",
        }]})
        self.assertEqual("host", cfg["volumes"][0]["type"])
        self.assertFalse(cfg["volumes"][0]["create"])
        self.assertTrue(cfg["volumes"][0]["read_only"])


class SidecarDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.features = mock.patch.object(server.HW, "features", return_value=[])
        self.features.start()
        self.addCleanup(self.features.stop)
        self.current = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "media", "namespace": "lab", "resourceVersion": "7"},
            "spec": {
                "selector": {"matchLabels": {"app": "media"}},
                "template": {"metadata": {"labels": {"app": "media"}}, "spec": {
                    "containers": [{"name": "main", "image": "example/main"}],
                    "volumes": [{"name": "shared", "persistentVolumeClaim": {"claimName": "media-data"}}],
                }},
            },
        }

    def test_sidecar_reuses_pod_volume_and_routes_service_to_owner(self):
        cfg = {
            "name": "helper", "image": "example/helper", "namespace": "lab",
            "target_workload": "media", "target_mode": "existing", "network_mode": "internal",
            "ports": [{"container": 9000, "host": 19000, "protocol": "TCP", "expose": True}],
            "volumes": [{"path": "/data", "source": "shared", "type": "pod", "read_only": True}],
            "env": {"MODE": "sidecar"}, "hardware": [],
        }
        updated, service = server.build_sidecar_deployment(cfg, copy.deepcopy(self.current))
        self.assertEqual("7", updated["metadata"]["resourceVersion"])
        helper = updated["spec"]["template"]["spec"]["containers"][1]
        self.assertEqual("helper", helper["name"])
        self.assertEqual([{"name": "shared", "mountPath": "/data", "readOnly": True}], helper["volumeMounts"])
        self.assertEqual({"app": "media"}, service["spec"]["selector"])
        self.assertEqual(19000, service["spec"]["ports"][0]["port"])

    def test_sidecar_new_claim_gets_a_collision_safe_pod_volume(self):
        cfg = {
            "name": "helper", "image": "example/helper", "target_workload": "media",
            "volumes": [{"path": "/cache", "source": "helper-cache", "type": "pvc"}],
            "ports": [], "hardware": [],
        }
        updated, _ = server.build_sidecar_deployment(cfg, copy.deepcopy(self.current))
        volumes = updated["spec"]["template"]["spec"]["volumes"]
        self.assertEqual("helper-cache", volumes[-1]["persistentVolumeClaim"]["claimName"])
        self.assertEqual(volumes[-1]["name"], updated["spec"]["template"]["spec"]["containers"][1]["volumeMounts"][0]["name"])

    def test_duplicate_container_and_missing_pod_volume_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            server.build_sidecar_deployment({"name": "main", "image": "x", "target_workload": "media"},
                                               copy.deepcopy(self.current))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            server.build_sidecar_deployment({"name": "helper", "image": "x", "target_workload": "media",
                                               "volumes": [{"path": "/x", "source": "missing", "type": "pod"}]},
                                               copy.deepcopy(self.current))

    def test_host_network_cannot_be_forced_on_shared_pod(self):
        with self.assertRaisesRegex(ValueError, "host networking"):
            server.build_sidecar_deployment({"name": "helper", "image": "x", "target_workload": "media",
                                               "network_mode": "host"}, copy.deepcopy(self.current))


class DeployOptionsTests(unittest.TestCase):
    def test_options_describe_claim_access_and_existing_pod_volumes(self):
        def get(path):
            if path.endswith("/deployments"):
                return {"items": [self._deployment()]}
            if path.endswith("/persistentvolumeclaims"):
                return {"items": [{"metadata": {"name": "shared"}, "spec": {
                    "accessModes": ["ReadWriteMany"], "storageClassName": "longhorn-r2",
                    "resources": {"requests": {"storage": "5Gi"}}},
                    "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}}}]}
            if path.endswith("/storageclasses"):
                return {"items": [{"metadata": {"name": "longhorn-r2"}}]}
            raise AssertionError(path)

        with mock.patch.object(server, "kget", side_effect=get):
            options = server.deploy_options("lab")
        self.assertEqual(["ReadWriteMany"], options["pvcs"][0]["access_modes"])
        self.assertEqual("pvc", options["deployments"][0]["volumes"][0]["kind"])

    def _deployment(self):
        return {"metadata": {"name": "media"}, "spec": {"template": {"spec": {
            "containers": [{"name": "main"}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "shared"}}],
        }}}}


if __name__ == "__main__":
    unittest.main()
