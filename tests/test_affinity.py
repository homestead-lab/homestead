import copy
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_affinity as AFFINITY


def deployment(name, ns="lab", labels=None):
    return {"metadata": {"name": name, "namespace": ns, "annotations": {}},
            "spec": {"selector": {"matchLabels": labels if labels is not None else {"app": name}},
                     "template": {"spec": {"containers": [{"name": name}]}}}}


class AffinityTests(unittest.TestCase):
    def setUp(self):
        self.others = {("lab", "mosquitto"): deployment("mosquitto"),
                       ("dns", "pihole-2"): deployment("pihole-2", "dns"),
                       ("lab", "odd"): deployment("odd", labels={})}

        def get(path):
            parts = path.split("/")
            key = (parts[5], parts[7])
            if key not in self.others:
                raise urllib.error.HTTPError(path, 404, "missing", None, None)
            return copy.deepcopy(self.others[key])

        AFFINITY.bind(get)
        self.dep = deployment("frigate")

    def affinity(self):
        return self.dep["spec"]["template"]["spec"].get("affinity")

    def test_rules_become_pod_affinity_terms_on_each_selector(self):
        AFFINITY.apply(self.dep, {"spread": "require",
                                  "with": [{"ns": "lab", "name": "mosquitto", "mode": "prefer"}],
                                  "apart": [{"ns": "dns", "name": "pihole-2", "mode": "require"}]})
        aff = self.affinity()
        anti = aff["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual([{"app": "frigate"}, {"app": "pihole-2"}], [t["labelSelector"]["matchLabels"] for t in anti])
        self.assertEqual(["lab", "dns"], [t["namespaces"][0] for t in anti])
        near = aff["podAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"][0]
        self.assertEqual(100, near["weight"])
        self.assertEqual("kubernetes.io/hostname", near["podAffinityTerm"]["topologyKey"])
        self.assertEqual({"spread": "require", "with": [{"ns": "lab", "name": "mosquitto", "mode": "prefer"}],
                          "apart": [{"ns": "dns", "name": "pihole-2", "mode": "require"}]}, AFFINITY.public(self.dep))

    def test_an_edit_replaces_only_what_homestead_wrote(self):
        mine = {"labelSelector": {"matchLabels": {"team": "cameras"}}, "topologyKey": "zone"}
        self.dep["spec"]["template"]["spec"]["affinity"] = {
            "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [mine]},
            "nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [{"weight": 1}]}}
        AFFINITY.apply(self.dep, {"spread": "require", "with": [{"ns": "lab", "name": "mosquitto", "mode": "require"}]})
        # The workload it ran with is later deleted; its rule still comes off cleanly.
        del self.others[("lab", "mosquitto")]
        AFFINITY.apply(self.dep, {"spread": "prefer"})
        aff = self.affinity()
        self.assertEqual([mine], aff["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"])
        self.assertNotIn("podAffinity", aff)
        self.assertEqual(1, len(aff["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]))
        self.assertIn("nodeAffinity", aff)

    def test_clearing_every_rule_leaves_no_trace(self):
        AFFINITY.apply(self.dep, {"spread": "prefer", "apart": [{"ns": "dns", "name": "pihole-2"}]})
        AFFINITY.apply(self.dep, {})
        self.assertIsNone(self.affinity())
        self.assertNotIn(AFFINITY.RULES, self.dep["metadata"]["annotations"])

    def test_impossible_rules_are_refused(self):
        cases = [{"spread": "always"},
                 {"with": [{"ns": "lab", "name": "frigate"}]},
                 {"with": [{"ns": "lab", "name": "mosquitto"}], "apart": [{"ns": "lab", "name": "mosquitto"}]},
                 {"apart": [{"ns": "lab", "name": "odd"}]},
                 {"with": [{"ns": "lab", "name": "mosquitto", "mode": "maybe"}]}]
        for request in cases:
            with self.subTest(request=request), self.assertRaises(ValueError):
                AFFINITY.apply(copy.deepcopy(self.dep), request)
        with self.assertRaises(urllib.error.HTTPError):
            AFFINITY.apply(copy.deepcopy(self.dep), {"with": [{"ns": "lab", "name": "nothing"}]})

    def test_a_garbled_annotation_reads_as_no_rules(self):
        self.dep["metadata"]["annotations"][AFFINITY.RULES] = "{not json"
        self.assertEqual({"spread": "", "with": [], "apart": []}, AFFINITY.public(self.dep))


from test_container_storage import WorkloadEditFixture


class EditorPlacementTests(WorkloadEditFixture, unittest.TestCase):
    def test_saving_the_editor_writes_the_rules(self):
        import homestead_lifecycle as lifecycle
        self.deployment["spec"]["selector"] = {"matchLabels": {"app": "frigate"}}
        lifecycle.edit_workload({"ns": "lab", "name": "frigate", "placement": {"spread": "prefer"}})
        saved = self.sent[-1][2]
        term = saved["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"][
            "preferredDuringSchedulingIgnoredDuringExecution"][0]["podAffinityTerm"]
        self.assertEqual({"app": "frigate"}, term["labelSelector"]["matchLabels"])
        self.assertEqual(["lab"], term["namespaces"])
        self.assertIn(AFFINITY.RULES, saved["metadata"]["annotations"])

    def test_an_edit_without_rules_leaves_placement_alone(self):
        import homestead_lifecycle as lifecycle
        self.deployment["spec"]["template"]["spec"]["affinity"] = {"podAffinity": {"x": 1}}
        lifecycle.edit_workload({"ns": "lab", "name": "frigate", "replicas": 1})
        self.assertEqual({"podAffinity": {"x": 1}}, self.sent[-1][2]["spec"]["template"]["spec"]["affinity"])


if __name__ == "__main__":
    unittest.main()
