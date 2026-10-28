"""The guide to adding a host, and removing a dead one."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_onboard as onboard
import homestead_operations as OPS
from fakes.kube import Kube

NODES = "/api/v1/nodes"
MACHINES = "/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines"
LONGHORN = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"


def node(name, ready=True, roles=(), machine=""):
    labels = {f"node-role.kubernetes.io/{r}": "true" for r in roles}
    annotations = {"cluster.x-k8s.io/machine": machine, "cluster.x-k8s.io/cluster-namespace": "fleet-local"} if machine else {}
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "labels": labels, "annotations": annotations},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown",
                                       "lastTransitionTime": "2026-09-20T10:00:00Z"}],
                       "addresses": [{"type": "InternalIP", "address": "192.0.2.51"}]}}


def machine(name, node_name, phase="Running", **meta):
    return {"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "Machine",
            "metadata": {"name": name, "namespace": "fleet-local", **meta},
            "status": {"nodeRef": {"name": node_name}, "phase": phase}}


def longhorn_node(name):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Node", "metadata": {"name": name, "namespace": "longhorn-system"}}


def replica(name, node_name, volume, state="running"):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Replica", "metadata": {"name": name, "namespace": "longhorn-system"},
            "spec": {"nodeID": node_name, "volumeName": volume}, "status": {"currentState": state}}


def setting(name, value):
    return {"apiVersion": "harvesterhci.io/v1beta1", "kind": "Setting", "metadata": {"name": name}, "value": value}


class Cluster:
    """Three control-plane hosts, their machines and Longhorn nodes, on the
    shared fake API (tests/fakes/kube.py)."""

    def __init__(self, test):
        self.kube = test.enterContext(Kube(
            node("node1", roles=("control-plane", "etcd"), machine="custom-1"),
            node("node2", roles=("control-plane", "etcd"), machine="custom-2"),
            node("node3", roles=("control-plane", "etcd"), machine="custom-3"),
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "vip", "namespace": "harvester-system"},
             "data": {"ip": "192.0.2.240"}},
            setting("server-version", "v1.4.1"),
            *[machine(f"custom-{i}", f"node{i}") for i in (1, 2, 3)],
            *[longhorn_node(f"node{i}") for i in (1, 2, 3)]))
        # The shared fake runs the real write guards; the storage guard takes the job store's lock under DATA_DIR.
        test.enterContext(mock.patch.object(OPS, "DATA_DIR", test.enterContext(tempfile.TemporaryDirectory())))

    @property
    def sent(self):
        return [(method, path) for method, path, _, _ in self.kube.writes]

    def edit(self, path, change):
        """Change an object as it stands, as something outside Homestead would."""
        obj = self.kube.object(path)
        change(obj)
        self.kube.add(obj)

    def names(self, path):
        return [item["metadata"]["name"] for item in K.kget(path)["items"]]

    @property
    def machines(self):
        return self.names(MACHINES)

    @property
    def lh_nodes(self):
        return self.names(f"{LONGHORN}/nodes")


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
        self.cluster = Cluster(self)
        self.ops = Ops()
        # onboard reads the cluster through the shared client here, whatever another test bound.
        self.enterContext(mock.patch.multiple(onboard, kget=onboard.kget, ksend=onboard.ksend, NS=onboard.NS, OPS=onboard.OPS))
        onboard.bind(K.kget, K.ksend, "lab", self.ops)


class GuideTests(Base):
    def test_the_guide_answers_from_the_cluster(self):
        self.cluster.kube.add(setting("ntp-servers", '{"ntpServers":["0.uk.pool.ntp.org","1.uk.pool.ntp.org"]}'))
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
        for name in ("homestead-onboard-abc", "homestead-auth"):
            self.cluster.kube.add({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "lab"}})

        self.assertEqual(1, onboard.tidy_old_plans())
        self.assertEqual(["homestead-auth"], self.cluster.names("/api/v1/namespaces/lab/secrets"))


class RemovalTests(Base):
    def kill(self, name):
        self.cluster.edit(f"{NODES}/{name}", lambda n: n["status"]["conditions"][0].update(status="Unknown"))

    def test_a_ready_node_is_not_removed_from_here(self):
        report = onboard.removal_plan("node3")

        self.assertFalse(report["ok"])
        self.assertIn("rke2-uninstall.sh", report["blockers"][0])

    def test_a_ready_k3s_node_gets_k3s_steps(self):
        self.cluster.edit(f"{NODES}/node3", lambda n: n["status"].setdefault("nodeInfo", {}).update(kubeletVersion="v1.33.4+k3s1"))

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
        self.cluster.kube.add(replica("only-here-r-3", "node3", "only-here"))
        self.cluster.kube.add(replica("mirrored-r-3", "node3", "mirrored"))
        self.cluster.kube.add(replica("mirrored-r-1", "node1", "mirrored"))

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
        self.assertNotIn("custom-3", self.cluster.machines)

    def hold_things_on(self, name):
        """A host that died holding a pod, a VM, a volume attachment and a replica."""
        self.kill(name)
        self.cluster.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "app-1", "namespace": "lab"},
                               "spec": {"nodeName": name}})
        self.cluster.kube.add({"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstance",
                               "metadata": {"name": "haos", "namespace": "lab"}, "status": {"nodeName": name}})
        self.cluster.kube.add({"apiVersion": "storage.k8s.io/v1", "kind": "VolumeAttachment", "metadata": {"name": "csi-abc"},
                               "spec": {"nodeName": name}})
        self.cluster.kube.add(replica("vol-r-3", name, "vol"))
        self.cluster.kube.add(replica("vol-r-1", "node1", "vol"))
        self.cluster.edit(f"{MACHINES}/custom-3", lambda m: m["metadata"].update(finalizers=["machine.cluster.x-k8s.io"]))

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
        self.assertNotIn("custom-3", self.cluster.machines, "the machine held by its finalizer is let go")
        self.assertNotIn("node3", self.cluster.lh_nodes, "no replicas left, so Longhorn's record goes too")
        self.assertTrue(any("finalizers" in line for line in result["log"]))

    def test_the_standard_removal_leaves_what_it_held_for_the_controllers(self):
        self.hold_things_on("node3")

        onboard.remove_node("node3")

        deleted = [p for m, p in self.cluster.sent if m == "DELETE"]
        self.assertNotIn("/api/v1/namespaces/lab/pods/app-1", deleted)
        self.assertIn("custom-3", self.cluster.machines, "still deleting")

    def test_a_node_that_only_just_went_down_gets_a_warning(self):
        self.kill("node3")
        self.cluster.edit(f"{NODES}/node3", lambda n: n["status"]["conditions"][0].update(
            lastTransitionTime=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

        report = onboard.removal_plan("node3")

        self.assertTrue(any("rebooting" in w for w in report["warnings"]))

    def test_a_stuck_machine_or_longhorn_node_needs_force(self):
        self.cluster.kube.add(machine("custom-9", "gone", phase="Deleting",
                                      deletionTimestamp="2026-09-23T10:00:00Z", finalizers=["x"]))
        self.cluster.kube.add(longhorn_node("gone"))
        self.cluster.kube.add(replica("r-gone", "gone", "v", state="error"))

        with self.assertRaisesRegex(ValueError, "force"):
            onboard.cleanup("machine", "custom-9")
        with self.assertRaisesRegex(ValueError, "force"):
            onboard.cleanup("longhorn", "gone")
        onboard.cleanup("machine", "custom-9", force=True)
        onboard.cleanup("longhorn", "gone", force=True)

        self.assertNotIn("custom-9", self.cluster.machines)
        self.assertNotIn("gone", self.cluster.lh_nodes)

    def test_the_cleanup_report_finds_leftovers(self):
        self.kill("node2")
        self.cluster.kube.add(machine("custom-9", "gone"))
        self.cluster.kube.add(longhorn_node("gone"))

        report = onboard.cleanup_report()

        self.assertEqual(["node2"], [n["name"] for n in report["dead_nodes"]])
        self.assertEqual(["custom-9"], [m["name"] for m in report["stale_machines"]])
        self.assertEqual(["gone"], [n["name"] for n in report["stale_longhorn"]])

    def test_leftovers_are_cleaned_one_at_a_time_and_only_if_leftover(self):
        self.cluster.kube.add(longhorn_node("gone"))

        onboard.cleanup("longhorn", "gone")

        self.assertNotIn("gone", self.cluster.lh_nodes)
        self.assertIn(("PATCH", "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/gone"), self.cluster.sent)
        with self.assertRaisesRegex(ValueError, "still has"):
            onboard.cleanup("longhorn", "node1")
        with self.assertRaisesRegex(ValueError, "not a leftover"):
            onboard.cleanup("machine", "custom-1")


if __name__ == "__main__":
    unittest.main()
