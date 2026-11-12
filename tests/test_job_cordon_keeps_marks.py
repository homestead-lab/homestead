"""A job's own cordon step keeps another job's cordon mark, as it always has:
only a person's Cordon or Uncordon on the Hosts page clears them all
(tests/test_cordon_clears_upgrade_mark.py). So a job that holds a host - a
Longhorn V2 clean-up, a cluster shutdown - still gives it back at its end
after a power or maintenance job cordoned the same host."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_lifecycle as L
import homestead_node_actions as NODE_ACTIONS
from fakes.kube import Kube

V2 = "homestead.io/cordoned-by"
SHUTDOWN = "homestead.io/cluster-shutdown"
UPGRADE = "homestead.io/upgrade-cordon"
MARKS = {V2: "op-3", SHUTDOWN: "run-42", UPGRADE: "op-7", "other": "kept"}
# What dev's cordon step leaves: the upgrade's mark goes (#1070), every other stays.
AFTER_A_JOBS_CORDON = {V2: "op-3", SHUTDOWN: "run-42", "other": "kept"}


class Drained(Exception):
    pass


class JobCordonKeepsMarksTests(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube({
            "apiVersion": "v1", "kind": "Node", "spec": {"unschedulable": True},
            "metadata": {"name": "node-1", "annotations": dict(MARKS)},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}))
        for name, value in (("ksend", K.ksend), ("kget", K.kget), ("_bust", lambda *keys: None), ("NODE_POWER_ENABLED", True)):
            self.enterContext(mock.patch.object(L, name, value))
        self.seen = None

        def drain(node, **kwargs):     # the step after the cordon: what the cordon left
            self.seen = dict(self.kube.object("/api/v1/nodes/node-1")["metadata"].get("annotations") or {})
            raise Drained()
        self.enterContext(mock.patch.object(NODE_ACTIONS, "drain", drain))

    def test_a_power_jobs_cordon_keeps_other_jobs_marks(self):
        with self.assertRaises(Drained):
            NODE_ACTIONS.node_power("node-1", "reboot", before_send=lambda: None, reviewed_pods=[])
        self.assertEqual(AFTER_A_JOBS_CORDON, self.seen)

    def test_a_maintenance_jobs_cordon_keeps_other_jobs_marks(self):
        with self.assertRaises(Drained):
            NODE_ACTIONS.node_maintain("node-1", lambda: None, [], lambda *args, **kwargs: None)
        self.assertEqual(AFTER_A_JOBS_CORDON, self.seen)


if __name__ == "__main__":
    unittest.main()
