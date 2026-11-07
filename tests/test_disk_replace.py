import sys
import contextlib
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_cancel as CANCEL
import homestead_disks as DISKS
import homestead_kube as K
import homestead_operations as JOBS
from fakes.disk_guards import disk_guards
from fakes.kube import Kube
from rebind import keep_bound

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
        keep_bound(test, DISKS)   # also puts back mutation_scope and v2_tasks
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
    JOBS._write([item])    # its step saves what it is about to do
    for _ in range(limit):
        status, progress, message = DISKS.retire_step(item)
        item.update(status=status, progress=progress, message=message)
        if status != "running":
            return item
    raise AssertionError("never finished: " + item["message"])


class RetireWaitTests(unittest.TestCase):
    """A retire waiting on Longhorn ends: its waits had no deadline and raise
    nothing, so no limit on failed reads applied, and the job stayed "running"
    with every disk and host job that waits for it. Through the real job list."""
    NODE = f"{DISKS.LH}/nodes/node3"
    LIMIT = 30 * 60

    def setUp(self):
        self.enterContext(mock.patch.object(JOBS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        self.c = Cluster(self)
        self.enterContext(mock.patch.dict(JOBS.RESOLVERS, {}))
        self.enterContext(mock.patch.dict(JOBS.RESUMABLE, {}))
        self.enterContext(mock.patch.object(JOBS, "RESUMING", {}, create=True))
        self.enterContext(mock.patch.dict(JOBS.CANCELLERS, {}))
        self.enterContext(mock.patch.object(JOBS, "CLEANUPS", set()))
        DISKS.register(JOBS)
        CANCEL.register(JOBS)
        self.now = 1000.0
        self.enterContext(mock.patch.object(DISKS, "_clock", lambda: self.now))

    def start(self, **cfg):
        return DISKS.retire_start({"node": "node3", "disk": "bd-sdb", **cfg}, JOBS)["id"]

    def pass_time(self, seconds):
        # The jobs loop looks every few seconds: a longer gap restarts a wait.
        while seconds > 0:
            step = min(240, seconds)
            self.now += step
            seconds -= step
            JOBS.list_operations()

    def look(self, ident, times=1):
        for _ in range(times):
            job = next(job for job in JOBS.list_operations() if job["id"] == ident)
        return job

    def force(self):
        return self.start(force=True, confirm="bd-sdb")

    def disk_in_longhorn(self):
        return "bd-sdb" in self.c.kube.object(self.NODE)["spec"]["disks"]

    def record(self, name):
        node = self.c.kube.object(self.NODE)
        node["status"]["diskStatus"]["bd-sdb"]["scheduledReplica"][name] = 1
        self.c.kube.add(node)

    def forget_record(self, name):
        node = self.c.kube.object(self.NODE)
        node["status"]["diskStatus"]["bd-sdb"]["scheduledReplica"].pop(name)
        self.c.kube.add(node)

    def scheduling(self):
        return self.c.kube.object(self.NODE)["spec"]["disks"]["bd-sdb"].get("allowScheduling")

    def ready_again(self):
        node = self.c.kube.object(self.NODE)
        node["status"]["diskStatus"]["bd-sdb"]["conditions"] = [{"type": "Ready", "status": "True"}]
        self.c.kube.add(node)

    def stuck(self, name="r-else"):
        replica_object = self.c.kube.object(f"{DISKS.LH}/replicas/{name}")
        replica_object["metadata"]["finalizers"] = ["longhorn.io"]
        self.c.kube.add(replica_object)

    def row(self, ident):
        return next(job for job in JOBS.snapshot() if job["id"] == ident)

    def refused_removing(self):
        """A forced retire past its replicas, failed at the disk's removal."""
        ident = self.force()
        self.look(ident, 3)
        self.c.kube.fail(self.NODE, 400, times=99, method="PATCH")
        self.look(ident)
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(ident)["status"])
        self.c.kube.stop_failing()
        self.c.kube.writes.clear()
        return ident

    def test_a_replica_record_longhorn_keeps_ends_the_job_with_the_disk_kept(self):
        self.record("r-ghost")
        ident = self.force()
        job = self.look(ident, 4)
        self.assertEqual("Waiting for Longhorn to let go of 1 replica record(s)", job["message"])
        self.pass_time(self.LIMIT - 1)
        self.assertEqual("running", self.look(ident)["status"])
        self.pass_time(1)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("1 replica record", job["message"])
        self.assertIn("30 minutes", job["message"])
        self.assertTrue(self.disk_in_longhorn(), "the disk stays, as reviewed: nothing was let go")

    def test_resuming_after_that_gets_a_fresh_wait_and_can_finish(self):
        self.no_only_copy()
        self.record("r-ghost")
        ident = self.start()
        self.look(ident, 4)
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(ident)["status"])
        JOBS.resume(ident)
        self.assertEqual("running", self.look(ident)["status"], "it is not refused again at once")
        self.forget_record("r-ghost")
        self.assertEqual("succeeded", self.look(ident, 4)["status"])

    def no_only_copy(self):
        # A kept only copy ends the retire at this step, with the disk kept.
        self.c.kube.discard(f"{DISKS.LH}/replicas/r-only")
        self.forget_record("r-only")

    def failed(self, **cfg):
        self.no_only_copy()
        self.record("r-ghost")
        ident = self.start(**cfg)
        self.look(ident, 4)
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(ident)["status"])
        self.c.kube.writes.clear()
        return ident

    def saved(self, kind, **ref):
        return JOBS.start(kind, kind, {"kind": "Node", "name": "node3"}, "/", {"node": "node3", "disk": "bd-sdb", **ref})["id"]

    def drop(self, ident):
        with JOBS.transaction():
            JOBS._write([item for item in JOBS._read() if item["id"] != ident])

    def test_a_failed_retire_lets_a_guarded_job_start_and_is_not_resumed_past_it(self):
        ident = self.failed()
        for kind, ref in (("disk-allocation", {"phase": "growing"}), ("disk-v2-convert", {"phase": "evacuating"}),
                          ("longhorn-runtime", {"phase": "staged"})):
            with self.subTest(kind=kind):
                other = self.saved(kind, **ref)
                self.assertFalse(next(j for j in JOBS.snapshot() if j["id"] == ident)["resumable"])
                with self.assertRaises(ValueError):
                    JOBS.resume(ident)
                self.assertEqual("failed", next(j for j in JOBS.snapshot() if j["id"] == ident)["status"])
                self.assertEqual([], self.c.kube.writes, "nothing runs past the other job")
                self.drop(other)
        JOBS.resume(ident)

    def test_a_second_retire_of_the_same_disk_stops_the_first_being_resumed(self):
        ident = self.failed()
        other = self.saved("disk-retire", phase="stop")
        with self.assertRaises(ValueError):
            JOBS.resume(ident)
        self.drop(other)
        JOBS.resume(ident)

    def test_a_forced_retire_with_replicas_to_go_needs_a_fresh_review(self):
        # Force takes replicas off with no check on their other copies: reviewed once,
        # and a failed job can be resumed days later.
        stuck = self.c.kube.object(f"{DISKS.LH}/replicas/r-else")
        stuck["metadata"]["finalizers"] = ["longhorn.io"]
        self.c.kube.add(stuck)
        ident = self.force()
        self.look(ident, 3)
        self.pass_time(self.LIMIT)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertFalse(job["resumable"])
        self.assertIn("start the retire again", job["message"])
        self.assertNotIn("Carry on", job["message"])
        with self.assertRaisesRegex(ValueError, "review"):
            JOBS.resume(ident)

    def test_a_forced_retire_past_its_replicas_can_be_resumed(self):
        ident = self.refused_removing()
        JOBS.resume(ident)
        self.assertEqual("succeeded", self.look(ident, 3)["status"])
        self.assertFalse(self.disk_in_longhorn())

    def test_a_resumed_retire_checks_the_other_copies_again(self):
        stuck = self.c.kube.object(f"{DISKS.LH}/replicas/r-else")
        stuck["metadata"]["finalizers"] = ["longhorn.io"]
        self.c.kube.add(stuck)
        self.no_only_copy()
        ident = self.start()
        self.look(ident, 3)
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(ident)["status"])
        self.c.change_replica("r-else-1", failedAt="2026-09-29T00:00:00Z")
        JOBS.resume(ident)
        self.c.kube.writes.clear()
        self.assertIn("could not confirm", self.look(ident, 2)["message"])
        self.assertEqual([], [w for w in self.c.kube.writes if w[0] == "DELETE"], "its last copy is not let go")

    def test_a_failed_retire_can_still_be_put_back(self):
        ident = self.failed()
        job = self.look(ident)
        self.assertTrue(job["cleanable"])
        self.assertIn("Clean up", job["message"])
        JOBS.cancel(ident)
        job = self.look(ident)
        self.assertIn("takes replicas again", job["message"])
        self.assertIs(True, self.c.kube.object(self.NODE)["spec"]["disks"]["bd-sdb"]["allowScheduling"])
        self.assertFalse(job["cleanable"], "once is enough")
        self.assertFalse(job["resumable"], "what was put back does not carry on")
        with self.assertRaises(ValueError):
            JOBS.resume(ident)
        JOBS.dismiss(ident)

    def test_it_is_not_put_back_past_a_guarded_job(self):
        ident = self.failed()
        other = self.saved("disk-allocation", phase="growing")
        with self.assertRaises(ValueError):
            JOBS.cancel(ident)
        self.assertEqual([], self.c.kube.writes)
        self.drop(other)
        JOBS.cancel(ident)

    def test_a_disk_being_removed_is_not_put_back_and_does_not_offer_it(self):
        ident = self.refused_removing()
        job = self.look(ident)
        self.assertFalse(job["cleanable"])
        self.assertIn("Carry on", job["message"])
        self.assertNotIn("Clean up", job["message"])
        with self.assertRaises(ValueError):
            JOBS.cancel(ident)

    def test_a_disk_longhorn_will_not_let_go_ends_the_job_in_longhorns_words(self):
        ident = self.force()
        job = self.look(ident, 3)
        self.c.kube.fail(self.NODE, 400, times=99, method="PATCH")
        job = self.look(ident)
        self.assertIn("not letting the disk go yet", job["message"])
        self.pass_time(self.LIMIT)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("30 minutes", job["message"])
        self.assertTrue(self.disk_in_longhorn())

    def test_a_replica_that_never_goes_ends_the_job(self):
        stuck = self.c.kube.object(f"{DISKS.LH}/replicas/r-else")
        stuck["metadata"]["finalizers"] = ["longhorn.io"]
        self.c.kube.add(stuck)
        ident = self.force()
        job = self.look(ident, 3)
        self.assertIn("Letting go of", job["message"])
        self.pass_time(self.LIMIT)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("replica", job["message"])
        self.assertIn("30 minutes", job["message"])

    def test_an_ordinary_retire_still_finishes(self):
        ident = self.force()
        self.assertEqual("succeeded", self.look(ident, 8)["status"])

    def test_a_kept_only_copy_ends_the_retire_with_its_record_there(self):
        # The only copy, kept as reviewed, keeps its record: the retire ends with it.
        ident = self.start()
        job = self.look(ident, 6)
        self.assertEqual("succeeded", job["status"])
        self.assertIn("only copy", job["message"])

    # F1: what a retire decided may be out of date when it carries on.
    def test_carrying_on_with_a_disk_that_works_again_fails_with_the_disk_kept(self):
        ident = self.failed()
        self.ready_again()
        JOBS.resume(ident)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("working again", job["message"])
        self.assertIn("Clean up", job["message"])
        self.assertEqual([], self.c.kube.writes)
        self.assertTrue(self.disk_in_longhorn())
        self.assertTrue(job["cleanable"])
        JOBS.cancel(ident)
        self.assertIs(True, self.scheduling())

    def test_no_replica_is_let_go_from_a_disk_that_works_again(self):
        self.stuck()
        self.no_only_copy()
        ident = self.start()
        self.look(ident, 3)
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(ident)["status"])
        self.ready_again()
        self.c.kube.writes.clear()
        JOBS.resume(ident)
        self.look(ident, 2)
        self.assertEqual([], [w for w in self.c.kube.writes if w[0] == "DELETE"])

    def test_a_disk_being_removed_that_works_again_stays(self):
        ident = self.refused_removing()
        self.ready_again()
        JOBS.resume(ident)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertIn("working again", job["message"])
        self.assertNotIn("Clean up", job["message"], "it can't be cleaned up at this step")
        self.assertIn("Allow new replicas", job["message"])
        self.assertEqual([], self.c.kube.writes)
        self.assertTrue(self.disk_in_longhorn())

    def test_carrying_on_stops_new_replicas_again(self):
        ident = self.failed()
        DISKS.set_scheduling("node3", "bd-sdb", True)
        JOBS.resume(ident)
        self.look(ident)
        self.assertIs(False, self.scheduling())

    def test_the_check_on_carrying_on_is_not_lost_when_its_write_fails(self):
        ident = self.failed()
        JOBS.resume(ident)
        self.c.kube.fail(self.NODE, 500, method="PATCH")
        self.assertEqual("running", self.look(ident)["status"])
        self.ready_again()
        self.assertIn("working again", self.look(ident)["message"])

    def test_carrying_on_with_the_disk_gone_does_not_bring_it_back(self):
        ident = self.failed()
        node = self.c.kube.object(self.NODE)
        del node["spec"]["disks"]["bd-sdb"]
        self.c.kube.add(node)
        JOBS.resume(ident)
        self.assertEqual("succeeded", self.look(ident)["status"])
        self.assertFalse(self.disk_in_longhorn())

    def test_carrying_on_gives_a_copy_not_confirmed_a_fresh_wait(self):
        self.stuck()
        self.no_only_copy()
        ident = self.start()
        for name in ("r-waits-1", "r-waits-2"):
            self.c.change_replica(name, failedAt="2026-09-29T00:00:00Z")
        self.look(ident, 3)
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(ident)["status"])
        self.c.kube.discard(f"{DISKS.LH}/replicas/r-else")
        JOBS.resume(ident)
        job = self.look(ident, 2)
        self.assertEqual("running", job["status"], job["message"])
        self.assertIn("could not confirm", job["message"])

    def test_longhorn_saying_nothing_of_the_disk_is_not_called_working(self):
        ident = self.failed()
        node = self.c.kube.object(self.NODE)
        node["status"]["diskStatus"].pop("bd-sdb")
        self.c.kube.add(node)
        JOBS.resume(ident)
        job = self.look(ident)
        self.assertEqual("failed", job["status"])
        self.assertNotIn("working again", job["message"])
        self.assertIn("does not say", job["message"])
        self.assertEqual([], self.c.kube.writes)
        self.assertTrue(job["resumable"])

    def test_a_carry_on_refused_for_half_an_hour_fails_back_where_it_was(self):
        ident = self.refused_removing()
        JOBS.resume(ident)
        self.c.kube.fail(self.NODE, 400, times=9999, method="PATCH")
        self.look(ident)
        self.pass_time(self.LIMIT - 1)
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        self.assertIn("refused", job["message"])
        self.pass_time(1)
        job = self.look(ident)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("30 minutes", job["message"])
        self.assertIn("Carry on", job["message"])
        self.assertNotIn("Clean up", job["message"], "it can't be cleaned up at this step")
        self.assertTrue(job["resumable"])
        self.assertFalse(job["cleanable"])
        self.assertTrue(job["dismissible"])
        self.assertTrue(self.disk_in_longhorn())
        self.c.kube.stop_failing()
        JOBS.resume(ident)
        self.assertEqual("succeeded", self.look(ident, 3)["status"])
        self.assertFalse(self.disk_in_longhorn())

    def test_a_refused_carry_on_before_removal_can_still_be_cleaned_up(self):
        ident = self.failed()
        JOBS.resume(ident)
        self.c.kube.fail(self.NODE, 400, times=9999, method="PATCH")
        self.look(ident)
        self.pass_time(self.LIMIT)
        job = self.look(ident)
        self.assertEqual("failed", job["status"], job["message"])
        self.assertIn("Clean up", job["message"])
        self.assertTrue(job["cleanable"])

    def test_a_pause_in_the_jobs_loop_does_not_use_up_the_unconfirmed_wait(self):
        self.no_only_copy()
        ident = self.start()
        for name in ("r-waits-1", "r-waits-2"):
            self.c.change_replica(name, failedAt="2026-09-29T00:00:00Z")
        self.look(ident, 3)
        self.now += 2000
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        self.assertIn("could not confirm", job["message"])
        self.pass_time(self.LIMIT - 1)
        self.assertEqual("running", self.look(ident)["status"])
        self.pass_time(1)
        job = self.look(ident)
        self.assertEqual("succeeded", job["status"], job["message"])
        self.assertIn("within 30 minutes", job["message"])

    def test_each_replica_let_go_gets_its_own_wait(self):
        self.stuck("r-else")
        self.no_only_copy()
        ident = self.start()
        for name in ("r-waits-1", "r-waits-2"):
            self.c.change_replica(name, failedAt="2026-09-29T00:00:00Z")
        self.look(ident, 3)
        self.pass_time(29 * 60)
        self.assertIn("Letting go of", self.look(ident)["message"])
        self.c.kube.discard(f"{DISKS.LH}/replicas/r-else")
        for name in ("r-waits-1", "r-waits-2"):
            self.c.change_replica(name, failedAt="")
        self.stuck("r-waits")
        self.assertIn("Letting go of", self.look(ident)["message"])
        self.pass_time(self.LIMIT - 1)
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        self.pass_time(1)
        self.assertEqual("failed", self.look(ident)["status"])

    def test_a_replica_flickering_in_and_out_does_not_keep_the_wait_going(self):
        self.stuck("r-else")
        self.stuck("r-waits")
        self.no_only_copy()
        ident = self.start()
        self.look(ident, 3)
        for flicker in range(8):
            for name in ("r-waits-1", "r-waits-2"):
                self.c.change_replica(name, failedAt="2026-09-29T00:00:00Z" if flicker % 2 else "")
            self.pass_time(240)
        self.assertEqual("failed", self.look(ident)["status"])

    # F2: the job list is read on every request, behind no lock.
    def test_reading_the_job_list_does_not_wait_for_the_store(self):
        ident = self.failed()
        held, let_go, seen = threading.Event(), threading.Event(), []

        def holder():
            with JOBS.transaction():
                held.set()
                let_go.wait(10)
        holding = threading.Thread(target=holder, daemon=True)
        holding.start()
        self.assertTrue(held.wait(5))
        reader = threading.Thread(target=lambda: seen.append(self.row(ident)), daemon=True)
        reader.start()
        reader.join(2)
        waited = reader.is_alive()
        let_go.set()
        holding.join(5)
        reader.join(5)
        self.assertFalse(waited, "it waited behind the store's lock")
        self.assertTrue(seen[0]["resumable"])

    def rival_during_the_check(self, act):
        """Starts a guarded job the moment the guards have answered, while act
        goes on. Returns its thread, whether it was still waiting then, and
        what became of it."""
        outcome, waiting = [], []

        def rival():
            try:
                self.saved("disk-allocation", phase="growing")
                outcome.append("started")
            except ValueError:
                outcome.append("refused")
        thread = threading.Thread(target=rival, daemon=True)
        asked = DISKS.GUARDS["longhorn_runtime"]

        @contextlib.contextmanager
        def scope(*args):
            with asked(*args):
                yield
            if not thread.ident:
                thread.start()
                thread.join(0.5)
                waiting.append(thread.is_alive())
        with mock.patch.dict(DISKS.GUARDS, {"longhorn_runtime": scope}):
            act()
        thread.join(5)
        return waiting, outcome

    def test_carrying_on_checks_and_marks_it_running_in_one_hold_of_the_store(self):
        ident = self.failed()
        waiting, outcome = self.rival_during_the_check(lambda: JOBS.resume(ident))
        self.assertEqual([True], waiting, "nothing may start between the check and marking it running")
        self.assertEqual(["refused"], outcome)
        self.assertEqual("running", self.row(ident)["status"])

    def test_cleaning_up_checks_and_marks_it_in_one_hold_of_the_store(self):
        ident = self.failed()
        waiting, _ = self.rival_during_the_check(lambda: JOBS.cancel(ident))
        self.assertEqual([True], waiting, "nothing may start between the check and marking it")

    # F3: "as before" has to be true.
    def test_a_retire_started_over_a_failed_one_puts_the_disk_back_as_it_was(self):
        self.failed(force=True, confirm="bd-sdb")
        again = self.force()
        self.look(again, 3)
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(again)["status"])
        self.assertIs(False, self.scheduling())
        JOBS.cancel(again)
        self.assertIn("takes replicas again", self.look(again)["message"])
        self.assertIs(True, self.scheduling())

    def test_a_disk_already_off_when_the_retire_started_is_left_off_and_says_so(self):
        DISKS.set_scheduling("node3", "bd-sdb", False)
        ident = self.failed()
        job = self.look(ident)
        self.assertNotIn("Clean up lets replicas", job["message"])
        self.assertIn("Allow new replicas", job["message"])
        plan = JOBS.cancel_plan(ident)
        self.assertEqual([], plan["undo"])
        self.assertIn("Nothing on the disk changes", " ".join(plan["keeps"]))
        self.assertNotEqual("Let replicas back", plan["action"])
        JOBS.cancel(ident)
        job = self.look(ident)
        self.assertNotIn("takes replicas again", job["message"])
        self.assertIn("nothing on the disk was changed", job["message"])
        self.assertIs(False, self.scheduling())
        self.assertEqual([], self.c.kube.writes)

    def test_closing_a_failed_retire_leaves_what_a_person_did_since(self):
        DISKS.set_scheduling("node3", "bd-sdb", False)
        ident = self.failed()
        DISKS.set_scheduling("node3", "bd-sdb", True)
        self.c.kube.writes.clear()
        JOBS.cancel(ident)
        self.assertEqual([], self.c.kube.writes, "it promised to change nothing on the disk")
        self.assertIs(True, self.scheduling())
        self.assertFalse(self.look(ident)["resumable"])

    def test_cancelling_a_running_retire_of_a_disk_already_off_says_it_stays_off(self):
        DISKS.set_scheduling("node3", "bd-sdb", False)
        ident = self.force()
        self.look(ident)
        self.assertIn("stays off", " ".join(JOBS.cancel_plan(ident)["keeps"]))
        self.c.kube.writes.clear()
        JOBS.cancel(ident)
        self.assertEqual(1, len([w for w in self.c.kube.writes if w[0] == "PATCH"]), "it is holding the disk")
        self.assertIs(False, self.scheduling())
        self.assertIn("stays off", self.look(ident)["message"])

    # What a retire started over a failed one takes from it.
    def failed_over(self, cfg_first=None):
        first = self.failed(**(cfg_first or {}))
        again = self.start(**(cfg_first or {}))
        return first, again

    def test_a_retire_over_a_failed_one_counts_the_replicas_it_let_go(self):
        first, again = self.failed_over(dict(force=True, confirm="bd-sdb"))
        said = next(i for i in JOBS.stored() if i["id"] == first)["ref"]["removed"]
        self.assertEqual(2, len(said))
        keeps = " ".join(JOBS.cancel_plan(again)["keeps"])
        self.assertNotIn("No replica has been removed", keeps)
        self.assertIn("2 failed replicas", keeps)

    def test_a_retire_over_a_failed_one_of_a_disk_that_was_off_finds_it_off(self):
        DISKS.set_scheduling("node3", "bd-sdb", False)
        _, again = self.failed_over()
        self.assertEqual([], JOBS.cancel_plan(again)["undo"])

    def older(self, **ref):
        ident = JOBS.start("disk-retire", "r", {"kind": "Node", "name": "node3"}, "/",
                           {"node": "node3", "disk": "bd-sdb", "phase": "replicas", "was_scheduling": True,
                            "removed": ["r-old"], **ref})["id"]
        with JOBS.transaction() as store:
            next(i for i in store.items if i["id"] == ident)["status"] = "failed"
            store.save()
        return ident

    def test_a_failed_retire_of_another_disk_or_host_passes_nothing_on(self):
        self.older(disk="bd-other")
        self.older(node="node1")
        DISKS.set_scheduling("node3", "bd-sdb", False)
        plan = JOBS.cancel_plan(self.force())
        self.assertEqual([], plan["undo"])
        self.assertIn("No replica has been removed", " ".join(plan["keeps"]))

    def test_a_retire_that_was_put_back_passes_nothing_on(self):
        self.older(phase="cancelled")
        DISKS.set_scheduling("node3", "bd-sdb", False)
        plan = JOBS.cancel_plan(self.force())
        self.assertEqual([], plan["undo"])
        self.assertIn("No replica has been removed", " ".join(plan["keeps"]))

    def test_the_failed_message_says_how_to_let_replicas_back_once_it_is_dismissed(self):
        ident = self.failed()
        self.assertIn("Nodes > Disks > Allow new replicas", self.look(ident)["message"])
        page = "".join(part.read_text() for part in sorted((Path(__file__).resolve().parents[1] / "web/js").glob("views-storage*.js")))
        self.assertIn("'Allow new replicas'", page, "the message names this menu entry")
        JOBS.dismiss(ident)
        DISKS.set_scheduling("node3", "bd-sdb", True)
        self.assertIs(True, self.scheduling())

    # F4
    def test_cleaning_up_when_the_disk_has_gone_writes_nothing_and_says_so(self):
        ident = self.failed()
        node = self.c.kube.object(self.NODE)
        del node["spec"]["disks"]["bd-sdb"]
        self.c.kube.add(node)
        JOBS.cancel(ident)
        self.assertEqual([], self.c.kube.writes)
        self.assertFalse(self.disk_in_longhorn())
        job = self.look(ident)
        self.assertNotIn("takes replicas again", job["message"])
        self.assertIn("no longer", job["message"])
        self.assertFalse(job["resumable"], "what is cleaned up does not carry on")

    def test_a_failed_read_is_not_taken_for_the_disk_having_gone(self):
        ident = self.failed()
        self.c.kube.fail(self.NODE, 500, method="GET")
        with self.assertRaisesRegex(ValueError, "did not finish"):
            JOBS.cancel(ident)
        job = self.row(ident)
        self.assertEqual("failed", job["status"])
        self.assertTrue(job["cleanable"], "it can be asked again")
        JOBS.cancel(ident)
        self.assertIs(True, self.scheduling())

    def test_cleaning_up_when_the_host_has_gone_says_so_in_words(self):
        ident = self.failed()
        self.c.kube.fail(self.NODE, 404, times=99, method="GET")
        JOBS.cancel(ident)
        self.assertEqual([], self.c.kube.writes)
        message = self.row(ident)["message"]
        self.assertIn("no longer", message)
        self.assertNotIn("HTTP", message)

    # F5: a write that landed and lost its reply, or a crash just after it.
    def test_replicas_whose_replies_were_lost_are_not_said_to_be_there_still(self):
        self.no_only_copy()
        self.record("r-ghost")
        for name in ("r-else", "r-waits"):
            self.c.kube.fail(f"{DISKS.LH}/replicas/{name}", 504, applied=True, method="DELETE")
        ident = self.start()
        self.look(ident, 6)
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(ident)["status"])
        keeps = " ".join(JOBS.cancel_plan(ident)["keeps"])
        self.assertNotIn("No replica has been removed", keeps)
        self.assertIn("2 failed replicas", keeps)

    def test_a_crash_while_letting_replicas_go_is_remembered(self):
        class Crash(BaseException):
            pass

        def crash(*_):
            raise Crash()
        self.no_only_copy()
        ident = self.start()
        self.look(ident)
        for name in ("r-else", "r-waits"):
            self.c.kube.react("DELETE", f"{DISKS.LH}/replicas/{name}", crash)
        with self.assertRaises(Crash):
            self.look(ident)
        saved = next(item for item in JOBS.stored() if item["id"] == ident)
        self.assertEqual(["r-else", "r-waits"], saved["ref"]["removed"])

    def test_a_disk_that_went_though_the_reply_was_lost_is_not_said_to_stay(self):
        ident = self.force()
        self.look(ident, 3)
        self.c.kube.fail(self.NODE, 400, times=99, method="PATCH")
        self.look(ident)
        self.pass_time(self.LIMIT - 1)
        self.c.kube.stop_failing()
        self.c.kube.fail(self.NODE, 504, applied=True, method="PATCH")
        self.pass_time(1)
        job = self.look(ident)
        self.assertFalse(self.disk_in_longhorn())
        self.assertEqual("succeeded", job["status"], job["message"])

    # F6
    def test_the_records_wait_is_not_counted_from_before_another_wait(self):
        self.no_only_copy()
        self.record("r-ghost")
        ident = self.start()
        self.look(ident, 4)
        self.c.kube.add(replica("r-new", "pvc-jelly", "node3", "u-dead", healthy=False))
        self.stuck("r-new")
        self.pass_time(self.LIMIT - 60)
        self.assertIn("Letting go of", self.look(ident)["message"])
        self.c.kube.discard(f"{DISKS.LH}/replicas/r-new")
        self.pass_time(60)
        self.assertEqual("running", self.look(ident)["status"], "this wait has only just begun")
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(ident)["status"])

    # F7: time the jobs loop did not run says nothing about Longhorn.
    def test_a_pause_in_the_jobs_loop_restarts_the_wait(self):
        self.stuck()
        ident = self.force()
        self.look(ident, 3)
        self.now += 31 * 60
        job = self.look(ident)
        self.assertEqual("running", job["status"], job["message"])
        self.pass_time(self.LIMIT - 1)
        self.assertEqual("running", self.look(ident)["status"])
        self.pass_time(1)
        self.assertEqual("failed", self.look(ident)["status"])

    def test_a_clock_set_back_restarts_the_wait(self):
        self.stuck()
        ident = self.force()
        self.look(ident, 3)
        self.now -= 3600
        self.look(ident)
        self.pass_time(self.LIMIT - 1)
        self.assertEqual("running", self.look(ident)["status"])
        self.pass_time(1)
        self.assertEqual("failed", self.look(ident)["status"], "the wait is not an hour longer")

    # F8: the words on the page.
    def test_the_failed_message_names_the_buttons_on_the_page(self):
        ident = self.failed()
        message = self.look(ident)["message"]
        self.assertIn("Carry on", message)
        self.assertIn("Clean up", message)
        self.assertNotIn("Resume", message)
        self.assertNotIn("Cancel", message)

    def test_a_forced_retire_says_to_start_again_and_how_to_clean_up(self):
        message = self.look(self.failed(force=True, confirm="bd-sdb"))["message"]
        self.assertIn("Clean up", message)
        self.assertIn("start the retire again", message)
        self.assertNotIn("Carry on", message)

    def test_the_plan_for_a_failed_retire_says_it_lets_replicas_back(self):
        plan = JOBS.cancel_plan(self.failed())
        self.assertEqual("Let replicas back", plan["action"])
        self.assertTrue(plan["cleanup_says_action"])
        self.assertIn("job stays", plan["lead"])

    def test_the_plan_for_a_running_retire_still_cancels(self):
        plan = JOBS.cancel_plan(self.force())
        self.assertEqual("Cancel and put back", plan["action"])

    # F10
    def test_a_retire_does_not_start_beside_a_running_one_of_the_same_disk(self):
        ident = self.failed()
        JOBS.resume(ident)
        with self.assertRaisesRegex(ValueError, "another retire"):
            self.start()

    # What may and may not stop it carrying on.
    def test_a_conversion_of_another_disk_on_the_host_stops_it_carrying_on(self):
        ident = self.failed()
        self.saved("disk-v2-convert", disk="bd-other", phase="evacuating")
        self.assertFalse(self.row(ident)["resumable"])
        with self.assertRaises(ValueError):
            JOBS.resume(ident)

    def test_a_retire_of_another_disk_or_host_does_not_stop_it_carrying_on(self):
        ident = self.failed()
        self.saved("disk-retire", disk="bd-other", phase="stop")
        JOBS.start("disk-retire", "r", {"kind": "Node", "name": "node1"}, "/",
                   {"node": "node1", "disk": "bd-sdb", "phase": "stop"})
        self.assertTrue(self.row(ident)["resumable"])
        JOBS.resume(ident)

    def test_a_failed_retire_of_the_same_disk_does_not_stop_it_carrying_on(self):
        ident = self.failed()
        other = self.start()
        self.look(other, 3)
        self.pass_time(self.LIMIT)
        self.assertEqual("failed", self.look(other)["status"])
        self.assertTrue(self.row(ident)["resumable"])
        JOBS.resume(ident)

    def test_a_forced_retire_failing_before_it_began_needs_a_fresh_review(self):
        ident = self.force()
        self.c.kube.fail(self.NODE, 404, method="GET")
        self.assertEqual("failed", self.look(ident)["status"])
        self.assertFalse(self.row(ident)["resumable"])
        with self.assertRaisesRegex(ValueError, "review"):
            JOBS.resume(ident)


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
        self.pass_time(item, clock, DISKS.UNCONFIRMED_WAIT + 1)
        item = run(item)
        self.assertEqual(["r-waits"], self.deleted(c))
        self.assertIn("r-else", item["ref"]["kept"])
        self.assertIn("could not confirm another whole copy", item["message"])

    def pass_time(self, item, clock, seconds):
        # The jobs loop looks every few seconds, whether its step works or not: a
        # longer gap restarts a wait.
        while seconds > 0:
            step = min(240, seconds)
            clock[0] += step
            seconds -= step
            try:
                self.step(item)
            except urllib.error.HTTPError:
                pass

    def step(self, item, times=1):
        JOBS._write([item])    # its step saves what it is about to do
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
        self.pass_time(item, clock, DISKS.UNCONFIRMED_WAIT + 1)
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
        self.pass_time(item, clock, 20 * 60)
        c.change_replica("r-waits-2", failedAt="2026-09-29T00:00:00Z")
        self.step(item)                                  # now nothing is let go: no refused DELETE
        # Past jellyfin's half hour, not frigate's: still waiting for frigate.
        self.pass_time(item, clock, 11 * 60)
        self.assertEqual("running", item["status"], item["message"])
        self.assertIn("could not confirm another whole copy of 1 volume", item["message"])
        self.pass_time(item, clock, 20 * 60)
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
