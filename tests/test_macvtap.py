import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_addons as ADDONS
import homestead_baseline as BASELINE
import homestead_kube as K
import homestead_macvtap as MACVTAP
import homestead_network_vms as VMNET
import homestead_vms as VMS
import homestead_vm_edit as VM_EDIT
import test_vm_network_create as network_fixture
from fakes.kube import Kube

KV = "/apis/kubevirt.io/v1/namespaces/kubevirt/kubevirts/kubevirt"
CHART_PATH = f"/apis/helm.cattle.io/v1/namespaces/{MACVTAP.NS}/helmcharts/{MACVTAP.CHART}"


class Scene:
    """KubeVirt at a version, and macvtap's DaemonSet if there, on the shared
    fake API (tests/fakes/kube.py)."""

    def __init__(self, test, kubevirt="v1.9.0", daemonset=None, binding=False, platform=None):
        self.charts = []
        self.kv = {"apiVersion": "kubevirt.io/v1", "kind": "KubeVirt", "metadata": {"name": "kubevirt", "namespace": "kubevirt"},
                   "status": {"observedKubeVirtVersion": kubevirt},
                   "spec": {"configuration": {"network": {"binding": dict(MACVTAP.BINDING)}} if binding else {}}}
        self.platform = platform or {"distribution": "k3s", "kubevirt": True, "helm_controller": True}
        self.kube = test.enterContext(Kube(self.kv, *([daemonset] if daemonset else [])))
        # The shared client's guards and the module's own binding, put back after.
        for name in ("kget", "ksend", "addons", "platform"):
            test.addCleanup(setattr, MACVTAP, name, getattr(MACVTAP, name))
        self.bind(lambda name, version, manifests, extra: {"version": version, "manifests": manifests})

    def bind(self, chart_archive):
        class Addons:
            pass
        Addons.chart_archive = staticmethod(chart_archive)
        Addons._post_chart = staticmethod(lambda name, spec: self.charts.append((name, spec)))
        Addons.preemption_hold = staticmethod(lambda what: None)
        MACVTAP.bind(K.kget, K.ksend, Addons, lambda force=False: self.platform)

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def save_kv(self):
        """KubeVirt's settings as someone left them: the stored object replaced."""
        self.kube.add(self.kv)


def daemonset(ready=True, image="quay.io/kubevirt/macvtap-cni:v0.13.2"):
    n = 1 if ready else 0
    return {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"namespace": MACVTAP.NS, "name": "macvtap-cni", "generation": 1},
            "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
            "status": {"observedGeneration": 1, "desiredNumberScheduled": 1, "updatedNumberScheduled": 1, "numberAvailable": n}}


class ManifestTests(unittest.TestCase):
    def test_the_plugin_goes_where_the_distribution_keeps_its_cni_plugins_at_a_pinned_version(self):
        k3s, rke2 = MACVTAP.manifests("k3s"), MACVTAP.manifests("rke2")
        self.assertIn("path: /var/lib/rancher/k3s/data/cni", k3s)
        self.assertIn("path: /opt/cni/bin", rke2)
        self.assertIn(f"image: {MACVTAP.IMAGE}:{MACVTAP.VERSION}", k3s)
        self.assertNotIn(":latest", k3s)
        self.assertEqual(["ConfigMap", "DaemonSet"], [ADDONS._kind(doc) for doc in ADDONS._documents(k3s)],
                         "the chart wrapper finds both objects")
        with self.assertRaises(ValueError):
            MACVTAP.manifests("k3s", "latest")


