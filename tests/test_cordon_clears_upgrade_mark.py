"""Cordon and Uncordon from the Hosts page take the cordon over from any
cluster upgrade: they clear its mark (homestead.io/upgrade-cordon) in the
same patch, so the upgrade's end never undoes a person's own cordon (#1070)."""
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

MARK = "homestead.io/upgrade-cordon"


def node(**annotations):
    return {"apiVersion": "v1", "kind": "Node",
            "metadata": {"name": "node-1", **({"annotations": annotations} if annotations else {})},
            "spec": {"unschedulable": True}}


class CordonClearsUpgradeMarkTests(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube(node(**{MARK: "op-7", "other": "kept"})))
        self.enterContext(mock.patch.object(L, "ksend", K.ksend))
        self.enterContext(mock.patch.object(L, "_bust", lambda *keys: None))

    def annotations(self):
        return self.kube.object("/api/v1/nodes/node-1")["metadata"].get("annotations") or {}

    def test_uncordon_clears_the_upgrade_mark(self):
        NODE_ACTIONS.set_cordon("node-1", False)
        self.assertNotIn(MARK, self.annotations())
        self.assertEqual("kept", self.annotations()["other"])
        self.assertEqual(1, len(self.kube.writes), "one patch: the cordon and the mark together")

    def test_cordon_makes_it_a_persons_cordon(self):
        NODE_ACTIONS.set_cordon("node-1", True)
        self.assertNotIn(MARK, self.annotations())
        self.assertTrue(self.kube.object("/api/v1/nodes/node-1")["spec"]["unschedulable"])

    def test_a_node_with_no_annotations_still_cordons(self):
        self.kube.add(node())
        NODE_ACTIONS.set_cordon("node-1", True)
        self.assertNotIn(MARK, self.annotations())


if __name__ == "__main__":
    unittest.main()
