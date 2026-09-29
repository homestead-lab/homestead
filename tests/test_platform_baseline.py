import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_baseline as BASELINE
import homestead_components as C

KUBE_VIP_INDEX = """apiVersion: v1
entries:
  kube-vip:
  - apiVersion: v2
    appVersion: v1.3.0
    name: kube-vip
    version: 0.12.0
  - apiVersion: v2
    appVersion: v1.2.3
    name: kube-vip
    version: 0.11.1
  - apiVersion: v2
    appVersion: v1.2.2
    name: kube-vip
    version: 0.11.0
  kube-vip-cloud-provider:
  - appVersion: v0.0.12
    version: 0.2.9
"""
RKE2_INDEX = """entries:
  rke2-multus:
  - appVersion: 4.3.1
    version: v4.3.103
  - appVersion: 4.3.1
    version: v4.3.102
  rke2-multus-crd:
  - version: v4.3.001
"""
CHART = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/"
DS = "/apis/apps/v1/namespaces/kube-system/daemonsets/"


def daemonset(image):
    return {"spec": {"template": {"spec": {"containers": [{"image": image}]}}}}


class Addons:
    @staticmethod
    def fetch(url):
        return (KUBE_VIP_INDEX if "kube-vip" in url else RKE2_INDEX), url


class Cluster:
    def __init__(self, platform, objects):
        self.platform, self.objects, self.helm_calls = platform, objects, []
        C._releases.clear()
        C._apps.clear()
        C.bind(self.get, lambda *a, **k: None, lambda force=False: self.platform, self.helm_calls.append, Addons(),
               lambda url: {"data": []} if "k3s.io" in url else [])

    def get(self, path):
        if path in self.objects:
            return self.objects[path]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)


K3S = {"distribution": "k3s", "load_balancer": "kube-vip", "multus": True, "servicelb": True, "helm_controller": True}


class IndexTests(unittest.TestCase):
    def test_one_chart_is_read_from_a_repository_index(self):
        self.assertEqual([("0.12.0", "v1.3.0"), ("0.11.1", "v1.2.3"), ("0.11.0", "v1.2.2")],
                         C.chart_index(KUBE_VIP_INDEX, "kube-vip"))
        self.assertEqual([("v4.3.103", "4.3.1"), ("v4.3.102", "4.3.1")], C.chart_index(RKE2_INDEX, "rke2-multus"))
        self.assertEqual([], C.chart_index(RKE2_INDEX, "nothing"))


class NetworkRowTests(unittest.TestCase):
    def row(self, component):
        return next(r for r in C.report()["components"] if r["id"] == component)

    def test_kube_vip_and_multus_installed_by_homestead_move_on_by_chart(self):
        Cluster(K3S, {"/api/v1/nodes": {"items": []},
                      CHART + "kube-vip": {"metadata": {"name": "kube-vip"}, "spec": {"chart": "kube-vip", "version": "0.11.1"}},
                      CHART + "multus": {"metadata": {"name": "multus"}, "spec": {"chart": "rke2-multus", "version": "v4.3.102"}},
                      DS + "kube-vip": daemonset("ghcr.io/kube-vip/kube-vip:v1.2.3")})
        vip, multus = self.row("kube-vip"), self.row("multus")
        self.assertEqual(("0.11.1", "0.12.0", "helmchart", "v1.2.3"), (vip["installed"], vip["next"], vip["how"], vip["app"]))
        self.assertEqual(("v4.3.102", "v4.3.103", "helmchart"), (multus["installed"], multus["next"], multus["how"]))

    def test_an_unpinned_chart_is_read_from_the_image_it_runs(self):
        Cluster(K3S, {"/api/v1/nodes": {"items": []},
                      CHART + "kube-vip": {"metadata": {"name": "kube-vip"}, "spec": {"chart": "kube-vip"}},
                      DS + "kube-vip": daemonset("ghcr.io/kube-vip/kube-vip:v1.2.2")})
        self.assertEqual(("0.11.0", "0.11.1"), (self.row("kube-vip")["installed"], self.row("kube-vip")["next"]))

    def test_installed_another_way_is_shown_not_upgraded(self):
        Cluster(K3S, {"/api/v1/nodes": {"items": []}, DS + "kube-vip-ds": daemonset("ghcr.io/kube-vip/kube-vip:v0.8.9")})
        self.assertEqual(("manual", "v0.8.9"), (self.row("kube-vip")["how"], self.row("kube-vip")["installed"]))
        with self.assertRaisesRegex(ValueError, "installed outside Homestead"):
            C.upgrade("kube-vip", "0.12.0")

    def test_harvester_s_own_are_shown_as_harvester_s(self):
        Cluster({"harvester": True, "load_balancer": "kube-vip", "multus": True},
                {"/apis/apps/v1/namespaces/harvester-system/daemonsets/kube-vip": daemonset("ghcr.io/kube-vip/kube-vip:v0.8.1")})
        self.assertEqual("harvester", self.row("kube-vip")["how"])

    def test_upgrading_moves_the_chart_version_and_keeps_its_values(self):
        cluster = Cluster(K3S, {"/api/v1/nodes": {"items": []},
                                CHART + "kube-vip": {"metadata": {"name": "kube-vip"}, "spec": {"chart": "kube-vip", "version": "0.11.1"}}})
        result = C.upgrade("kube-vip", "0.12.0")
        self.assertEqual([{"namespace": "kube-system", "name": "kube-vip", "version": "0.12.0"}], cluster.helm_calls)
        self.assertIn("0.12.0 (v1.3.0)", result["detail"])