class InstallTests(unittest.TestCase):
    def test_install_wraps_the_manifests_as_a_chart_and_registers_the_binding(self):
        cluster = Scene(self)
        result = MACVTAP.install()
        self.assertEqual("homestead-macvtap", cluster.charts[0][0])
        self.assertIn("/var/lib/rancher/k3s/data/cni", cluster.charts[0][1]["chartContent"]["manifests"])
        method, path, body = cluster.sent[-1]
        self.assertEqual(("PATCH", KV), (method, path))
        self.assertEqual({"macvtap": {"domainAttachmentType": "tap"}}, body["spec"]["configuration"]["network"]["binding"])
        self.assertEqual({"disabledFeatureGates": ["ExternalNetResourceInjection"]}, body["spec"]["configuration"]["developerConfiguration"],
                         "KubeVirt 1.8 and later must read the network's device itself, or the pod never gets one")
        self.assertIn("macvtap v0.13.2", result["detail"])

    def test_a_kubevirt_still_deploying_gets_the_gate_too(self):
        """KubeVirt reports no version until it has finished deploying, and
        Add-ons installs 1.8 or later: a network added in that window must
        still switch the gate off, or its VMs never get their device (#551)."""
        cluster = Scene(self, kubevirt="")
        MACVTAP.register_binding()
        developer = cluster.sent[-1][2]["spec"]["configuration"]["developerConfiguration"]
        self.assertEqual({"disabledFeatureGates": ["ExternalNetResourceInjection"]}, developer,
                         "the gate is assumed needed while the version is unknown; the pre-1.5 gate is not")
        self.assertFalse(MACVTAP.nad_lookup(cluster.kv), "an unknown version with the gate on is not ready")
        cluster.kv["spec"]["configuration"]["developerConfiguration"] = {"disabledFeatureGates": ["ExternalNetResourceInjection"]}
        self.assertTrue(MACVTAP.nad_lookup(cluster.kv))
        cluster = Scene(self, kubevirt="", daemonset=daemonset(), binding=True)
        self.assertFalse(MACVTAP.inspect()["ready"])
        MACVTAP.install()
        self.assertEqual([], cluster.charts, "installed already: only the binding is registered")
        self.assertIn("ExternalNetResourceInjection", cluster.sent[-1][2]["spec"]["configuration"]["developerConfiguration"]["disabledFeatureGates"])

    def test_older_kubevirt_also_gets_the_binding_plugins_gate(self):
        cluster = Scene(self, kubevirt="v1.4.2")
        MACVTAP.register_binding()
        self.assertEqual(["NetworkBindingPlugins"], cluster.sent[-1][2]["spec"]["configuration"]["developerConfiguration"]["featureGates"])

    def test_ready_needs_kubevirt_to_read_the_networks_device(self):
        cluster = Scene(self, daemonset=daemonset(), binding=True)
        self.assertFalse(MACVTAP.inspect()["ready"], "1.9 with ExternalNetResourceInjection on by default")
        cluster.kv["spec"]["configuration"]["developerConfiguration"] = {"disabledFeatureGates": ["ExternalNetResourceInjection"]}
        cluster.save_kv()
        self.assertTrue(MACVTAP.inspect()["ready"])
        cluster.kv["status"]["observedKubeVirtVersion"] = "v1.7.2"
        cluster.kv["spec"]["configuration"]["developerConfiguration"] = {}
        cluster.save_kv()
        self.assertTrue(MACVTAP.inspect()["ready"], "before 1.8 KubeVirt reads it anyway")
        self.assertIn("ExternalNetResourceInjection", ADDONS.kubevirt_cr(False, None, ["ExternalNetResourceInjection"]),
                      "a KubeVirt upgrade keeps the gate off")

    def test_where_it_is_not_for(self):
        Scene(self, platform={"harvester": True, "kubevirt": True, "helm_controller": True})
        with self.assertRaisesRegex(ValueError, "Harvester"):
            MACVTAP.install()
        Scene(self, platform={"distribution": "k3s", "kubevirt": False, "helm_controller": True})
        with self.assertRaisesRegex(ValueError, "install KubeVirt first"):
            MACVTAP.install()

    def test_ready_needs_every_node_and_the_binding(self):
        Scene(self, daemonset=daemonset(), binding=False)
        state = MACVTAP.inspect()
        self.assertEqual((True, False, "v0.13.2"), (state["installed"], state["ready"], state["version"]))
        self.assertIn("not set up for it yet", state["detail"])
        Scene(self, daemonset=daemonset(ready=False), binding=True)
        self.assertFalse(MACVTAP.inspect()["ready"])
        Scene(self, daemonset=daemonset(), binding=True, kubevirt="v1.7.0")
        self.assertTrue(MACVTAP.inspect()["ready"])
        Scene(self)
        self.assertEqual((False, "absent"), (MACVTAP.inspect()["installed"], MACVTAP.inspect()["state"]))

    def test_installed_without_the_binding_only_registers_it(self):
        cluster = Scene(self, daemonset=daemonset())
        MACVTAP.install()
        self.assertEqual([], cluster.charts)
        self.assertEqual("PATCH", cluster.sent[-1][0])


