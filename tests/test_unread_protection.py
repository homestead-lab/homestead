"""Data protection that could not be read is not data protection that is fine.

Longhorn's recurring jobs, its volumes' job labels, its backup volumes and
Homestead's object store are each read by something that used to answer an
empty list when the read failed: Delete group then took a group out of every
job while no volume was un-labelled, the Dashboard's Backup and Trim health
issues (and their alerts) went away, Data protection said there were no
backups yet, and Homestead's health card said "Backup storage: none".
Everything here runs on the shared fake (tests/fakes/kube.py)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as K
import homestead_operations as OPS
from test_longhorn_unread import LonghornFixture
from test_page_snapshot import LH, setUpModule  # noqa: F401 (restart record off)

NS = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
LH_VOLUMES = f"{NS}/volumes"
RECURRING = f"{NS}/recurringjobs"
G = "recurring-job-group.longhorn.io/"


def recurring_job(name, task, groups):
    return {"apiVersion": LH, "kind": "RecurringJob", "metadata": {"namespace": "longhorn-system", "name": name},
            "spec": {"name": name, "task": task, "cron": "0 2 * * *", "retain": 7, "concurrency": 1,
                     "groups": list(groups)}}


class ProtectionFixture(LonghornFixture):
    def setUp(self):
        super().setUp()
        self.LH = self.cluster.server.LH
        self.VOLUMES = sys.modules["homestead_longhorn_volumes"]
        # Other test files bind homestead_longhorn to fakes of their own and
        # leave it so: read and write through the shared fake here.
        for name, value in (("kget", K.kget), ("ksend", K.ksend)):
            self.enterContext(mock.patch.object(self.LH, name, value))
        # A Longhorn write takes the jobs' shared lock, in their data dir (/data).
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.enterContext(tempfile.TemporaryDirectory())))
        # pvc-1 is in the "media" group, which the job "nightly" backs up.
        volume = self.cluster.kube.object(f"{NS}/volumes/pvc-1")
        volume["metadata"]["labels"] = {G + "media": "enabled"}
        self.cluster.kube.add(volume)
        self.cluster.kube.add(recurring_job("nightly", "backup", ["media", "default"]))

    def writes(self):
        return [(method, path) for method, path, _, _ in self.cluster.kube.writes]


class DeleteGroupTests(ProtectionFixture):
    def test_a_strict_volume_read_that_fails_raises_and_says_longhorn_could_not_be_read(self):
        self.break_read(LH_VOLUMES)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's volumes could not be read"):
            self.VOLUMES._volumes(strict=True)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's recurring jobs could not be read"):
            self.break_read(RECURRING)
            self.VOLUMES._job_objects(strict=True)

    def test_the_lenient_reads_still_answer_empty_for_their_other_callers(self):
        # The #944 convention: only a caller that must know asks strictly.
        self.break_read(LH_VOLUMES)
        self.break_read(RECURRING)
        self.assertEqual(([], []), (self.VOLUMES._volumes(), self.VOLUMES._job_objects()))

    def test_a_cluster_without_longhorn_has_no_volumes(self):
        self.cluster.kube.absent_groups.add("longhorn.io")
        self.assertEqual([], self.VOLUMES._volumes(strict=True))
        self.assertEqual([], self.VOLUMES._job_objects(strict=True))

    def test_delete_group_with_volumes_unread_changes_nothing(self):
        self.break_read(LH_VOLUMES)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's volumes could not be read"):
            self.VOLUMES.delete_group("media")
        self.assertEqual([], self.writes())
        self.assertEqual(["media", "default"], self.cluster.kube.object(f"{RECURRING}/nightly")["spec"]["groups"])

    def test_delete_group_with_jobs_unread_changes_nothing(self):
        self.break_read(RECURRING)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's recurring jobs could not be read"):
            self.VOLUMES.delete_group("media")
        self.assertEqual([], self.writes())
        self.assertEqual({G + "media": "enabled"}, self.cluster.kube.object(f"{NS}/volumes/pvc-1")["metadata"]["labels"])

    def test_delete_group_with_everything_read_takes_the_group_off_the_volume_and_the_job(self):
        result = self.VOLUMES.delete_group("media")
        self.assertEqual(["data"], result["back_to_default"])
        self.assertEqual(["default"], self.cluster.kube.object(f"{RECURRING}/nightly")["spec"]["groups"])
        self.assertNotIn(G + "media", self.cluster.kube.object(f"{NS}/volumes/pvc-1")["metadata"].get("labels") or {})


if __name__ == "__main__":
    unittest.main()
