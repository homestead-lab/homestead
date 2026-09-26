"""Longhorn and KubeVirt added to a cluster that lacks them."""
import base64
import io
import json
import sys
import tarfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_addons as ADDONS
import homestead_k3scluster as K3S

OPERATOR = """---
apiVersion: v1
kind: Namespace
metadata:
  name: kubevirt
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: kubevirts.kubevirt.io
spec:
  description: "a {{ braced }} word"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: virt-operator
  namespace: kubevirt
  annotations:
    note: "{{ not a template }}"
"""


class Fake:
    def __init__(self, platform=None, kvm=None, nodes=2):
        self.platform = {"distribution": "k3s", "harvester": False, "helm_controller": True,
                         "longhorn": False, "kubevirt": False, "cdi": False, **(platform or {})}
        self.kvm = kvm if kvm is not None else {"node1": True, "node2": True}
        self.nodes = nodes
        self.sent = []
        self.fetched = []
        ADDONS.bind(self.get, self.send, lambda force=False: dict(self.platform),
                    lambda: {n: {"kvm": on} for n, on in self.kvm.items()}, self.fetch)

    def get(self, path):
        if path.endswith("/helmcharts"):
            return {"items": [body for _, _, body in self.sent]}
        if path == "/api/v1/nodes":
            return {"items": [{}] * self.nodes}
        if path == ADDONS.MULTUS.NAD_LIST:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        raise AssertionError(path)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        return body

    def fetch(self, url):
        self.fetched.append(url)
        if url.endswith("stable.txt"):
            return "v1.9.0\n", url
        if url.endswith("/releases/latest"):
            return "", "https://github.com/kubevirt/containerized-data-importer/releases/tag/v1.62.0"
        return OPERATOR, url


def unpack(chart):
    files = {}
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(chart)), mode="r:gz") as tar:
        for member in tar.getmembers():
            files[member.name] = tar.extractfile(member).read().decode()
    return files


class KubeVirtTests(unittest.TestCase):
    def test_kubevirt_and_cdi_become_charts_with_their_crds_first(self):
        fake = Fake()
        result = ADDONS.install_kubevirt()

        self.assertEqual(("v1.9.0", "v1.62.0", False), (result["kubevirt"], result["cdi"], result["emulation"]))
        charts = {body["metadata"]["name"]: body for _, _, body in fake.sent}
        self.assertEqual({"homestead-kubevirt", "homestead-cdi"}, set(charts))
        files = unpack(charts["homestead-kubevirt"]["spec"]["chartContent"])
        self.assertIn("kind: CustomResourceDefinition", files["kubevirt/crds/crds.yaml"])
        self.assertNotIn("CustomResourceDefinition", files["kubevirt/templates/release.yaml"])
        # Helm reads templates, so braces in the release's own text are kept as text.
        self.assertIn('{{ "{{" }} not a template }}', files["kubevirt/templates/release.yaml"])
        self.assertIn("{{ braced }}", files["kubevirt/crds/crds.yaml"])
        switch = json.loads(files["kubevirt/templates/switch-on.yaml"])
        self.assertEqual({"featureGates": []}, switch["spec"]["configuration"]["developerConfiguration"])
        self.assertIn("version: 1.9.0", files["kubevirt/Chart.yaml"])

    def test_no_hardware_virtualisation_means_emulation(self):
        fake = Fake(kvm={"node1": False})
        ADDONS.install_kubevirt()
        files = unpack(fake.sent[0][2]["spec"]["chartContent"])
        self.assertTrue(json.loads(files["kubevirt/templates/switch-on.yaml"])["spec"]["configuration"]
                        ["developerConfiguration"]["useEmulation"])

    def test_unknown_hardware_virtualisation_uses_safe_fallback(self):
        fake = Fake(kvm={})
        result = ADDONS.install_kubevirt()
        files = unpack(fake.sent[0][2]["spec"]["chartContent"])
        self.assertTrue(result["emulation"])
        self.assertTrue(json.loads(files["kubevirt/templates/switch-on.yaml"])["spec"]["configuration"]
                        ["developerConfiguration"]["useEmulation"])

    def test_cdi_falls_back_to_githubs_api(self):
        fake = Fake()

        def fetch(url):
            if url.endswith("/releases/latest") and "api.github.com" not in url:
                raise OSError("blocked")
            if "api.github.com" in url:
                return json.dumps({"tag_name": "v1.61.2"}), url
            return Fake.fetch(fake, url)
        ADDONS.fetch = fetch
        self.assertEqual(("v1.9.0", "v1.61.2"), ADDONS.latest_versions())

    def test_refused_where_it_cannot_or_need_not(self):
        Fake(platform={"harvester": True})
        with self.assertRaisesRegex(ValueError, "Harvester includes"):
            ADDONS.install_kubevirt()
        Fake(platform={"helm_controller": False})
        with self.assertRaisesRegex(ValueError, "no Helm controller"):
            ADDONS.install_kubevirt()
        Fake(platform={"kubevirt": True})
        with self.assertRaisesRegex(ValueError, "installed already"):
            ADDONS.install_kubevirt()

    def test_software_fallback_can_be_enabled_after_install(self):
        fake = Fake(platform={"kubevirt": True})
        result = ADDONS.set_kubevirt_emulation(True)
        self.assertTrue(result["emulation"])
        self.assertEqual(
            ("PATCH", ADDONS.KUBEVIRT_CR,
             {"spec": {"configuration": {"developerConfiguration": {"useEmulation": True}}}}),
            fake.sent[-1],
        )