class FakeAddons:
    def __init__(self, status):
        self._status, self.installed = status, []

    def status(self):
        return self._status

    def install_kube_vip(self, cfg):
        self.installed.append(("kube-vip", cfg))
        return {"detail": "kube-vip is being installed", "job": "helm-install-kube-vip"}

    def install_multus(self, cfg):
        self.installed.append(("multus", cfg))
        return {"detail": "Multus is being installed", "job": "helm-install-multus"}


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.request = {"data": {"kube-vip": "yes", "kube-vip-version": "", "multus": "yes", "multus-version": "v4.3.101"}}
        self.platform = {"distribution": "k3s", "helm_controller": True, "load_balancer": "servicelb", "multus": False}
        self.addons = FakeAddons({"kube_vip": {"installing": False}, "multus": {"installed": False, "installing": False}})
        BASELINE.bind(self.get, self.addons, lambda force=False: self.platform, "lab", self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path):
        if path == "/api/v1/namespaces/lab/configmaps/homestead-install" and self.request:
            return self.request
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def test_the_installer_s_request_is_installed_once(self):
        results = BASELINE.tick()
        self.assertEqual([("kube-vip", {}), ("multus", {"version": "v4.3.101"})], self.addons.installed)
        self.assertTrue(all(row["ok"] for row in results))
        self.assertEqual([], BASELINE.tick(), "asked once: a part removed later stays removed")
        done = json.loads(Path(self.tmp.name, "baseline.json").read_text())["done"]
        self.assertEqual({"kube-vip", "multus"}, set(done))

    def test_what_is_there_already_is_not_installed(self):
        self.platform["load_balancer"] = "metallb"
        BASELINE.tick()
        self.assertEqual(["multus"], [part for part, _ in self.addons.installed])

    def test_no_request_no_install(self):
        self.request = None
        self.assertEqual([], BASELINE.tick())
        self.assertEqual([], self.addons.installed)

    def test_harvester_and_plain_kubernetes_are_left_alone(self):
        for platform in ({"harvester": True, "distribution": "harvester", "helm_controller": True},
                         {"distribution": "kubernetes", "helm_controller": False}):
            self.platform = platform
            self.assertEqual([], BASELINE.tick())
            self.assertFalse(BASELINE.report()["applies"])
        self.assertEqual([], self.addons.installed)

    def test_the_report_lists_what_is_missing(self):
        report = BASELINE.report()
        self.assertEqual((True, ["kube-vip", "multus"]), (report["applies"], report["missing"]))
        self.addons._status["multus"]["installing"] = True
        self.assertEqual(["kube-vip"], BASELINE.report()["missing"])

    def test_the_node_probe_the_installer_asked_for_is_installed_once(self):
        made = []
        probe = type("P", (), {"installed": staticmethod(lambda: "homestead-nodeprobe" if made else ""),
                               "install": staticmethod(lambda version: made.append(version) or {"detail": "installed"})})
        BASELINE.bind(self.get, self.addons, lambda force=False: self.platform, "lab", self.tmp.name, None, probe, "2.8.235")
        self.request["data"]["node-probe"] = "yes"
        BASELINE.tick()
        BASELINE.tick()
        self.assertEqual(["2.8.235"], made, "asked once: a probe removed later stays removed")

    def test_no_node_probe_unless_asked(self):
        made = []
        probe = type("P", (), {"installed": staticmethod(lambda: ""), "install": staticmethod(made.append)})
        BASELINE.bind(self.get, self.addons, lambda force=False: self.platform, "lab", self.tmp.name, None, probe)
        BASELINE.tick()
        self.assertEqual([], made)

    def test_a_failed_install_is_recorded_with_its_reason(self):
        def refuse(cfg):
            raise ValueError("kube-vip is already being installed")
        self.addons.install_kube_vip = refuse
        results = BASELINE.install(["kube-vip"])
        self.assertEqual((False, "kube-vip is already being installed"), (results[0]["ok"], results[0]["detail"]))


if __name__ == "__main__":
    unittest.main()
