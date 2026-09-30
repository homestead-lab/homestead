import sys, time, unittest, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_components as C

K3S_CHANNELS = {"data": [{"name": "stable", "latest": "v1.33.4+k3s1"}, {"name": "latest", "latest": "v1.34.1+k3s1"},
                         {"name": "v1.31", "latest": "v1.31.12+k3s1"}, {"name": "v1.32", "latest": "v1.32.8+k3s1"},
                         {"name": "v1.33", "latest": "v1.33.4+k3s1"}, {"name": "testing", "latest": "v1.34.2-rc1+k3s1"}]}
LONGHORN = [{"tag_name": "v1.10.0-rc1", "prerelease": True}, {"tag_name": "v1.9.1"}, {"tag_name": "v1.9.0"},
            {"tag_name": "v1.8.2"}, {"tag_name": "v1.8.1"}, {"tag_name": "v1.7.3"}]


class VersionTests(unittest.TestCase):
    def test_one_minor_at_a_time(self):
        available = ["v1.9.1", "v1.9.0", "v1.8.2", "v1.8.1", "v1.7.3"]
        self.assertEqual("v1.7.3", C.next_step("v1.7.1", available), "the newest patch of this minor first")
        self.assertEqual("v1.8.2", C.next_step("v1.7.3", available), "then the next minor, never two")
        self.assertIsNone(C.next_step("v1.9.1", available))
        self.assertEqual("v1.32.8+k3s1", C.next_step("v1.31.12+k3s1", ["v1.33.4+k3s1", "v1.32.8+k3s1"]))
        self.assertEqual("v1.31.12+k3s2", C.next_step("v1.31.12+k3s1", ["v1.31.12+k3s2"]))

    def test_test_builds_are_not_releases(self):
        self.assertIsNone(C.parse("v1.10.0-rc1"))
        self.assertEqual((1, 31, 4, 1), C.parse("v1.31.4+k3s1"))
        self.assertEqual((1, 31, 4, 2), C.parse("v1.31.4+rke2r2"))


class Cluster:
    def __init__(self, platform, objects):
        self.platform, self.objects, self.sent = platform, objects, []
        C._releases.clear()
        C.bind(self.get, self.send, lambda force=False: self.platform, self.helm, Addons(),
               lambda url: K3S_CHANNELS if "k3s.io" in url else LONGHORN)
        self.helm_calls = []

    def get(self, path):
        if path in self.objects:
            return self.objects[path]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        return body

    def helm(self, cfg):
        self.helm_calls.append(cfg)


class Addons:
    KUBEVIRT, CDI = "https://kv", "https://cdi"

    @staticmethod
    def fetch(url):
        if url.endswith("/latest"):
            return "", "https://github.com/rancher/system-upgrade-controller/releases/tag/v0.16.0"
        return f"kind: Thing\n# {url}\n", url

    @staticmethod
    def chart_archive(name, version, manifests, extra):
        return f"archive:{name}:{version}"

    kubevirt_cr = staticmethod(lambda emulation: f"kubevirt emulation={emulation}")
    cdi_cr = staticmethod(lambda: "cdi")


NODES = {"items": [
    {"metadata": {"name": "node-1"}, "status": {"nodeInfo": {"kubeletVersion": "v1.31.12+k3s1"}}},
    {"metadata": {"name": "node-2"}, "status": {"nodeInfo": {"kubeletVersion": "v1.31.12+k3s1"}}}]}
LH_SETTING = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/settings/current-longhorn-version"
LH_CHART = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/longhorn"


