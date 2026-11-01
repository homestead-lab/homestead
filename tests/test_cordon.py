"""homestead_cordon: how a job cordons a host and gives it back. The cordon
mark names every job holding the host; the last one to let go uncordons it,
and a cordon a person made (cordoned, no mark) is never undone by a job."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_cordon as CORDON
from fakes.kube import Kube

PATH = "/api/v1/nodes/n1"


def node(cordoned=False, uid="u1", **annotations):
    obj = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "n1", "uid": uid}}
    if annotations:
        obj["metadata"]["annotations"] = {CORDON.NAMES.key(k.replace("_", "-")): v for k, v in annotations.items()}
    if cordoned:
        obj["spec"] = {"unschedulable": True}
    return obj


def cordoned(kube):
    return bool((kube.object(PATH).get("spec") or {}).get("unschedulable"))


def mark(kube):
    return (kube.object(PATH)["metadata"].get("annotations") or {}).get(CORDON.MARK)


class CordonTest(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.enterContext(mock.patch.object(CORDON.OPS, "stored", lambda: list(self.jobs)))

    def kube(self, obj):
        return self.enterContext(Kube(obj))

    def job(self, job_id, kind, node="n1", status="running", **ref):
        """A job in the store; a job that holds a host is stored before it cordons."""
        item = {"id": job_id, "kind": kind, "title": f"{kind} {job_id}", "status": status,
                "resource": {"kind": "Node", "name": node}, "ref": {"node": node, **ref}}
        self.jobs.append(item)
        return item

    def test_cordon_marks_and_cordons_in_one_fenced_patch(self):
        kube = self.kube(node())
        rv = kube.object(PATH)["metadata"]["resourceVersion"]
        self.assertEqual(CORDON.Outcome(True, "n1", "cordoned"), CORDON.cordon("n1", "host-os", "j1"))
        self.assertEqual(1, len(kube.writes))
        method, path, ops, ctype = kube.writes[0]
        self.assertEqual(("PATCH", PATH, "application/json-patch+json"), (method, path, ctype))
        self.assertIn({"op": "test", "path": "/metadata/uid", "value": "u1"}, ops)
        self.assertIn({"op": "test", "path": "/metadata/resourceVersion", "value": rv}, ops)
        self.assertTrue(cordoned(kube))
        self.assertEqual("host-os:j1", mark(kube))
        self.assertEqual({"host-os:j1"}, CORDON.holders(kube.object(PATH)))

    def test_cordon_again_after_a_lost_reply_writes_nothing(self):
        kube = self.kube(node(cordoned=True, cordoned_by="host-os:j1"))
        self.assertTrue(CORDON.cordon("n1", "host-os", "j1").done)
        self.assertEqual([], kube.writes)

    def test_cordon_gives_up_after_four_conflicts_and_says_so(self):
        kube = self.kube(node())
        kube.fail(PATH, 409, times=CORDON.WRITE_ATTEMPTS, method="PATCH")
        out = CORDON.cordon("n1", "host-os", "j1")
        self.assertFalse(out.done)
        self.assertIn("kept changing", out.reason)
        self.assertEqual(4, len(kube.writes))
        self.assertFalse(cordoned(kube))

    def test_a_refused_write_is_not_taken_for_a_conflict(self):
        kube = self.kube(node())
        kube.fail(PATH, 422, times=CORDON.WRITE_ATTEMPTS, method="PATCH")
        out = CORDON.cordon("n1", "host-os", "j1")
        self.assertFalse(out.done)
        self.assertIn("refused", out.reason)
        self.assertEqual(1, len(kube.writes), "the Node had not changed, so asking again would be refused again")

    def test_a_failed_test_op_after_a_change_is_read_again(self):
        kube = self.kube(node())
        stale = kube.object(PATH)
        changed = {**stale, "metadata": {k: v for k, v in stale["metadata"].items() if k != "resourceVersion"}}
        kube.add(changed)  # someone changed it after Homestead's first read
        reads = []
        kube.react("GET", PATH, lambda *_: None if reads else reads.append(1) or stale)
        self.assertTrue(CORDON.cordon("n1", "host-os", "j1").done)
        self.assertEqual(2, len(kube.writes))
        self.assertTrue(cordoned(kube))

    def test_a_second_job_joins_the_holders(self):
        self.job("j1", "host-os")
        kube = self.kube(node(cordoned=True, cordoned_by="host-os:j1"))
        self.assertTrue(CORDON.cordon("n1", "node-power", "j2").done)
        self.assertEqual({"host-os:j1", "node-power:j2"}, CORDON.holders(kube.object(PATH)))

    def test_a_mark_left_on_a_host_not_cordoned_holds_nothing(self):
        kube = self.kube(node(cordoned_by="host-os:old"))
        self.assertTrue(CORDON.cordon("n1", "node-power", "j2").done)
        self.assertEqual("node-power:j2", mark(kube))

    def test_cordon_of_a_missing_host(self):
        self.kube({"apiVersion": "v1", "kind": "Node", "metadata": {"name": "other"}})
        self.assertEqual(CORDON.Outcome(False, "n1", "no such host"), CORDON.cordon("n1", "host-os", "j1"))

    def test_a_persons_cordon_is_never_released(self):
        kube = self.kube(node(cordoned=True))
        self.assertEqual(CORDON.Outcome(False, "n1", "cordoned by a person"), CORDON.cordon("n1", "host-os", "j1"))
        self.assertIsNone(mark(kube))
        CORDON.release("n1", "host-os", "j1")
        self.assertTrue(cordoned(kube))
        self.assertEqual([], kube.writes)

    def test_last_holder_uncordons(self):
        kube = self.kube(node())
        a, _ = self.job("A", "host-os"), self.job("B", "node-power")
        CORDON.cordon("n1", "host-os", "A")
        CORDON.cordon("n1", "node-power", "B")
        self.assertEqual(CORDON.Outcome(False, "n1", "still held by node-power:B"), CORDON.release("n1", "host-os", "A"))
        self.assertTrue(cordoned(kube))
        self.assertEqual("node-power:B", mark(kube))
        a["status"] = "succeeded"  # A ends once it has let go
        self.assertEqual(CORDON.Outcome(True, "n1", "uncordoned"), CORDON.release("n1", "node-power", "B"))
        self.assertFalse(cordoned(kube))
        self.assertIsNone(mark(kube))

    def test_release_retries_a_conflict_then_succeeds(self):
        kube = self.kube(node(cordoned=True, cordoned_by="host-os:j1"))
        kube.fail(PATH, 409, times=1, method="PATCH")
        self.assertTrue(CORDON.release("n1", "host-os", "j1").done)
        self.assertFalse(cordoned(kube))
        self.assertEqual(2, len(kube.writes))

    def test_release_gives_up_after_four_conflicts_and_says_so(self):
        kube = self.kube(node(cordoned=True, cordoned_by="host-os:j1"))
        kube.fail(PATH, 409, times=CORDON.WRITE_ATTEMPTS, method="PATCH")
        out = CORDON.release("n1", "host-os", "j1")
        self.assertFalse(out.done)
        self.assertIn("kept changing", out.reason)
        self.assertEqual(4, len(kube.writes))
        self.assertTrue(cordoned(kube))

    def test_a_gone_or_replaced_node_counts_as_released(self):
        kube = self.kube({"apiVersion": "v1", "kind": "Node", "metadata": {"name": "other"}})
        self.assertEqual(CORDON.Outcome(True, "n1", "host is gone"), CORDON.release("n1", "host-os", "j1"))
        kube.add(node(cordoned=True, uid="u2", cordoned_by="host-os:j1"))
        self.assertEqual(CORDON.Outcome(True, "n1", "host is gone"), CORDON.release("n1", "host-os", "j1", uid="u1"))
        self.assertTrue(cordoned(kube))
        self.assertEqual([], kube.writes)

    def test_old_marks_are_read_as_holders(self):
        obj = node(cordoned=True, cluster_shutdown="run1", upgrade_cordon="up1", cordoned_by="lh1")
        self.assertEqual({"cluster-shutdown:run1", "platform-upgrade:up1", "longhorn-v2-cleanup:lh1"}, CORDON.holders(obj))
        self.assertEqual(set(), CORDON.holders(node()))

    def test_release_takes_away_this_jobs_old_mark(self):
        kube = self.kube(node(cordoned=True, upgrade_cordon="up1"))
        self.assertTrue(CORDON.release("n1", "platform-upgrade", "up1").done)
        self.assertFalse(cordoned(kube))
        self.assertNotIn(CORDON.NAMES.key("upgrade-cordon"), kube.object(PATH)["metadata"].get("annotations") or {})

    def test_another_active_host_job_keeps_the_cordon(self):
        kube = self.kube(node(cordoned=True, cordoned_by="node-power:j1"))
        self.job("j1", "node-power")
        self.job("j9", "host-os")["title"] = "Update n1"
        self.assertEqual("Update n1", CORDON.busy_with("n1", "j1"))
        out = CORDON.release("n1", "node-power", "j1")
        self.assertEqual(CORDON.Outcome(False, "n1", "still held by Update n1"), out)
        self.assertTrue(cordoned(kube))
        self.assertEqual("node-power:j1", mark(kube), "kept, so a later release can still give the host back")
        self.jobs[1]["status"] = "succeeded"
        self.assertTrue(CORDON.release("n1", "node-power", "j1").done)
        self.assertFalse(cordoned(kube))

    def test_busy_with(self):
        self.jobs[:] = [{"id": "own", "kind": "host-os", "status": "running", "ref": {"node": "n1"}},
                        {"id": "x", "kind": "vm-power", "status": "running", "ref": {"node": "n1"}},
                        {"id": "y", "kind": "host-bond", "status": "running", "ref": {"node": "n2"}}]
        self.assertEqual("", CORDON.busy_with("n1", "own"))
        self.jobs.append({"id": "z", "kind": "platform-upgrade", "title": "Upgrade k3s", "status": "running",
                          "ref": {"component": "cluster"}})
        self.assertEqual("Upgrade k3s", CORDON.busy_with("n1", "own"))

    def test_a_rollouts_node_power_job_gives_its_host_back(self):
        # os-rollout cordons nothing itself: the node-power job it runs holds the host.
        self.jobs.append({"id": "R", "kind": "os-rollout", "title": "Update every host's OS (1 hosts)", "status": "running",
                          "resource": {"kind": "Node", "name": "n1"}, "ref": {"rollout": "r1"}})
        self.job("P", "node-power")
        kube = self.kube(node())
        self.assertTrue(CORDON.cordon("n1", "node-power", "P").done)
        self.assertEqual(CORDON.Outcome(True, "n1", "uncordoned"), CORDON.release("n1", "node-power", "P"))
        self.assertFalse(cordoned(kube))

    def test_a_shutdown_holds_only_the_hosts_it_marked(self):
        self.jobs.append({"id": "S", "kind": "cluster-shutdown", "title": "Shut down cluster", "status": "running",
                          "resource": {"kind": "Cluster", "name": "cluster"}, "ref": {"run": "run1"}})
        self.job("P", "node-power")
        kube = self.kube(node())
        CORDON.cordon("n1", "node-power", "P")
        self.assertTrue(CORDON.release("n1", "node-power", "P").done, "unmarked: the shutdown does not hold it")
        self.assertFalse(cordoned(kube))
        kube.add(node(cordoned=True, cluster_shutdown="run1"))
        self.assertTrue(CORDON.cordon("n1", "node-power", "P").done)
        self.assertEqual(CORDON.Outcome(False, "n1", "still held by cluster-shutdown:run1"),
                         CORDON.release("n1", "node-power", "P"))
        self.assertTrue(cordoned(kube))

    def test_a_stale_old_mark_on_a_host_not_cordoned_is_dropped(self):
        # A failed upgrade left its mark; a person uncordoned the host by hand.
        kube = self.kube(node(upgrade_cordon="up1"))
        self.job("j1", "host-os")
        self.assertTrue(CORDON.cordon("n1", "host-os", "j1").done)
        self.assertNotIn(CORDON.NAMES.key("upgrade-cordon"), kube.object(PATH)["metadata"]["annotations"])
        self.assertEqual(CORDON.Outcome(True, "n1", "uncordoned"), CORDON.release("n1", "host-os", "j1"))
        self.assertFalse(cordoned(kube))

    def test_a_mark_of_an_ended_job_does_not_keep_the_host(self):
        self.job("dead", "host-os", status="failed")
        self.job("B", "node-power")
        kube = self.kube(node(cordoned=True, cordoned_by="host-os:dead,node-power:B", upgrade_cordon="gone"))
        self.assertEqual({"node-power:B"}, CORDON.holders(kube.object(PATH), CORDON.live_holders()))
        self.assertEqual(CORDON.Outcome(True, "n1", "uncordoned"), CORDON.release("n1", "node-power", "B"))
        self.assertFalse(cordoned(kube))
        self.assertEqual({}, kube.object(PATH)["metadata"]["annotations"], "the stale marks go in the same patch")

    def test_a_cordon_left_by_an_ended_job_is_not_taken_over(self):
        # The upgrade ended leaving it cordoned for a person to look at.
        kube = self.kube(node(cordoned=True, upgrade_cordon="up1"))
        self.job("up1", "platform-upgrade", status="failed")
        self.assertEqual(CORDON.Outcome(False, "n1", CORDON.LEFT), CORDON.cordon("n1", "host-os", "j1"))
        self.assertEqual([], kube.writes)

    def test_an_unreadable_job_list_never_joins_or_releases(self):
        kube = self.kube(node(cordoned=True, cordoned_by="host-os:j1"))
        def broken():
            raise OSError("store damaged")
        with mock.patch.object(CORDON.OPS, "stored", broken):
            self.assertFalse(CORDON.cordon("n1", "node-power", "j2").done)
            self.assertFalse(CORDON.release("n1", "host-os", "j1").done)
        self.assertEqual([], kube.writes)
        self.assertTrue(cordoned(kube))

    def test_an_unreadable_job_list_counts_as_busy(self):
        def broken():
            raise OSError("store damaged")
        with mock.patch.object(CORDON.OPS, "stored", broken):
            self.assertTrue(CORDON.busy_with("n1", "own"))

    def test_a_failed_read_never_uncordons(self):
        kube = self.kube(node(cordoned=True, cordoned_by="host-os:j1"))
        kube.fail(PATH, 500)
        out = CORDON.release("n1", "host-os", "j1")
        self.assertFalse(out.done)
        self.assertTrue(cordoned(kube))
        self.assertEqual([], kube.writes)


if __name__ == "__main__":
    unittest.main()
