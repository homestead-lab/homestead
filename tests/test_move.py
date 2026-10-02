"""What one cluster offers another, and what of it can actually be moved."""
import json
import sys
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move as move


def deployment(name, volumes=(), mounts=(), replicas=1, ready=1, image="app:1"):
    return {
        "metadata": {"name": name, "namespace": "lab", "annotations": {}},
        "spec": {"replicas": replicas, "template": {"spec": {
            "containers": [{"name": name, "image": image,
                            "volumeMounts": list(mounts),
                            "ports": [{"containerPort": 8080, "protocol": "TCP"}]}],
            "volumes": list(volumes)}}},
        "status": {"readyReplicas": ready},
    }


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.deployments = []
        self.claims = {}
        move.bind(self._get, lambda *a, **k: {}, "lab")

    def _get(self, path):
        if path == "/apis/apps/v1/deployments":
            return {"items": self.deployments}
        if "/persistentvolumeclaims/" in path:
            name = path.rsplit("/", 1)[-1]
            if name not in self.claims:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return self.claims[name]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def _claim(self, name, size="20Gi", storage_class="longhorn-r2"):
        self.claims[name] = {"metadata": {"name": name},
                             "spec": {"resources": {"requests": {"storage": size}},
                                      "storageClassName": storage_class,
                                      "accessModes": ["ReadWriteOnce"]}}

    def test_a_workload_on_a_longhorn_claim_can_be_moved(self):
        self._claim("frigate-config")
        self.deployments = [deployment(
            "frigate",
            volumes=[{"name": "cfg", "persistentVolumeClaim": {"claimName": "frigate-config"}}],
            mounts=[{"name": "cfg", "mountPath": "/config"}])]

        report = move.inventory()

        row = report["workloads"][0]
        self.assertTrue(row["movable"])
        self.assertEqual([], row["blockers"])
        self.assertEqual(1, report["movable"])
        self.assertEqual({"claim": "frigate-config", "path": "/config", "size_gb": 20,
                          "storage_class": "longhorn-r2"},
                         {k: row["volumes"][0][k] for k in
                          ("claim", "path", "size_gb", "storage_class")})

    def test_a_subpath_travels_with_the_mount_it_belongs_to(self):
        """Imports put several folders in one claim; the far side must know."""
        self._claim("frigate-appdata")
        self.deployments = [deployment(
            "frigate",
            volumes=[{"name": "d", "persistentVolumeClaim": {"claimName": "frigate-appdata"}}],
            mounts=[{"name": "d", "mountPath": "/config", "subPath": "config"}])]

        self.assertEqual("config", move.inventory()["workloads"][0]["volumes"][0]["sub_path"])

    def test_a_ram_disk_is_not_something_to_carry(self):
        """It is rebuilt empty on the far side, which is the point of it."""
        self.deployments = [deployment(
            "frigate",
            volumes=[{"name": "shm", "emptyDir": {"medium": "Memory"}}],
            mounts=[{"name": "shm", "mountPath": "/dev/shm"}])]

        row = move.inventory()["workloads"][0]

        self.assertEqual([], row["volumes"])
        self.assertTrue(row["movable"], "nothing to carry is not a reason to refuse")

    def test_a_mount_homestead_did_not_create_blocks_the_move(self):
        """Moving half a workload silently is worse than not moving it."""
        self.deployments = [deployment(
            "app",
            volumes=[{"name": "conf", "configMap": {"name": "hand-written"}}],
            mounts=[{"name": "conf", "mountPath": "/etc/app"}])]

        row = move.inventory()["workloads"][0]

        self.assertFalse(row["movable"])
        self.assertIn("/etc/app", row["blockers"][0])
        self.assertIn("configMap", row["blockers"][0])

    def test_a_host_path_blocks_it_too(self):
        self.deployments = [deployment(
            "app", volumes=[{"name": "h", "hostPath": {"path": "/mnt/thing"}}],
            mounts=[{"name": "h", "mountPath": "/data"}])]

        self.assertFalse(move.inventory()["workloads"][0]["movable"])

    def test_a_passed_through_device_travels_and_asks_for_the_same_hardware(self):
        self.deployments = [deployment(
            "birdnet", volumes=[{"name": "hw-igpu", "hostPath": {"path": "/dev/dri"}}],
            mounts=[{"name": "hw-igpu", "mountPath": "/dev/dri"}])]

        row = move.inventory()["workloads"][0]
        self.assertTrue(row["movable"])
        self.assertIn("/dev/dri", row["warnings"][0])

    def test_workloads_outside_the_namespace_are_not_offered(self):
        other = deployment("elsewhere")
        other["metadata"]["namespace"] = "kube-system"
        self.deployments = [other]

        self.assertEqual([], move.inventory()["workloads"])

    def test_whether_it_is_running_is_reported_because_a_move_stops_it(self):
        self.deployments = [deployment("live", ready=1), deployment("stopped", ready=0)]

        states = {row["name"]: row["running"] for row in move.inventory()["workloads"]}

        self.assertEqual({"live": True, "stopped": False}, states)