class LonghornTests(unittest.TestCase):
    def test_one_copy_per_node_up_to_three(self):
        for nodes, copies in ((1, 1), (2, 2), (5, 3)):
            fake = Fake(nodes=nodes)
            result = ADDONS.install_longhorn()
            spec = fake.sent[0][2]["spec"]
            self.assertEqual(copies, result["copies"])
            self.assertIn(f"defaultReplicaCount: {copies}", spec["valuesContent"])
            self.assertEqual(("longhorn", "longhorn-system"), (spec["chart"], spec["targetNamespace"]))

    def test_status_says_what_is_there(self):
        Fake(kvm={"node1": True, "node2": False})
        status = ADDONS.status()
        self.assertEqual((True, False, False), (status["kvm_known"], status["kvm_everywhere"], status["kvm_nowhere"]))
        self.assertFalse(status["longhorn"]["installed"])


class ClusterCreatorTests(unittest.TestCase):
    CFG = {"name": "lab", "servers": 1, "agents": 1, "network": "lan", "addresses": ["192.168.1.50", "192.168.1.51"],
           "password": "long enough pass", "setup": "homestead"}

    def test_kubevirt_is_asked_of_the_first_server_only(self):
        built = K3S.plan(dict(self.CFG, kubevirt=True))
        first = K3S.user_data(built["nodes"][0], built["first"], "tok", "pw", built["setup"], "", built["kubevirt"])
        agent = K3S.user_data(built["nodes"][1], built["first"], "tok", "pw", built["setup"], "", built["kubevirt"])
        self.assertIn("server --kubevirt", first)
        self.assertNotIn("--kubevirt", agent)

    def test_nodes_get_the_hosts_cpu_for_vms_inside(self):
        made = []
        K3S.bind(None, made.append, lambda address: "")

        class Ops:
            def start(self, *args):
                return {"id": "op"}
        K3S.start(dict(self.CFG, kubevirt=True), Ops())
        self.assertEqual(["host-passthrough"] * 2, [vm.get("cpu_model") for vm in made])
        made.clear()
        K3S.start(dict(self.CFG), Ops())
        self.assertEqual([None, None], [vm.get("cpu_model") for vm in made])

    def test_k3s_alone_takes_no_kubevirt(self):
        with self.assertRaisesRegex(ValueError, "k3s alone"):
            K3S.plan(dict(self.CFG, setup="k3s", kubevirt=True))


class MultusTests(unittest.TestCase):
    """A LAN network needs Multus, which k3s leaves out: an add-on now."""

    def test_k3s_gets_the_chart_with_its_own_cni_folders(self):
        fake = Fake()
        result = ADDONS.install_multus()
        method, path, body = fake.sent[-1]
        self.assertEqual("rke2-multus-crd", fake.sent[0][2]["spec"]["chart"])
        self.assertEqual(("multus", "rke2-multus", "https://rke2-charts.rancher.io"),
                         (body["metadata"]["name"], body["spec"]["chart"], body["spec"]["repo"]))
        values = body["spec"]["valuesContent"]
        self.assertIn("confDir: /var/lib/rancher/k3s/agent/etc/cni/net.d", values)
        self.assertIn("binDir: /var/lib/rancher/k3s/data/cni/", values)
        self.assertEqual("helm-install-multus", result["job"])

    def test_status_gives_a_pasteable_k3s_log_command(self):
        Fake()
        status = ADDONS.status()["multus"]
        command = status["diagnostic_command"]
        self.assertIn("sudo k3s kubectl -n kube-system", command)
        self.assertIn("logs job/helm-install-multus --all-containers --tail=200", command)

    def test_rke2_log_command_uses_its_real_kubeconfig(self):
        command = ADDONS.diagnostic_command("rke2")
        self.assertIn("KUBECONFIG=/etc/rancher/rke2/rke2.yaml", command)
        self.assertIn("/var/lib/rancher/rke2/bin/kubectl", command)

    def test_rke2_keeps_the_charts_own_folders(self):
        fake = Fake({"distribution": "rke2"})
        ADDONS.install_multus()
        self.assertNotIn("k3s", fake.sent[-1][2]["spec"]["valuesContent"])

    def test_it_is_refused_where_it_is_there_or_cannot_go(self):
        with self.assertRaisesRegex(ValueError, "includes Multus"):
            Fake({"harvester": True}) and ADDONS.install_multus()
        with self.assertRaisesRegex(ValueError, "on k3s and RKE2"):
            Fake({"distribution": "kubernetes"}) and ADDONS.install_multus()
        fake = Fake()
        fake.get = lambda path: ({} if path == ADDONS.MULTUS.NAD_LIST else
                                {"items": [{"metadata": {"name": "multus"}}]} if path == ADDONS.MULTUS.DAEMONSETS else Fake.get(fake, path))
        ADDONS.bind(fake.get, fake.send, lambda force=False: dict(fake.platform), lambda: {}, fake.fetch)
        with self.assertRaisesRegex(ValueError, "installed already"):
            ADDONS.install_multus()
        status = ADDONS.status()["multus"]
        self.assertTrue(status["installed"])
        self.assertFalse(status["installing"])
        self.assertFalse(status["ready"])


if __name__ == "__main__":
    unittest.main()
