import copy, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_failover as FAILOVER
import homestead_lhcapacity as LHCAP

UNREACHABLE, NOT_READY = FAILOVER.KEYS
GPU = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}


class ModeTests(unittest.TestCase):
    """Every container Homestead deployed moved after fifteen seconds, and
    nothing said so or let it be changed."""

    def test_reading_the_mode_from_tolerations(self):
        self.assertEqual("default", FAILOVER.mode_of({}))
        self.assertEqual("move", FAILOVER.mode_of({"tolerations": FAILOVER.tolerations({}, "move")}))
        self.assertEqual("wait", FAILOVER.mode_of({"tolerations": FAILOVER.tolerations({}, "wait")}))
        five_minutes = [{"key": k, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300}
                        for k in FAILOVER.KEYS]
        self.assertEqual("default", FAILOVER.mode_of({"tolerations": five_minutes}))

    def test_waiting_tolerates_a_failed_node_for_good(self):
        rows = FAILOVER.tolerations({}, "wait")
        self.assertEqual({UNREACHABLE, NOT_READY}, {r["key"] for r in rows})
        self.assertTrue(all("tolerationSeconds" not in r for r in rows))

    def test_other_tolerations_are_kept(self):
        spec = {"tolerations": [GPU] + FAILOVER.tolerations({}, "move")}
        FAILOVER.apply(spec, "default")
        self.assertEqual([GPU], spec["tolerations"])
        FAILOVER.apply(spec, "wait")
        self.assertIn(GPU, spec["tolerations"])

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaisesRegex(ValueError, "moves, waits"):
            FAILOVER.tolerations({}, "sometimes")


class SetManyTests(unittest.TestCase):
    def setUp(self):
        self.deps = {"frigate": {"spec": {"template": {"spec": {"tolerations": FAILOVER.tolerations({}, "move")}}}},
                     "zigbee2mqtt": {"spec": {"template": {"spec": {"tolerations": [GPU]}}}}}
        self.sent = []
        FAILOVER.bind(lambda path: copy.deepcopy(self.deps[path.rsplit("/", 1)[1]]),
                      lambda method, path, body=None, **kw: self.sent.append((method, path, body)))

    def test_only_changed_containers_are_patched(self):
        result = FAILOVER.set_many([{"ns": "lab", "name": "frigate", "mode": "move"},
                                    {"ns": "lab", "name": "zigbee2mqtt", "mode": "wait"}])
        self.assertEqual(["zigbee2mqtt"], result["changed"])
        method, path, body = self.sent[0]
        self.assertEqual(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/zigbee2mqtt"), (method, path))
        wanted = body["spec"]["template"]["spec"]["tolerations"]
        self.assertIn(GPU, wanted)
        self.assertEqual("wait", FAILOVER.mode_of({"tolerations": wanted}))

    def test_back_to_the_default_clears_the_list(self):
        self.deps["frigate"]["spec"]["template"]["spec"]["tolerations"] = FAILOVER.tolerations({}, "move")
        FAILOVER.set_many([{"ns": "lab", "name": "frigate", "mode": "default"}])
        self.assertIsNone(self.sent[0][2]["spec"]["template"]["spec"]["tolerations"])


class LonghornPolicyTests(unittest.TestCase):
    """A container moved off a dead node cannot mount a single-node volume
    unless Longhorn lets go of the dead node's pods."""

    def setUp(self):
        self.values = {LHCAP.NODE_DOWN: "do-nothing", LHCAP.OVER: "100", LHCAP.MINIMAL: "25"}
        self.sent = []

        def kget(path):
            name = path.rsplit("/", 1)[1]
            if name in self.values:
                return {"value": self.values[name]}
            return {"items": []}

        def ksend(method, path, body=None, **kw):
            self.sent.append((method, path, body))
            self.values[path.rsplit("/", 1)[1]] = body["value"]
        LHCAP.bind(kget, ksend, lambda: {"enabled": False})

    def test_the_policy_is_read_and_set(self):
        self.assertEqual("do-nothing", LHCAP.settings()["node_down"])
        LHCAP.save({"node_down": "delete-both-statefulset-and-deployment-pod"})
        self.assertEqual("delete-both-statefulset-and-deployment-pod", self.values[LHCAP.NODE_DOWN])

    def test_an_unknown_policy_is_refused(self):
        with self.assertRaisesRegex(ValueError, "one of"):
            LHCAP.save({"node_down": "delete-everything"})
        self.assertEqual([], self.sent)


class DeployTests(unittest.TestCase):
    def build(self, **extra):
        from unittest import mock
        import server
        with mock.patch.object(server.HW, "features", lambda: []):
            cfg = dict(name="coral-app", namespace="lab", image="busybox", ports=[], volumes=[], **extra)
            dep, _ = server.build_deployment(cfg)
        return dep["spec"]["template"]["spec"]

    def test_a_new_container_moves_unless_told_otherwise(self):
        self.assertEqual("move", FAILOVER.mode_of(self.build()))

    def test_a_new_container_can_wait_for_its_node(self):
        self.assertEqual("wait", FAILOVER.mode_of(self.build(failover="wait")))


if __name__ == "__main__":
    unittest.main()
