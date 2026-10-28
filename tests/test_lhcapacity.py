import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_lhcapacity as LHCAP
from fakes.kube import Kube

GiB = 1024 ** 3
LH_VERSION = "longhorn.io/v1beta2"


def node(name, size, scheduled, available, allow=True):
    return {"apiVersion": LH_VERSION, "kind": "Node", "metadata": {"namespace": "longhorn-system", "name": name},
            "spec": {"allowScheduling": allow, "disks": {"d": {"path": "/var/lib/harvester/defaultdisk"}}},
            "status": {"diskStatus": {"d": {"storageMaximum": size * GiB, "storageScheduled": scheduled * GiB,
                                            "storageAvailable": available * GiB}}}}


def setting(name, value):
    return {"apiVersion": LH_VERSION, "kind": "Setting", "metadata": {"namespace": "longhorn-system", "name": name},
            "value": value}


class Cluster:
    """Longhorn's settings and nodes on the shared fake API (tests/fakes/kube.py).
    Every Longhorn setting exists, as after an install; one never set has an
    empty value."""
    def __init__(self, test, over="100", harvester=False, nodes=None):
        self.kube = test.enterContext(Kube(
            setting(LHCAP.OVER, over), setting(LHCAP.MINIMAL, "25"), setting(LHCAP.V2, "false"),
            setting(LHCAP.REBUILD, ""),
            {"apiVersion": "harvesterhci.io/v1beta1", "kind": "Setting",
             "metadata": {"name": "longhorn-v2-data-engine-enabled"}, "value": "false"},
            *(nodes or (node("node1", 117, 99, 90), node("node2", 396, 160, 380)))))
        test.addCleanup(LHCAP.bind, *(getattr(LHCAP, name, None) for name in ("kget", "ksend", "v2_status")))
        LHCAP.bind(K.kget, K.ksend, lambda: {"enabled": False, "harvester_setting": False if harvester else None})

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]


