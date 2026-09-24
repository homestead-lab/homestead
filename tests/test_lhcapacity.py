import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_lhcapacity as LHCAP

GiB = 1024 ** 3


def node(name, size, scheduled, available, allow=True):
    return {"metadata": {"name": name}, "spec": {"allowScheduling": allow, "disks": {"d": {"path": "/var/lib/harvester/defaultdisk"}}},
            "status": {"diskStatus": {"d": {"storageMaximum": size * GiB, "storageScheduled": scheduled * GiB,
                                            "storageAvailable": available * GiB}}}}


class Cluster:
    def __init__(self, over="100", harvester=False):
        self.settings = {LHCAP.OVER: {"value": over}, LHCAP.MINIMAL: {"value": "25"}}
        self.nodes = [node("node1", 117, 99, 90), node("node2", 396, 160, 380)]
        self.sent = []
        self.harvester = harvester
        LHCAP.bind(self.get, self.send, lambda: {"enabled": False, "harvester_setting": False if harvester else None})

    def get(self, path):
        if path.endswith("/nodes"):
            return {"items": self.nodes}
        if "/settings/" in path and path.startswith(LHCAP.LH):
            return self.settings[path.rsplit("/", 1)[1]]
        if path == LHCAP.HARVESTER_V2:
            return {"metadata": {"name": "longhorn-v2-data-engine-enabled"}, "value": "false"}
        raise urllib.error.HTTPError(path, 404, "missing", None, None)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        return body


class CapacityTests(unittest.TestCase):
    def test_room_and_the_largest_volume_that_fits(self):
        Cluster()
        cap = LHCAP.status()
        one, two = cap["nodes"]
        self.assertEqual((99.0, 117.0, 18.0, "warn"), (one["allocated_gb"], one["limit_gb"], one["room_gb"], one["level"]))
        self.assertEqual((236.0, "ok"), (two["room_gb"], two["level"]))
        # Two copies need two nodes with room: node1's is the limit.
        self.assertEqual({"1": 236.0, "2": 18.0, "3": 0.0}, cap["largest"])

    def test_over_provisioning_raises_the_limit(self):
        Cluster(over="200")
        one = LHCAP.status()["nodes"][0]
        self.assertEqual((234.0, 135.0), (one["limit_gb"], one["room_gb"]))

    def test_a_disk_short_of_free_space_or_switched_off_takes_nothing(self):
        c = Cluster()
        c.nodes = [node("node1", 100, 10, 20), node("node2", 100, 10, 90, allow=False)]
        one, two = LHCAP.status()["nodes"]
        self.assertIn("physically free", one["blocked"])
        self.assertEqual((0.0, "crit"), (one["room_gb"], one["level"]))
        self.assertIn("scheduling is off", two["blocked"])
        facts = LHCAP.alert_facts(LHCAP.status())
        self.assertEqual(["capacity:node1", "capacity:node2"], [f["key"] for f in facts])

    def test_saving_settings(self):
        c = Cluster()
        result = LHCAP.save({"over_provisioning": 150, "minimal_available": 25, "v2": True})
        self.assertIn(("PATCH", f"{LHCAP.LH}/settings/{LHCAP.OVER}", {"value": "150"}), c.sent)
        self.assertIn(("PATCH", f"{LHCAP.LH}/settings/{LHCAP.V2}", {"value": "true"}), c.sent)
        self.assertFalse([p for m, p, b in c.sent if p.endswith(LHCAP.MINIMAL)])      # unchanged
        self.assertIn("over-provisioning 150%", result["detail"])
        for bad in ({"over_provisioning": 50}, {"minimal_available": 101}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                LHCAP.save(bad)

    def test_on_harvester_v2_goes_through_harvesters_setting(self):
        c = Cluster(harvester=True)
        LHCAP.save({"v2": True})
        method, path, body = c.sent[-1]
        self.assertEqual(("PUT", LHCAP.HARVESTER_V2, "true"), (method, path, body["value"]))


if __name__ == "__main__":
    unittest.main()
