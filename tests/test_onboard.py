"""The guide to adding a host, and removing a dead one."""
import base64
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_onboard as onboard


def node(name, ready=True, roles=(), machine=""):
    labels = {f"node-role.kubernetes.io/{r}": "true" for r in roles}
    annotations = {"cluster.x-k8s.io/machine": machine, "cluster.x-k8s.io/cluster-namespace": "fleet-local"} if machine else {}
    return {"metadata": {"name": name, "labels": labels, "annotations": annotations},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown",
                                       "lastTransitionTime": "2026-09-20T10:00:00Z"}],
                       "addresses": [{"type": "InternalIP", "address": "192.0.2.51"}]}}


class Cluster:
    def __init__(self):
        self.objects = {
            "/api/v1/nodes": {"items": [node("node1", roles=("control-plane", "etcd"), machine="custom-1"),
                                        node("node2", roles=("control-plane", "etcd"), machine="custom-2"),
                                        node("node3", roles=("control-plane", "etcd"), machine="custom-3")]},
            "/api/v1/namespaces/harvester-system/configmaps/vip": {"data": {"ip": "192.0.2.240"}},
            "/apis/harvesterhci.io/v1beta1/settings/server-version": {"value": "v1.4.1"},
        }
        self.machines = [{"metadata": {"name": f"custom-{i}", "namespace": "fleet-local"},
                          "status": {"nodeRef": {"name": f"node{i}"}, "phase": "Running"}} for i in (1, 2, 3)]
        self.replicas = []
        self.lh_nodes = ["node1", "node2", "node3"]
        self.sent = []
        self.secrets = {}

    def get(self, path):
        if path == "/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines":
            return {"items": self.machines}
        if path.startswith("/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines/"):
            found = [m for m in self.machines if m["metadata"]["name"] == path.rsplit("/", 1)[1]]
            if found:
                return found[0]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path.startswith("/api/v1/pods"):
            return {"items": getattr(self, "pods", [])}
        if path == "/apis/kubevirt.io/v1/virtualmachineinstances":
            return {"items": getattr(self, "vmis", [])}
        if path == "/apis/storage.k8s.io/v1/volumeattachments":
            return {"items": getattr(self, "attachments", [])}
        if path == "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas":
            return {"items": self.replicas}
        if path == "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes":
            return {"items": [{"metadata": {"name": n}} for n in self.lh_nodes]}
        if path.startswith("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/"):
            name = path.rsplit("/", 1)[1]
            if name in self.lh_nodes:
                return {"metadata": {"name": name}}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path.startswith("/api/v1/nodes/"):
            name = path.rsplit("/", 1)[1]
            found = [n for n in self.objects["/api/v1/nodes"]["items"] if n["metadata"]["name"] == name]
            if found:
                return found[0]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if "/secrets/" in path:
            name = path.rsplit("/", 1)[1]
            if name in self.secrets:
                return self.secrets[name]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path in self.objects:
            return self.objects[path]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, ctype="application/json", **kwargs):
        self.sent.append((method, path))
        if method == "POST" and path.endswith("/secrets"):
            data = {k: base64.b64encode(v.encode()).decode() for k, v in body["stringData"].items()}
            self.secrets[body["metadata"]["name"]] = {"data": data}
        if method == "DELETE" and "/secrets/" in path:
            self.secrets.pop(path.rsplit("/", 1)[1], None)
        if method == "DELETE" and path.startswith("/api/v1/nodes/"):
            name = path.rsplit("/", 1)[1]
            self.objects["/api/v1/nodes"]["items"] = [n for n in self.objects["/api/v1/nodes"]["items"]
                                                      if n["metadata"]["name"] != name]
        if method == "DELETE" and "/machines/" in path:
            name = path.rsplit("/", 1)[1]
            for m in self.machines:
                if m["metadata"]["name"] == name and m["metadata"].get("finalizers"):
                    # A finalizer holds it: deletion starts and waits.
                    m["metadata"]["deletionTimestamp"] = "2026-09-23T10:00:00Z"
                    return body or {}
            self.machines = [m for m in self.machines if m["metadata"]["name"] != name]
        if method == "PATCH" and "/machines/" in path and body.get("metadata", {}).get("finalizers", 1) is None:
            name = path.rsplit("/", 1)[1]
            self.machines = [m for m in self.machines if m["metadata"]["name"] != name]
        if method == "DELETE" and "/replicas/" in path:
            self.replicas = [r for r in self.replicas if r["metadata"]["name"] != path.rsplit("/", 1)[1]]
        if method == "DELETE" and "/longhorn-system/nodes/" in path:
            self.lh_nodes.remove(path.rsplit("/", 1)[1])
        return body or {}


