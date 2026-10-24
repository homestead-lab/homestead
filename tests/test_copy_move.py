"""Moving one volume's copies onto disks with other tags, while it runs.

k3s-test is one host with an NVMe (tagged nvme) and a SATA disk (tagged
ssd). A Windows VM's 70 GiB disk had its only copy on the SATA disk; moving
it to the NVMe must not stop the VM."""
import copy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_copy_move as M
import homestead_rebalance as REBALANCE
import homestead_reclass as RECLASS

LH, GIB = M.LH, M.GIB


class Cluster:
    def __init__(self, state="attached", soft=True, offline_field=True):
        self.node = {"metadata": {"name": "k3s-test"}, "spec": {"allowScheduling": True, "tags": [], "disks": {
            "default": {"path": "/var/lib/longhorn/", "tags": ["nvme", "os", "ssd"], "allowScheduling": True, "storageReserved": 10 * GIB},
            "sda": {"path": "/mnt/sda", "tags": ["ssd"], "allowScheduling": True, "storageReserved": 0}}},
            "status": {"diskStatus": {
                "default": {"diskUUID": "u-nvme", "storageMaximum": 200 * GIB, "storageAvailable": 175 * GIB, "storageScheduled": 0},
                "sda": {"diskUUID": "u-sda", "storageMaximum": 900 * GIB, "storageAvailable": 700 * GIB, "storageScheduled": 70 * GIB}}}}
        spec = {"numberOfReplicas": 1, "size": str(70 * GIB), "diskSelector": [], "nodeSelector": []}
        if soft:
            spec["replicaSoftAntiAffinity"] = "ignored"
        if offline_field:
            spec["offlineRebuilding"] = "ignored"
        self.volume = {"metadata": {"name": "pvc-win", "annotations": {}}, "spec": spec,
                       "status": {"state": state, "robustness": "healthy" if state == "attached" else "unknown",
                                  "currentNodeID": "k3s-test" if state == "attached" else "", "actualSize": 12 * GIB,
                                  "kubernetesStatus": {"namespace": "lab", "pvcName": "win-dev-disk"}}}
        self.replicas = [self.replica("pvc-win-r-old", "u-sda", "/mnt/sda")]
        self.settings = {"offline-replica-rebuilding": "false"}
        self.sent = []

    def replica(self, name, uuid, path, healthy=True):
        return {"metadata": {"name": name}, "spec": {"volumeName": "pvc-win", "nodeID": "k3s-test", "diskID": uuid, "diskPath": path,
                                                     **({"healthyAt": "2026-10-24T00:00:00Z"} if healthy else {})},
                "status": {"currentState": "running" if healthy else "starting"}}

    def get(self, path):
        if path == f"{LH}/nodes":
            return {"items": [copy.deepcopy(self.node)]}
        if path == f"{LH}/volumes/pvc-win":
            return copy.deepcopy(self.volume)
        if path == f"{LH}/replicas":
            return {"items": copy.deepcopy(self.replicas)}
        if path == f"{LH}/engines":
            return {"items": [{"spec": {"volumeName": "pvc-win"},
                               "status": {"replicaModeMap": {r["metadata"]["name"]: "RW" for r in self.replicas if r["spec"].get("healthyAt")}}}]}
        if path.startswith(f"{LH}/settings/") and path.rsplit("/", 1)[1] in self.settings:
            return {"value": self.settings[path.rsplit("/", 1)[1]]}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, ctype=None):
        self.sent.append((method, path, copy.deepcopy(body)))
        if method == "PATCH" and path == f"{LH}/volumes/pvc-win":
            self.volume["spec"].update(body.get("spec") or {})
            notes = self.volume["metadata"]["annotations"]
            for key, value in ((body.get("metadata") or {}).get("annotations") or {}).items():
                notes.pop(key, None) if value is None else notes.__setitem__(key, value)
        if method == "DELETE" and "/replicas/" in path:
            self.replicas = [r for r in self.replicas if r["metadata"]["name"] != path.rsplit("/", 1)[1]]
        return {}


