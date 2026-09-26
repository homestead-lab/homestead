import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_multus as MULTUS
import homestead_addons as ADDONS


class MultusTests(unittest.TestCase):
    def setUp(self):
        self.objects = {MULTUS.NAD_LIST: {"items": []}, MULTUS.DAEMONSETS: {"items": [{
            "metadata": {"name": "multus", "uid": "ds1", "generation": 2},
            "status": {"desiredNumberScheduled": 2, "numberAvailable": 2,
                       "updatedNumberScheduled": 2, "observedGeneration": 2}}]}}

    def get(self, path):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.objects[path])

    def test_ready_requires_api_and_current_agents(self):
        self.assertTrue(MULTUS.inspect(self.get)["ready"])
        self.objects.pop(MULTUS.NAD_LIST)
        self.assertFalse(MULTUS.inspect(self.get)["ready"])

    def test_successful_chart_or_crd_alone_is_not_ready(self):
        self.objects[MULTUS.DAEMONSETS]["items"] = []
        result = MULTUS.inspect(self.get)
        self.assertTrue(result["installed"])
        self.assertFalse(result["ready"])
        self.assertEqual("unknown", result["state"])

    def test_stale_generation_is_not_ready(self):
        self.objects[MULTUS.DAEMONSETS]["items"][0]["status"]["observedGeneration"] = 1
        self.assertFalse(MULTUS.inspect(self.get)["ready"])

    def test_zero_nodes_are_not_ready(self):
        self.objects[MULTUS.DAEMONSETS]["items"][0]["status"].update(desiredNumberScheduled=0, numberAvailable=0)
        self.assertFalse(MULTUS.inspect(self.get)["ready"])

    def test_crashing_agents_are_reported(self):
        self.objects[MULTUS.DAEMONSETS]["items"][0]["status"]["numberAvailable"] = 1
        self.objects["/api/v1/namespaces/kube-system/pods"] = {"items": [{
            "metadata": {"name": "multus-broken", "ownerReferences": [{"uid": "ds1"}]},
            "status": {"containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}]}
        result = MULTUS.inspect(self.get)
        self.assertIn("multus-broken: CrashLoopBackOff", result["issues"])
        self.assertEqual((1, 2), (result["available"], result["desired"]))

    def test_permission_failure_is_unknown_not_absent(self):
        def denied(path):
            raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
        self.assertEqual("unknown", MULTUS.inspect(denied)["state"])
        with patch.object(ADDONS, "kget", denied):
            with self.assertRaises(urllib.error.HTTPError):
                ADDONS._ensure_multus_crd()

    def test_repair_is_scoped_and_preserves_metadata_and_version(self):
        old = ADDONS.K3S_MULTUS_VALUES.replace("    multusAutoconfigDir: /var/lib/rancher/k3s/agent/etc/cni/net.d\n", "")
        chart = {"metadata": {"name": "multus", "resourceVersion": "7", "labels": {"homestead.io/managed": "true"}},
                 "spec": {"chart": "rke2-multus", "repo": ADDONS.RKE2_CHARTS, "targetNamespace": "kube-system",
                          "version": "v4.3.102", "valuesContent": old}}
        self.objects["/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts"] = {"items": [chart]}
        sent = []
        with patch.object(ADDONS, "kget", self.get), patch.object(ADDONS, "ksend", lambda *args: sent.append(args)), \
                patch.object(ADDONS, "platform", return_value={"distribution": "k3s", "helm_controller": True}):
            ADDONS.repair_multus()
        self.assertEqual("7", sent[-1][2]["metadata"]["resourceVersion"])
        self.assertEqual("v4.3.102", sent[-1][2]["spec"]["version"])
        self.assertIn("multusAutoconfigDir", sent[-1][2]["spec"]["valuesContent"])
        chart["spec"]["valuesContent"] += "custom: true\n"
        self.assertFalse(ADDONS._repairable_multus(chart, {"distribution": "k3s"}))

    def test_job_times_out_instead_of_installing_forever(self):
        with patch.object(ADDONS, "status", return_value={"multus": {
                "ready": False, "installed": True, "issues": [], "detail": "not ready"}}), \
                patch.object(ADDONS, "kget", self.get):
            result = ADDONS.multus_progress({"started_at": "2000-01-01T00:00:00Z"})
        self.assertEqual("failed", result[0])

    def test_diagnostics_include_missing_crd_and_node_agent(self):
        command = ADDONS.diagnostic_command("k3s")
        self.assertIn("logs -l app=rke2-multus", command)
        self.assertIn("--previous", command)


if __name__ == "__main__":
    unittest.main()
