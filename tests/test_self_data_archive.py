import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import server
import homestead_operations as OPS
import homestead_capacity_review as SIGN
import homestead_self_data_prepare as P
from homestead_storage_journal import Held


class PreparationArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(OPS, "DATA_DIR", directory.name),
                      mock.patch.object(OPS, "WRITE_GUARD", None),
                      mock.patch.object(SIGN, "_key", lambda: b"archive-fixture")):
            patch.start(); self.addCleanup(patch.stop)
        self.job = {"id": "prepared-job", "kind": P.KIND, "title": "Prepare Homestead data volume",
                    "status": "succeeded", "progress": 100, "message": "Destination prepared",
                    "ref": {"namespace": "lab", "deployment": {"name": "homestead", "uid": "deployment-uid"},
                            "source": {"name": "source", "uid": "source-uid"}, "destination": "destination",
                            "operation": "a" * 24, "node": "node1", "storage_class": "destination-class",
                            "prepared": {"claim_uid": "destination-uid", "volume_uid": "pv-uid", "volume": "pv"},
                            "retain_resources": False, "storage_writes": [{"state": "observed", "after": {"uid": "destination-uid"}}]}}
        OPS._write([self.job])

    def archive(self, body=None, *, start=False, actor="admin", namespace="lab"):
        return P.archive(body or {"id": self.job["id"]}, actor, namespace, "homestead", OPS, start=start)

    def confirmed(self, review):
        return {"id": review["id"], "capacity_token": review["capacity_token"], "confirm_archive": True}

    def test_archive_keeps_both_volume_identities_and_receipts_through_pruning(self):
        before = OPS._read()
        with mock.patch.object(OPS, "_refresh", side_effect=AssertionError("must not resolve")):
            review = self.archive()
            self.assertEqual(before, OPS._read(), "preview is read only")
            self.assertEqual("destination", review["destination"])
            self.archive(self.confirmed(review), start=True)
        saved = OPS._read()[0]
        for key in ("source", "prepared", "destination", "storage_writes"):
            self.assertEqual(self.job["ref"][key], saved["ref"][key])
        self.assertTrue(saved["ref"]["preparation_archived"])
        self.assertEqual([], OPS.snapshot())
        self.assertEqual([], OPS.list_operations())
        with mock.patch.object(OPS, "MAX_OPERATIONS", 0):
            OPS._write(OPS._read())
            self.assertEqual(1, len(OPS._read()))
        self.assertEqual(0, OPS.dismiss_finished()["remaining"])
        self.assertEqual(1, len(OPS._read()))
        self.assertIn("volumes and receipts retained", OPS._read()[0]["history"][-1]["m"])
        with self.assertRaises(Held): self.archive(self.confirmed(review), start=True)

    def test_archiving_a_legacy_preparation_preserves_its_original_store_and_log(self):
        OPS._write([])
        OPS.SHARED.write_json(Path(OPS.DATA_DIR) / OPS.LEGACY_STORE, [self.job], durable=True)
        self.archive(self.confirmed(self.archive()), start=True)
        saved = OPS._read()[0]
        self.assertTrue(saved["_legacy_store"])
        self.assertTrue(saved["ref"]["preparation_archived"])
        self.assertEqual([], OPS.snapshot())
        log = OPS.log(self.job["id"])
        self.assertIn("Preparation record archived", str(log))
        with open(Path(OPS.DATA_DIR) / OPS.STORE, encoding="utf-8") as handle:
            self.assertEqual("[]", handle.read())

    def test_archive_refuses_running_failed_or_retained_recovery_records(self):
        for status, retained, prepared in (("running", False, True), ("failed", False, True),
                                           ("succeeded", True, True), ("succeeded", False, False)):
            with self.subTest(status=status, retained=retained, prepared=prepared):
                job = copy.deepcopy(self.job); job["status"] = status
                job["ref"]["retain_resources"] = retained
                if not prepared: job["ref"].pop("prepared")
                OPS._write([job])
                with self.assertRaises(Held): self.archive()
                self.assertFalse(OPS._public(job)["preparation_archivable"])
                job["ref"]["preparation_archived"] = True
                self.assertFalse(OPS._archived_preparation(job))

    def test_archive_requires_confirmation_bound_to_actor_namespace_and_exact_saved_ref(self):
        review = self.archive(); body = self.confirmed(review)
        for changed in ({**body, "confirm_archive": False}, {**body, "capacity_token": "invalid"}, {**body, "unknown": True}):
            with self.assertRaises(Held): self.archive(changed, start=True)
        with self.assertRaises(Held): self.archive(body, start=True, actor="another-admin")
        with self.assertRaises(Held): self.archive(body, start=True, namespace="other")
        job = copy.deepcopy(self.job); job["ref"]["source"]["uid"] = "replacement"
        OPS._write([job])
        with self.assertRaises(Held): self.archive(body, start=True)
        self.assertEqual([job], OPS._read())

    def test_archiving_completed_record_does_not_bypass_other_running_or_recovery_jobs(self):
        blocker = {"id": "recovery", "kind": "backup", "title": "Recover backup", "status": "failed",
                   "message": "inspect retained helper", "ref": {"retain_resources": True}}
        running = {"id": "running", "kind": "backup", "status": "running", "ref": {}}
        OPS._write([self.job, blocker, running])
        self.archive(self.confirmed(self.archive()), start=True)
        blockers = P.blocking_jobs(OPS)
        self.assertEqual(["recovery", "running"], [job["id"] for job in blockers])
        self.assertIn("Recover backup (failed, recovery)", P.blocked_message("Blocked", blockers))
        self.assertEqual(["recovery"], [job["id"] for job in P.blocking_jobs(OPS, "running")])

    def test_archive_is_admin_only_and_never_calls_the_cluster(self):
        for path in ("/api/self/data/prepare/archive", "/api/self/data/prepare/archive/preview"):
            self.assertEqual("admin", server.needed_role(path, "POST"))
        with mock.patch.object(server, "kget", side_effect=AssertionError("cluster read")), mock.patch.object(server, "ksend", side_effect=AssertionError("cluster mutation")):
            self.archive(self.confirmed(self.archive()), start=True)

    def test_settings_labels_historical_and_current_volume_preparations_and_hides_archived(self):
        info = {"pvc": "destination", "classes": []}
        with mock.patch.object(server.SELF, "NS", "lab"), mock.patch.object(server, "homestead_data_volume", return_value=info), mock.patch.object(server, "kget", return_value={"items": []}):
            state = server.self_data_preparation_state()
            item = state["preparations"][0]
            self.assertFalse(item["prepared"])
            self.assertTrue(item["archivable"])
            self.assertIn("currently uses", item["message"])
            info["pvc"] = "new-source"
            self.assertIn("earlier source", server.self_data_preparation_state()["preparations"][0]["message"])
            info["pvc"] = "source"
            self.assertTrue(server.self_data_preparation_state()["preparations"][0]["prepared"])
            self.archive(self.confirmed(self.archive()), start=True)
            self.assertEqual([], server.self_data_preparation_state()["preparations"])


if __name__ == "__main__": unittest.main()
