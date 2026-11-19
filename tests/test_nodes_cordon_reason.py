"""The Hosts list says which Homestead job holds a host's cordon, from the
marks the jobs write (homestead_cordon), so a person can tell a job's cordon
from their own: a job still running, or one that has ended and left it."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server
from fakes.kube import Kube

NODES = server.NODES


def node(name, cordoned=True, **annotations):
    return {"apiVersion": "v1", "kind": "Node",
            "metadata": {"name": name, "labels": {}, "annotations": dict(annotations)},
            "spec": {"unschedulable": True} if cordoned else {},
            "status": {"capacity": {"cpu": "4", "memory": "8Gi"}, "allocatable": {},
                       "conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {}, "addresses": []}}


def job(job_id, kind, status="running", title="", **ref):
    return {"id": job_id, "kind": kind, "status": status, "title": title, "ref": ref}


UPGRADE = "homestead.io/upgrade-cordon"
MARK = "homestead.io/cordoned-by"
SHUTDOWN = "homestead.io/cluster-shutdown"


class CordonReasonTests(unittest.TestCase):
    def rows(self, nodes, jobs):
        self.reads = 0

        def stored():
            self.reads += 1
            if isinstance(jobs, Exception):
                raise jobs
            return [dict(item) for item in jobs]
        with Kube(*nodes), \
                mock.patch.multiple(NODES, node_temps=lambda **kw: {}, get_app_settings=lambda: {},
                                    DEFAULT_APP_SETTINGS={"smart": {}}, node_duties=lambda pods, vols: {},
                                    node_stats=lambda name: {}, workload_names=lambda pods, system: ([], [])), \
                mock.patch.object(NODES.DISKS, "summary", lambda: {}), \
                mock.patch.object(NODES.HOST_OS, "report", lambda: {"hosts": {}}), \
                mock.patch.object(NODES.OPS, "stored", stored):
            return {row["name"]: row for row in NODES.get_nodes()}

    def reason(self, annotations, jobs, cordoned=True):
        return self.rows([node("node-a", cordoned, **annotations)], jobs)["node-a"]["cordoned_by"]

    def test_a_running_upgrade_holds_the_cordon(self):
        self.assertEqual({"job": "op-7", "kind": "platform-upgrade", "title": "Upgrade k3s", "running": True},
                         self.reason({UPGRADE: "op-7"}, [job("op-7", "platform-upgrade", title="Upgrade k3s")]))

    def test_a_v2_clean_up_that_ended_left_the_cordon(self):
        self.assertEqual({"job": "op-3", "kind": "longhorn-v2-cleanup", "title": "Stop Longhorn V2", "running": False},
                         self.reason({MARK: "op-3"}, [job("op-3", "longhorn-v2-cleanup", "failed", "Stop Longhorn V2")]))

    def test_a_cluster_shutdown_mark_is_its_run_mapped_to_the_job(self):
        jobs = [job("op-9", "cluster-shutdown", title="Shut down the cluster", run="run-42")]
        self.assertEqual({"job": "op-9", "kind": "cluster-shutdown", "title": "Shut down the cluster", "running": True},
                         self.reason({SHUTDOWN: "run-42"}, jobs))

    def test_a_cordon_left_for_a_person_names_the_job_that_left_it(self):
        jobs = [job("op-5", "node-power", "succeeded", "Restart node-a")]
        self.assertEqual({"job": "op-5", "kind": "node-power", "title": "Restart node-a", "running": False},
                         self.reason({MARK: "person:op-5"}, jobs))

    def test_a_cordon_left_for_a_person_is_never_a_running_jobs(self):
        # Left for a person on purpose: even while the job that left it still runs.
        jobs = [job("op-5", "node-power", "running", "Restart node-a")]
        self.assertFalse(self.reason({MARK: "person:op-5"}, jobs)["running"])

    def test_a_job_with_no_title_gives_no_raw_kind(self):
        self.assertEqual({"job": "op-7", "kind": "platform-upgrade", "title": "", "running": True},
                         self.reason({UPGRADE: "op-7"}, [job("op-7", "platform-upgrade")]))

    def test_a_mark_whose_job_is_gone_from_the_list_is_a_job_that_has_ended(self):
        self.assertEqual({"job": "", "kind": "platform-upgrade", "title": "", "running": False}, self.reason({UPGRADE: "op-1"}, []))

    def test_a_running_holder_comes_before_one_that_ended(self):
        # The ended holder sorts first (longhorn-… before platform-…): the running one still wins.
        jobs = [job("op-1", "platform-upgrade", title="Upgrade k3s"),
                job("op-2", "longhorn-v2-cleanup", "failed", "Stop Longhorn V2")]
        self.assertEqual({"job": "op-1", "kind": "platform-upgrade", "title": "Upgrade k3s", "running": True},
                         self.reason({UPGRADE: "op-1", MARK: "longhorn-v2-cleanup:op-2"}, jobs))

    def test_a_job_of_another_kind_with_the_same_id_is_not_the_holder(self):
        self.assertEqual({"job": "", "kind": "platform-upgrade", "title": "", "running": False},
                         self.reason({UPGRADE: "op-7"}, [job("op-7", "node-power", title="Restart node-a")]))

    def test_a_cordon_with_no_homestead_mark_is_a_persons(self):
        self.assertIsNone(self.reason({"other": "kept"}, [job("op-7", "platform-upgrade")]))

    def test_a_mark_on_a_host_not_cordoned_holds_nothing(self):
        self.assertIsNone(self.reason({UPGRADE: "op-7"}, [job("op-7", "platform-upgrade")], cordoned=False))

    def test_an_unread_job_list_never_says_a_job_holds_it(self):
        self.assertIsNone(self.reason({UPGRADE: "op-7"}, OSError("store unreadable")))

    def test_the_job_list_is_read_once_for_every_host(self):
        rows = self.rows([node(f"node-{c}", **{UPGRADE: "op-7"}) for c in "abcdef"],
                         [job("op-7", "platform-upgrade", title="Upgrade k3s")])
        self.assertTrue(all(row["cordoned_by"]["running"] for row in rows.values()))
        self.assertEqual(1, self.reads)

    def test_no_host_cordoned_reads_no_job_list(self):
        rows = self.rows([node("node-a", cordoned=False, **{UPGRADE: "op-7"})], [job("op-7", "platform-upgrade")])
        self.assertIsNone(rows["node-a"]["cordoned_by"])
        self.assertEqual(0, self.reads)


if __name__ == "__main__":
    unittest.main()
