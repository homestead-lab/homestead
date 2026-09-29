import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_addons as ADDONS
import homestead_baseline as BASELINE
import homestead_macvtap as MACVTAP
import homestead_networking as NET
import homestead_vms as VMS
import test_vm_network_create as network_fixture

KV = "/apis/kubevirt.io/v1/namespaces/kubevirt/kubevirts/kubevirt"


def missing(path):
    raise urllib.error.HTTPError(path, 404, "missing", {}, None)


class Cluster:
    """KubeVirt at a version, and macvtap's DaemonSet if there."""

    def __init__(self, kubevirt="v1.9.0", daemonset=None, binding=False, platform=None):
        self.sent, self.charts = [], []
        self.kv = {"metadata": {"name": "kubevirt", "namespace": "kubevirt"}, "status": {"observedKubeVirtVersion": kubevirt},
                   "spec": {"configuration": {"network": {"binding": dict(MACVTAP.BINDING)}} if binding else {}}}
        self.daemonset = daemonset
        self.platform = platform or {"distribution": "k3s", "kubevirt": True, "helm_controller": True}

        class Addons:
            chart_archive = staticmethod(lambda name, version, manifests, extra: {"version": version, "manifests": manifests})
            _post_chart = staticmethod(lambda name, spec: self.charts.append((name, spec)))
        MACVTAP.bind(self.get, self.send, Addons, lambda force=False: self.platform)

    def get(self, path):
        if path == MACVTAP.KUBEVIRTS:
            return {"items": [self.kv]}
        if path == MACVTAP.DAEMONSET and self.daemonset:
            return self.daemonset
        return missing(path)

    def send(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, body))


def daemonset(ready=True, image="quay.io/kubevirt/macvtap-cni:v0.13.2"):
    n = 1 if ready else 0
    return {"metadata": {"generation": 1}, "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
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
        cluster = Cluster()
        result = MACVTAP.install()
        self.assertEqual("homestead-macvtap", cluster.charts[0][0])
        self.assertIn("/var/lib/rancher/k3s/data/cni", cluster.charts[0][1]["chartContent"]["manifests"])
        method, path, body = cluster.sent[-1]
        self.assertEqual(("PATCH", KV), (method, path))
        self.assertEqual({"macvtap": {"domainAttachmentType": "tap"}}, body["spec"]["configuration"]["network"]["binding"])
        self.assertNotIn("developerConfiguration", body["spec"]["configuration"], "KubeVirt 1.5 and later need no gate")
        self.assertIn("macvtap v0.13.2", result["detail"])

    def test_older_kubevirt_also_gets_the_binding_plugins_gate(self):
        cluster = Cluster(kubevirt="v1.4.2")
        MACVTAP.register_binding()
        self.assertEqual(["NetworkBindingPlugins"], cluster.sent[-1][2]["spec"]["configuration"]["developerConfiguration"]["featureGates"])

    def test_where_it_is_not_for(self):
        Cluster(platform={"harvester": True, "kubevirt": True, "helm_controller": True})
        with self.assertRaisesRegex(ValueError, "Harvester"):
            MACVTAP.install()
        Cluster(platform={"distribution": "k3s", "kubevirt": False, "helm_controller": True})
        with self.assertRaisesRegex(ValueError, "install KubeVirt first"):
            MACVTAP.install()

    def test_ready_needs_every_node_and_the_binding(self):
        Cluster(daemonset=daemonset(), binding=False)
        state = MACVTAP.inspect()
        self.assertEqual((True, False, "v0.13.2"), (state["installed"], state["ready"], state["version"]))
        self.assertIn("binding is not registered", state["detail"])
        Cluster(daemonset=daemonset(ready=False), binding=True)
        self.assertFalse(MACVTAP.inspect()["ready"])
        Cluster(daemonset=daemonset(), binding=True)
        self.assertTrue(MACVTAP.inspect()["ready"])
        Cluster()
        self.assertEqual((False, "absent"), (MACVTAP.inspect()["installed"], MACVTAP.inspect()["state"]))

    def test_installed_without_the_binding_only_registers_it(self):
        cluster = Cluster(daemonset=daemonset())
        MACVTAP.install()
        self.assertEqual([], cluster.charts)
        self.assertEqual("PATCH", cluster.sent[-1][0])


class NetworkTests(network_fixture.HostInterfaceNetworkTests):
    """A plain NIC for VMs: macvtap, with the device the plugin offers."""

    def test_a_plain_nic_gives_vms_macvtap(self):
        with mock.patch.object(NET, "_macvtap", lambda: True):
            config = self.make(interface="eth0", **{"for": "vms"})
            self.assertEqual({"cniVersion": "0.3.1", "name": "lan", "type": "macvtap"}, config)
            self.assertEqual({"k8s.v1.cni.cncf.io/resourceName": "macvtap.network.kubevirt.io/eth0"},
                             self.sent[-1][2]["metadata"]["annotations"])
            with self.assertRaisesRegex(ValueError, "host bridge"):
                self.make(interface="eth0", vlan="20", **{"for": "vms"})
            self.assertEqual("bridge", self.make(interface="br0", **{"for": "vms"})["type"], "a bridge carries VMs as it is")

    def test_without_macvtap_it_says_how_to_get_it(self):
        with mock.patch.object(NET, "_macvtap", lambda: False), self.assertRaisesRegex(ValueError, "install it"):
            self.make(interface="eth0", **{"for": "vms"})

    test_options_list_the_hosts_interfaces_those_on_every_host_first = None


class VmJoinTests(unittest.TestCase):
    def join(self, config):
        nad = {"spec": {"config": json.dumps(config)}}
        with mock.patch.object(VMS, "_optional", lambda path: nad):
            iface, net = {"name": "nic-0", "masquerade": {}}, {"name": "nic-0", "pod": {}}
            VMS._set_network(iface, net, "default/lan")
        return iface, net

    def test_a_vm_joins_macvtap_through_its_binding_and_a_bridge_directly(self):
        iface, net = self.join({"type": "macvtap"})
        self.assertEqual(({"name": "nic-0", "binding": {"name": "macvtap"}}, {"name": "nic-0", "multus": {"networkName": "default/lan"}}),
                         (iface, net))
        self.assertEqual({"name": "nic-0", "bridge": {}}, self.join({"type": "bridge", "bridge": "br0"})[0])
        with self.assertRaisesRegex(ValueError, "containers only"):
            self.join({"type": "macvlan"})


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
