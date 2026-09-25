import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_history as H

DAY = 86400


def overview(ready, boot="boot-1"):
    return {"cpu_pct": 1, "mem_pct": 1, "nodes_ready": int(ready), "nodes_total": 1,
            "nodes": [{"name": "node1", "status": "Ready" if ready else "NotReady", "cpu_pct": 1, "mem_pct": 1,
                       "boot_id": boot}]}


class NodeUptimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = H.DATA_DIR
        H.bind(self.tmp.name)
        self.start = 1_700_000_000 - 1_700_000_000 % DAY

    def tearDown(self):
        H.DATA_DIR = self.saved
        self.tmp.cleanup()

    def run_for(self, minutes, down_between=(), boot_changes_at=None):
        for i in range(0, minutes, 5):
            t = self.start + i * 60
            boot = "boot-2" if boot_changes_at is not None and i >= boot_changes_at else "boot-1"
            H.record(overview(not (down_between and down_between[0] <= i < down_between[1]), boot), now=t)
        return self.start + minutes * 60

    def test_an_outage_is_found_to_the_five_minutes(self):
        now = self.run_for(180, down_between=(60, 90))
        node = H.uptime(now)["nodes"]["node1"]
        outage = node["outages"][-1]
        self.assertEqual((self.start + 3600, 30 * 60, True), (outage["start"], outage["down_s"], outage["exact"]))
        self.assertFalse(outage["ongoing"])
        self.assertAlmostEqual(83.333, node["windows"]["24h"], places=2)

    def test_a_node_down_now_is_an_ongoing_outage(self):
        now = self.run_for(60, down_between=(40, 60))
        self.assertTrue(H.uptime(now)["nodes"]["node1"]["outages"][-1]["ongoing"])

    def test_a_new_boot_id_is_a_reboot(self):
        now = self.run_for(60, boot_changes_at=30)
        self.assertEqual([self.start + 30 * 60], H.uptime(now)["nodes"]["node1"]["reboots"])

    def test_always_up_is_a_hundred(self):
        now = self.run_for(60)
        node = H.uptime(now)["nodes"]["node1"]
        self.assertEqual(100.0, node["windows"]["24h"])
        self.assertEqual([], node["outages"])
        self.assertEqual(90, len(node["days"]))


if __name__ == "__main__":
    unittest.main()
