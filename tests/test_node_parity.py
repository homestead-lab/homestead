import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_node_parity as PARITY

HELM = PARITY.HELMCHARTS
INSTALLER_LONGHORN = "persistence:\n  defaultClassReplicaCount: 1\ndefaultSettings:\n  defaultReplicaCount: 1\n"
KUBE_VIP = 'env:\n  vip_arp: "true"\n  vip_interface: "enp1s0"\n  svc_enable: "true"\n'


def node(name, ready=True):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class Cluster:
    def __init__(self, nodes, objects=None, probes=None, host_out="MULTIPATH set\nISCSI kept\nEND\n"):
        self.objects = {"/api/v1/nodes": {"items": nodes}, **(objects or {})}
        self.probes, self.host_out, self.sent, self.ran = probes or {}, host_out, [], []

    def kget(self, path):
        if path not in self.objects:
            raise KeyError(path)
        return self.objects[path]

    def ksend(self, method, path, body=None, ctype=""):
        self.sent.append((method, path, body))

    def run(self, name, script, timeout=60):
        self.ran.append(name)
        return self.host_out, ""


class NodeParityTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()

    def bind(self, cluster, longhorn=True):
        hostrun = type("H", (), {"run": staticmethod(cluster.run)})
        PARITY.bind(cluster.kget, cluster.ksend, hostrun, lambda force=False: {"distribution": "k3s", "longhorn": longhorn},
                    lambda: cluster.probes, self.data)

    def test_each_new_host_is_kept_off_multipathd_once(self):
        cluster = Cluster([node("node-1"), node("node-2"), node("node-3", ready=False)])
        self.bind(cluster)
        changes = PARITY.tick()
        self.assertEqual(["node-1", "node-2"], cluster.ran, "a node that is not Ready waits")
        self.assertIn(("node-2", "multipathd kept off Longhorn's devices"), changes)
        PARITY.tick()
        self.assertEqual(["node-1", "node-2"], cluster.ran, "done once per node")

    def test_a_host_that_boots_from_multipath_is_left_alone(self):
        self.assertIn('grep -q mpath; then echo "MULTIPATH root"', PARITY.HOST_SCRIPT)
        cluster = Cluster([node("san")], host_out="MULTIPATH root\nISCSI kept\nEND\n")
        self.bind(cluster)
        self.assertEqual([], PARITY.tick())

    def test_a_host_that_cannot_be_reached_is_tried_again(self):
        cluster = Cluster([node("node-1")], host_out="")
        self.bind(cluster)
        self.assertIn("could not check", PARITY.tick()[0][1])
        cluster.host_out = "MULTIPATH kept\nISCSI kept\nEND\n"
        PARITY.tick()
        self.assertEqual(["node-1", "node-1"], cluster.ran)

    def test_a_host_done_by_an_older_release_gets_the_journal_cap_too(self):
        import json
        Path(self.data, "node-parity.json").write_text(json.dumps({"hosts": {"node-1": {"at": 1, "multipath": "set"}}}))
        cluster = Cluster([node("node-1")], host_out="JOURNAL capped\nMULTIPATH kept\nISCSI kept\nEND\n")
        self.bind(cluster, longhorn=False)
        self.assertIn(("node-1", "journal capped at 1 GB"), PARITY.tick())
        self.assertEqual([], PARITY.tick(), "then done")
        self.assertIn("grep -qs '^SystemMaxUse='", PARITY.HOST_SCRIPT, "a cap someone set is kept")
        self.assertIn('[ "$LONGHORN" = 1 ] || { echo END; exit 0; }', PARITY.HOST_SCRIPT,
                      "without Longhorn, multipathd is left alone")

    def test_kube_vip_finds_each_nodes_interface_once_they_differ(self):
        chart = {"spec": {"valuesContent": KUBE_VIP}}
        cluster = Cluster([node("a")], {f"{HELM}/kube-vip": chart},
                          probes={"a": {"default_interface": "enp1s0"}, "b": {"default_interface": "eno1"}})
        self.bind(cluster, longhorn=False)
        PARITY.tick()
        method, path, body = cluster.sent[0]
        self.assertEqual(("PATCH", f"{HELM}/kube-vip"), (method, path))
        self.assertNotIn("vip_interface", body["spec"]["valuesContent"])
        self.assertIn("svc_enable", body["spec"]["valuesContent"])

    def test_kube_vip_stays_pinned_while_the_nodes_agree_or_have_not_said(self):
        for probes in ({"a": {"default_interface": "enp1s0"}, "b": {"default_interface": "enp1s0"}}, {}):
            with self.subTest(probes=probes):
                cluster = Cluster([node("a")], {f"{HELM}/kube-vip": {"spec": {"valuesContent": KUBE_VIP}}}, probes=probes)
                self.bind(cluster, longhorn=False)
                PARITY.tick()
                self.assertEqual([], cluster.sent)

    OLD_KUBE_VIP = ('env:\n  vip_arp: "true"\n  cp_enable: "false"\n  lb_enable: "false"\n  svc_enable: "true"\n'
                    '  svc_election: "true"\n  vip_leaderelection: "false"\n  lb_class_only: "true"\n'
                    '  lb_class_name: "kube-vip.io/kube-vip-class"\n')

    def kube_vip_cluster(self, values, services=(), probes=None):
        return Cluster([node("node-3")], {f"{HELM}/kube-vip": {"spec": {"valuesContent": values}},
                                         "/api/v1/services": {"items": list(services)}},
                       probes=probes if probes is not None else {"node-3": {"default_interface": "enp2s0"}})

    def test_kube_vip_moves_to_one_leader_its_interface_and_its_capabilities(self):
        cluster = self.kube_vip_cluster(self.OLD_KUBE_VIP)
        self.bind(cluster, longhorn=False)
        changes = PARITY.tick()
        method, path, body = cluster.sent[0]
        values = body["spec"]["valuesContent"]
        self.assertEqual(("PATCH", f"{HELM}/kube-vip"), (method, path))
        for line in ('svc_election: "false"', 'vip_leaderelection: "true"', 'vip_interface: "enp2s0"',
                     'lb_class_only: "true"', "- NET_ADMIN", "- NET_RAW"):
            self.assertIn(line, values)
        self.assertTrue(any("global election" in note for _, note in changes), changes)
        cluster.objects[f"{HELM}/kube-vip"]["spec"]["valuesContent"] = values
        cluster.sent.clear()
        PARITY.tick()
        self.assertEqual([], cluster.sent, "done once")

    def test_kube_vip_is_not_pinned_while_a_node_has_not_said_its_interface(self):
        cluster = self.kube_vip_cluster(self.OLD_KUBE_VIP, probes={"node-3": {"default_interface": "enp2s0"}})
        cluster.objects["/api/v1/nodes"]["items"].append(node("node-4"))
        self.bind(cluster, longhorn=False)
        PARITY.tick()
        self.assertNotIn("vip_interface", cluster.sent[0][2]["spec"]["valuesContent"],
                         "node-4 may use another interface; kube-vip finds its own there")

    def test_kube_vip_keeps_per_service_election_for_a_local_traffic_service(self):
        nfs = {"metadata": {"namespace": "lab", "name": "nfs"},
               "spec": {"type": "LoadBalancer", "externalTrafficPolicy": "Local"}}
        cluster = self.kube_vip_cluster(self.OLD_KUBE_VIP, [nfs])
        self.bind(cluster, longhorn=False)
        PARITY.tick()
        values = cluster.sent[0][2]["spec"]["valuesContent"]
        self.assertIn('svc_election: "true"', values)
        self.assertIn("- NET_RAW", values)

    def test_kube_vip_values_someone_else_wrote_are_left_alone(self):
        cluster = self.kube_vip_cluster('env:\n  svc_election: "true"\nextraArgs:\n  - --foo\n')
        self.bind(cluster, longhorn=False)
        PARITY.tick()
        self.assertEqual([], cluster.sent)

    def stale_cluster(self, deployments=(), created="2026-09-01T00:00:00Z", labels=None, missing=()):
        service = {"metadata": {"namespace": "lab", "name": "homestead-objectstore", "creationTimestamp": created,
                                "labels": {"homestead.io/managed": "true"} if labels is None else labels},
                   "spec": {"type": "LoadBalancer", "selector": {"app": "homestead-objectstore"}}}
        objects = {"/api/v1/services": {"items": [service]},
                   "/apis/apps/v1/namespaces/lab/deployments": {"items": list(deployments)},
                   "/apis/apps/v1/namespaces/lab/statefulsets": {"items": []},
                   "/apis/apps/v1/namespaces/lab/daemonsets": {"items": []},
                   "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines": {"items": []},
                   "/api/v1/namespaces/lab/pods": {"items": []}}
        for path in missing:
            objects.pop(path)
        cluster = Cluster([node("node-3")], objects)
        self.bind(cluster, longhorn=False)
        return cluster

    def test_a_service_whose_workload_is_gone_is_removed(self):
        cluster = self.stale_cluster()
        self.assertEqual(["lab/homestead-objectstore"], PARITY.stale_services())
        self.assertEqual([("DELETE", "/api/v1/namespaces/lab/services/homestead-objectstore", None)], cluster.sent)

    def test_a_service_that_does_not_say_whose_it_is_is_left_alone(self):
        cluster = self.stale_cluster()
        cluster.objects["/api/v1/services"]["items"][0]["metadata"]["name"] = "old-vm-ssh"
        self.bind(cluster, longhorn=False)
        self.assertEqual([], PARITY.stale_services())
        self.assertEqual([], cluster.sent)

    def test_a_service_whose_named_workload_is_gone_goes_but_a_stopped_one_stays(self):
        cluster = self.stale_cluster()
        svc = cluster.objects["/api/v1/services"]["items"][0]
        svc["metadata"]["name"] = "win11-rdp"
        svc["metadata"]["annotations"] = {"homestead.io/workload-kind": "VirtualMachine", "homestead.io/workload": "win11"}
        svc["spec"]["selector"] = {"vm.kubevirt.io/name": "win11"}      # on the VMI's pod, not the VM's template
        cluster.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines"]["items"] = [
            {"metadata": {"name": "win11"}, "spec": {"running": False, "template": {"metadata": {"labels": {}}}}}]
        self.bind(cluster, longhorn=False)
        self.assertEqual([], PARITY.stale_services(), "the VM is stopped, not gone")
        cluster.objects["/apis/kubevirt.io/v1/namespaces/lab/virtualmachines"]["items"] = []
        self.assertEqual(["lab/win11-rdp"], PARITY.stale_services())

    def test_a_service_that_could_still_be_used_is_kept(self):
        stopped = {"metadata": {"name": "homestead-objectstore"}, "spec": {"replicas": 0, "template": {
            "metadata": {"labels": {"app": "homestead-objectstore"}}}}}
        cases = {"its workload is stopped, not gone": self.stale_cluster([stopped]),
                 "made minutes ago": self.stale_cluster(created="2999-01-01T00:00:00Z"),
                 "not Homestead's": self.stale_cluster(labels={}),
                 "a read failed": self.stale_cluster(missing=("/api/v1/namespaces/lab/pods",))}
        for why, cluster in cases.items():
            with self.subTest(why=why):
                self.bind(cluster, longhorn=False)
                self.assertEqual([], PARITY.stale_services())
                self.assertEqual([], cluster.sent)

    def test_the_installers_one_copy_rises_with_the_nodes(self):
        setting = {"value": "1"}
        cluster = Cluster([node("a"), node("b"), node("c"), node("d")],
                          {f"{HELM}/longhorn": {"spec": {"valuesContent": INSTALLER_LONGHORN}},
                           PARITY.LONGHORN_SETTING: setting}, host_out="MULTIPATH none\nISCSI kept\nEND\n")
        self.bind(cluster)
        PARITY.tick()
        chart = next(body for _, path, body in cluster.sent if path == f"{HELM}/longhorn")
        self.assertIn("defaultReplicaCount: 3", chart["spec"]["valuesContent"])
        self.assertIn(("PATCH", PARITY.LONGHORN_SETTING, {"value": "3"}), cluster.sent)

    def test_copies_rise_again_as_more_nodes_join_and_never_fall(self):
        setting = {"value": "1"}
        chart = {"spec": {"valuesContent": INSTALLER_LONGHORN}}
        cluster = Cluster([node("a"), node("b")], {f"{HELM}/longhorn": chart, PARITY.LONGHORN_SETTING: setting})
        cluster.ksend = lambda method, path, body=None, ctype="": (
            cluster.sent.append((method, path, body)),
            (chart if path.endswith("/longhorn") else setting).update(body))
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIn("keep 2 copies", PARITY.copies_tick())
        self.assertEqual("2", setting["value"])
        self.assertIsNone(PARITY.copies_tick(), "nothing changed: nothing asked")
        cluster.objects["/api/v1/nodes"]["items"].append(node("c"))
        self.assertIn("keep 3 copies", PARITY.copies_tick())
        self.assertIn("defaultClassReplicaCount: 3", chart["spec"]["valuesContent"])
        self.assertEqual("3", setting["value"])
        cluster.objects["/api/v1/nodes"]["items"].pop()
        self.assertIsNone(PARITY.copies_tick(), "a node leaving lowers nothing")
        self.assertIn("defaultReplicaCount: 3", chart["spec"]["valuesContent"])

    def test_a_count_someone_set_after_homestead_is_kept(self):
        chart = {"spec": {"valuesContent": INSTALLER_LONGHORN.replace("1", "2")}}
        cluster = Cluster([node("a"), node("b"), node("c")], {f"{HELM}/longhorn": chart})
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIsNone(PARITY.copies_tick(), "2 was not Homestead's: it stays")
        self.assertEqual([], cluster.sent)

    def test_copies_someone_chose_are_kept(self):
        for values in ("persistence:\n  defaultClassReplicaCount: 2\ndefaultSettings:\n  defaultReplicaCount: 2\n",
                       INSTALLER_LONGHORN + "longhornUI:\n  replicas: 1\n"):
            with self.subTest(values=values):
                cluster = Cluster([node("a"), node("b")], {f"{HELM}/longhorn": {"spec": {"valuesContent": values}}},
                                  host_out="MULTIPATH none\nISCSI kept\nEND\n")
                self.bind(cluster)
                PARITY.tick()
                self.assertEqual([], cluster.sent)

    def test_one_node_keeps_one_copy(self):
        cluster = Cluster([node("a")], {f"{HELM}/longhorn": {"spec": {"valuesContent": INSTALLER_LONGHORN}}},
                          host_out="MULTIPATH none\nISCSI kept\nEND\n")
        self.bind(cluster)
        PARITY.tick()
        self.assertEqual([], cluster.sent)

    def test_harvester_is_not_touched(self):
        cluster = Cluster([node("a")])
        PARITY.bind(cluster.kget, cluster.ksend, None, lambda force=False: {"harvester": True, "distribution": "rke2"},
                    lambda: {}, self.data)
        self.assertEqual([], PARITY.tick())


if __name__ == "__main__":
    unittest.main()
