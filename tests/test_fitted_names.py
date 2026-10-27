"""Names Homestead gives Kubernetes objects from a volume, a node or a path
stay distinct when they are too long to fit: two that start the same never
share one object, so one never reuses, overwrites or deletes the other's."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_names as NAMES
import homestead_nodeshell as SHELL
import homestead_imports as imports
import homestead_container_import as CONTAINER_IMPORT
import homestead_disks as DISKS

LONG_A = "data-postgres-cluster-production-database-instance-0"
LONG_B = "data-postgres-cluster-production-database-instance-1"


class FittedTests(unittest.TestCase):
    def test_a_name_that_fits_is_kept(self):
        self.assertEqual("homestead-chown-media", NAMES.fitted("homestead-chown-media"))

    def test_names_too_long_stay_distinct_and_fit(self):
        a, b = NAMES.fitted("homestead-chown-" + LONG_A), NAMES.fitted("homestead-chown-" + LONG_B)
        self.assertNotEqual(a, b)
        for name in (a, b):
            self.assertLessEqual(len(name), 63)
            self.assertRegex(name, r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
        self.assertLessEqual(len(NAMES.fitted("x" * 80, limit=55)), 55)


class ChownTests(unittest.TestCase):
    def setUp(self):
        self.jobs, self.sent = {}, []

        def get(path):
            name = path.rsplit("/", 1)[-1]
            if "/jobs/" in path and name in self.jobs:
                return self.jobs[name]
            if "/jobs/" in path:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return {"items": []}

        def send(method, path, body=None, **kw):
            self.sent.append((method, path.split("?")[0].rsplit("/", 1)[-1], body))
            if method == "POST":
                self.jobs[body["metadata"]["name"]] = body
            elif method == "DELETE":
                self.jobs.pop(path.split("?")[0].rsplit("/", 1)[-1], None)
            return body or {}
        imports.bind(get, send, lambda *a, **k: {}, lambda cfg: ({"metadata": {"name": "x"}}, None), "lab", {})

    def run_chown(self, pvc):
        with mock.patch.object(imports.time, "sleep"):
            return CONTAINER_IMPORT.chown_claim("lab", pvc, "1000", "")

    def test_two_volumes_that_start_the_same_get_jobs_of_their_own(self):
        self.run_chown(LONG_A)
        self.run_chown(LONG_B)
        created = [body["metadata"]["name"] for m, _, body in self.sent if m == "POST"]
        self.assertEqual(2, len(set(created)))
        self.assertEqual([], [n for m, n, _ in self.sent if m == "DELETE"], "neither chown stopped the other")

    def test_a_job_under_this_name_for_another_volume_is_never_deleted(self):
        name = NAMES.fitted("homestead-chown-" + LONG_B)
        self.jobs[name] = {"metadata": {"name": name, "labels": NAMES.labels("chown", app=LONG_A)}}
        with self.assertRaisesRegex(ValueError, "another volume"):
            self.run_chown(LONG_B)
        self.assertEqual([], [m for m, _, _ in self.sent if m in ("DELETE", "POST")])

    def test_a_finished_chown_of_the_same_volume_is_replaced(self):
        self.run_chown("media")
        self.run_chown("media")
        self.assertEqual(["POST", "DELETE", "POST"], [m for m, _, _ in self.sent])


class ShellTests(unittest.TestCase):
    NODE_A = "worker-pool-production-eu-central-zone-a-instance-0001"
    NODE_B = "worker-pool-production-eu-central-zone-a-instance-0002"

    def setUp(self):
        self.pods, self.sent = {}, []

        def get(path):
            if path.startswith("/api/v1/nodes/"):
                return {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            name = path.rsplit("/", 1)[1]
            if name in self.pods:
                return self.pods[name]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        def send(method, path, body=None, **kw):
            self.sent.append((method, path.rsplit("/", 1)[1]))
            if method == "POST":
                self.pods[body["metadata"]["name"]] = dict(body, status={"phase": "Running"})
            elif method == "DELETE":
                self.pods.pop(path.rsplit("/", 1)[1], None)
        SHELL.bind(get, send, "lab")
        SHELL._sessions.clear()

    def test_two_nodes_that_start_the_same_get_shells_of_their_own(self):
        a = SHELL.open_shell(self.NODE_A, sleep=lambda s: None)
        b = SHELL.open_shell(self.NODE_B, sleep=lambda s: None)
        self.assertNotEqual(a["pod"], b["pod"])
        self.assertEqual(self.NODE_B, self.pods[b["pod"]]["spec"]["nodeName"])

    def test_a_shell_pod_on_another_node_is_never_entered_or_removed(self):
        name = SHELL.pod_name(self.NODE_B)
        self.pods[name] = dict(SHELL.body(self.NODE_A), status={"phase": "Running"})
        self.pods[name]["metadata"] = dict(self.pods[name]["metadata"], name=name)
        with self.assertRaisesRegex(ValueError, "in use for worker"):
            SHELL.open_shell(self.NODE_B, sleep=lambda s: None)
        SHELL.session_started(self.NODE_B)
        SHELL.session_ended(self.NODE_B)
        self.assertIn(name, self.pods, "closing B's session never removes A's shell")


class ShellCloseTests(ShellTests):
    def test_closing_still_finishes_when_the_pod_cannot_be_read(self):
        SHELL.open_shell(self.NODE_A, sleep=lambda s: None)
        SHELL.session_started(self.NODE_A)
        SHELL.bind(lambda path: (_ for _ in ()).throw(urllib.error.HTTPError(path, 500, "busy", {}, None)),
                   lambda *a, **k: None, "lab")
        SHELL.session_ended(self.NODE_A)   # must not raise in the terminal's close path


class DiskIdTests(unittest.TestCase):
    A = "/dev/disk/by-id/nvme-Samsung_SSD_980_PRO_2TB_S6B0NL0W123456A"
    B = "/dev/disk/by-id/nvme-Samsung_SSD_980_PRO_2TB_S6B0NL0W654321B"

    def setUp(self):
        self.disks, self.sent = {}, []
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)

        def get(path):
            return {"spec": {"disks": self.disks}, "status": {"diskStatus": {}}}

        def patch(path, body, what):
            self.sent.append(body)
            for key, value in body["spec"]["disks"].items():
                self.disks[key] = value
        self.patches = [mock.patch.object(DISKS, "kget", get, create=True),
                        mock.patch.object(DISKS, "_patch", patch),
                        mock.patch.object(DISKS, "harvester", lambda: False, create=True),
                        # Adding a disk takes the operations lock in the data
                        # folder: this test's own, never the real /data.
                        mock.patch.object(DISKS.OPS, "DATA_DIR", self.data.name)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_two_drives_of_one_model_get_disks_of_their_own(self):
        DISKS.add({"node": "node1", "path": self.A, "engine": "v2"})
        DISKS.add({"node": "node1", "path": self.B, "engine": "v2"})
        self.assertEqual({self.A, self.B}, {d["path"] for d in self.disks.values()},
                         "the second drive never replaces the first one's entry")


import homestead_lan as LAN


class LanNetworkTests(unittest.TestCase):
    """Each app with a LAN address has its own network definition holding it."""
    APP_A = "home-automation-zigbee-to-mqtt-bridge-for-the-upstairs-floor-a"
    APP_B = "home-automation-zigbee-to-mqtt-bridge-for-the-upstairs-floor-b"
    API = "/apis/k8s.cni.cncf.io/v1/namespaces"
    BASE = API + "/default/network-attachment-definitions/vlan1"
    WANTED = {"network": "default/vlan1", "address": "192.0.2.70", "prefix": 24, "gateway": "192.0.2.1"}

    def setUp(self):
        import json
        self.objects = {self.BASE: {"metadata": {"name": "vlan1"}, "spec": {"config": json.dumps(
            {"cniVersion": "0.3.1", "name": "vlan1", "type": "bridge", "bridge": "br0", "ipam": {}})}}}
        self.sent = []

        def get(path):
            if path in self.objects:
                return self.objects[path]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        def send(method, path, body=None, **kw):
            self.sent.append((method, path))
            if method == "POST":
                self.objects[f"{path}/{body['metadata']['name']}"] = body
            elif method == "PUT":
                self.objects[path] = body
            elif method == "DELETE":
                self.objects.pop(path, None)
            return body
        LAN.bind(get, send)

    def path(self, name):
        return f"{self.API}/lab/network-attachment-definitions/{name}"

    def test_two_apps_that_start_the_same_get_networks_of_their_own(self):
        LAN.ensure_nad("lab", self.APP_A, LAN.clean(self.WANTED))
        LAN.ensure_nad("lab", self.APP_B, LAN.clean(dict(self.WANTED, address="192.0.2.71")))
        self.assertNotEqual(LAN.nad_name(self.APP_A), LAN.nad_name(self.APP_B))
        self.assertEqual(self.APP_A, self.objects[self.path(LAN.nad_name(self.APP_A))]["metadata"]["labels"]["app"])
        self.assertNotIn("PUT", [m for m, _ in self.sent], "neither app's address replaced the other's")

    def test_another_apps_network_is_never_replaced_or_removed(self):
        name = LAN.nad_name(self.APP_B)
        self.objects[self.path(name)] = {"metadata": {"name": name, "labels": {"app": self.APP_A}}}
        with self.assertRaisesRegex(ValueError, "belongs to"):
            LAN.ensure_nad("lab", self.APP_B, LAN.clean(self.WANTED))
        LAN.remove_nad("lab", self.APP_B)
        self.assertIn(self.path(name), self.objects)
        self.assertEqual([], [m for m, _ in self.sent if m in ("PUT", "DELETE")])

    def test_removing_also_clears_this_apps_network_named_the_old_way(self):
        old = f"{self.APP_A[:59].rstrip('-')}-lan"
        self.objects[self.path(old)] = {"metadata": {"name": old, "labels": {"app": self.APP_A}}}
        LAN.remove_nad("lab", self.APP_A)
        self.assertNotIn(self.path(old), self.objects)


class DiskKeyClashTests(DiskIdTests):
    def test_two_folders_whose_keys_clash_are_both_added(self):
        DISKS.add({"node": "node1", "path": "/mnt/disk_2"})
        DISKS.add({"node": "node1", "path": "/mnt/disk-2"})
        self.assertEqual({"/mnt/disk_2", "/mnt/disk-2"}, {d["path"] for d in self.disks.values()})
