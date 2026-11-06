import sys
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_disks as DISKS
import homestead_kube as K
from fakes.disk_guards import disk_guards
from fakes.kube import Kube

GiB = 1024 ** 3
EXTRA = "/var/lib/harvester/extra-disks/7f2c"
NOT_READY = [{"type": "Ready", "status": "False", "reason": "DiskNotReady", "message": "failed to get disk config"}]
LH_VERSION = "longhorn.io/v1beta2"


def replica(name, volume, node, disk_uuid, healthy=True):
    return {"apiVersion": LH_VERSION, "kind": "Replica", "metadata": {"namespace": "longhorn-system", "name": name},
            "spec": {"volumeName": volume, "nodeID": node, "diskID": disk_uuid, "diskPath": "",
                     "healthyAt": "2026-09-01T00:00:00Z" if healthy else "", "failedAt": "" if healthy else "2026-09-24T00:00:00Z"},
            "status": {"currentState": "running" if healthy else "error"}}


def volume(name, copies, claim):
    return {"apiVersion": LH_VERSION, "kind": "Volume", "metadata": {"namespace": "longhorn-system", "name": name},
            "spec": {"numberOfReplicas": copies},
            "status": {"kubernetesStatus": {"namespace": "lab", "pvcName": claim}}}


class Cluster:
    """Three nodes, on the shared fake API (tests/fakes/kube.py). node3's extra
    drive has died: Harvester's block device for it is inactive, and Longhorn
    still lists the disk with three replicas."""

    def __init__(self, test, harvester=True, probe=None, disk_path=EXTRA, ready=False):
        nodes = []
        for n in ("node1", "node2", "node3"):
            disks = {"default-disk": {"path": "/var/lib/harvester/defaultdisk", "allowScheduling": True}}
            status = {"default-disk": {"storageMaximum": 100 * GiB, "storageAvailable": 80 * GiB,
                                       "conditions": [{"type": "Ready", "status": "True"}], "diskUUID": f"u-{n}"}}
            nodes.append({"apiVersion": LH_VERSION, "kind": "Node", "metadata": {"namespace": "longhorn-system", "name": n},
                          "spec": {"allowScheduling": True, "disks": disks},
                          "status": {"conditions": [{"type": "Ready", "status": "True"}], "diskStatus": status}})
        nodes[2]["spec"]["disks"]["bd-sdb"] = {"path": disk_path, "allowScheduling": True}
        nodes[2]["status"]["diskStatus"]["bd-sdb"] = {
            "storageMaximum": 900 * GiB, "storageAvailable": 0, "diskUUID": "u-dead",
            "conditions": [{"type": "Ready", "status": "True"}] if ready else NOT_READY,
            "scheduledReplica": {"r-only": 1, "r-waits": 1, "r-else": 1}}
        replicas = [
            replica("r-only", "pvc-scratch", "node3", "u-dead", healthy=False),
            replica("r-waits", "pvc-frigate", "node3", "u-dead", healthy=False),
            replica("r-waits-1", "pvc-frigate", "node1", "u-node1"),
            replica("r-waits-2", "pvc-frigate", "node2", "u-node2"),
            replica("r-else", "pvc-jelly", "node3", "u-dead", healthy=False),
            replica("r-else-1", "pvc-jelly", "node1", "u-node1")]
        volumes = [volume("pvc-scratch", 1, "scratch"), volume("pvc-frigate", 3, "frigate-config"),
                   volume("pvc-jelly", 2, "jellyfin-config")]
        bds = [{"apiVersion": "harvesterhci.io/v1beta1", "kind": "BlockDevice",
                "metadata": {"namespace": "longhorn-system", "name": "bd-sdb"},
                "spec": {"nodeName": "node3", "devPath": "/dev/sdb", "provision": True},
                "status": {"state": "Inactive", "deviceStatus": {"devPath": "/dev/sdb", "capacity": {"sizeBytes": 931 * GiB},
                                                                "fileSystem": {"mountPoint": disk_path}}}}]
        # The hosts are up; each volume's running engine has its healthy copies
        # as RW and has dropped the failed ones, as Longhorn does.
        hosts = [{"apiVersion": "v1", "kind": "Node", "metadata": {"name": n},
                  "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for n in ("node1", "node2", "node3")]
        engines = [{"apiVersion": LH_VERSION, "kind": "Engine", "metadata": {"namespace": "longhorn-system", "name": v["metadata"]["name"] + "-e-0"},
                    "spec": {"volumeName": v["metadata"]["name"]},
                    "status": {"currentState": "running", "replicaModeMap": {
                        r["metadata"]["name"]: "RW" for r in replicas
                        if r["spec"]["volumeName"] == v["metadata"]["name"] and r["spec"]["healthyAt"]}}} for v in volumes]
        # Off Harvester, its block devices are not there at all.
        self.kube = test.enterContext(Kube(*nodes, *hosts, *engines, *replicas, *volumes, *(bds if harvester else ()),
                                           absent_groups=() if harvester else ("harvesterhci.io",)))
        for item in replicas:
            self.kube.react("DELETE", self.kube.path_of(item), self._node_controller)
        self.probe = probe if probe is not None else {}
        disk_guards(test)
        test.addCleanup(DISKS.bind, *(getattr(DISKS, name, None) for name in ("kget", "ksend", "temps")))
        DISKS.bind(K.kget, K.ksend, lambda: self.probe)

    def _node_controller(self, method, path, body):
        # Longhorn's node controller drops the record shortly after.
        node = self.kube.object(f"{DISKS.LH}/nodes/node3")
        node["status"]["diskStatus"]["bd-sdb"]["scheduledReplica"].pop(path.rsplit("/", 1)[1], None)
        self.kube.add(node)

    @property
    def nodes(self):
        return {n["metadata"]["name"]: n for n in self.kube("GET", f"{DISKS.LH}/nodes")["items"]}

    @property
    def replicas(self):
        return self.kube("GET", f"{DISKS.LH}/replicas")["items"]

    @property
    def bds(self):
        return self.kube("GET", DISKS.BD)["items"]

    @property
    def sent(self):
        return [(method, path, body) for method, path, body, _ in self.kube.writes]

    def change_replica(self, name, **spec):
        replica_object = self.kube.object(f"{DISKS.LH}/replicas/{name}")
        replica_object["spec"].update(spec)
        self.kube.add(replica_object)


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
        Cluster(self)
        disks = [d for row in DISKS.inventory()["nodes"]["node3"] for d in row["longhorn"]]
        dead = next(d for d in disks if d["id"] == "bd-sdb")
        self.assertTrue(dead["failed"])
        self.assertIn("Harvester no longer finds this drive", dead["missing"])

    def test_a_folder_whose_drive_is_not_mounted_sits_on_the_system_disk(self):
        """Booting with the drive gone: with nofail the host starts, and the
        mount folder is just a folder on the system disk."""
        Cluster(self, harvester=False, disk_path="/mnt/disk2", probe={"node3": {
            "disks": [{"name": "nvme0n1", "size_gb": 465}],
            "mounts": [{"disk": "nvme0n1", "mountpoint": "/"}]}})
        disks = [d for row in DISKS.inventory()["nodes"]["node3"] for d in row["longhorn"]]
        dead = next(d for d in disks if d["id"] == "bd-sdb")
        self.assertIn("nothing is mounted at /mnt/disk2", dead["missing"])
        self.assertIn("when the host started", dead["missing"])

    def test_a_failed_disk_raises_an_alert(self):
        Cluster(self)
        facts = DISKS.alert_facts(DISKS.inventory())
        self.assertEqual(["disks:node3:bd-sdb"], [f["key"] for f in facts])
        self.assertEqual("critical", facts[0]["severity"])


class ReplaceFailedDiskTests(unittest.TestCase):
    def test_the_review_says_what_happens_to_every_volume(self):
        Cluster(self)
        plan = DISKS.retire_plan("node3", "bd-sdb")
        outcomes = {row["claim"]: row["outcome"] for row in plan["volumes"]}
        self.assertEqual({"lab/scratch": "only-copy", "lab/frigate-config": "waits",
                          "lab/jellyfin-config": "elsewhere"}, outcomes)
        self.assertEqual(1, plan["only_copies"])
        self.assertIn("rebuilds on node2", next(r for r in plan["volumes"] if r["outcome"] == "elsewhere")["why"])

    def test_a_working_disk_is_not_replaced_this_way(self):
        Cluster(self, ready=True)
        with self.assertRaisesRegex(ValueError, "is working"):
            DISKS.retire_plan("node3", "bd-sdb")

    def test_an_only_copy_is_kept_and_so_is_the_disk(self):
        """A drive that is only unplugged comes back with its data."""
        c = Cluster(self)
        item = run(DISKS.retire_start({"node": "node3", "disk": "bd-sdb"}, OPS()))
        self.assertEqual("succeeded", item["status"])
        self.assertEqual(["r-only"], [r["metadata"]["name"] for r in c.replicas if r["spec"]["nodeID"] == "node3"])
        self.assertIn("bd-sdb", c.nodes["node3"]["spec"]["disks"])
        self.assertFalse(c.nodes["node3"]["spec"]["disks"]["bd-sdb"]["allowScheduling"])
        self.assertIn("only copy", item["message"])

    def test_giving_up_the_only_copies_takes_the_disk_out_and_clears_harvesters_record(self):
        c = Cluster(self)
        with self.assertRaisesRegex(ValueError, "type bd-sdb"):
            DISKS.retire_start({"node": "node3", "disk": "bd-sdb", "force": True}, OPS())
        item = run(DISKS.retire_start({"node": "node3", "disk": "bd-sdb", "force": True, "confirm": "bd-sdb"}, OPS()))
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertEqual([], [r for r in c.replicas if r["spec"]["nodeID"] == "node3"])
        self.assertNotIn("bd-sdb", c.nodes["node3"]["spec"]["disks"])
        self.assertEqual([], c.bds)                     # the dead drive's inactive record
        self.assertIn("Add the new drive", item["message"])

    def test_a_copy_that_became_the_last_since_the_review_is_kept(self):
        c = Cluster(self)
        item = DISKS.retire_start({"node": "node3", "disk": "bd-sdb"}, OPS())
        # jellyfin's copy on node1 fails after the review: node3's is its last.
        c.change_replica("r-else-1", failedAt="2026-09-29T00:00:00Z")
        clock = [1000.0]
        self.enterContext(mock.patch.object(DISKS, "_clock", lambda: clock[0]))
        self.step(item, 3)
        clock[0] += DISKS.UNCONFIRMED_WAIT + 1
        item = run(item)
        self.assertEqual(["r-waits"], self.deleted(c))
        self.assertIn("r-else", item["ref"]["kept"])
        self.assertIn("could not confirm another whole copy", item["message"])

    def step(self, item, times=1):
        for _ in range(times):
            status, progress, message = DISKS.retire_step(item)
            item.update(status=status, progress=progress, message=message)
        return item

    def node_ready(self, c, name, ready):
        node = c.kube.object(f"/api/v1/nodes/{name}")
        node["status"]["conditions"] = [{"type": "Ready", "status": "True" if ready else "False"}]
        c.kube.add(node)

    def deleted(self, c):
        return [p.rsplit("/", 1)[1] for m, p, b in c.sent if m == "DELETE" and "/replicas/" in p]

    def test_a_copy_elsewhere_that_cannot_be_confirmed_for_a_moment_is_asked_again(self):
        # jellyfin's copy on node1 is whole, but node1 is NotReady for a moment.
        c = Cluster(self)
        clock = [1000.0]
        self.enterContext(mock.patch.object(DISKS, "_clock", lambda: clock[0]))
        item = DISKS.retire_start({"node": "node3", "disk": "bd-sdb"}, OPS())
        self.node_ready(c, "node1", False)
        self.step(item, 3)
        self.assertEqual("running", item["status"], item["message"])
        self.assertIn("could not confirm another whole copy", item["message"])
        self.assertNotIn("r-else", self.deleted(c), "its replica here stays while the copy elsewhere is unconfirmed")
        self.assertNotIn("r-else", item["ref"].get("keep") or [], "not kept for good")
        # node1 is back: the copy is confirmed and the failed replica here goes.
        clock[0] += 60
        self.node_ready(c, "node1", True)
        item = run(item)
        self.assertIn("r-else", self.deleted(c))
        self.assertEqual("succeeded", item["status"])
        # Only scratch, the only copy at review, is still named; jellyfin is not.
        self.assertIn("1 volume had the only copy on it", item["message"])
        self.assertNotIn("could not confirm", item["message"])
        self.assertEqual(["r-only"], item["ref"]["kept"])

    def test_a_copy_still_unconfirmed_after_half_an_hour_is_kept_and_said_so(self):
        c = Cluster(self)
        clock = [1000.0]
        self.enterContext(mock.patch.object(DISKS, "_clock", lambda: clock[0]))
        item = DISKS.retire_start({"node": "node3", "disk": "bd-sdb"}, OPS())
        self.node_ready(c, "node1", False)
        self.step(item, 3)
        clock[0] += DISKS.UNCONFIRMED_WAIT + 1
        item = run(item)
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertNotIn("r-else", self.deleted(c))
        # The reviewed only copy (scratch) and the unconfirmed one (jellyfin) are told apart.
        self.assertIn("1 volume had the only copy on it", item["message"])
        self.assertIn("Homestead could not confirm another whole copy of 1 volume", item["message"])
        # What to do differs: an only copy needs the drive or a backup; an unconfirmed one, another retire later.
        self.assertIn("retire this disk again once their other copy is whole", item["message"])

    def test_a_volume_unconfirmed_later_still_gets_its_own_half_hour(self):
        c = Cluster(self)
        clock = [1000.0]
        self.enterContext(mock.patch.object(DISKS, "_clock", lambda: clock[0]))
        item = DISKS.retire_start({"node": "node3", "disk": "bd-sdb"}, OPS())
        # jellyfin is unconfirmed from the start (node1 NotReady); frigate's failed
        # replica here could not be let go at first (the API refused), and twenty
        # minutes later its other copies cannot be confirmed either.
        self.node_ready(c, "node1", False)
        c.kube.fail(f"{DISKS.LH}/replicas/r-waits", 500, times=99, method="DELETE")
        self.step(item)                                  # no new replicas onto the disk
        with self.assertRaises(urllib.error.HTTPError):
            self.step(item)
        clock[0] += 20 * 60
        c.change_replica("r-waits-2", failedAt="2026-09-29T00:00:00Z")
        self.step(item)                                  # now nothing is let go: no refused DELETE
        # Past jellyfin's half hour, not frigate's: still waiting for frigate.
        clock[0] += 11 * 60
        self.step(item)
        self.assertEqual("running", item["status"], item["message"])
        self.assertIn("could not confirm another whole copy of 1 volume", item["message"])
        clock[0] += 20 * 60
        item = run(item)
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertLessEqual({"r-else", "r-waits"}, {r["metadata"]["name"] for r in c.replicas}, "both kept")
        self.assertIn("could not confirm another whole copy of 2 volumes", item["message"])

    def test_counts_are_of_volumes_not_replicas(self):
        c = Cluster(self)
        # A second failed replica of scratch on the same disk.
        c.kube.add(replica("r-only-b", "pvc-scratch", "node3", "u-dead", healthy=False))
        item = run(DISKS.retire_start({"node": "node3", "disk": "bd-sdb"}, OPS()))
        self.assertIn("1 volume had the only copy on it", item["message"])

    def test_nothing_is_deleted_when_the_replicas_cannot_be_read(self):
        c = Cluster(self)
        c.kube.fail(f"{DISKS.LH}/replicas", 500, times=99, method="GET")
        with self.assertRaises(urllib.error.HTTPError):
            DISKS.retire_plan("node3", "bd-sdb")
        self.assertFalse([m for m, p, b in c.sent if m == "DELETE"])

    def test_healthy_copies_elsewhere_are_never_touched(self):
        c = Cluster(self)
        run(DISKS.retire_start({"node": "node3", "disk": "bd-sdb", "force": True, "confirm": "bd-sdb"}, OPS()))
        self.assertEqual({"r-waits-1", "r-waits-2", "r-else-1"}, {r["metadata"]["name"] for r in c.replicas})

    def test_off_harvester_the_disk_leaves_longhorn_directly(self):
        c = Cluster(self, harvester=False, disk_path="/mnt/disk2")
        item = run(DISKS.retire_start({"node": "node3", "disk": "bd-sdb", "force": True, "confirm": "bd-sdb"}, OPS()))
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertNotIn("bd-sdb", c.nodes["node3"]["spec"]["disks"])


if __name__ == "__main__":
    unittest.main()
