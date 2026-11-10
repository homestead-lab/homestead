import sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_failover as FAILOVER
import homestead_kube as K
import homestead_lhcapacity as LHCAP
import homestead_operations as OPS
import homestead_deploy_manifests as MANIFESTS
from fakes.kube import Kube
from rebind import keep_bound

UNREACHABLE, NOT_READY = FAILOVER.KEYS
GPU = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}


def after(seconds):
    """Node-failure tolerations as another tool would write them."""
    return [{"key": k, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": seconds} for k in FAILOVER.KEYS]


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

    def test_a_time_homestead_does_not_offer_is_shown_as_it_is(self):
        # #1308: 90 s read as "default", which says five minutes.
        self.assertEqual("custom", FAILOVER.mode_of({"tolerations": after(120)}))
        self.assertEqual(120, FAILOVER.seconds_of({"tolerations": after(120)}))
        self.assertEqual(300, FAILOVER.seconds_of({}))
        self.assertEqual(15, FAILOVER.seconds_of({"tolerations": FAILOVER.tolerations({}, "move")}))
        self.assertIsNone(FAILOVER.seconds_of({"tolerations": FAILOVER.tolerations({}, "wait")}))

    def test_a_short_time_homestead_did_not_set_is_not_read_as_15_seconds(self):
        # Review of #1308: 30 s and 45 s read as "move", and a save rewrote them to 15 s.
        for seconds in (30, 45):
            with self.subTest(seconds=seconds):
                self.assertEqual("custom", FAILOVER.mode_of({"tolerations": after(seconds)}))
                self.assertEqual(seconds, FAILOVER.seconds_of({"tolerations": after(seconds)}))

    def test_two_keys_with_different_times_are_custom(self):
        # A host that stops answering is tainted unreachable: its time is the one shown.
        mixed = [dict(after(90)[0]), dict(after(300)[1])]
        self.assertEqual("custom", FAILOVER.mode_of({"tolerations": mixed}))
        self.assertEqual(90, FAILOVER.seconds_of({"tolerations": mixed}))
        self.assertEqual("custom", FAILOVER.mode_of({"tolerations": [after(15)[0]]}), "one key only")
        self.assertEqual(15, FAILOVER.seconds_of({"tolerations": [after(15)[0]]}))
        self.assertEqual(300, FAILOVER.seconds_of({"tolerations": [after(15)[1]]}), "unreachable keeps the default")
        waits = FAILOVER.tolerations({}, "wait")
        self.assertEqual("custom", FAILOVER.mode_of({"tolerations": [waits[0]]}))
        self.assertIsNone(FAILOVER.seconds_of({"tolerations": [waits[0]]}))
        spec = {"tolerations": [GPU] + mixed}
        FAILOVER.apply(spec, "custom")
        self.assertEqual([GPU] + mixed, spec["tolerations"], "both kept as they are")

    def test_whether_it_waits_follows_the_unreachable_key_alone(self):
        # A pod that tolerates unreachable for good never leaves a host that stops
        # answering, whatever the Edit choice calls it (set_restart asks this).
        waits = FAILOVER.tolerations({}, "wait")
        self.assertTrue(FAILOVER.waits_when_unreachable({"tolerations": [waits[0]]}))
        self.assertTrue(FAILOVER.waits_when_unreachable({"tolerations": waits}))
        self.assertFalse(FAILOVER.waits_when_unreachable({"tolerations": [waits[1]]}))
        self.assertFalse(FAILOVER.waits_when_unreachable({}))

    def test_moving_after_90_seconds_is_a_choice(self):
        # #1308, part 2: between 15 s and five minutes.
        rows = FAILOVER.tolerations({}, "slow")
        self.assertEqual(after(90), rows)
        self.assertEqual("slow", FAILOVER.mode_of({"tolerations": rows}))
        self.assertEqual(90, FAILOVER.seconds_of({"tolerations": rows}))

    def test_keeping_a_time_homestead_does_not_offer_changes_nothing(self):
        spec = {"tolerations": [GPU] + after(120)}
        FAILOVER.apply(spec, "custom")
        self.assertEqual([GPU] + after(120), spec["tolerations"])

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
        with self.assertRaisesRegex(ValueError, "moves after 15 or 90 seconds, waits"):
            FAILOVER.tolerations({}, "sometimes")


def deployment(name, tolerations):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": "lab"},
            "spec": {"template": {"spec": {"containers": [{"name": name, "image": "busybox"}],
                                           "tolerations": tolerations}}}}


class EditShowsTests(unittest.TestCase):
    def test_edit_shows_the_time_an_app_has(self):
        import homestead_priority as PRIORITY
        import homestead_workloads as WORKLOADS
        self.enterContext(mock.patch.dict(PRIORITY._LADDER, at=0.0, value=None))
        with Kube():
            payload = WORKLOADS.workload_edit_payload("lab", "tasmota", deployment("tasmota", after(120)), [], services=[])
        self.assertEqual(("custom", 120), (payload["failover"], payload["failover_seconds"]))


class SetManyTests(unittest.TestCase):
    """On the shared fake API (tests/fakes/kube.py)."""
    def setUp(self):
        self.kube = self.enterContext(Kube(deployment("frigate", FAILOVER.tolerations({}, "move")),
                                           deployment("zigbee2mqtt", [GPU])))
        # The shared fake runs the real write guards; the storage guard takes the job
        # store's lock, which lives under its DATA_DIR.
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        saved = FAILOVER.kget, FAILOVER.ksend
        self.addCleanup(lambda: FAILOVER.bind(*saved))
        FAILOVER.bind(K.kget, K.ksend)

    def tolerations(self, name):
        return self.kube.object(f"/apis/apps/v1/namespaces/lab/deployments/{name}")["spec"]["template"]["spec"].get("tolerations")

    def test_only_changed_containers_are_patched(self):
        result = FAILOVER.set_many([{"ns": "lab", "name": "frigate", "mode": "move"},
                                    {"ns": "lab", "name": "zigbee2mqtt", "mode": "wait"}])
        self.assertEqual(["zigbee2mqtt"], result["changed"])
        self.assertEqual([("PATCH", "/apis/apps/v1/namespaces/lab/deployments/zigbee2mqtt")],
                         [(method, path) for method, path, *_ in self.kube.writes])
        wanted = self.tolerations("zigbee2mqtt")
        self.assertIn(GPU, wanted)
        self.assertEqual("wait", FAILOVER.mode_of({"tolerations": wanted}))

    def test_keeping_its_own_time_writes_nothing(self):
        self.kube.add(deployment("tasmota", after(120)))
        result = FAILOVER.set_many([{"ns": "lab", "name": "tasmota", "mode": "custom"}])
        self.assertEqual([], result["changed"])
        self.assertEqual([], self.kube.writes)

    def test_back_to_the_default_clears_the_list(self):
        FAILOVER.set_many([{"ns": "lab", "name": "frigate", "mode": "default"}])
        self.assertIsNone(self.kube.writes[0][2]["spec"]["template"]["spec"]["tolerations"])
        self.assertIsNone(self.tolerations("frigate"))


class LonghornPolicyTests(unittest.TestCase):
    """A container moved off a dead node cannot mount a single-node volume
    unless Longhorn lets go of the dead node's pods."""

    def setUp(self):
        keep_bound(self, LHCAP)
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
            dep, _ = MANIFESTS.build_deployment(cfg)
        return dep["spec"]["template"]["spec"]

    def test_a_new_container_moves_unless_told_otherwise(self):
        self.assertEqual("move", FAILOVER.mode_of(self.build()))

    def test_a_new_container_can_move_after_90_seconds(self):
        self.assertEqual("slow", FAILOVER.mode_of(self.build(failover="slow")))

    def test_a_new_container_can_wait_for_its_node(self):
        self.assertEqual("wait", FAILOVER.mode_of(self.build(failover="wait")))


if __name__ == "__main__":
    unittest.main()
