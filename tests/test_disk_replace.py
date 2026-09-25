import copy
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_disks as DISKS

GiB = 1024 ** 3
EXTRA = "/var/lib/harvester/extra-disks/7f2c"
NOT_READY = [{"type": "Ready", "status": "False", "reason": "DiskNotReady", "message": "failed to get disk config"}]


def replica(name, volume, node, disk_uuid, healthy=True):
    return {"metadata": {"name": name},
            "spec": {"volumeName": volume, "nodeID": node, "diskID": disk_uuid, "diskPath": "",
                     "healthyAt": "2026-09-01T00:00:00Z" if healthy else "", "failedAt": "" if healthy else "2026-09-24T00:00:00Z"},
            "status": {"currentState": "running" if healthy else "error"}}


def volume(name, copies, claim):
    return {"metadata": {"name": name}, "spec": {"numberOfReplicas": copies},
            "status": {"kubernetesStatus": {"namespace": "lab", "pvcName": claim}}}


class Cluster:
    """Three nodes. node3's extra drive has died: Harvester's block device for
    it is inactive, and Longhorn still lists the disk with three replicas."""

    def __init__(self, harvester=True, probe=None, disk_path=EXTRA, ready=False):
        self.nodes = {}
        for n in ("node1", "node2", "node3"):
            disks = {"default-disk": {"path": "/var/lib/harvester/defaultdisk", "allowScheduling": True}}
            status = {"default-disk": {"storageMaximum": 100 * GiB, "storageAvailable": 80 * GiB,
                                       "conditions": [{"type": "Ready", "status": "True"}], "diskUUID": f"u-{n}"}}
            self.nodes[n] = {"metadata": {"name": n}, "spec": {"allowScheduling": True, "disks": disks},
                             "status": {"diskStatus": status}}
        self.nodes["node3"]["spec"]["disks"]["bd-sdb"] = {"path": disk_path, "allowScheduling": True}
        self.nodes["node3"]["status"]["diskStatus"]["bd-sdb"] = {
            "storageMaximum": 900 * GiB, "storageAvailable": 0, "diskUUID": "u-dead",
            "conditions": [{"type": "Ready", "status": "True"}] if ready else NOT_READY,
            "scheduledReplica": {"r-only": 1, "r-waits": 1, "r-else": 1}}
        self.replicas = [
            replica("r-only", "pvc-scratch", "node3", "u-dead", healthy=False),
            replica("r-waits", "pvc-frigate", "node3", "u-dead", healthy=False),
            replica("r-waits-1", "pvc-frigate", "node1", "u-node1"),
            replica("r-waits-2", "pvc-frigate", "node2", "u-node2"),
            replica("r-else", "pvc-jelly", "node3", "u-dead", healthy=False),
            replica("r-else-1", "pvc-jelly", "node1", "u-node1")]
        self.volumes = [volume("pvc-scratch", 1, "scratch"), volume("pvc-frigate", 3, "frigate-config"),
                        volume("pvc-jelly", 2, "jellyfin-config")]
        self.bds = ([{"metadata": {"name": "bd-sdb"},
                      "spec": {"nodeName": "node3", "devPath": "/dev/sdb", "provision": True},
                      "status": {"state": "Inactive", "deviceStatus": {"devPath": "/dev/sdb", "capacity": {"sizeBytes": 931 * GiB},
                                                                      "fileSystem": {"mountPoint": disk_path}}}}]
                    if harvester else None)
        self.probe = probe if probe is not None else {}
        self.sent = []
        DISKS.bind(self.get, self.send, lambda: self.probe)

    def get(self, path):
        if path == DISKS.BD:
            if self.bds is None:
                raise urllib.error.HTTPError(path, 404, "no", None, None)
            return {"items": copy.deepcopy(self.bds)}
        if path.endswith("/nodes"):
            return {"items": copy.deepcopy(list(self.nodes.values()))}
        if "/nodes/" in path:
            return copy.deepcopy(self.nodes[path.rsplit("/", 1)[1]])
        if path.endswith("/replicas"):
            return {"items": copy.deepcopy(self.replicas)}
        if path.endswith("/volumes"):
            return {"items": copy.deepcopy(self.volumes)}
        raise AssertionError(path)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        name = path.rsplit("/", 1)[1]
        if method == "DELETE" and "/replicas/" in path:
            self.replicas = [r for r in self.replicas if r["metadata"]["name"] != name]
            # Longhorn's node controller drops the record shortly after.
            self.nodes["node3"]["status"]["diskStatus"]["bd-sdb"]["scheduledReplica"].pop(name, None)
        elif method == "PATCH" and "/nodes/" in path:
            disks = self.nodes[name]["spec"]["disks"]
            for disk_id, change in body["spec"]["disks"].items():
                if change is None:
                    disks.pop(disk_id, None)
                else:
                    disks[disk_id].update(change)
        elif method == "PATCH" and DISKS.BD in path:
            self.bds[0]["spec"].update(body["spec"])
        elif method == "DELETE" and DISKS.BD in path:
            self.bds = []
        return body