class CapacityTests(unittest.TestCase):
    def test_room_and_the_largest_volume_that_fits(self):
        Cluster(self)
        cap = LHCAP.status()
        one, two = cap["nodes"]
        self.assertEqual((99.0, 117.0, 18.0, "warn"), (one["allocated_gb"], one["limit_gb"], one["room_gb"], one["level"]))
        self.assertEqual((236.0, "ok"), (two["room_gb"], two["level"]))
        self.assertIn("allocation limit", LHCAP.alert_facts(cap)[0]["title"])
        # Two copies need two nodes with room: node1's is the limit.
        self.assertEqual({"1": 236.0, "2": 18.0, "3": 0.0}, cap["largest"])

    def test_over_provisioning_raises_the_limit(self):
        Cluster(self, over="200")
        one = LHCAP.status()["nodes"][0]
        self.assertEqual((234.0, 135.0), (one["limit_gb"], one["room_gb"]))

    def test_physical_rebuild_budget_is_not_logical_overprovisioning(self):
        Cluster(self, over="200", nodes=[node("node1", 458, 193, 219)])
        n = LHCAP.status()["nodes"][0]
        self.assertEqual(723, n["room_gb"])
        self.assertEqual(104.5, n["physical_room_gb"])
        self.assertLess(n["physical_room_gb"], 191)  # existing replica footprint cannot rebuild here

    def test_rebuild_budget_is_best_eligible_disk_not_sum_of_disks(self):
        n = node("node1", 100, 10, 80)
        n["spec"]["disks"]["other"] = {}
        n["status"]["diskStatus"]["other"] = node("other", 100, 10, 90)["status"]["diskStatus"]["d"]
        c = Cluster(self, nodes=[n])
        self.assertEqual(65, LHCAP.status()["nodes"][0]["physical_room_gb"])
        n["spec"]["disks"]["other"]["allowScheduling"] = False
        c.kube.add(n)
        self.assertEqual(55, LHCAP.status()["nodes"][0]["physical_room_gb"])
        n["spec"]["allowScheduling"] = False
        c.kube.add(n)
        self.assertEqual(0, LHCAP.status()["nodes"][0]["physical_room_gb"])

    def test_a_disk_short_of_free_space_or_switched_off_takes_nothing(self):
        Cluster(self, nodes=[node("node1", 100, 10, 20), node("node2", 100, 10, 90, allow=False)])
        one, two = LHCAP.status()["nodes"]
        self.assertIn("physically free", one["blocked"])
        self.assertEqual((0.0, "crit"), (one["room_gb"], one["level"]))
        self.assertIn("scheduling is off", two["blocked"])
        facts = LHCAP.alert_facts(LHCAP.status())
        self.assertEqual(["capacity:node1", "capacity:node2"], [f["key"] for f in facts])
        self.assertTrue(all("scheduling is blocked" in f["title"] for f in facts))

    def test_saving_settings(self):
        c = Cluster(self)
        result = LHCAP.save({"over_provisioning": 150, "minimal_available": 25, "v2": True}, allow_v2_enable=True)
        self.assertIn(("PATCH", f"{LHCAP.LH}/settings/{LHCAP.OVER}", {"value": "150"}), c.sent)
        self.assertIn(("PATCH", f"{LHCAP.LH}/settings/{LHCAP.V2}", {"value": "true"}), c.sent)
        self.assertFalse([p for m, p, b in c.sent if p.endswith(LHCAP.MINIMAL)])      # unchanged
        self.assertIn("over-provisioning 150%", result["detail"])
        self.assertEqual("150", c.kube.object(f"{LHCAP.LH}/settings/{LHCAP.OVER}")["value"])
        for bad in ({"over_provisioning": 50}, {"minimal_available": 101}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                LHCAP.save(bad)

    def test_direct_enable_is_refused_before_any_settings_are_written(self):
        c = Cluster(self)
        with self.assertRaisesRegex(ValueError, "Set up Longhorn V2"):
            LHCAP.save({"v2": True, "over_provisioning": 150, "allow_v2_enable": True})
        self.assertEqual([], c.sent)

    def test_replicas_rebuilt_at_once_per_node(self):
        c = Cluster(self)
        self.assertEqual(5, LHCAP.settings()["rebuild_limit"], "Longhorn's default when unset")
        result = LHCAP.save({"rebuild_limit": 2})
        self.assertIn(("PATCH", f"{LHCAP.LH}/settings/{LHCAP.REBUILD}", {"value": "2"}), c.sent)
        self.assertIn("2 replica rebuilds at once per node", result["detail"])
        for bad in (0, 11, "x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                LHCAP.save({"rebuild_limit": bad})

    def test_on_harvester_v2_goes_through_harvesters_setting(self):
        c = Cluster(self, harvester=True)
        LHCAP.save({"v2": True}, allow_v2_enable=True)
        method, path, body = c.sent[-1]
        self.assertEqual(("PUT", LHCAP.HARVESTER_V2, "true"), (method, path, body["value"]))
        self.assertEqual("true", c.kube.object(LHCAP.HARVESTER_V2)["value"])


class NodeDownTests(unittest.TestCase):
    """#440: the impact preview needs to know when the policy couldn't be
    read, which _setting hides behind its default."""
    PATH = f"{LHCAP.LH}/settings/{LHCAP.NODE_DOWN}"

    def test_the_policy_is_read(self):
        self.assertEqual("delete-statefulset-pod",
                         LHCAP.node_down(lambda p: {"value": "delete-statefulset-pod"} if p == self.PATH else {}))
        self.assertEqual("do-nothing", LHCAP.node_down(lambda p: {"value": "", "default": "do-nothing"}))

    def test_a_failed_read_is_none_not_the_default(self):
        def missing(path):
            raise urllib.error.HTTPError(path, 404, "missing", None, None)
        self.assertIsNone(LHCAP.node_down(missing))


if __name__ == "__main__":
    unittest.main()