class NetworkTests(network_fixture.HostInterfaceNetworkTests):
    """A plain NIC for VMs: macvtap, with the device the plugin offers."""

    def test_a_plain_nic_gives_vms_macvtap(self):
        with mock.patch.object(VMNET, "_macvtap", lambda: True):
            config = self.make(interface="eth0", **{"for": "vms"})
            self.assertEqual({"cniVersion": "0.3.1", "name": "lan", "type": "macvtap"}, config)
            self.assertEqual({"k8s.v1.cni.cncf.io/resourceName": "macvtap.network.kubevirt.io/eth0"},
                             self.sent[-1][2]["metadata"]["annotations"])
            with self.assertRaisesRegex(ValueError, "host bridge"):
                self.make(interface="eth0", vlan="20", **{"for": "vms"})
            self.assertEqual("bridge", self.make(interface="br0", **{"for": "vms"})["type"], "a bridge carries VMs as it is")

    def test_without_macvtap_it_says_how_to_get_it(self):
        with mock.patch.object(VMNET, "_macvtap", lambda: False), self.assertRaisesRegex(ValueError, "install it"):
            self.make(interface="eth0", **{"for": "vms"})

    test_options_list_the_hosts_interfaces_those_on_every_host_first = None


class VmJoinTests(unittest.TestCase):
    def join(self, config):
        nad = {"spec": {"config": json.dumps(config)}}
        with mock.patch.object(VMS.KUBE, "get_optional", lambda path, read=None: nad):
            iface, net = {"name": "nic-0", "masquerade": {}}, {"name": "nic-0", "pod": {}}
            VM_EDIT._set_network(iface, net, "default/lan")
        return iface, net

    def test_a_vm_joins_macvtap_through_its_binding_and_a_bridge_directly(self):
        iface, net = self.join({"type": "macvtap"})
        self.assertEqual(({"name": "nic-0", "binding": {"name": "macvtap"}}, {"name": "nic-0", "multus": {"networkName": "default/lan"}}),
                         (iface, net))
        self.assertEqual({"name": "nic-0", "bridge": {}}, self.join({"type": "bridge", "bridge": "br0"})[0])
        with self.assertRaisesRegex(ValueError, "containers only"):
            self.join({"type": "macvlan"})


class ChartTests(unittest.TestCase):
    def test_a_chart_without_crds_has_no_crd_file_for_helm_to_refuse(self):
        # Helm: "failed to install CRD crds/crds.yaml: resources are empty".
        import base64, io, tarfile
        names = lambda content: sorted(m.name for m in tarfile.open(fileobj=io.BytesIO(base64.b64decode(content))))
        self.assertEqual(["macvtap/Chart.yaml", "macvtap/templates/release.yaml"],
                         names(ADDONS.chart_archive("macvtap", MACVTAP.VERSION, MACVTAP.manifests("k3s"), "")))
        crd = "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: x.example.com\n"
        self.assertIn("kubevirt/crds/crds.yaml", names(ADDONS.chart_archive("kubevirt", "v1.9.0", crd + "---\n" + MACVTAP.manifests("k3s"), "kind: KubeVirt\n")))

    def test_a_failed_install_is_replaced_not_refused(self):
        cluster = Scene(self)
        cluster.kube.add({"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
                          "metadata": {"namespace": MACVTAP.NS, "name": MACVTAP.CHART}, "spec": {"chartContent": "old"}})
        cluster.bind(lambda *a: "new")
        MACVTAP.install()
        put = next(body for method, path, body in cluster.sent if method == "PUT")
        self.assertEqual("new", put["spec"]["chartContent"])
        self.assertEqual("new", cluster.kube.object(CHART_PATH)["spec"]["chartContent"])
        self.assertEqual([], cluster.charts)


