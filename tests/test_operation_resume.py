import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_operations as OPS
import homestead_reclass as RC


class ResumeTests(unittest.TestCase):
    """A storage class change failed part-way through its swap had no way on:
    starting another was refused, and the stopped one could only be dismissed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = OPS.DATA_DIR, dict(OPS.RESOLVERS), dict(OPS.RESUMABLE)
        OPS.DATA_DIR = self.tmp.name
        OPS.RESUMABLE["reclass"] = RC.resumable
        # The poll must not advance anything here.
        OPS.RESOLVERS["reclass"] = lambda item: (item["status"], item.get("progress", 0), item.get("message", ""))

    def tearDown(self):
        OPS.DATA_DIR = self.saved[0]
        OPS.RESOLVERS.clear(); OPS.RESOLVERS.update(self.saved[1])
        OPS.RESUMABLE.clear(); OPS.RESUMABLE.update(self.saved[2])
        self.tmp.cleanup()

    def stopped(self, phase):
        op = OPS.start("reclass", "Move qdirstat-appdata to longhorn-v1-1x",
                       {"kind": "PersistentVolumeClaim", "name": "qdirstat-appdata", "namespace": "lab"},
                       "/volumes", {"phase": phase})
        items = OPS._read()
        items[-1].update(status="failed", finished_at=OPS._now(),
                         message="The Kubernetes operation resource no longer exists")
        OPS._write(items)
        return op["id"]

    def test_a_move_stopped_mid_swap_carries_on(self):
        op = self.stopped("swap")
        listed = next(o for o in OPS.list_operations() if o["id"] == op)
        self.assertTrue(listed["resumable"])
        self.assertNotIn("ref", listed)

        OPS.resume(op)

        item = next(o for o in OPS._read() if o["id"] == op)
        self.assertEqual(("running", ""), (item["status"], item["finished_at"]))

    def test_a_move_that_was_put_back_does_not(self):
        op = self.stopped("rolled-back")
        self.assertFalse(next(o for o in OPS.list_operations() if o["id"] == op)["resumable"])
        with self.assertRaisesRegex(ValueError, "put back or completed"):
            OPS.resume(op)

    def test_kinds_without_safe_steps_never_resume(self):
        op = OPS.start("backup", "Back up frigate", {"kind": "Backup", "name": "b"}, "/", {})
        items = OPS._read(); items[-1]["status"] = "failed"; OPS._write(items)
        with self.assertRaisesRegex(ValueError, "can carry on"):
            OPS.resume(op["id"])


if __name__ == "__main__":
    unittest.main()