class K3sTests(unittest.TestCase):
    def setUp(self):
        self.c = Cluster({"distribution": "k3s", "longhorn": True, "version": "1.31.12+k3s1"},
                         {"/api/v1/nodes": NODES, LH_SETTING: {"value": "v1.8.1"},
                          LH_CHART: {"metadata": {"name": "longhorn"}, "spec": {"chart": "longhorn"}}})

    def row(self, component):
        return next(r for r in C.report()["components"] if r["id"] == component)

    def test_the_report_names_versions_and_the_next_step(self):
        cluster, longhorn = self.row("cluster"), self.row("longhorn")
        self.assertEqual(("v1.31.12+k3s1", "v1.32.8+k3s1", "v1.33.4+k3s1", "suc", True),
                         (cluster["installed"], cluster["next"], cluster["newest"], cluster["how"], cluster["steps_left"]))
        self.assertEqual(("v1.8.1", "v1.8.2", "v1.9.1", "helmchart"),
                         (longhorn["installed"], longhorn["next"], longhorn["newest"], longhorn["how"]))

    def test_longhorn_installed_elsewhere_is_shown_not_upgraded(self):
        del self.c.objects[LH_CHART]
        self.assertEqual(("manual", ""), (self.row("longhorn")["how"], self.row("longhorn")["next"]))
        with self.assertRaisesRegex(ValueError, "installed outside Homestead"):
            C.upgrade("longhorn", "v1.8.2")

    def test_skipping_a_step_is_refused(self):
        with self.assertRaisesRegex(ValueError, "v1.9.1 would skip a step"):
            C.upgrade("longhorn", "v1.9.1")
        C.upgrade("longhorn", "v1.8.2")
        self.assertEqual([{"namespace": "longhorn-system", "name": "longhorn", "version": "1.8.2"}], self.c.helm_calls)

    def test_the_cluster_installs_the_controller_then_writes_plans_and_follows_the_nodes(self):
        result = C.upgrade("cluster", "v1.32.8+k3s1")
        self.assertIn("system-upgrade-controller", result["detail"])
        method, path, chart = self.c.sent[0]
        self.assertEqual(("POST", "homestead-system-upgrade"), (method, chart["metadata"]["name"]))
        item = {"ref": {"component": "cluster", "to": "v1.32.8+k3s1", "phase": "controller", "started": time.time()}}
        self.assertEqual("running", C.status(item)[0])
        self.c.objects["/apis/upgrade.cattle.io/v1"] = {}
        self.c.objects[f"/apis/helm.cattle.io/v1/namespaces/{C.HELM_NS}/helmcharts/{C.SUC_CHART}"] = {
            "metadata": {"name": C.SUC_CHART}}
        status, _, message = C.status(item)
        plans = [body for m, p, body in self.c.sent if p == C.PLANS]
        self.assertEqual(["homestead-server", "homestead-agent"], [p["metadata"]["name"] for p in plans])
        self.assertEqual("rancher/k3s-upgrade", plans[0]["spec"]["upgrade"]["image"])
        self.assertEqual(["prepare", "homestead-server"], plans[1]["spec"]["prepare"]["args"])
        self.assertEqual(("running", "nodes"), (status, item["ref"]["phase"]))
        self.assertIn("0 of 2 nodes", message)
        for body in plans:
            self.c.objects[f"{C.PLANS}/{body['metadata']['name']}"] = body
        for node in NODES["items"]:
            node["status"]["nodeInfo"]["kubeletVersion"] = "v1.32.8+k3s1"
        try:
            self.assertEqual("succeeded", C.status(item)[0])
            self.assertIn(("DELETE", f"{C.PLANS}/homestead-server", None), self.c.sent)
        finally:
            for node in NODES["items"]:
                node["status"]["nodeInfo"]["kubeletVersion"] = "v1.31.12+k3s1"

    def test_a_failed_node_job_fails_the_upgrade(self):
        self.c.objects["/apis/upgrade.cattle.io/v1"] = {}
        self.c.objects["/apis/batch/v1/namespaces/system-upgrade/jobs"] = {"items": [{
            "metadata": {"name": "apply-homestead-server-on-node-1", "labels": {
                "upgrade.cattle.io/plan": "homestead-server", "upgrade.cattle.io/node": "node-1"}},
            "status": {"failed": 1}}]}
        status, _, message = C.status({"ref": {"component": "cluster", "to": "v1.32.8+k3s1", "phase": "nodes"}})
        self.assertEqual("failed", status)
        self.assertIn("node-1 failed", message)

    def test_external_upgrade_controller_is_not_taken_over(self):
        self.c.objects["/apis/upgrade.cattle.io/v1"] = {}
        with self.assertRaisesRegex(ValueError, "managed outside Homestead"):
            C.upgrade("cluster", "v1.32.8+k3s1")
        self.assertFalse(self.c.sent)

    def test_external_plan_is_neither_overwritten_nor_deleted(self):
        self.c.objects["/apis/upgrade.cattle.io/v1"] = {}
        self.c.objects[f"/apis/helm.cattle.io/v1/namespaces/{C.HELM_NS}/helmcharts/{C.SUC_CHART}"] = {
            "metadata": {"name": C.SUC_CHART}}
        self.c.objects[f"{C.PLANS}/{C.PLAN_NAMES[1]}"] = {"metadata": {"name": C.PLAN_NAMES[1]}}
        with self.assertRaisesRegex(ValueError, "not owned by Homestead"):
            C.upgrade("cluster", "v1.32.8+k3s1")
        self.assertFalse(self.c.sent, "ownership of both Plans must be checked before writing either")
        C.remove_plans()
        self.assertFalse(self.c.sent)


class HarvesterTests(unittest.TestCase):
    def test_harvesters_own_parts_are_shown_and_left_to_harvester(self):
        Cluster({"distribution": "harvester", "harvester": True, "longhorn": True, "kubevirt": True},
                {LH_SETTING: {"value": "v1.8.1"},
                 "/apis/kubevirt.io/v1/kubevirts": {"items": [{"status": {"observedKubeVirtVersion": "v1.4.0",
                                                                          "phase": "Deployed"}}]}})
        rows = {r["id"]: r for r in C.report()["components"]}
        self.assertEqual(("harvester", "v1.8.1", ""), (rows["longhorn"]["how"], rows["longhorn"]["installed"],
                                                       rows["longhorn"]["next"]))
        self.assertEqual("v1.4.0", rows["kubevirt"]["installed"])
        with self.assertRaisesRegex(ValueError, "upgraded with it"):
            C.upgrade("longhorn", "v1.8.2")

    def test_only_an_offered_version_is_started(self):
        c = Cluster({"harvester": True}, {})
        with self.assertRaisesRegex(ValueError, "does not offer v1.6.0"):
            C.start_harvester("v1.6.0", [{"version": "v1.5.1"}])
        C.start_harvester("v1.5.1", [{"version": "v1.5.1"}])
        method, path, body = c.sent[0]
        self.assertEqual(("POST", {"version": "v1.5.1"}), (method, body["spec"]))


if __name__ == "__main__":
    unittest.main()
