import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_node_parity as PARITY
import homestead_hostrun as HOSTRUN
import homestead_operations as OPS
from fakes.kube import Kube

HELM = PARITY.HELMCHARTS
INSTALLER_LONGHORN = "persistence:\n  defaultClassReplicaCount: 1\ndefaultSettings:\n  defaultReplicaCount: 1\n"
KUBE_VIP = 'env:\n  vip_arp: "true"\n  vip_interface: "enp1s0"\n  svc_enable: "true"\n'
LH_VERSION = "longhorn.io/v1beta2"
TIEBREAKER = {"node-role/tiebreaker": "true"}


def node(name, ready=True, labels=None):
    meta = {"name": name, **({"labels": dict(labels)} if labels else {})}
    return {"apiVersion": "v1", "kind": "Node", "metadata": meta,
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


LH_NODES = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes"


def lh_node(name, schedulable=True, disks=({"allowScheduling": True},), ready=True, evicting=False):
    return {"apiVersion": LH_VERSION, "kind": "Node", "metadata": {"namespace": "longhorn-system", "name": name},
            "spec": {"allowScheduling": schedulable, "evictionRequested": evicting,
                     "disks": {f"disk-{i}": dict(d) for i, d in enumerate(disks)}},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def chart(name, values):
    return {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart", "metadata": {"namespace": "kube-system", "name": name},
            "spec": {"valuesContent": values}}


def setting(value):
    return {"apiVersion": LH_VERSION, "kind": "Setting",
            "metadata": {"namespace": "longhorn-system", "name": PARITY.LONGHORN_SETTING.rsplit("/", 1)[1]}, "value": value}


class Cluster:
    """The cluster on the shared fake API (tests/fakes/kube.py), started when a
    test binds it, so what a test changes first is what the fake starts with.
    Longhorn stores on every node unless lh_nodes says otherwise; a path in
    fail is refused with a 500; the host is run by the cluster's own run()."""
    def __init__(self, nodes, objects=(), probes=None, host_out="MULTIPATH set\nISCSI kept\nEND\n", lh_nodes=None, fail=()):
        self.nodes, self.objects, self.lh_nodes, self.fail = list(nodes), list(objects), lh_nodes, list(fail)
        self.probes, self.host_out, self.ran = probes or {}, host_out, []
        self.held, self.changes = set(), []

    def start(self, test):
        lh = self.lh_nodes if self.lh_nodes is not None else [lh_node(n["metadata"]["name"]) for n in self.nodes]
        self.kube = test.enterContext(Kube(*self.nodes, *lh, *self.objects))
        for path in self.fail:
            self.kube.fail(path, 500, times=99, method="GET")

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def object(self, path):
        return self.kube.object(path)

    def world(self, method, path):
        """Something other than Homestead changes the cluster: not a write to count."""
        before = len(self.kube.writes)
        self.kube(method, path)
        del self.kube.writes[before:]

    def add_node(self, name):
        self.kube.add(node(name))
        if self.lh_nodes is None:
            self.kube.add(lh_node(name))

    def remove_node(self, name):
        self.world("DELETE", f"/api/v1/nodes/{name}")
        if self.lh_nodes is None:
            self.world("DELETE", f"{LH_NODES}/{name}")

    def run(self, name, script, timeout=60, change=""):
        if name in self.held:
            raise HOSTRUN.Busy(f"another change is running on {name}: Prepare /dev/sdb on {name}")
        self.ran.append(name)
        self.changes.append(change)
        return self.host_out, ""


class NodeParityTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()

    def bind(self, cluster, longhorn=True, platform=None, own=("", ""), hostrun=True):
        cluster.start(self)
        # The shared fake runs the real write guards; the storage guard takes the
        # job store's lock, which lives under its DATA_DIR.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.data))
        self.addCleanup(PARITY.bind, *(getattr(PARITY, name, None) for name in (
            "kget", "ksend", "hostrun", "platform", "probes", "DATA_DIR", "OWN")))
        PARITY.bind(K.kget, K.ksend, type("H", (), {"run": staticmethod(cluster.run)}) if hostrun else None,
                    platform or (lambda force=False: {"distribution": "k3s", "longhorn": longhorn}),
                    lambda: cluster.probes, self.data, own)

    def test_each_new_host_is_kept_off_multipathd_once(self):
        cluster = Cluster([node("node-1"), node("node-2"), node("node-3", ready=False)])
        self.bind(cluster)
        changes = PARITY.tick()
        self.assertEqual(["node-1", "node-2"], cluster.ran, "a node that is not Ready waits")
        self.assertIn(("node-2", "multipathd kept off Longhorn's devices"), changes)
        PARITY.tick()
        self.assertEqual(["node-1", "node-2"], cluster.ran, "done once per node")

    def test_setting_a_host_up_is_a_change_and_a_held_host_waits_for_the_next_round(self):
        cluster = Cluster([node("node-1"), node("node-2")])
        cluster.held = {"node-1"}
        self.bind(cluster)
        PARITY.tick()
        self.assertEqual(["node-2"], cluster.ran, "node-2 is not held up by node-1")
        self.assertEqual(["Set node-2 up as the other hosts are"], cluster.changes,
                         "journald, multipathd and iscsid are changed: it takes the host's lease (#597)")
        cluster.held = set()
        PARITY.tick()
        self.assertIn("node-1", cluster.ran, "tried again once free")

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
        self.assertIn(("node-1", "journal capped at 1 GiB"), PARITY.tick())
        self.assertEqual([], PARITY.tick(), "then done")
        self.assertIn("grep -qs '^SystemMaxUse='", PARITY.HOST_SCRIPT, "a cap someone set is kept")
        self.assertIn('[ "$LONGHORN" = 1 ] || { echo END; exit 0; }', PARITY.HOST_SCRIPT,
                      "without Longhorn, multipathd is left alone")

    def test_kube_vip_finds_each_nodes_interface_once_they_differ(self):
        cluster = Cluster([node("a")], [chart("kube-vip", KUBE_VIP)],
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
                # Homestead's own current values, pinned: only the pin is in question. The old
                # minimal values drew the one-leader move once the services could be read.
                cluster = Cluster([node("a")], [chart("kube-vip", PARITY.addons.kube_vip_values("enp1s0", False))], probes=probes)
                self.bind(cluster, longhorn=False)
                PARITY.tick()
                self.assertEqual([], cluster.sent)

    OLD_KUBE_VIP = ('env:\n  vip_arp: "true"\n  cp_enable: "false"\n  lb_enable: "false"\n  svc_enable: "true"\n'
                    '  svc_election: "true"\n  vip_leaderelection: "false"\n  lb_class_only: "true"\n'
                    '  lb_class_name: "kube-vip.io/kube-vip-class"\n')

    def kube_vip_cluster(self, values, services=(), probes=None, nodes=("node-3",)):
        return Cluster([node(n) for n in nodes], [chart("kube-vip", values), *services],
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
        self.assertEqual(values, cluster.object(f"{HELM}/kube-vip")["spec"]["valuesContent"])
        cluster.kube.writes.clear()
        PARITY.tick()
        self.assertEqual([], cluster.sent, "done once")

    def test_kube_vip_is_not_pinned_while_a_node_has_not_said_its_interface(self):
        cluster = self.kube_vip_cluster(self.OLD_KUBE_VIP, probes={"node-3": {"default_interface": "enp2s0"}},
                                        nodes=("node-3", "node-4"))
        self.bind(cluster, longhorn=False)
        PARITY.tick()
        self.assertNotIn("vip_interface", cluster.sent[0][2]["spec"]["valuesContent"],
                         "node-4 may use another interface; kube-vip finds its own there")

    def test_kube_vip_keeps_per_service_election_for_a_local_traffic_service(self):
        nfs = {"apiVersion": "v1", "kind": "Service", "metadata": {"namespace": "lab", "name": "nfs"},
               "spec": {"type": "LoadBalancer", "externalTrafficPolicy": "Local"}}
        cluster = self.kube_vip_cluster(self.OLD_KUBE_VIP, [nfs])
        self.bind(cluster, longhorn=False)
        PARITY.tick()
        values = cluster.sent[0][2]["spec"]["valuesContent"]
        self.assertIn('svc_election: "true"', values)
        self.assertIn("- NET_RAW", values)

    def vip_cluster(self, values, nodes=("a", "b"), probes=None, labels=None):
        cluster = Cluster([node(n, labels=(labels or {}).get(n)) for n in nodes], [chart("kube-vip", values)],
                          probes=probes or {})
        self.bind(cluster)
        return cluster

    def test_homesteads_kube_vip_keeps_off_tiebreakers_and_stays_homesteads(self):
        values = PARITY.addons.kube_vip_values("", False)
        self.assertTrue(values.endswith(PARITY.addons.KUBE_VIP_SECURITY + PARITY.addons.KUBE_VIP_AFFINITY))
        self.assertIn("key: node-role/tiebreaker\n          operator: DoesNotExist\n", values)
        self.assertIsNotNone(PARITY._ours(values))

    def test_older_values_without_the_affinity_gain_it_once(self):
        older = PARITY.addons.kube_vip_values("", False).replace(PARITY.addons.KUBE_VIP_AFFINITY, "")
        cluster = self.vip_cluster(older)
        self.assertTrue(PARITY.kube_vip_settings())
        sent = next(body for _, path, body in cluster.sent if path == f"{HELM}/kube-vip")
        self.assertTrue(sent["spec"]["valuesContent"].endswith(PARITY.addons.KUBE_VIP_AFFINITY))

    def test_values_with_another_affinity_are_left_alone(self):
        theirs = (PARITY.addons.kube_vip_values("", False).replace(PARITY.addons.KUBE_VIP_AFFINITY, "")
                  + "affinity:\n  nodeAffinity:\n    requiredDuringSchedulingIgnoredDuringExecution:\n"
                    "      nodeSelectorTerms:\n      - matchExpressions:\n        - key: kubernetes.io/hostname\n"
                    "          operator: NotIn\n          values: [pi]\n")
        cluster = self.vip_cluster(theirs)
        self.assertIsNone(PARITY._ours(theirs))
        self.assertEqual("", PARITY.kube_vip_settings())
        self.assertEqual([], cluster.sent)

    def test_the_affinity_names_the_tiebreaker_key(self):
        import homestead_roles as ROLES
        self.assertIn(f"key: {ROLES.KEY}\n", PARITY.addons.KUBE_VIP_AFFINITY)

    def test_a_tiebreaker_on_another_interface_does_not_stop_the_pin(self):
        probes = {"a": {"default_interface": "enp3s0"}, "b": {"default_interface": "enp3s0"},
                  "pi": {"default_interface": "eth0"}}
        cluster = self.vip_cluster(PARITY.addons.kube_vip_values("", False), nodes=("a", "b", "pi"), probes=probes,
                                   labels={"pi": TIEBREAKER})
        PARITY.kube_vip_settings()
        sent = next(body for _, path, body in cluster.sent if path == f"{HELM}/kube-vip")
        self.assertIn('vip_interface: "enp3s0"', sent["spec"]["valuesContent"])

    def test_a_tiebreakers_interface_does_not_unpin_the_others(self):
        probes = {"a": {"default_interface": "enp3s0"}, "pi": {"default_interface": "eth0"}}
        cluster = self.vip_cluster(PARITY.addons.kube_vip_values("enp3s0", False), nodes=("a", "pi"), probes=probes,
                                   labels={"pi": TIEBREAKER})
        self.assertEqual("", PARITY.kube_vip())
        self.assertEqual([], cluster.sent)

    def test_a_new_kube_vip_install_is_pinned_without_asking_the_tiebreaker(self):
        self.enterContext(Kube(node("a"), node("pi", labels=TIEBREAKER)))
        probes = {"a": {"default_interface": "enp3s0"}, "pi": {"default_interface": "eth0"}}
        saved = (PARITY.addons.kget, PARITY.addons.probes)
        self.addCleanup(lambda: setattr(PARITY.addons, "kget", saved[0]) or setattr(PARITY.addons, "probes", saved[1]))
        PARITY.addons.kget, PARITY.addons.probes = K.kget, (lambda: probes)
        self.assertEqual("enp3s0", PARITY.addons.vip_interface())

    def test_kube_vip_values_someone_else_wrote_are_left_alone(self):
        cluster = self.kube_vip_cluster('env:\n  svc_election: "true"\nextraArgs:\n  - --foo\n')
        self.bind(cluster, longhorn=False)
        PARITY.tick()
        self.assertEqual([], cluster.sent)

    def stale_cluster(self, deployments=(), created="2026-09-01T00:00:00Z", labels=None, missing=(),
                      name="homestead-objectstore", annotations=None, selector=None, vms=(), bind=True):
        service = {"apiVersion": "v1", "kind": "Service",
                   "metadata": {"namespace": "lab", "name": name, "creationTimestamp": created,
                                "labels": {"homestead.io/managed": "true"} if labels is None else labels,
                                **({"annotations": annotations} if annotations else {})},
                   "spec": {"type": "LoadBalancer", "selector": selector or {"app": "homestead-objectstore"}}}
        cluster = Cluster([node("node-3")], [service, *deployments, *vms], fail=missing)
        if bind:
            self.bind(cluster, longhorn=False)
        return cluster

    def test_a_service_whose_workload_is_gone_is_removed(self):
        cluster = self.stale_cluster()
        self.assertEqual(["lab/homestead-objectstore"], PARITY.stale_services())
        self.assertEqual([("DELETE", "/api/v1/namespaces/lab/services/homestead-objectstore", None)], cluster.sent)

    def test_a_service_that_does_not_say_whose_it_is_is_left_alone(self):
        cluster = self.stale_cluster(name="old-vm-ssh")
        self.assertEqual([], PARITY.stale_services())
        self.assertEqual([], cluster.sent)

    def test_a_service_whose_named_workload_is_gone_goes_but_a_stopped_one_stays(self):
        stopped = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": {"namespace": "lab", "name": "win11"},
                   "spec": {"running": False, "template": {"metadata": {"labels": {}}}}}
        cluster = self.stale_cluster(
            name="win11-rdp", annotations={"homestead.io/workload-kind": "VirtualMachine", "homestead.io/workload": "win11"},
            selector={"vm.kubevirt.io/name": "win11"},      # on the VMI's pod, not the VM's template
            vms=[stopped])
        self.assertEqual([], PARITY.stale_services(), "the VM is stopped, not gone")
        cluster.world("DELETE", "/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/win11")
        self.assertEqual(["lab/win11-rdp"], PARITY.stale_services())

    def test_a_service_that_could_still_be_used_is_kept(self):
        stopped = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": "homestead-objectstore"},
                   "spec": {"replicas": 0, "template": {"metadata": {"labels": {"app": "homestead-objectstore"}}}}}
        cases = {"its workload is stopped, not gone": self.stale_cluster([stopped], bind=False),
                 "made minutes ago": self.stale_cluster(created="2999-01-01T00:00:00Z", bind=False),
                 "not Homestead's": self.stale_cluster(labels={}, bind=False),
                 "a read failed": self.stale_cluster(missing=("/api/v1/namespaces/lab/pods",), bind=False)}
        for why, cluster in cases.items():
            with self.subTest(why=why):
                self.bind(cluster, longhorn=False)
                self.assertEqual([], PARITY.stale_services())
                self.assertEqual([], cluster.sent)

    def test_the_installers_one_copy_rises_with_the_nodes(self):
        cluster = Cluster([node("a"), node("b"), node("c"), node("d")],
                          [chart("longhorn", INSTALLER_LONGHORN), setting("1")], host_out="MULTIPATH none\nISCSI kept\nEND\n")
        self.bind(cluster)
        PARITY.tick()
        sent = next(body for _, path, body in cluster.sent if path == f"{HELM}/longhorn")
        self.assertIn("defaultReplicaCount: 3", sent["spec"]["valuesContent"])
        self.assertIn(("PATCH", PARITY.LONGHORN_SETTING, {"value": "3"}), cluster.sent)

    def test_copies_rise_again_as_more_nodes_join_and_never_fall(self):
        cluster = Cluster([node("a"), node("b")], [chart("longhorn", INSTALLER_LONGHORN), setting("1")])
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIn("keep 2 copies", PARITY.copies_tick())
        self.assertEqual("2", cluster.object(PARITY.LONGHORN_SETTING)["value"])
        self.assertIsNone(PARITY.copies_tick(), "nothing changed: nothing asked")
        cluster.add_node("c")
        self.assertIn("keep 3 copies", PARITY.copies_tick())
        self.assertIn("defaultClassReplicaCount: 3", cluster.object(f"{HELM}/longhorn")["spec"]["valuesContent"])
        self.assertEqual("3", cluster.object(PARITY.LONGHORN_SETTING)["value"])
        cluster.remove_node("c")
        self.assertIsNone(PARITY.copies_tick(), "a node leaving lowers nothing")
        self.assertIn("defaultReplicaCount: 3", cluster.object(f"{HELM}/longhorn")["spec"]["valuesContent"])

    def test_longhorn_18_keeps_the_setting_per_data_engine_and_it_rises_too(self):
        # Longhorn 1.8 and later keep default-replica-count as JSON, one count per data engine.
        cluster = Cluster([node("a"), node("b")], [chart("longhorn", INSTALLER_LONGHORN), setting('{"v1":"1","v2":"1"}')])
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIn("keep 2 copies", PARITY.copies_tick())
        self.assertIn(("PATCH", PARITY.LONGHORN_SETTING, {"value": '{"v1":"2","v2":"2"}'}), cluster.sent)
        cluster.add_node("c")
        self.assertIn("keep 3 copies", PARITY.copies_tick())
        self.assertEqual({"v1": "3", "v2": "3"}, json.loads(cluster.object(PARITY.LONGHORN_SETTING)["value"]),
                         "Homestead's own 2 rises again")

    def test_a_data_engine_count_someone_chose_is_kept_while_the_others_rise(self):
        cluster = Cluster([node("a"), node("b")], [chart("longhorn", INSTALLER_LONGHORN), setting('{"v1":"1","v2":"3"}')])
        self.bind(cluster)
        PARITY._seen_ready = None
        PARITY.copies_tick()
        self.assertIn(("PATCH", PARITY.LONGHORN_SETTING, {"value": '{"v1":"2","v2":"3"}'}), cluster.sent)

    def setting_sent(self, value, state=None):
        """The PATCHes to the setting when two hosts store and it holds value."""
        cluster = Cluster([node("a"), node("b")], [chart("longhorn", INSTALLER_LONGHORN), setting(value)])
        self.bind(cluster)
        if state:
            Path(PARITY._path()).write_text(json.dumps(state), encoding="utf-8")
        PARITY._seen_ready = None
        self.assertIn("keep 2 copies", PARITY.copies_tick(), "the chart still rises")
        return [body for _, path, body in cluster.sent if path == PARITY.LONGHORN_SETTING]

    def test_a_setting_homestead_cannot_read_is_left_alone(self):
        for value in ('{"v1":"1"', '["1"]', '{}', "", '"1"'):
            with self.subTest(value=value):
                self.assertEqual([], self.setting_sent(value))

    def test_counts_someone_chose_or_already_raised_are_kept(self):
        for value in ('{"v1":"3","v2":"2"}', "3", '{"v1":"2","v2":"2"}', "2"):
            with self.subTest(value=value):
                self.assertEqual([], self.setting_sent(value))

    def test_a_count_homestead_set_before_is_never_lowered(self):
        # Homestead raised it to 3 on a larger cluster; Longhorn was installed
        # again since (the chart is back at 1), and two hosts store now.
        for value in ("3", '{"v1":"3","v2":"3"}'):
            with self.subTest(value=value):
                self.assertEqual([], self.setting_sent(value, state={"longhorn_copies": 3}))

    def test_other_ways_longhorn_may_write_the_json_still_rise(self):
        for value in ('{"v1":1,"v2":1}', ' {"v1": "1", "v2": "1"}\n'):
            with self.subTest(value=value):
                self.assertEqual([{"value": '{"v1":"2","v2":"2"}'}], self.setting_sent(value))

    def test_copies_rise_on_an_install_that_gave_longhorn_its_priority_and_it_is_kept(self):
        cluster = Cluster([node("a"), node("b"), node("c")],
                          [chart("longhorn", PARITY.addons.longhorn_values(1)), setting("1")])
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIn("keep 3 copies", PARITY.copies_tick())
        sent = next(body for _, path, body in cluster.sent if path == f"{HELM}/longhorn")["spec"]["valuesContent"]
        self.assertEqual(PARITY.addons.longhorn_values(3), sent)
        self.assertIn("priorityClass: system-node-critical", sent)

    def test_an_older_install_rises_without_gaining_a_priority_longhorn_would_refuse(self):
        cluster = Cluster([node("a"), node("b")], [chart("longhorn", INSTALLER_LONGHORN), setting("1")])
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIn("keep 2 copies", PARITY.copies_tick())
        sent = next(body for _, path, body in cluster.sent if path == f"{HELM}/longhorn")["spec"]["valuesContent"]
        self.assertNotIn("priorityClass", sent)

    def test_new_installer_storage_path_is_preserved_when_more_hosts_join(self):
        for path in ("/mnt/longhorn-os", "/var/lib/longhorn"):
            with self.subTest(path=path):
                values = PARITY.addons.longhorn_values(1) + f"  defaultDataPath: {path}\n"
                cluster = Cluster([node("a"), node("b"), node("c")], [chart("longhorn", values), setting("1")])
                self.bind(cluster)
                PARITY._seen_ready = None
                self.assertIn("keep 3 copies", PARITY.copies_tick())
                sent = next(body for _, target, body in cluster.sent if target == f"{HELM}/longhorn")["spec"]["valuesContent"]
                self.assertEqual(PARITY.addons.longhorn_values(3) + f"  defaultDataPath: {path}\n", sent)

    def test_the_installer_writes_the_same_longhorn_values_as_homestead(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-k3s.sh").read_text(encoding="utf-8")
        block = script.split("  valuesContent: |\n", 1)[1].split("EOF\n", 1)[0]
        # Only a fresh installation sets the default path. Reconciliation must
        # preserve an existing cluster's path rather than adopting this default.
        self.assertEqual(PARITY.addons.longhorn_values(1) + "  defaultDataPath: $LONGHORN_DATA_PATH\n", textwrap.dedent(block))

    def test_a_count_someone_set_after_homestead_is_kept(self):
        cluster = Cluster([node("a"), node("b"), node("c")], [chart("longhorn", INSTALLER_LONGHORN.replace("1", "2"))])
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIsNone(PARITY.copies_tick(), "2 was not Homestead's: it stays")
        self.assertEqual([], cluster.sent)

    def own_cluster(self, nodes, copies, lh_nodes=None):
        deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"namespace": "lab", "name": "homestead"},
                      "spec": {"template": {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "homestead-data"}}]}}}}
        claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"namespace": "lab", "name": "homestead-data"},
                 "spec": {"volumeName": "pvc-1"}}
        volume = {"apiVersion": LH_VERSION, "kind": "Volume", "metadata": {"namespace": "longhorn-system", "name": "pvc-1"},
                  "spec": {"numberOfReplicas": copies}}
        cluster = Cluster([node(n) for n in nodes], [deployment, claim, volume], lh_nodes=lh_nodes)
        self.bind(cluster, own=("lab", "homestead"))
        return cluster

    def replicas(self, cluster):
        return cluster.object(f"{PARITY.LH_VOLUMES}/pvc-1")["spec"]["numberOfReplicas"]

    def test_homesteads_own_data_volume_rises_with_the_nodes(self):
        cluster = self.own_cluster(["a", "b"], 1)
        self.assertEqual([("homestead-data", 2)], PARITY.own_copies(PARITY._ready_nodes()))
        cluster.add_node("c")
        self.assertEqual([("homestead-data", 3)], PARITY.own_copies(PARITY._ready_nodes()))
        self.assertEqual(3, self.replicas(cluster))
        cluster.remove_node("c")
        self.assertEqual([], PARITY.own_copies(PARITY._ready_nodes()), "never lowered")

    def test_a_tiebreaker_longhorn_does_not_store_on_leaves_homesteads_data_at_two_copies(self):
        # #364: a Pi kept for etcd votes is Ready, tainted, and runs no Longhorn.
        cluster = self.own_cluster(["a", "b", "pi"], 1, lh_nodes=[lh_node("a"), lh_node("b")])
        PARITY._seen_ready = None
        PARITY.copies_tick()
        self.assertEqual(2, self.replicas(cluster))

    def test_homesteads_data_volume_set_by_someone_else_is_kept(self):
        cluster = self.own_cluster(["a", "b", "c"], 2)
        self.assertEqual([], PARITY.own_copies(PARITY._ready_nodes()))
        self.assertEqual([], cluster.sent)

    def test_copies_someone_chose_are_kept(self):
        for values in ("persistence:\n  defaultClassReplicaCount: 2\ndefaultSettings:\n  defaultReplicaCount: 2\n",
                       INSTALLER_LONGHORN + "longhornUI:\n  replicas: 1\n"):
            with self.subTest(values=values):
                cluster = Cluster([node("a"), node("b")], [chart("longhorn", values)],
                                  host_out="MULTIPATH none\nISCSI kept\nEND\n")
                self.bind(cluster)
                PARITY.tick()
                self.assertEqual([], cluster.sent)

    def test_new_volumes_keep_as_many_copies_as_hosts_longhorn_stores_on(self):
        cluster = Cluster([node("a"), node("b"), node("pi")], [chart("longhorn", INSTALLER_LONGHORN), setting("1")],
                          lh_nodes=[lh_node("a"), lh_node("b")])
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIn("keep 2 copies", PARITY.copies_tick())
        self.assertIn(("PATCH", PARITY.LONGHORN_SETTING, {"value": "2"}), cluster.sent)

    def test_a_host_longhorn_may_not_schedule_on_does_not_count(self):
        for unschedulable in (lh_node("c", schedulable=False), lh_node("c", disks=({"allowScheduling": False},)), lh_node("c", disks=()),
                              lh_node("c", evicting=True), lh_node("c", disks=({"allowScheduling": True, "evictionRequested": True},))):
            with self.subTest(node=unschedulable):
                cluster = Cluster([node("a"), node("b"), node("c")], [chart("longhorn", INSTALLER_LONGHORN)],
                                  lh_nodes=[lh_node("a"), lh_node("b"), unschedulable])
                self.bind(cluster)
                PARITY._seen_ready = None
                self.assertIn("keep 2 copies", PARITY.copies_tick())

    def test_a_longhorn_record_left_behind_on_a_host_it_no_longer_runs_on_does_not_count(self):
        # Longhorn ran on the Pi once, then was moved off it: its node record
        # stays, still allowing scheduling, but is no longer Ready.
        cluster = Cluster([node("a"), node("b"), node("pi")], [chart("longhorn", INSTALLER_LONGHORN)],
                          lh_nodes=[lh_node("a"), lh_node("b"), lh_node("pi", ready=False)])
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIn("keep 2 copies", PARITY.copies_tick())

    def test_the_platform_look_counts_the_same_hosts(self):
        cluster = Cluster([node("a"), node("b"), node("pi")], [chart("longhorn", INSTALLER_LONGHORN)],
                          lh_nodes=[lh_node("a"), lh_node("b")], host_out="MULTIPATH none\nISCSI kept\nEND\n")
        self.bind(cluster)
        PARITY.tick()
        sent = next(body for _, path, body in cluster.sent if path == f"{HELM}/longhorn")
        self.assertIn("defaultReplicaCount: 2", sent["spec"]["valuesContent"])

    def test_nothing_is_raised_while_longhorn_cannot_say_where_it_stores(self):
        cluster = Cluster([node("a"), node("b"), node("c")], [chart("longhorn", INSTALLER_LONGHORN)], fail=[LH_NODES])
        self.bind(cluster)
        PARITY._seen_ready = None
        self.assertIsNone(PARITY.copies_tick())
        self.assertEqual([], cluster.sent)

    def test_one_node_keeps_one_copy(self):
        cluster = Cluster([node("a")], [chart("longhorn", INSTALLER_LONGHORN)],
                          host_out="MULTIPATH none\nISCSI kept\nEND\n")
        self.bind(cluster)
        PARITY.tick()
        self.assertEqual([], cluster.sent)

    def test_harvester_is_not_touched(self):
        cluster = Cluster([node("a")])
        self.bind(cluster, platform=lambda force=False: {"harvester": True, "distribution": "rke2"}, hostrun=False)
        self.assertEqual([], PARITY.tick())


if __name__ == "__main__":
    unittest.main()
