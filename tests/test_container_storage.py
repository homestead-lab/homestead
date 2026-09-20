import copy
import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import harvui_lifecycle as lifecycle
import server


FEATURES = [{"id": "igpu", "label": "harvui.io/igpu", "host_path": "/dev/dri",
             "container_path": "/dev/dri", "path_type": "Directory"}]

DEPLOYMENT = {
    "metadata": {"name": "frigate", "annotations": {}},
    "spec": {
        "replicas": 1,
        "template": {
            "metadata": {},
            "spec": {
                "containers": [{
                    "name": "frigate", "image": "frigate:test",
                    "volumeMounts": [
                        {"name": "config", "mountPath": "/config"},
                        {"name": "cfgsrc", "mountPath": "/seed"},
                        {"name": "dri", "mountPath": "/dev/dri"},
                    ],
                }],
                "volumes": [
                    {"name": "config", "persistentVolumeClaim": {"claimName": "frigate-config"}},
                    {"name": "cfgsrc", "configMap": {"name": "frigate-config-map"}},
                    {"name": "dri", "hostPath": {"path": "/dev/dri", "type": "Directory"}},
                ],
            },
        },
    },
}


class WorkloadEditFixture:
    """A Deployment with one claim, one ConfigMap and one device mount."""

    def setUp(self):
        self.deployment = copy.deepcopy(DEPLOYMENT)
        self.sent = []
        self.created = []
        self.existing_pvcs = {"frigate-config", "media"}

        def get(path):
            if path.endswith("/deployments/frigate"):
                return self.deployment
            if "/persistentvolumeclaims/" in path:
                if path.rsplit("/", 1)[-1] in self.existing_pvcs:
                    return {"metadata": {"name": path.rsplit("/", 1)[-1]}}
                raise urllib.error.HTTPError(path, 404, "not found", None, None)
            raise AssertionError(path)

        def send(method, path, body, **kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            return body

        def create_pvc(ns, name, size_gb, storage_class=None, access_mode="ReadWriteOnce"):
            self.created.append((ns, name, size_gb, storage_class, access_mode))
            self.existing_pvcs.add(name)
            return {"metadata": {"name": name}}

        lifecycle.bind(get, send, set(), {}, lambda: FEATURES, create_pvc, "longhorn-r2")

    def saved_spec(self):
        return self.sent[-1][2]["spec"]["template"]["spec"]

    def edit(self, volumes, **extra):
        change = {"original_name": "frigate", "name": "frigate", "image": "frigate:test",
                  "volumes": volumes}
        change.update(extra)
        return lifecycle.edit_workload({"ns": "lab", "name": "frigate", "containers": [change]})


class ContainerStorageTests(WorkloadEditFixture, unittest.TestCase):
    def test_new_claim_is_created_and_mounted(self):
        self.edit([
            {"path": "/config", "kind": "existing", "source": "frigate-config"},
            {"path": "/media", "kind": "new-rwx", "source": "frigate-media", "size_gb": 40,
             "storage_class": "longhorn-r2", "read_only": True},
        ])

        self.assertEqual([("lab", "frigate-media", 40, "longhorn-r2", "ReadWriteMany")], self.created)
        spec = self.saved_spec()
        claims = {volume["name"]: volume["persistentVolumeClaim"]["claimName"]
                  for volume in spec["volumes"] if volume.get("persistentVolumeClaim")}
        self.assertEqual("frigate-media", claims["hs-frigate-2"])
        mounts = {mount["mountPath"]: mount for mount in spec["containers"][0]["volumeMounts"]}
        self.assertEqual("hs-frigate-2", mounts["/media"]["name"])
        self.assertTrue(mounts["/media"]["readOnly"])
        # The claim already backing /config keeps its existing pod volume entry.
        self.assertEqual("config", mounts["/config"]["name"])

    def test_configmap_and_device_mounts_are_left_alone(self):
        self.edit([{"path": "/config", "kind": "existing", "source": "frigate-config"}],
                  hardware=["igpu"])

        spec = self.saved_spec()
        names = {volume["name"] for volume in spec["volumes"]}
        self.assertIn("cfgsrc", names)
        paths = {mount["mountPath"] for mount in spec["containers"][0]["volumeMounts"]}
        self.assertEqual({"/config", "/seed", "/dev/dri"}, paths)

    def test_removed_mapping_prunes_its_pod_volume(self):
        self.edit([{"path": "/data", "kind": "ephemeral", "source": ""}])

        spec = self.saved_spec()
        names = {volume["name"] for volume in spec["volumes"]}
        self.assertNotIn("config", names)
        self.assertIn("cfgsrc", names)
        self.assertEqual([{}], [volume["emptyDir"] for volume in spec["volumes"]
                                if "emptyDir" in volume])
        self.assertEqual([], self.created)

    def test_second_container_can_share_a_volume_already_in_the_pod(self):
        self.deployment["spec"]["template"]["spec"]["containers"].append(
            {"name": "sync", "image": "rclone:1"})
        lifecycle.edit_workload({"ns": "lab", "name": "frigate", "containers": [
            {"original_name": "frigate", "name": "frigate", "volumes": [
                {"path": "/config", "kind": "existing", "source": "frigate-config"}]},
            {"original_name": "sync", "name": "sync", "volumes": [
                {"path": "/backup", "kind": "pod", "source": "config", "read_only": True}]},
        ]})

        spec = self.saved_spec()
        self.assertEqual(1, len([volume for volume in spec["volumes"]
                                 if volume.get("persistentVolumeClaim")]))
        self.assertEqual([{"name": "config", "mountPath": "/backup", "readOnly": True}],
                         spec["containers"][1]["volumeMounts"])

    def test_invalid_storage_requests_are_rejected_before_any_claim_is_created(self):
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            self.edit([{"path": "config", "kind": "new-rwo", "source": "frigate-extra"}])
        with self.assertRaisesRegex(ValueError, "mounted twice"):
            self.edit([{"path": "/config", "kind": "existing", "source": "frigate-config"},
                       {"path": "/config", "kind": "ephemeral", "source": ""}])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.edit([{"path": "/shared", "kind": "pod", "source": "nope"}])
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.edit([{"path": "/media", "kind": "new-rwo", "source": "media"}])
        self.assertEqual([], self.created)
        self.assertEqual([], self.sent)

    def test_untouched_containers_keep_their_storage(self):
        lifecycle.edit_workload({"ns": "lab", "name": "frigate", "containers": [
            {"original_name": "frigate", "name": "detector", "image": "frigate:2"}]})

        spec = self.saved_spec()
        self.assertEqual(3, len(spec["volumes"]))
        self.assertEqual(3, len(spec["containers"][0]["volumeMounts"]))


class AutostartTests(WorkloadEditFixture, unittest.TestCase):
    """Autostart is the replica count expressed the way people think about it."""

    def test_switching_autostart_off_parks_the_replica_count(self):
        lifecycle.edit_workload({"ns": "lab", "name": "frigate", "replicas": 3,
                                 "autostart": False})

        saved = self.sent[-1][2]
        self.assertEqual(0, saved["spec"]["replicas"])
        self.assertEqual("3", saved["metadata"]["annotations"][lifecycle.AUTOSTART_REPLICAS])

        payload = server.workload_edit_payload("lab", "frigate", saved, FEATURES)
        self.assertFalse(payload["autostart"])
        self.assertEqual(3, payload["start_replicas"])

    def test_switching_autostart_on_restores_at_least_one_replica(self):
        lifecycle.edit_workload({"ns": "lab", "name": "frigate", "replicas": 0,
                                 "autostart": True})

        saved = self.sent[-1][2]
        self.assertEqual(1, saved["spec"]["replicas"])
        self.assertNotIn(lifecycle.AUTOSTART_REPLICAS, saved["metadata"]["annotations"])
        self.assertTrue(server.workload_edit_payload("lab", "frigate", saved, FEATURES)["autostart"])

    def test_a_replica_edit_without_autostart_keeps_working(self):
        lifecycle.edit_workload({"ns": "lab", "name": "frigate", "replicas": 2})

        self.assertEqual(2, self.sent[-1][2]["spec"]["replicas"])


class EditPayloadStorageTests(unittest.TestCase):
    def test_payload_separates_editable_storage_from_kubernetes_wiring(self):
        payload = server.workload_edit_payload("lab", "frigate", copy.deepcopy(DEPLOYMENT), FEATURES)

        mounts = {mount["path"]: mount for mount in payload["containers"][0]["volumes"]}
        self.assertEqual(("existing", "frigate-config", False),
                         (mounts["/config"]["kind"], mounts["/config"]["value"],
                          mounts["/config"]["managed"]))
        self.assertTrue(mounts["/seed"]["managed"])
        self.assertEqual("configMap", mounts["/seed"]["kind"])
        self.assertTrue(mounts["/dev/dri"]["managed"])

    def test_payload_lists_pod_volumes_without_devices_or_wiring(self):
        payload = server.workload_edit_payload("lab", "frigate", copy.deepcopy(DEPLOYMENT), FEATURES)

        self.assertEqual([{"name": "config", "kind": "pvc", "source": "frigate-config"}],
                         payload["pod_volumes"])


if __name__ == "__main__":
    unittest.main()
