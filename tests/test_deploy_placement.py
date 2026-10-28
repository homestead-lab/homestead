"""Deploy chooses an app's placement as Edit does (#844): its hosts, how its
copies spread and which apps it runs beside, applied in the first rollout."""
import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import server  # noqa: E402
import homestead_deploy_manifests as MANIFESTS  # noqa: E402
import homestead_workload_edit as WORKLOAD_EDIT  # noqa: E402
import homestead_affinity as AFFINITY  # noqa: E402

PLACEMENT = {"spread": "prefer",
             "with": [{"ns": "lab", "name": "broker", "mode": "prefer"}],
             "apart": [{"ns": "dns", "name": "dns-2", "mode": "require"}]}


def other(name, ns):
    return {"metadata": {"name": name, "namespace": ns},
            "spec": {"selector": {"matchLabels": {"app": name}}}}


class DeployPlacementTests(unittest.TestCase):
    def setUp(self):
        self.others = {("lab", "broker"): other("broker", "lab"), ("dns", "dns-2"): other("dns-2", "dns")}
        self.read = []

        def get(path):
            self.read.append(path)
            parts = path.split("/")
            key = (parts[5], parts[7])
            if key not in self.others:
                raise urllib.error.HTTPError(path, 404, "missing", None, None)
            return copy.deepcopy(self.others[key])

        for patch in (mock.patch.object(server.HW, "features", return_value=[]),
                      mock.patch.object(AFFINITY, "kget", side_effect=get)):
            patch.start()
            self.addCleanup(patch.stop)

    def cfg(self, **extra):
        return {"name": "app", "image": "app:1", "namespace": "lab", "ports": [], "volumes": [], "hardware": [], **extra}

    def build(self, cfg):
        return MANIFESTS.build_deployment(cfg)[0]

    @staticmethod
    def placed(dep):
        spec = dep["spec"]["template"]["spec"]
        return {"affinity": spec.get("affinity"), "nodeSelector": spec.get("nodeSelector"),
                "tolerations": spec.get("tolerations"),
                "rules": (dep["metadata"].get("annotations") or {}).get(AFFINITY.RULES)}

    def test_no_placement_builds_the_app_as_before(self):
        dep = self.build(self.cfg())
        spec = dep["spec"]["template"]["spec"]
        self.assertNotIn("affinity", spec)
        self.assertNotIn("nodeSelector", spec)
        self.assertNotIn(AFFINITY.RULES, dep["metadata"].get("annotations") or {})
        self.assertEqual("move", server.FAILOVER.mode_of(spec))
        empty = self.build(self.cfg(placement={"spread": "", "with": [], "apart": []}, node="", failover=""))
        self.assertEqual(self.placed(dep), self.placed(empty))
        self.assertEqual(dep, empty, "the whole manifest, pod annotations included")
        self.assertEqual([], self.read)

    def test_a_deployed_apps_placement_is_exactly_what_edit_writes(self):
        choices = {"placement": PLACEMENT, "node": "node-1", "failover": "wait"}
        deployed = self.build(self.cfg(**choices))
        edited = WORKLOAD_EDIT.prepare_edit({"ns": "lab", "name": "app", **choices}, current=self.build(self.cfg()))["deployment"]
        self.assertEqual(self.placed(edited), self.placed(deployed))
        self.assertTrue(self.placed(deployed)["affinity"]["podAffinity"])
        self.assertTrue(self.placed(deployed)["affinity"]["podAntiAffinity"])

    def test_the_chosen_host_is_a_preference_as_in_edit(self):
        spec = self.build(self.cfg(node="node-1"))["spec"]["template"]["spec"]
        self.assertNotIn("kubernetes.io/hostname", spec.get("nodeSelector") or {})
        preferred = spec["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(["node-1"], preferred[0]["preference"]["matchExpressions"][0]["values"])

    def test_a_host_keeps_the_hardware_selector(self):
        feature = {"id": "igpu", "label": "example.com/igpu", "host_path": "/dev/dri", "container_path": "/dev/dri"}
        with mock.patch.object(server.HW, "features", return_value=[feature]), \
                mock.patch.object(server.HW, "mount_spec", return_value={"label": "example.com/igpu", "name": "dri",
                                  "container_path": "/dev/dri", "host_path": "/dev/dri", "path_type": "Directory"}):
            spec = self.build(self.cfg(node="node-1", hardware=["igpu"]))["spec"]["template"]["spec"]
        self.assertEqual({"example.com/igpu": "true"}, spec["nodeSelector"])

    def test_deploy_refuses_what_edit_refuses(self):
        cases = [{"placement": {"spread": "always"}},
                 {"placement": {"with": [{"ns": "lab", "name": "app"}]}},
                 {"placement": {"with": [{"ns": "lab", "name": "broker"}], "apart": [{"ns": "lab", "name": "broker"}]}},
                 {"placement": {"with": [{"ns": "lab", "name": "broker", "mode": "maybe"}]}},
                 {"failover": "sometimes"}]
        for extra in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError) as edit:
                    WORKLOAD_EDIT.prepare_edit({"ns": "lab", "name": "app", **extra}, current=self.build(self.cfg()))
                with self.assertRaises(ValueError) as deploy:
                    self.build(self.cfg(**extra))
                self.assertEqual(str(edit.exception), str(deploy.exception))
        with self.assertRaises(urllib.error.HTTPError):
            self.build(self.cfg(placement={"apart": [{"ns": "lab", "name": "nothing"}]}))

    def test_a_deploy_with_more_containers_gets_the_placement(self):
        cfg = self.cfg(placement=PLACEMENT, node="node-1", additional_containers=[
            {"name": "side", "container_name": "side", "image": "side:1", "namespace": "lab"}])
        self.assertEqual(self.placed(self.build(self.cfg(placement=PLACEMENT, node="node-1"))), self.placed(self.build(cfg)))

    def test_joining_an_app_keeps_its_placement(self):
        current = {"apiVersion": "apps/v1", "kind": "Deployment",
                   "metadata": {"name": "media", "namespace": "lab", "resourceVersion": "7"},
                   "spec": {"selector": {"matchLabels": {"app": "media"}},
                            "template": {"metadata": {"labels": {"app": "media"}},
                                         "spec": {"containers": [{"name": "main", "image": "example/main"}]}}}}
        cfg = {"name": "helper", "image": "example/helper", "namespace": "lab", "target_workload": "media",
               "target_mode": "existing", "network_mode": "internal", "ports": [], "volumes": [], "hardware": [],
               "placement": PLACEMENT, "node": "node-1", "failover": "wait"}
        updated, _ = MANIFESTS.build_sidecar_deployment(cfg, copy.deepcopy(current))
        pod = updated["spec"]["template"]["spec"]
        self.assertNotIn("affinity", pod)
        self.assertNotIn(AFFINITY.RULES, updated["metadata"].get("annotations") or {})
        self.assertEqual([], self.read)


