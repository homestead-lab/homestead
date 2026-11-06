"""homestead_whole_copy beyond the table (#1126): a volume with two engines
while it migrates, an engine going away, and the volume-level "rebuilding"."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_whole_copy as WHOLE


def replica(name="vol-a-r-1", state="running", **spec):
    return {"metadata": {"name": name}, "spec": {"volumeName": "vol-a", "nodeID": "node-b",
                                                  "healthyAt": "2026-11-01T08:00:00Z", "failedAt": "", **spec},
            "status": {"currentState": state}}


def engine(modes, state="running", **meta):
    return {"metadata": {"name": "vol-a-e-0", **meta}, "spec": {"volumeName": "vol-a"},
            "status": {"currentState": state, "replicaModeMap": modes}}


class WholeCopyTests(unittest.TestCase):
    def test_while_it_migrates_rw_in_either_running_engine_counts(self):
        engines = [engine({"vol-a-r-1": "WO"}), engine({"vol-a-r-1": "RW"})]
        self.assertTrue(WHOLE.whole_copy(replica(), engines, detached_ok=True))

    def test_an_engine_being_deleted_does_not_make_a_copy_whole(self):
        gone = engine({"vol-a-r-1": "RW"}, deletionTimestamp="2026-11-01T10:00:00Z")
        self.assertFalse(WHOLE.whole_copy(replica(), [gone], detached_ok=True))
        # With no running engine, on a detached volume: only a stopped copy counts, and only if asked.
        self.assertTrue(WHOLE.whole_copy(replica(state="stopped"), [gone], detached_ok=True, volume_state="detached"))
        self.assertFalse(WHOLE.whole_copy(replica(state="stopped"), [gone], detached_ok=False, volume_state="detached"))

    def test_a_stopped_copy_counts_only_when_its_volume_says_detached(self):
        stopped, down = replica(state="stopped"), engine({"vol-a-r-1": "RW"}, state="stopped")
        detached = {"metadata": {"name": "vol-a"}, "status": {"state": "detached"}}
        attached = {"metadata": {"name": "vol-a"}, "status": {"state": "attached"}}
        self.assertTrue(WHOLE.judge([down], detached_ok=True, volumes=[detached])(stopped))
        # Attached with no running engine: Longhorn is not serving it, so no copy is whole.
        self.assertFalse(WHOLE.judge([down], detached_ok=True, volumes=[attached])(stopped))
        # A volume not read is not taken to be detached.
        self.assertFalse(WHOLE.judge([down], detached_ok=True)(stopped))

    def test_a_host_missing_from_the_hosts_given_is_not_ready(self):
        whole = WHOLE.judge([engine({"vol-a-r-1": "RW"})], detached_ok=True, nodes={})
        self.assertFalse(whole(replica()))
        self.assertTrue(WHOLE.judge([engine({"vol-a-r-1": "RW"})], detached_ok=True)(replica()))

    def test_rebuilding_names_volumes_with_a_wo_copy(self):
        engines = [engine({"vol-a-r-1": "RW", "vol-a-r-2": "WO"}),
                   {"spec": {}, "status": {"replicaModeMap": {"x": "WO"}}}]   # no volumeName: the one asked for
        self.assertEqual({"vol-a", None}, WHOLE.rebuilding(engines))
        self.assertTrue(WHOLE.rebuilding(engines[1:], "vol-b"))
        self.assertFalse(WHOLE.rebuilding([engine({"vol-a-r-1": "RW"})], "vol-a"))


if __name__ == "__main__":
    unittest.main()