class HostLimitTests(unittest.TestCase):
    def test_limits_are_raised_once_per_node_and_never_lowered(self):
        import homestead_host_limits as LIMITS
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runs = []
        answers = {"a": "RAISED 128 123938 8192 524288\n", "b": "KEPT 16384 1048576\n"}
        nodes = {"items": [{"metadata": {"name": n}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for n in "ab"]}
        host = type("H", (), {"run": staticmethod(lambda node, script, timeout=60, change="": runs.append((node, script)) or (answers[node], ""))})
        LIMITS.bind(lambda path: nodes, host, lambda force=False: {"distribution": "k3s"}, tmp.name)
        self.assertEqual({"a": "128 instances, 123938 watches -> 8192, 524288"}, LIMITS.tick())
        self.assertEqual({}, LIMITS.tick(), "once per node")
        self.assertEqual(2, len(runs))
        script = runs[0][1]
        self.assertIn('[ "$NI" -lt 8192 ] && NI=8192', script, "only raised")
        self.assertIn("/etc/sysctl.d/90-homestead.conf", script)
        LIMITS.bind(lambda path: nodes, host, lambda force=False: {"harvester": True}, tmp.name)
        self.assertEqual({}, LIMITS.tick())


    def test_raising_limits_is_a_change_and_a_held_host_is_skipped_not_the_round(self):
        import homestead_host_limits as LIMITS
        import homestead_hostrun as HOSTRUN
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        held, changes = {"a"}, []

        def run(node, script, timeout=60, change=""):
            if node in held:
                raise HOSTRUN.Busy(f"another change is running on {node}: Install 3 updates on {node}")
            changes.append(change)
            return "KEPT 16384 1048576\n", ""

        nodes = {"items": [{"metadata": {"name": n}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for n in "ab"]}
        LIMITS.bind(lambda path: nodes, type("H", (), {"run": staticmethod(run)}), lambda force=False: {"distribution": "k3s"}, tmp.name)
        LIMITS.tick()
        self.assertEqual(["Raise the inotify limits on b"], changes, "b is done though a is held (#597)")
        held.clear()
        LIMITS.tick()
        self.assertEqual(2, len(changes), "a is tried again next round")


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.platform = {"distribution": "k3s", "helm_controller": True, "load_balancer": "kube-vip", "kubevirt": False}
        self.installed = []
        self.macvtap_ready = False

        class Addons:
            status = staticmethod(lambda: {"kube_vip": {}, "multus": {"ready": True}})
            install_kube_vip = install_multus = staticmethod(lambda cfg: {})

        class Macvtap:
            inspect = staticmethod(lambda: {"ready": self.macvtap_ready, "installed": self.macvtap_ready})
            install = staticmethod(lambda cfg: self.installed.append("macvtap") or {"detail": "installing"})

        def get(path):
            return {"data": {"multus": "yes", "kube-vip": "yes"}}
        BASELINE.bind(get, Addons, lambda force=False: self.platform, "lab", self.tmp.name, Macvtap)
        self.addCleanup(BASELINE.bind, None, None, None)

    def test_macvtap_is_needed_only_where_vms_run_and_installed_once_they_can(self):
        self.assertEqual(["kube-vip", "multus"], [row["id"] for row in BASELINE.report()["parts"]])
        BASELINE.tick()
        self.assertEqual([], self.installed)
        self.assertNotIn("macvtap", BASELINE._load().get("done", {}), "not given up on before KubeVirt came")
        self.platform["kubevirt"] = True
        self.assertEqual(["macvtap"], BASELINE.report()["missing"])
        BASELINE.tick()
        self.assertEqual(["macvtap"], self.installed)
        BASELINE.tick()
        self.assertEqual(["macvtap"], self.installed, "once only")


if __name__ == "__main__":
    unittest.main()
