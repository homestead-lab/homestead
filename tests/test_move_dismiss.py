import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_move_engine as ENGINE


def move(id, status, source_removed=False):
    return {"id": id, "cluster": "oldcluster", "name": id, "kind": "container", "status": status,
            "phase": "done", "created_at": id, "source_removed": source_removed}


class DismissTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = ENGINE.DATA_DIR
        ENGINE.DATA_DIR = self.dir.name
        with open(os.path.join(self.dir.name, ENGINE.STORE), "w", encoding="utf-8") as handle:
            json.dump([move("mosquitto", "succeeded"), move("zigbee", "running"), move("birdnet", "failed"),
                       move("frigate", "cancelled"), move("ha", "succeeded", True)], handle)

    def tearDown(self):
        ENGINE.DATA_DIR = self.saved
        self.dir.cleanup()

    def names(self):
        return sorted(m["id"] for m in ENGINE.moves())

    def test_one_finished_move_is_cleared_and_the_source_is_not_asked_anything(self):
        with mock.patch.object(ENGINE.CLIENT, "remote", side_effect=AssertionError("nothing is asked of the source")):
            result = ENGINE.dismiss("mosquitto")
        self.assertEqual(["birdnet", "frigate", "ha", "zigbee"], self.names())
        self.assertIn("oldcluster still has the stopped original of mosquitto", result["detail"])

    def test_all_finished_moves_clear_at_once_and_failed_ones_stay(self):
        ENGINE.dismiss()
        self.assertEqual(["birdnet", "zigbee"], self.names())

    def test_a_failed_move_is_not_cleared(self):
        with self.assertRaisesRegex(ValueError, "retry or put back"):
            ENGINE.dismiss("birdnet")
        self.assertIn("birdnet", self.names())


if __name__ == "__main__":
    unittest.main()
