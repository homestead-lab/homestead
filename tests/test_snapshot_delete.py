import copy
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_snapshot_delete as sd


class SnapshotDeleteTests(unittest.TestCase):
    def setUp(self):
        self.snap = {"metadata": {"name": "expand-200", "uid": "snap-uid"}, "spec": {"volume": "vol"},
                     "status": {"userCreated": False, "readyToUse": True, "children": {"volume-head": True}}}
        self.volume = {"metadata": {"uid": "vol-uid"}, "status": {"state": "attached"}}
        self.engine = {"spec": {"volumeName": "vol"}, "status": {"snapshots": {"expand-200": {}}}}
        self.objects = {f"{sd.API}/snapshots/expand-200": self.snap, f"{sd.API}/volumes/vol": self.volume,
                        f"{sd.API}/engines": {"items": [self.engine]},
                        f"{sd.API}/settings/disable-snapshot-purge": {"value": "false"}}
        self.sent = []
        sd.bind(self.get, lambda *a, **kw: self.sent.append((a, kw)))
        self.cfg = dict(volume="vol", name="expand-200", uid="snap-uid", volume_uid="vol-uid", confirmation="expand-200")

    def get(self, path):
        if path not in self.objects:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.objects[path])

    def item(self):
        ops = Mock()
        sd.start(self.cfg, ops)
        return {"kind": "snapshot-delete", "ref": ops.start.call_args.args[4], "progress": 0}

    def test_source_is_system_not_scheduled_and_only_labels_identify_schedule(self):
        self.assertEqual("system", sd.source(self.snap))
        self.snap["status"]["userCreated"] = True
        self.assertEqual("user", sd.source(self.snap))
        self.snap["status"]["labels"] = {"recurring-job": "daily"}
        self.assertEqual("scheduled", sd.source(self.snap))
        del self.snap["status"]["userCreated"]
        self.assertEqual("unknown", sd.source(self.snap))

    def test_job_is_persisted_before_delete_and_uses_uid_precondition(self):
        item = self.item()
        self.assertEqual([], self.sent)
        self.assertEqual("running", sd.resolve(item)[0])
        args, _ = self.sent[0]
        self.assertEqual("DELETE", args[0])
        self.assertEqual("snap-uid", args[2]["preconditions"]["uid"])
        self.assertEqual("monitor", item["ref"]["phase"])

    def test_head_unknown_standby_and_disabled_purge_are_blocked(self):
        with self.assertRaisesRegex(ValueError, "live data"):
            sd.plan("vol", "volume-head")
        self.volume["status"]["isStandby"] = True
        self.assertFalse(sd.plan("vol", "expand-200")["ready"])
        self.volume["status"]["isStandby"] = False
        self.objects[f"{sd.API}/settings/disable-snapshot-purge"]["value"] = "true"
        with self.assertRaises(ValueError):
            self.item()
        self.assertEqual([], self.sent)

    def test_stale_confirmation_or_wrong_volume_never_mutates(self):
        for change in [{"uid": "old"}, {"volume_uid": "old"}, {"confirmation": "no"}, {"volume": "other"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                sd.start({**self.cfg, **change}, Mock())
        self.assertEqual([], self.sent)

    def test_replaced_snapshot_before_execution_is_untouched(self):
        item = self.item()
        self.snap["metadata"]["uid"] = "replacement"
        self.assertEqual("failed", sd.resolve(item)[0])
        self.assertEqual([], self.sent)

    def test_resume_after_request_was_sent_does_not_delete_again(self):
        item = self.item()
        self.snap["metadata"]["deletionTimestamp"] = "2026-09-26T00:00:00Z"
        sd.resolve(item)
        self.assertEqual([], self.sent)

    def test_head_parent_reports_dependency_not_success(self):
        item = self.item()
        item["ref"]["phase"] = "monitor"
        self.snap["status"]["markRemoved"] = True
        state, pct, message = sd.resolve(item)
        self.assertEqual(("running", 20), (state, pct))
        self.assertIn("fresh snapshot", message)
        self.assertEqual([], self.sent)

    def test_purge_progress_and_errors(self):
        item = self.item()
        item["ref"]["phase"] = "monitor"
        self.engine["status"]["purgeStatus"] = {"a": {"isPurging": True, "progress": 75}, "b": {"isPurging": True, "progress": 25}}
        self.assertEqual(("running", 30), sd.resolve(item)[:2])
        self.engine["status"]["purgeStatus"]["a"]["error"] = "replica unavailable"
        self.assertEqual("failed", sd.resolve(item)[0])

    def test_only_verified_cr_and_engine_removal_completes(self):
        item = self.item()
        item["ref"]["phase"] = "monitor"
        del self.objects[f"{sd.API}/snapshots/expand-200"]
        self.assertEqual("running", sd.resolve(item)[0])
        self.engine["status"]["snapshots"] = None
        self.assertEqual("running", sd.resolve(item)[0])
        self.engine["status"]["snapshots"] = {}
        self.assertEqual("succeeded", sd.resolve(item)[0])

    def test_timeout_is_not_success_and_resume_restarts_monitor_clock(self):
        item = self.item()
        item["ref"]["since"] = time.time() - sd.TIMEOUT - 1
        self.assertEqual("failed", sd.resolve(item)[0])
        item["message"] = "Carrying on from where it stopped"
        self.assertEqual("running", sd.resume_resolve(item)[0])


if __name__ == "__main__":
    unittest.main()
