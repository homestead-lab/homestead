"""Longhorn's filesystem freeze around snapshots (item 5a of spike 5).

Longhorn 1.7 and later can freeze a volume's filesystem for the moment of
each snapshot, its own recurring jobs and backups included, so the copy is
filesystem-consistent instead of crash-consistent. Homestead shows the
cluster setting and each volume's override on Data protection, lets an admin
change them, and marks each write with who made it."""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_changes as CH

AUTHOR = CH.AUTHOR


class Marks(unittest.TestCase):
    """The changedBy mark on objects other than Deployments: a Longhorn
    Setting or Volume, signed over its kind, namespace and name."""

    def setUp(self):
        for name, value in (("_author_key", lambda: b"test-key"),
                            ("_authors", lambda: {"users": ["alice", "bob"], "api_keys": ["ci"]})):
            patcher = mock.patch.object(CH, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, CH.REQUEST, "user", None)

    def marked(self, raw):
        return {"metadata": {"annotations": {AUTHOR: raw}}}

    def test_a_mark_reads_back_as_who_made_it(self):
        CH.REQUEST.user = "alice"
        raw = CH.object_mark("Setting", "longhorn-system", "freeze-filesystem-for-snapshot", now=1000)
        self.assertEqual(json.loads(raw)["at"], 1000)
        self.assertEqual(CH.object_author("Setting", "longhorn-system", "freeze-filesystem-for-snapshot", self.marked(raw)),
                         {"who": "alice", "at": 1000})

    def test_an_api_key_and_a_job_read_back_too(self):
        CH.REQUEST.user = "api-key:ci"
        raw = CH.object_mark("Volume", "longhorn-system", "pvc-1", now=5)
        self.assertEqual(CH.object_author("Volume", "longhorn-system", "pvc-1", self.marked(raw))["who"], "api-key:ci")
        CH.REQUEST.user = None
        raw = CH.object_mark("Volume", "longhorn-system", "pvc-1", now=5)
        self.assertEqual(CH.object_author("Volume", "longhorn-system", "pvc-1", self.marked(raw))["who"], "")

    def test_a_mark_copied_to_another_object_or_kind_does_not_check_out(self):
        CH.REQUEST.user = "alice"
        raw = CH.object_mark("Volume", "longhorn-system", "pvc-1", now=5)
        self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-2", self.marked(raw)))
        self.assertIsNone(CH.object_author("Setting", "longhorn-system", "pvc-1", self.marked(raw)))
        self.assertIsNone(CH.object_author("Deployment", "longhorn-system", "pvc-1", self.marked(raw)))

    def test_an_altered_or_odd_mark_reads_as_nobody(self):
        CH.REQUEST.user = "alice"
        mark = json.loads(CH.object_mark("Volume", "longhorn-system", "pvc-1", now=5))
        for raw in (json.dumps({**mark, "at": 6}), json.dumps({**mark, "by": "0" * 32}), "x" * 300, "not json", None, 7):
            self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-1", self.marked(raw)), raw)
        self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-1", {}))
        self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-1", None))

    def test_a_deployment_mark_is_not_an_object_mark(self):
        """The Deployment digest stays exactly as it was, and the two never match."""
        self.assertEqual(CH._signed(b"test-key", "lab", "app", 5, "alice"),
                         CH._signed(b"test-key", "lab", "app", 5, "alice"))
        CH.REQUEST.user = "alice"
        raw = CH.object_mark("Volume", "lab", "app", now=5)
        self.assertNotEqual(json.loads(raw)["by"], CH._signed(b"test-key", "lab", "app", 5, "alice"))

    def test_no_signing_key_means_no_mark(self):
        with mock.patch.object(CH, "_author_key", mock.Mock(side_effect=LookupError("no key yet"))):
            self.assertIsNone(CH.object_mark("Volume", "longhorn-system", "pvc-1"))
            self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-1",
                                               self.marked('{"at":5,"by":"' + "0" * 32 + '"}')))


if __name__ == "__main__":
    unittest.main()