class OPS:
    def start(self, kind, title, resource, href, ref, message=""):
        return {"id": "op", "kind": kind, "title": title, "ref": ref, "status": "running"}


def run(item, limit=20):
    for _ in range(limit):
        status, progress, message = DISKS.retire_step(item)
        item.update(status=status, progress=progress, message=message)
        if status != "running":
            return item
    raise AssertionError("never finished: " + item["message"])


class MissingDriveTests(unittest.TestCase):
    """Longhorn only says a disk is not ready; whether the drive has gone -
    dead, or missing when the host started - is said in words."""

    def test_harvester_names_a_drive_it_has_lost(self):
        Cluster()
        disks = [d for row in DISKS.inventory()["nodes"]["node3"] for d in row["longhorn"]]
        dead = next(d for d in disks if d["id"] == "bd-sdb")
        self.assertTrue(dead["failed"])
        self.assertIn("Harvester no longer finds this drive", dead["missing"])

    def test_a_folder_whose_drive_is_not_mounted_sits_on_the_system_disk(self):
        """Booting with the drive gone: with nofail the host starts, and the
        mount folder is just a folder on the system disk."""
        Cluster(harvester=False, disk_path="/mnt/disk2", probe={"node3": {
            "disks": [{"name": "nvme0n1", "size_gb": 465}],
            "mounts": [{"disk": "nvme0n1", "mountpoint": "/"}]}})
        disks = [d for row in DISKS.inventory()["nodes"]["node3"] for d in row["longhorn"]]
        dead = next(d for d in disks if d["id"] == "bd-sdb")
        self.assertIn("nothing is mounted at /mnt/disk2", dead["missing"])
        self.assertIn("when the host started", dead["missing"])

    def test_a_failed_disk_raises_an_alert(self):
        Cluster()
        facts = DISKS.alert_facts(DISKS.inventory())
        self.assertEqual(["disks:node3:bd-sdb"], [f["key"] for f in facts])
        self.assertEqual("critical", facts[0]["severity"])


class ReplaceFailedDiskTests(unittest.TestCase):
    def test_the_review_says_what_happens_to_every_volume(self):
        Cluster()
        plan = DISKS.retire_plan("node3", "bd-sdb")
        outcomes = {row["claim"]: row["outcome"] for row in plan["volumes"]}
        self.assertEqual({"lab/scratch": "only-copy", "lab/frigate-config": "waits",
                          "lab/jellyfin-config": "elsewhere"}, outcomes)
        self.assertEqual(1, plan["only_copies"])
        self.assertIn("rebuilds on node2", next(r for r in plan["volumes"] if r["outcome"] == "elsewhere")["why"])

    def test_a_working_disk_is_not_replaced_this_way(self):
        Cluster(ready=True)
        with self.assertRaisesRegex(ValueError, "is working"):
            DISKS.retire_plan("node3", "bd-sdb")

    def test_an_only_copy_is_kept_and_so_is_the_disk(self):
        """A drive that is only unplugged comes back with its data."""
        c = Cluster()
        item = run(DISKS.retire_start({"node": "node3", "disk": "bd-sdb"}, OPS()))
        self.assertEqual("succeeded", item["status"])
        self.assertEqual(["r-only"], [r["metadata"]["name"] for r in c.replicas if r["spec"]["nodeID"] == "node3"])
        self.assertIn("bd-sdb", c.nodes["node3"]["spec"]["disks"])
        self.assertFalse(c.nodes["node3"]["spec"]["disks"]["bd-sdb"]["allowScheduling"])
        self.assertIn("only copy", item["message"])

    def test_giving_up_the_only_copies_takes_the_disk_out_and_clears_harvesters_record(self):
        c = Cluster()
        with self.assertRaisesRegex(ValueError, "type bd-sdb"):
            DISKS.retire_start({"node": "node3", "disk": "bd-sdb", "force": True}, OPS())
        item = run(DISKS.retire_start({"node": "node3", "disk": "bd-sdb", "force": True, "confirm": "bd-sdb"}, OPS()))
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertEqual([], [r for r in c.replicas if r["spec"]["nodeID"] == "node3"])
        self.assertNotIn("bd-sdb", c.nodes["node3"]["spec"]["disks"])
        self.assertEqual([], c.bds)                     # the dead drive's inactive record
        self.assertIn("Add the new drive", item["message"])

    def test_healthy_copies_elsewhere_are_never_touched(self):
        c = Cluster()
        run(DISKS.retire_start({"node": "node3", "disk": "bd-sdb", "force": True, "confirm": "bd-sdb"}, OPS()))
        self.assertEqual({"r-waits-1", "r-waits-2", "r-else-1"}, {r["metadata"]["name"] for r in c.replicas})

    def test_off_harvester_the_disk_leaves_longhorn_directly(self):
        c = Cluster(harvester=False, disk_path="/mnt/disk2")
        item = run(DISKS.retire_start({"node": "node3", "disk": "bd-sdb", "force": True, "confirm": "bd-sdb"}, OPS()))
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertNotIn("bd-sdb", c.nodes["node3"]["spec"]["disks"])


if __name__ == "__main__":
    unittest.main()