class Ops:
    def __init__(self):
        self.started = []

    def start(self, kind, title, resource, href, ref, message=""):
        self.started.append((kind, title))
        return {"id": "op1"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cluster = Cluster()
        self.ops = Ops()
        onboard.bind(self.cluster.get, self.cluster.send, "lab", self.ops)


class GuideTests(Base):
    def test_the_guide_answers_from_the_cluster(self):
        self.cluster.objects["/apis/harvesterhci.io/v1beta1/settings/ntp-servers"] = {
            "value": '{"ntpServers":["0.uk.pool.ntp.org","1.uk.pool.ntp.org"]}'}
        g = onboard.guide()

        self.assertEqual("1.4.1", g["version"])
        self.assertEqual("https://releases.rancher.com/harvester/v1.4.1/harvester-v1.4.1-amd64.iso", g["iso"])
        self.assertEqual("https://releases.rancher.com/harvester/v1.4.1/harvester-v1.4.1-amd64.sha512", g["checksums"])
        self.assertEqual("192.0.2.240", g["vip"])
        self.assertEqual(["0.uk.pool.ntp.org", "1.uk.pool.ntp.org"], g["ntp"])
        self.assertEqual("node4", g["hostname"])
        self.assertEqual(3, g["management_count"])
        self.assertEqual("192.0.2.51", g["token_host"])
        self.assertEqual("sudo grep '^token:' /etc/rancher/rancherd/config.yaml", g["token_command"])

    def test_the_guide_never_reads_the_token(self):
        onboard.guide()
        self.assertFalse(any("secret" in path for _, path in self.cluster.sent))
        self.assertNotIn("token", {k for k in onboard.guide() if not k.startswith("token_")})

    def test_the_next_name_follows_the_pattern(self):
        self.assertEqual("harvester-04", onboard.next_hostname(["harvester-01", "harvester-02", "harvester-03"]))
        self.assertEqual("node3", onboard.next_hostname(["node1", "node2", "witness"]))
        self.assertEqual("", onboard.next_hostname(["alpha"]))

    def test_join_plans_left_by_an_older_release_are_deleted_with_their_tokens(self):
        self.cluster.secrets = {"homestead-onboard-abc": {}, "homestead-auth": {}}
        self.cluster.objects["/api/v1/namespaces/lab/secrets"] = {"items": [
            {"metadata": {"name": n}} for n in self.cluster.secrets]}
        self.cluster.objects["/api/v1/namespaces/lab/pods?labelSelector=app%3Dhomestead-pxe"] = {"items": []}

        self.assertEqual(1, onboard.tidy_old_plans())
        self.assertEqual({"homestead-auth"}, set(self.cluster.secrets))


class RemovalTests(Base):
    def kill(self, name):
        for n in self.cluster.objects["/api/v1/nodes"]["items"]:
            if n["metadata"]["name"] == name:
                n["status"]["conditions"][0]["status"] = "Unknown"

    def test_a_ready_node_is_not_removed_from_here(self):
        report = onboard.removal_plan("node3")

        self.assertFalse(report["ok"])
        self.assertIn("rke2-uninstall.sh", report["blockers"][0])

    def test_a_ready_k3s_node_gets_k3s_steps(self):
        for n in self.cluster.objects["/api/v1/nodes"]["items"]:
            if n["metadata"]["name"] == "node3":
                n["status"].setdefault("nodeInfo", {})["kubeletVersion"] = "v1.33.4+k3s1"

        blocker = onboard.removal_plan("node3")["blockers"][0]

        self.assertIn("k3s-agent-uninstall.sh", blocker)
        self.assertNotIn("Harvester", blocker)

    def test_removing_a_dead_control_plane_node_keeps_quorum_and_says_the_margin(self):
        self.kill("node3")

        report = onboard.removal_plan("node3")

        self.assertTrue(report["ok"], report["blockers"])
        self.assertTrue(any("one more failure" in w for w in report["warnings"]))

    def test_losing_quorum_is_refused(self):
        self.kill("node2")
        self.kill("node3")

        report = onboard.removal_plan("node3")

        self.assertFalse(report["ok"])
        self.assertIn("quorum", report["blockers"][0])

    def test_the_last_copy_of_a_volume_is_called_out(self):
        self.kill("node3")
        self.cluster.replicas = [
            {"spec": {"nodeID": "node3", "volumeName": "only-here"}, "status": {"currentState": "running"}},
            {"spec": {"nodeID": "node3", "volumeName": "mirrored"}, "status": {"currentState": "running"}},
            {"spec": {"nodeID": "node1", "volumeName": "mirrored"}, "status": {"currentState": "running"}},
        ]

        report = onboard.removal_plan("node3")
        self.assertEqual((["only-here"], ["mirrored"]), (report["lost_volumes"], report["rebuilt_volumes"]))
        with self.assertRaisesRegex(ValueError, "only copy"):
            onboard.remove_node("node3")

    def test_a_dead_node_is_removed_in_harvesters_order(self):
        self.kill("node3")

        result = onboard.remove_node("node3")

        order = [(m, p.split("/")[-1]) for m, p in self.cluster.sent]
        self.assertEqual([("PATCH", "node3"), ("DELETE", "node3"), ("DELETE", "custom-3"), ("DELETE", "node3")], order)
        self.assertEqual(4, len(result["log"]))
        self.assertNotIn("custom-3", [m["metadata"]["name"] for m in self.cluster.machines])

    def hold_things_on(self, name):
        """A host that died holding a pod, a VM, a volume attachment and a replica."""
        self.kill(name)
        self.cluster.pods = [{"metadata": {"name": "app-1", "namespace": "lab"}, "spec": {"nodeName": name}}]
        self.cluster.vmis = [{"metadata": {"name": "haos", "namespace": "lab"}, "status": {"nodeName": name}}]
        self.cluster.attachments = [{"metadata": {"name": "csi-abc"}, "spec": {"nodeName": name}}]
        self.cluster.replicas = [
            {"metadata": {"name": "vol-r-3"}, "spec": {"nodeID": name, "volumeName": "vol"}, "status": {"currentState": "running"}},
            {"metadata": {"name": "vol-r-1"}, "spec": {"nodeID": "node1", "volumeName": "vol"}, "status": {"currentState": "running"}}]
        for m in self.cluster.machines:
            if m["status"]["nodeRef"]["name"] == name:
                m["metadata"]["finalizers"] = ["machine.cluster.x-k8s.io"]

    def test_the_plan_says_what_a_dead_host_still_holds(self):
        self.hold_things_on("node3")

        report = onboard.removal_plan("node3")

        self.assertEqual({"pods": 1, "vms": ["haos"], "attachments": 1, "replicas": 1}, report["stuck"])
        self.assertTrue(any("haos" in step for step in report["gone_steps"]))

    def test_a_host_gone_for_good_lets_go_of_everything_it_held(self):
        self.hold_things_on("node3")

        result = onboard.remove_node("node3", gone=True)

        deleted = [p for m, p in self.cluster.sent if m == "DELETE"]
        self.assertIn("/apis/kubevirt.io/v1/namespaces/lab/virtualmachineinstances/haos", deleted)
        self.assertIn("/api/v1/namespaces/lab/pods/app-1", deleted)
        self.assertIn("/apis/storage.k8s.io/v1/volumeattachments/csi-abc", deleted)
        self.assertIn("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas/vol-r-3", deleted)
        self.assertNotIn("custom-3", [m["metadata"]["name"] for m in self.cluster.machines],
                         "the machine held by its finalizer is let go")
        self.assertNotIn("node3", self.cluster.lh_nodes, "no replicas left, so Longhorn's record goes too")
        self.assertTrue(any("finalizers" in line for line in result["log"]))

    def test_the_standard_removal_leaves_what_it_held_for_the_controllers(self):
        self.hold_things_on("node3")

        onboard.remove_node("node3")

        deleted = [p for m, p in self.cluster.sent if m == "DELETE"]
        self.assertNotIn("/api/v1/namespaces/lab/pods/app-1", deleted)
        self.assertIn("custom-3", [m["metadata"]["name"] for m in self.cluster.machines], "still deleting")

    def test_a_node_that_only_just_went_down_gets_a_warning(self):
        self.kill("node3")
        for n in self.cluster.objects["/api/v1/nodes"]["items"]:
            if n["metadata"]["name"] == "node3":
                n["status"]["conditions"][0]["lastTransitionTime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        report = onboard.removal_plan("node3")

        self.assertTrue(any("rebooting" in w for w in report["warnings"]))

    def test_a_stuck_machine_or_longhorn_node_needs_force(self):
        self.cluster.machines.append({"metadata": {"name": "custom-9", "namespace": "fleet-local",
                                                   "deletionTimestamp": "2026-09-23T10:00:00Z", "finalizers": ["x"]},
                                      "status": {"nodeRef": {"name": "gone"}, "phase": "Deleting"}})
        self.cluster.lh_nodes.append("gone")
        self.cluster.replicas = [{"metadata": {"name": "r-gone"}, "spec": {"nodeID": "gone", "volumeName": "v"},
                                  "status": {"currentState": "error"}}]

        with self.assertRaisesRegex(ValueError, "force"):
            onboard.cleanup("machine", "custom-9")
        with self.assertRaisesRegex(ValueError, "force"):
            onboard.cleanup("longhorn", "gone")
        onboard.cleanup("machine", "custom-9", force=True)
        onboard.cleanup("longhorn", "gone", force=True)

        self.assertNotIn("custom-9", [m["metadata"]["name"] for m in self.cluster.machines])
        self.assertNotIn("gone", self.cluster.lh_nodes)

    def test_the_cleanup_report_finds_leftovers(self):
        self.kill("node2")
        self.cluster.machines.append({"metadata": {"name": "custom-9", "namespace": "fleet-local"},
                                      "status": {"nodeRef": {"name": "gone"}, "phase": "Running"}})
        self.cluster.lh_nodes.append("gone")

        report = onboard.cleanup_report()

        self.assertEqual(["node2"], [n["name"] for n in report["dead_nodes"]])
        self.assertEqual(["custom-9"], [m["name"] for m in report["stale_machines"]])
        self.assertEqual(["gone"], [n["name"] for n in report["stale_longhorn"]])

    def test_leftovers_are_cleaned_one_at_a_time_and_only_if_leftover(self):
        self.cluster.lh_nodes.append("gone")

        onboard.cleanup("longhorn", "gone")

        self.assertNotIn("gone", self.cluster.lh_nodes)
        self.assertIn(("PATCH", "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/gone"), self.cluster.sent)
        with self.assertRaisesRegex(ValueError, "still has"):
            onboard.cleanup("longhorn", "node1")
        with self.assertRaisesRegex(ValueError, "not a leftover"):
            onboard.cleanup("machine", "custom-1")


if __name__ == "__main__":
    unittest.main()