class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.sent = []
        move.bind(self._get, self._send, "lab")
        move._tokens.clear()

    def _get(self, path):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return self.objects[path]

    def _send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, body))
        if method in ("PUT", "POST") and body:
            name = (body.get("metadata") or {}).get("name", "")
            kind = "configmaps" if body.get("kind") == "ConfigMap" else "secrets"
            self.objects[f"/api/v1/namespaces/lab/{kind}/{name}"] = body
        return body or {}

    def test_a_cluster_is_remembered_without_its_password(self):
        rows = move.add_cluster("loft", "http://192.0.2.242:8088", "admin", "hunter2")

        self.assertEqual({"name", "url", "user", "added"}, set(rows[0]))
        stored = json.dumps(self.objects["/api/v1/namespaces/lab/configmaps/homestead-clusters"])
        self.assertNotIn("hunter2", stored, "a password never belongs in a ConfigMap")

    def test_the_password_goes_to_a_secret(self):
        move.add_cluster("loft", "http://192.0.2.242:8088", "admin", "hunter2")

        secret = self.objects["/api/v1/namespaces/lab/secrets/homestead-cluster-loft"]
        self.assertEqual("hunter2", secret["stringData"]["password"])

    def test_an_address_that_is_not_a_url_is_refused(self):
        for url in ("192.0.2.242", "ftp://box", "", "not a url"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "http"):
                    move.add_cluster("loft", url, "admin", "x")

    def test_a_silly_name_is_refused(self):
        with self.assertRaisesRegex(ValueError, "lowercase"):
            move.add_cluster("Loft Rack", "http://x:8088", "admin", "x")

    def test_removing_a_cluster_takes_its_credentials_with_it(self):
        move.add_cluster("loft", "http://192.0.2.242:8088", "admin", "hunter2")
        self.sent.clear()

        rows = move.remove_cluster("loft")

        self.assertEqual([], rows)
        self.assertIn(("DELETE", "/api/v1/namespaces/lab/secrets/homestead-cluster-loft", None),
                      self.sent)

    def test_asking_an_unknown_cluster_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no cluster named"):
            move.remote_inventory("nowhere")



class VersionTests(unittest.TestCase):
    """Two Homesteads say which release they run before anything moves."""

    def setUp(self):
        move.bind(lambda path: {}, lambda *a, **k: {}, "lab", "2.8.59")
        self.answers = {}
        real = move.remote

        def remote(name, path, body=None):
            answer = self.answers.get(path)
            if isinstance(answer, Exception):
                raise answer
            if answer is None:
                raise move.Missing(f"{name}: HTTP 404")
            return answer

        move.remote = remote
        self.addCleanup(setattr, move, "remote", real)

    def test_this_homestead_says_what_it_runs(self):
        self.assertEqual({"version": "2.8.59", "protocol": move.PROTOCOL, "namespace": "lab",
                          "capabilities": ["copy-source-lease", "copy-source-fence", "copy-destination"]},
                         move.hello())

    def test_the_same_release_is_plainly_fine(self):
        self.answers["/api/move/hello"] = {"version": "2.8.59", "protocol": move.PROTOCOL}

        check = move.check_cluster("shed")

        self.assertEqual("same", check["state"])
        self.assertTrue(check["compatible"])

    def test_different_releases_on_one_protocol_still_move(self):
        self.answers["/api/move/hello"] = {"version": "2.8.61", "protocol": move.PROTOCOL}

        check = move.check_cluster("shed")

        self.assertEqual("differs", check["state"])
        self.assertTrue(check["compatible"])
        self.assertIn("shed is the newer", check["message"])

    def test_a_newer_protocol_there_says_to_update_here(self):
        self.answers["/api/move/hello"] = {"version": "3.0.0", "protocol": move.PROTOCOL + 1}

        check = move.check_cluster("shed")

        self.assertEqual("ahead", check["state"])
        self.assertFalse(check["compatible"])
        self.assertIn("Update this Homestead", check["message"])

    def test_a_release_before_the_handshake_is_read_from_its_settings(self):
        self.answers["/api/settings"] = {"info": {"version": "2.8.55"}}

        check = move.check_cluster("shed")

        self.assertEqual(("behind", "2.8.55", 0), (check["state"], check["version"], check["protocol"]))
        self.assertIn("Update shed first", check["message"])

    def test_the_first_release_that_could_send_counts_as_able(self):
        self.answers["/api/settings"] = {"info": {"version": "2.8.58"}}

        self.assertTrue(move.check_cluster("shed")["compatible"])

    def test_a_cluster_that_does_not_answer_is_said_so_not_called_old(self):
        self.answers["/api/move/hello"] = move.Unreachable("could not reach shed: timed out")

        check = move.check_cluster("shed")

        self.assertEqual("unreachable", check["state"])
        self.assertNotIn("compatible", check)

    def test_the_inventory_carries_the_release_too(self):
        report = move.inventory()

        self.assertEqual(("2.8.59", move.PROTOCOL), (report["version"], report["protocol"]))


if __name__ == "__main__":
    unittest.main()
