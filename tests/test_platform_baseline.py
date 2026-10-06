import json
import base64
import gzip
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
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


class NetworkUpgradeStatusTests(unittest.TestCase):
    def seed(self, component="multus", namespace="kube-system"):
        self.component = component
        self.target = "v4.3.103" if component == "multus" else "0.12.0"
        self.previous = "v4.3.102" if component == "multus" else "0.11.1"
        self.name = "Multus" if component == "multus" else "kube-vip"
        self.namespace = namespace
        self.item = {"ref": {"component": component, "name": self.name, "from": self.previous, "to": self.target}}
        self.secret_path = (f"/api/v1/namespaces/{namespace}/secrets?labelSelector="
                            + C.urllib.parse.quote(f"owner=helm,name={component}", safe=""))
        self.agent_path = DS.replace("kube-system", namespace) + component
        agent = daemonset("example.test/network-agent:4.3.1")
        agent["metadata"] = {"name": component, "generation": 2}
        agent["status"] = {"observedGeneration": 2, "desiredNumberScheduled": 3,
                           "updatedNumberScheduled": 3, "numberAvailable": 3}
        self.cluster = Cluster(K3S, {
            CHART + component: {"metadata": {"name": component}, "spec": {
                "chart": "rke2-multus" if component == "multus" else component,
                "version": self.target, "targetNamespace": namespace}},
            self.agent_path: agent,
            "/apis/k8s.cni.cncf.io/v1/network-attachment-definitions": {"items": []}})
        self.release(self.target)

    def release(self, version, state="deployed", revision=2):
        record = {"name": self.component, "namespace": self.namespace, "version": revision,
                  "chart": {"metadata": {"name": "rke2-multus" if self.component == "multus" else self.component,
                                         "version": version, "appVersion": "4.3.1" if self.component == "multus" else "v1.3.0"}},
                  "info": {"status": state}}
        encoded = base64.b64encode(base64.b64encode(gzip.compress(json.dumps(record).encode()))).decode()
        secret = {"metadata": {"labels": {"version": str(revision)}}, "data": {"release": encoded}}
        self.cluster.objects[self.secret_path] = {"items": [secret]}
        return secret

    def test_both_network_upgrades_complete_without_reading_cdi(self):
        for component in ("multus", "kube-vip"):
            with self.subTest(component=component):
                self.seed(component)
                with patch.object(C, "cdi_version", side_effect=AssertionError("CDI is unrelated")):
                    state, progress, message = C.status(self.item)
                self.assertEqual(("succeeded", 100), (state, progress))
                self.assertIn(f"chart {self.target}", message)
                self.assertIn("all 3 scheduled nodes", message)

    def test_requested_version_and_unchanged_multus_image_do_not_prove_chart_revision(self):
        self.seed()
        self.release(self.previous)
        state, progress, message = C.status(self.item)
        self.assertEqual(("running", 20), (state, progress))
        self.assertIn(f"chart {self.previous}", message)
        self.assertIn(self.target, message)
        self.assertNotIn("1.66.1", message)

    def test_newest_pending_release_does_not_fall_back_to_older_deployed_revision(self):
        self.seed()
        older = self.release(self.target, revision=1)
        newest = self.release(self.target, "pending-upgrade", revision=2)
        self.cluster.objects[self.secret_path] = {"items": [older, newest]}
        self.assertEqual("running", C.status(self.item)[0])

    def test_v_prefix_difference_is_normalized_for_deployed_chart(self):
        self.seed()
        self.release(self.target.lstrip("v"))
        self.assertEqual("succeeded", C.status(self.item)[0])

    def test_missing_or_unreadable_release_does_not_complete(self):
        self.seed()
        for secrets in ([], [{"metadata": {"labels": {"version": "2"}}, "data": {"release": "invalid"}}]):
            with self.subTest(secrets=secrets):
                self.cluster.objects[self.secret_path] = {"items": secrets}
                self.assertEqual("running", C.status(self.item)[0])

    def test_release_api_denial_does_not_complete_or_leak_error_body(self):
        self.seed()
        original = self.cluster.get
        def get(path):
            if path == self.secret_path:
                raise urllib.error.HTTPError(path, 403, "private error body", {}, None)
            return original(path)
        with patch.object(C, "kget", side_effect=get):
            state, _, message = C.status(self.item)
        self.assertEqual("running", state)
        self.assertNotIn("private error body", message)

    def test_an_unrelated_chart_or_release_identity_does_not_complete(self):
        for field in ("name", "namespace", "chart"):
            with self.subTest(field=field):
                self.seed()
                secret = self.cluster.objects[self.secret_path]["items"][0]
                record = C.HELM.decode(secret)
                if field == "chart":
                    record["chart"]["metadata"]["name"] = "other-chart"
                else:
                    record[field] = "other"
                secret["data"]["release"] = base64.b64encode(base64.b64encode(gzip.compress(json.dumps(record).encode()))).decode()
                self.assertEqual("running", C.status(self.item)[0])

    def test_deployed_chart_waits_for_observed_updated_available_agents(self):
        for change in ({"observedGeneration": 1}, {"updatedNumberScheduled": 2}, {"numberAvailable": 2},
                       {"desiredNumberScheduled": 0, "updatedNumberScheduled": 0, "numberAvailable": 0}):
            with self.subTest(change=change):
                self.seed()
                self.cluster.objects[self.agent_path]["status"].update(change)
                self.assertEqual("running", C.status(self.item)[0])

    def test_missing_or_deleting_agents_and_missing_multus_api_do_not_complete(self):
        for missing in ("agent", "api", "deleting"):
            with self.subTest(missing=missing):
                self.seed()
                if missing == "deleting":
                    self.cluster.objects[self.agent_path]["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
                else:
                    self.cluster.objects.pop(self.agent_path if missing == "agent" else
                                             "/apis/k8s.cni.cncf.io/v1/network-attachment-definitions")
                self.assertEqual("running", C.status(self.item)[0])

    def test_alternate_agent_name_and_target_namespace_are_supported(self):
        self.seed(namespace="network-system")
        self.cluster.objects[self.agent_path.replace("/daemonsets/multus", "/daemonsets/rke2-multus")] = self.cluster.objects.pop(self.agent_path)
        self.assertEqual("succeeded", C.status(self.item)[0])

    def test_failed_release_and_active_retry_are_distinguished(self):
        self.seed()
        self.release(self.target, "failed")
        self.assertEqual("failed", C.status(self.item)[0])
        self.cluster.objects["/apis/batch/v1/namespaces/kube-system/jobs/helm-install-multus"] = {"status": {"active": 1}}
        self.assertEqual(("running", 50), C.status(self.item)[:2])

    def test_controller_job_failure_uses_its_reported_job_name(self):
        self.seed()
        self.release(self.previous)
        self.cluster.objects[CHART + self.component]["status"] = {"jobName": "helm-install-network"}
        self.cluster.objects["/apis/batch/v1/namespaces/kube-system/jobs/helm-install-network"] = {"status": {"failed": 1}}
        with patch.object(C, "_elapsed", return_value=121):
            state, _, message = C.status(self.item)
        self.assertEqual("failed", state)
        self.assertIn("helm-install-network", message)

    def test_unknown_component_never_falls_through_to_cdi(self):
        self.seed()
        with patch.object(C, "cdi_version", side_effect=AssertionError("CDI is unrelated")):
            self.assertEqual("failed", C.status({"ref": {"component": "other", "to": "1.0.0"}})[0])


class FakeAddons:
    Held = type("Held", (ValueError,), {})

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

    def test_node_probe_is_automatic_on_existing_clusters_and_all_distributions(self):
        made = []
        probe = type("P", (), {"installed": staticmethod(lambda: ""),
                               "install": staticmethod(lambda version: made.append(version) or {"detail": "installed"})})
        self.request = None
        for distribution in ("k3s", "rke2", "harvester", "kubernetes"):
            with self.subTest(distribution=distribution), tempfile.TemporaryDirectory() as directory:
                self.platform = {"distribution": distribution, "harvester": distribution == "harvester"}
                BASELINE.bind(self.get, self.addons, lambda force=False: self.platform, "lab", directory, None, probe, "2.8.283")
                self.assertTrue(BASELINE.probe_tick()["ok"])
                self.assertIsNone(BASELINE.probe_tick(), "a later intentional removal stays removed")
        self.assertEqual(["2.8.283"] * 4, made)

    def test_node_probe_retries_failed_installation_and_respects_explicit_opt_out(self):
        attempts = []
        def install(version):
            attempts.append(version)
            if len(attempts) == 1:
                raise ValueError("API temporarily unavailable")
            return {"detail": "installed"}
        probe = type("P", (), {"installed": staticmethod(lambda: ""), "install": staticmethod(install)})
        BASELINE.bind(self.get, self.addons, lambda force=False: self.platform, "lab", self.tmp.name, None, probe)
        with patch.dict(BASELINE.os.environ, {"HOMESTEAD_NODEPROBE_AUTO_INSTALL": "false"}):
            self.assertIsNone(BASELINE.probe_tick(), "Helm opt-out is preserved")
        self.request["data"]["node-probe"] = "no"
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual([], attempts)
        self.request["data"].pop("node-probe")
        self.assertFalse(BASELINE.probe_tick()["ok"])
        self.assertTrue(BASELINE.probe_tick()["ok"])
        self.assertIsNone(BASELINE.probe_tick())
        self.assertEqual(2, len(attempts))

    def test_a_failed_install_is_recorded_with_its_reason(self):
        def refuse(cfg):
            raise ValueError("kube-vip is already being installed")
        self.addons.install_kube_vip = refuse
        results = BASELINE.install(["kube-vip"])
        self.assertEqual((False, "kube-vip is already being installed"), (results[0]["ok"], results[0]["detail"]))

    def test_a_part_held_back_to_spare_longhorns_volumes_is_asked_again(self):
        held, original = [True], self.addons.install_kube_vip
        def maybe(cfg):
            if held[0]:
                raise FakeAddons.Held("kube-vip is waiting: node-1 has 0m CPU unrequested")
            return original(cfg)
        self.addons.install_kube_vip = maybe
        first = {row["id"]: row for row in BASELINE.tick()}
        self.assertTrue(first["kube-vip"]["held"])
        self.assertNotIn("kube-vip", BASELINE._load().get("done", {}), "held is not tried: it is not recorded")
        self.assertEqual(["multus"], [part for part, _ in self.addons.installed])
        held[0] = False
        self.assertEqual(["kube-vip"], [row["id"] for row in BASELINE.tick()])
        self.assertEqual(["multus", "kube-vip"], [part for part, _ in self.addons.installed])
        self.assertEqual([], BASELINE.tick(), "and then, as any part, never again")


if __name__ == "__main__":
    unittest.main()
