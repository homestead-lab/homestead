"""Longhorn's recurring jobs that could not be read (#927, the #620 rule): a
failed read is not an empty list. list_jobs(strict=True) raises, and
/api/lh/overview says the jobs are unread instead of answering with no jobs,
no clashes and every volume unprotected."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_longhorn as LH
import homestead_longhorn_jobs as JOBS
import homestead_longhorn_target as BACKUP_TARGET
import homestead_longhorn_volumes as VOLUMES


def refused(path, **_):
    if path.endswith("/recurringjobs"):
        raise RuntimeError("recurringjobs is forbidden")
    return {"items": []}


class ListJobsTests(unittest.TestCase):
    def test_a_strict_read_that_fails_raises_instead_of_answering_no_jobs(self):
        with mock.patch.object(LH, "kget", refused):
            with self.assertRaisesRegex(RuntimeError, "forbidden"):
                JOBS.list_jobs(strict=True)
            # The Dashboard's health still reads a cluster without Longhorn as no jobs.
            self.assertEqual([], JOBS.list_jobs())

    def test_a_read_that_answers_lists_the_jobs(self):
        with mock.patch.object(LH, "kget", lambda path, **_: {"items": []}):
            self.assertEqual([], JOBS.list_jobs(strict=True))


class OverviewTests(unittest.TestCase):
    def overview(self, list_jobs):
        volumes = [{"name": "pvc-a", "pvc": "data", "groups": ["media"], "labels": {}}]
        with mock.patch.object(JOBS, "list_jobs", list_jobs), \
                mock.patch.object(VOLUMES, "_volumes", return_value=volumes), \
                mock.patch.object(BACKUP_TARGET, "backup_target", return_value={"configured": True}):
            return LH.overview()

    def test_jobs_that_could_not_be_read_are_named_not_left_out(self):
        def fails(strict=False):
            self.assertTrue(strict, "the overview reads the jobs strictly")
            raise RuntimeError("recurringjobs is forbidden")
        overview = self.overview(fails)
        self.assertIn("forbidden", overview["jobs_unread"])
        self.assertEqual([], overview["jobs"])
        self.assertEqual([], overview["clashes"])
        # Coverage is unknown, not none: nothing offers to protect a volume on a guess.
        self.assertEqual([], overview["unprotected"])
        self.assertIsNone(overview["protected"])
        self.assertIsNone(overview["backed_up"])
        self.assertIsNone(overview["volumes"][0]["backed_up"])
        self.assertIsNone(overview["volumes"][0]["snapshotted"])
        self.assertIsNone(overview["volumes"][0]["protected_by"], "not known, not covered by nothing")
        # The groups the volumes wear are still listed.
        self.assertEqual(["default", "media"], overview["groups"])

    def test_jobs_that_were_read_carry_no_unread_mark(self):
        overview = self.overview(lambda strict=False: [])
        self.assertNotIn("jobs_unread", overview)
        self.assertEqual(["data"], overview["unprotected"])
        self.assertEqual(0, overview["protected"])
        self.assertIs(False, overview["volumes"][0]["backed_up"])


if __name__ == "__main__":
    unittest.main()