class CopyMoveTests(unittest.TestCase):
    def setUp(self, **kw):
        self.lh = Cluster(**kw)
        for patch in (mock.patch.object(REBALANCE, "kget", self.lh.get), mock.patch.object(REBALANCE, "ksend", self.lh.send),
                      mock.patch.object(REBALANCE, "is_leader", lambda: True)):
            patch.start()
            self.addCleanup(patch.stop)

    def job(self, plan):
        return {"ref": {"volume": "pvc-win", "claim": plan["claim"], "disk_tags": plan["disk_tags"], "node_tags": plan["node_tags"],
                        "total": plan["moves"], "moved": 0, "stage": "prepare", "size_gb": plan["size_gb"]}}

    def step(self, item, times=1):
        result = None
        for _ in range(times):
            result = M.status(item)
        return result

    def test_the_plan_moves_the_copy_off_the_sata_disk_onto_the_nvme(self):
        plan = M.plan("pvc-win", ["nvme"])
        self.assertTrue(plan["ok"], plan["blockers"])
        self.assertEqual("lab/win-dev-disk", plan["claim"])
        self.assertEqual([("/mnt/sda", True)], [(c["path"], c["moves"]) for c in plan["copies"]])
        self.assertEqual(["/var/lib/longhorn/"], [t["path"] for t in plan["targets"] if t["fits"]])
        self.assertEqual(["nvme", "os", "ssd"], plan["choices"]["disk_tags"])

    def test_nothing_chosen_or_nothing_to_move_or_no_room_cannot_start(self):
        self.assertIn("Choose the disk tags", M.plan("pvc-win")["blockers"][0])
        self.assertIn("already", M.plan("pvc-win", ["ssd"])["blockers"][0])
        self.assertIn("No disk Longhorn may use carries", M.plan("pvc-win", ["hdd"])["blockers"][0])
        self.lh.node["status"]["diskStatus"]["default"]["storageAvailable"] = 40 * GIB
        self.assertIn("room", " ".join(M.plan("pvc-win", ["nvme"])["blockers"]))

    def test_a_running_vms_copy_moves_without_ever_having_fewer_whole_copies(self):
        plan = M.plan("pvc-win", ["nvme"])
        item = self.job(plan)
        self.assertEqual("running", self.step(item)[0])                         # tags set
        self.assertEqual(["nvme"], self.lh.volume["spec"]["diskSelector"])
        self.assertEqual("enabled", self.lh.volume["spec"]["replicaSoftAntiAffinity"], "beside the old copy, on one host")
        self.step(item)                                                         # one copy more asked for
        self.assertEqual(2, self.lh.volume["spec"]["numberOfReplicas"])
        self.assertEqual("1", self.lh.volume["metadata"]["annotations"][REBALANCE.MOVING])
        self.lh.replicas.append(self.lh.replica("pvc-win-r-new", "u-nvme", "/var/lib/longhorn/", healthy=False))
        self.assertIn("Building", self.step(item)[2])
        self.assertEqual(["pvc-win-r-old", "pvc-win-r-new"], [r["metadata"]["name"] for r in self.lh.replicas], "the old copy stays while it builds")
        self.lh.replicas[1] = self.lh.replica("pvc-win-r-new", "u-nvme", "/var/lib/longhorn/")
        self.step(item)                                                         # whole: the old copy goes
        self.assertEqual(["pvc-win-r-new"], [r["metadata"]["name"] for r in self.lh.replicas])
        self.step(item, 2)                                                      # settles, then nothing left off the NVMe
        state, progress, message = self.step(item)
        self.assertEqual(("succeeded", 100), (state, progress), message)
        spec = self.lh.volume["spec"]
        self.assertEqual((1, ["nvme"], "ignored"), (spec["numberOfReplicas"], spec["diskSelector"], spec["replicaSoftAntiAffinity"]))
        self.assertNotIn(REBALANCE.MOVING, self.lh.volume["metadata"]["annotations"])

    def test_a_copy_longhorn_puts_on_the_wrong_disk_is_dropped_and_the_old_tags_come_back(self):
        item = self.job(M.plan("pvc-win", ["nvme"]))
        self.step(item, 2)
        self.lh.replicas.append(self.lh.replica("pvc-win-r-new", "u-sda", "/mnt/sda"))
        state, _, message = self.step(item)
        self.assertEqual("failed", state)
        self.assertIn("/mnt/sda", message)
        self.assertEqual(["pvc-win-r-old"], [r["metadata"]["name"] for r in self.lh.replicas])
        self.assertEqual((1, [], "ignored"), (self.lh.volume["spec"]["numberOfReplicas"], self.lh.volume["spec"]["diskSelector"],
                                              self.lh.volume["spec"]["replicaSoftAntiAffinity"]))

    def test_stopping_puts_the_count_and_tags_back_and_keeps_the_old_copy(self):
        item = self.job(M.plan("pvc-win", ["nvme"]))
        self.step(item, 2)
        M.cancel_run(item, None)
        self.assertEqual((1, []), (self.lh.volume["spec"]["numberOfReplicas"], self.lh.volume["spec"]["diskSelector"]))
        self.assertEqual(["pvc-win-r-old"], [r["metadata"]["name"] for r in self.lh.replicas])

    def test_a_detached_volume_moves_with_offline_rebuilding_on_for_it(self):
        self.lh.volume["status"].update(state="detached", robustness="unknown", currentNodeID="")
        item = self.job(M.plan("pvc-win", ["nvme"]))
        self.step(item)
        self.assertEqual("enabled", self.lh.volume["spec"]["offlineRebuilding"])

    def test_a_detached_volume_on_a_longhorn_without_offline_rebuilding_cannot_start(self):
        del self.lh.volume["spec"]["offlineRebuilding"]
        self.lh.volume["status"].update(state="detached", robustness="unknown", currentNodeID="")
        self.assertIn("detached", M.plan("pvc-win", ["nvme"])["blockers"][0])

    def test_a_move_starts_only_as_reviewed(self):
        with mock.patch.object(M.OPS, "snapshot", return_value=[]), mock.patch.object(M.OPS, "start", return_value={"id": "op"}) as start:
            stale = M.start_reviewed({"volume": "pvc-win", "disk_tags": ["nvme"], "review_token": "old"})
            self.assertEqual(409, stale.code)
            good = M.start_reviewed({"volume": "pvc-win", "disk_tags": ["nvme"], "review_token": M.plan("pvc-win", ["nvme"])["review_token"]})
        self.assertEqual(202, good.code)
        self.assertEqual(["nvme"], start.call_args.args[4]["disk_tags"])


class LiveReclassTests(unittest.TestCase):
    LONGHORN = {"provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "1", "staleReplicaTimeout": "30", "fsType": "ext4"}}

    def test_a_class_differing_only_in_disk_tags_can_move_live(self):
        nvme = {"provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "1", "diskSelector": "nvme"}}
        self.assertEqual({"disk_tags": ["nvme"], "node_tags": []}, RECLASS.live_move(self.LONGHORN, nvme))

    def test_another_engine_copy_count_or_driver_needs_the_copy(self):
        for params in ({"numberOfReplicas": "1", "diskSelector": "nvme", "dataEngine": "v2"},
                       {"numberOfReplicas": "2", "diskSelector": "nvme"}, {"numberOfReplicas": "1"}):
            with self.subTest(params=params):
                self.assertIsNone(RECLASS.live_move(self.LONGHORN, {"provisioner": "driver.longhorn.io", "parameters": params}))
        self.assertIsNone(RECLASS.live_move(self.LONGHORN, {"provisioner": "rancher.io/local-path", "parameters": {}}))


if __name__ == "__main__":
    unittest.main()
