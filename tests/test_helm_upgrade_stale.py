"""The Upgrade dialog never puts back a chart that changed after it opened.

The dialog sends the HelmChart generation it showed; the server refuses the
upgrade when the chart's spec moved since. A status write by the Helm
controller leaves the generation alone, so it does not stop the upgrade."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_helm as HELM
from fakes.kube import Kube
from test_helm import secret

CHART = "/apis/helm.cattle.io/v1/namespaces/kube-system/helmcharts/app"


def helmchart():
    return {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
            "metadata": {"name": "app", "namespace": "kube-system", "generation": 1,
                         "labels": {"homestead.io/managed": "true"}},
            "spec": {"repo": "https://charts.example.com", "chart": "app", "version": "1.0.0",
                     "targetNamespace": "lab", "valuesContent": "replicas: 1\n"}}


class HelmUpgradeStaleTests(unittest.TestCase):
    def setUp(self):
        release = secret("lab", "app", 1)
        release.update(apiVersion="v1", kind="Secret")
        self.kube = Kube(helmchart(), release)
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        HELM.bind(K.kget, K.ksend)

    def opened(self):
        """What the Upgrade dialog read when it opened."""
        return HELM.release("lab", "app")["source"]

    def ours(self):
        return [w for w in self.kube.writes if w[0] == "PUT" and w[1] == CHART]

    def test_the_dialog_is_given_the_generation_it_shows(self):
        self.assertEqual("1", str(self.opened()["generation"]))

    def test_a_change_made_after_the_dialog_opened_is_not_reverted(self):
        shown = self.opened()
        other = K.kget(CHART)
        other["spec"]["valuesContent"] = "replicas: 3\n"
        K.ksend("PUT", CHART, other)          # another admin, kubectl or GitOps
        before = len(self.ours())
        with self.assertRaisesRegex(ValueError, "changed since"):
            HELM.upgrade({"namespace": "lab", "name": "app", "version": shown["version"],
                          "values": "replicas: 2\n", "generation": shown["generation"]})
        self.assertEqual(before, len(self.ours()), "nothing was sent")
        self.assertEqual("replicas: 3\n", self.kube.object(CHART)["spec"]["valuesContent"])

    def test_a_status_write_after_the_dialog_opened_does_not_stop_the_upgrade(self):
        shown = self.opened()
        K.ksend("PUT", CHART + "/status", {"status": {"jobName": "helm-install-app"}})   # the Helm controller
        result = HELM.upgrade({"namespace": "lab", "name": "app", "version": "1.1.0",
                               "values": "replicas: 2\n", "generation": shown["generation"]})
        self.assertTrue(result["ok"])
        spec = self.kube.object(CHART)["spec"]
        self.assertEqual(("1.1.0", "replicas: 2\n"), (spec["version"], spec["valuesContent"]))


if __name__ == "__main__":
    unittest.main()