class DeployPlacementReviewTests(unittest.TestCase):
    """The capacity review warns when the host chosen in Deploy can't take the app."""

    def plan(self, cfg, eligible):
        planned = {"additional": 1, "warnings": [], "requires_confirmation": False, "blocked": False,
                   "placement": {"pinned": None, "preferred": cfg.get("node") or None, "resident": None},
                   "candidates": [{"name": "node-1", "eligible": eligible}, {"name": "node-2", "eligible": True}]}
        routes = server.DEPLOY_ROUTES
        with mock.patch.object(server.HW, "features", return_value=[]), \
                mock.patch.object(routes.PLACE, "manifest_plan", return_value=planned), \
                mock.patch.object(routes, "get_app_settings", return_value={"thresholds": {"memory": {"critical": 90}}}):
            return routes.deploy_capacity_plan({"name": "app", "image": "app:1", "namespace": "lab", "ports": [],
                                                "volumes": [], "hardware": [], **cfg})

    def test_a_chosen_host_that_cannot_take_it_is_a_warning(self):
        plan = self.plan({"node": "node-1"}, eligible=False)
        self.assertTrue(any("node-1" in warning for warning in plan["warnings"]), plan["warnings"])
        self.assertTrue(plan["requires_confirmation"])
        self.assertFalse(plan["blocked"])

    def test_a_chosen_host_that_can_take_it_says_nothing(self):
        plan = self.plan({"node": "node-1"}, eligible=True)
        self.assertEqual([], plan["warnings"])
        self.assertFalse(plan["requires_confirmation"])

    def test_no_copies_to_start_says_nothing_of_the_host(self):
        with mock.patch.object(server.DEPLOY_ROUTES.PLACE, "manifest_plan", return_value={
                "additional": 0, "warnings": [], "requires_confirmation": False, "blocked": False,
                "candidates": [{"name": "node-1", "eligible": False}]}), \
                mock.patch.object(server.HW, "features", return_value=[]), \
                mock.patch.object(server.DEPLOY_ROUTES, "get_app_settings", return_value={"thresholds": {"memory": {"critical": 90}}}):
            plan = server.DEPLOY_ROUTES.deploy_capacity_plan({"name": "app", "image": "app:1", "namespace": "lab", "ports": [],
                                                              "volumes": [], "hardware": [], "replicas": 0, "node": "node-1"})
        self.assertEqual([], plan["warnings"])
        self.assertFalse(plan["requires_confirmation"])

    def test_no_chosen_host_says_nothing(self):
        plan = self.plan({}, eligible=False)
        self.assertEqual([], plan["warnings"])


if __name__ == "__main__":
    unittest.main()
