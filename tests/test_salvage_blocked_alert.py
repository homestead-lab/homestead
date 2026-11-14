"""A volume whose only copy is on a disk Longhorn's auto-salvage refuses is
named before a restart finds it (November 2026: Homestead's own data, on a disk
under DiskPressure, did not come back after an undrained reboot)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_lhcapacity as LHCAP

GiB = 1024 ** 3


def lhnode(name, disks, allow=True):
    """disks: {disk name: (uuid, schedulable, reason, available GiB, maximum GiB)}"""
    return {"metadata": {"name": name},
            "spec": {"allowScheduling": allow, "disks": {d: {"path": f"/mnt/{d}", "allowScheduling": True} for d in disks}},
            "status": {"diskStatus": {d: {"diskUUID": uuid, "storageAvailable": free * GiB, "storageMaximum": size * GiB,
                                          "conditions": [{"type": "Schedulable", "status": "True" if ok else "False", "reason": reason}]}
                                      for d, (uuid, ok, reason, free, size) in disks.items()}}}


def replica(volume, node, disk, healthy=True, failed=False):
    return {"spec": {"volumeName": volume, "nodeID": node, "diskID": disk, "diskPath": f"/mnt/{disk}-path",
                     "healthyAt": "2026-11-10T00:00:00Z" if healthy else "", "failedAt": "2026-11-10T01:00:00Z" if failed else ""},
            "status": {"currentState": "stopped"}}


def volume(name, ns, claim):
    return {"metadata": {"name": name}, "status": {"kubernetesStatus": {"namespace": ns, "pvcName": claim}}}


class SalvageBlockedTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [lhnode("node1", {"sata": ("u-sata", False, "DiskPressure", 20, 100), "nvme": ("u-nvme", True, "", 300, 400)}),
                      lhnode("node2", {"d": ("u-d", True, "", 50, 100)}, allow=False)]
        self.volumes = [volume("pvc-data", "lab", "homestead-data"), volume("pvc-app", "apps", "jellyfin-config"),
                        volume("pvc-two", "apps", "two-copies"), volume("pvc-fine", "apps", "on-nvme"),
                        volume("pvc-host", "apps", "host-off")]
        self.replicas = [replica("pvc-data", "node1", "u-sata"),
                         replica("pvc-data", "node1", "u-nvme", failed=True),   # a failed copy cannot be salvaged from
                         replica("pvc-app", "node1", "u-sata"),
                         replica("pvc-two", "node1", "u-sata"), replica("pvc-two", "node1", "u-nvme"),
                         replica("pvc-fine", "node1", "u-nvme"),
                         replica("pvc-host", "node2", "u-d")]

    def rows(self):
        return LHCAP.salvage_blocked(self.volumes, self.replicas, self.nodes, ["lab/homestead-data"])

    def test_only_a_lone_copy_on_a_refusing_disk_or_host_is_named_and_homesteads_own_first(self):
        rows = self.rows()
        self.assertEqual(["lab/homestead-data", "apps/host-off", "apps/jellyfin-config"], [row["claim"] for row in rows])
        self.assertEqual([True, False, False], [row["own"] for row in rows])
        self.assertEqual(("node1", "/mnt/u-sata-path", "DiskPressure", 20 * GiB, 100 * GiB),
                         tuple(rows[0][key] for key in ("node", "path", "reason", "available", "maximum")))
        self.assertEqual("host", rows[1]["reason"])

    def test_alerts_lead_with_the_outcome_then_the_disk_space_and_the_fixes(self):
        facts = LHCAP.salvage_alert_facts({"rows": self.rows(), "minimal": 25})
        own, host, app = facts
        self.assertEqual(("salvage-blocked:pvc-data", "critical"), (own["key"], own["severity"]))
        self.assertEqual("After an unclean restart, Homestead's own data will not come back on its own", own["title"])
        self.assertIn("/mnt/u-sata-path on node1, which has 20.0 GiB free of 100.0 GiB (20%)", own["body"])
        self.assertIn("Longhorn keeps 25% of a disk free", own["body"])
        self.assertIn("so Homestead does not start", own["body"])
        for word in ("Free space on that disk", "Move copies", "Minimal free space in Settings, Hardware and storage"):
            self.assertIn(word, own["body"])
        self.assertEqual(("degraded", "After an unclean restart, apps/jellyfin-config will not come back on its own"),
                         (app["severity"], app["title"]))
        self.assertNotIn("Homestead does not start", app["body"])
        self.assertIn("does not place copies on node2", host["body"])
        for fact in facts:
            self.assertEqual("/volumes", fact["href"])
            self.assertTrue(fact["body"].endswith("."))

    def test_room_again_clears_it(self):
        self.nodes[0]["status"]["diskStatus"]["sata"]["conditions"][0].update(status="True", reason="")
        self.nodes[1]["spec"]["allowScheduling"] = True
        self.assertEqual([], self.rows())
        self.assertEqual([], LHCAP.salvage_alert_facts({"rows": [], "minimal": 25}))

    def test_a_restart_review_names_the_volumes_on_that_host_only(self):
        rows = self.rows()
        text = LHCAP.unclean_restart_warning(rows, "node1")
        self.assertTrue(text.startswith("If node1 stops without a drain, for example in a power cut, 2 volumes would not "
                                        "come back on their own: Homestead's own data, apps/jellyfin-config. "))
        self.assertIn("Move copies on Volumes", text)
        self.assertIn("1 volume would not come back on its own: apps/host-off. Its only copy is",
                      LHCAP.unclean_restart_warning(rows, "node2"))
        self.assertEqual("", LHCAP.unclean_restart_warning(rows, "node3"))


if __name__ == "__main__":
    unittest.main()
