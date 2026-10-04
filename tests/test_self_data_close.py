"""A cancelled data move that kept both volumes can be closed, once
Homestead runs on its original volume again.

Its retained record blocked every later preparation - "Finish or review
these jobs" - and nothing in Homestead could finish or review it."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_operations as OPS
import homestead_capacity_review as SIGN
import homestead_self_data_prepare as P
from homestead_storage_journal import Held


class CloseRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(OPS, "DATA_DIR", directory.name),
                      mock.patch.object(OPS, "WRITE_GUARD", None),
                      mock.patch.object(SIGN, "_key", lambda: b"close-fixture")):
            patch.start(); self.addCleanup(patch.stop)
        self.job = {"id": "move-job", "kind": P.HANDOFF, "title": "Move Homestead data", "status": "cancelled",
                    "progress": 100, "href": "/settings",
                    "message": "Recovered Homestead on its original volume. Both data volumes and the recovery audit are retained.",
                    "ref": {"namespace": "lab", "source": "homestead-data", "destination": "homestead-data-e3c6",
                            "retain_resources": True}}
        OPS._write([self.job])
        self.running = ("homestead-data", True)

    def close(self, body=None, *, start=False):
        return P.close_recovery(body or {"id": "move-job"}, "admin", "lab", "homestead", OPS,
                                lambda: self.running, start=start)

    def test_it_blocks_until_closed_then_keeps_everything(self):
        self.assertEqual(["move-job"], [j["id"] for j in P.blocking_jobs(OPS)])
        self.assertTrue(P.blocking_jobs(OPS)[0]["closable"])
        before = OPS._read()
        review = self.close()
        self.assertEqual(before, OPS._read(), "the review writes nothing")
        self.assertEqual(("homestead-data", "homestead-data-e3c6"), (review["source"], review["destination"]))
        self.close({"id": "move-job", "capacity_token": review["capacity_token"], "confirm_close": True}, start=True)
        self.assertEqual([], P.blocking_jobs(OPS), "another preparation can start")
        saved = OPS._read()[0]
        self.assertTrue(saved["ref"]["retain_resources"], "both volumes stay protected")
        self.assertEqual("homestead-data", saved["ref"]["recovery_closed"]["source"])
        self.assertIn("Recovery closed", saved["history"][-1]["m"])

    def test_it_is_refused_unless_homestead_is_ready_on_the_original_volume(self):
        self.running = ("homestead-data-e3c6", True)
        with self.assertRaisesRegex(Held, "not running on the move's original volume"):
            self.close()
        self.running = ("homestead-data", False)
        with self.assertRaisesRegex(Held, "not ready"):
            self.close()
        # A review made while it was fine does not carry over when it changes.
        self.running = ("homestead-data", True)
        review = self.close()
        self.running = ("homestead-data-e3c6", True)
        with self.assertRaises(Held):
            self.close({"id": "move-job", "capacity_token": review["capacity_token"], "confirm_close": True}, start=True)
        self.assertEqual(1, len(P.blocking_jobs(OPS)))

    def test_only_a_cancelled_move_that_kept_its_volumes_can_be_closed(self):
        for change in ({"status": "running"}, {"status": "succeeded"}, {"kind": P.KIND}):
            OPS._write([{**self.job, **change}])
            with self.assertRaises(Held):
                self.close()
        OPS._write([{**self.job, "ref": {**self.job["ref"], "retain_resources": False}}])
        with self.assertRaises(Held):
            self.close()

    def test_closing_needs_the_reviewed_token_and_confirmation(self):
        review = self.close()
        for body in ({"id": "move-job", "capacity_token": review["capacity_token"]},
                     {"id": "move-job", "capacity_token": "forged", "confirm_close": True}):
            with self.assertRaises(Held):
                self.close(body, start=True)
        self.assertEqual(1, len(P.blocking_jobs(OPS)))


if __name__ == "__main__":
    unittest.main()
