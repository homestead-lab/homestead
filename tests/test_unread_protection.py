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
import homestead_alerts as ALERTS
import homestead_cache as CACHE
import homestead_cluster as CLUSTER
import homestead_kube as K
import homestead_operations as OPS
import homestead_shared as SHARED_MODULE
from test_longhorn_unread import LonghornFixture
from test_page_snapshot import LH, setUpModule  # noqa: F401 (restart record off)

NS = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
LH_VOLUMES = f"{NS}/volumes"
RECURRING = f"{NS}/recurringjobs"
BACKUP_VOLUMES = f"{NS}/backupvolumes"
BACKUPS = f"{NS}/backups"
CRONJOBS = "/apis/batch/v1/namespaces/longhorn-system/cronjobs"
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


class BackupVolumesTests(ProtectionFixture):
    def setUp(self):
        super().setUp()
        self.BACKUPS = sys.modules["homestead_longhorn_backups"]
        self.cluster.kube.add({"apiVersion": LH, "kind": "BackupVolume",
                               "metadata": {"namespace": "longhorn-system", "name": "pvc-1"},
                               "status": {"lastBackupAt": "2026-09-20T02:00:00Z", "size": "1048576"}})

    def test_a_normal_read_lists_the_target_s_backup_volumes(self):
        self.assertEqual([("pvc-1", "data", True)],
                         [(row["name"], row["pvc"], row["exists"]) for row in self.BACKUPS.backup_volumes()])

    def test_a_failed_read_raises_and_says_longhorn_s_backups_could_not_be_read(self):
        # Data protection shows a failed call as unread; [] read as "No backups on the target yet."
        self.break_read(BACKUP_VOLUMES)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's backups could not be read"):
            self.BACKUPS.backup_volumes()

    def test_backups_unread_do_not_count_as_none_taken(self):
        self.break_read(BACKUPS)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's backups could not be read"):
            self.BACKUPS.backup_volumes()

    def test_volumes_unread_do_not_make_every_backup_one_of_a_deleted_volume(self):
        self.break_read(LH_VOLUMES)
        with self.assertRaisesRegex(RuntimeError, "Longhorn's backups could not be read"):
            self.BACKUPS.backup_volumes()

    def test_a_cluster_without_longhorn_has_no_backups(self):
        self.cluster.kube.absent_groups.add("longhorn.io")
        self.assertEqual([], self.BACKUPS.backup_volumes())

    def test_metrics_say_the_backups_source_is_down_rather_than_leave_out_the_last_backup(self):
        METRICS = sys.modules["homestead_metrics"]
        self.assertTrue(METRICS.READERS["backups"]())
        self.break_read(BACKUP_VOLUMES)
        with self.assertRaises(RuntimeError):
            METRICS.READERS["backups"]()


class ProtectionHealthTests(ProtectionFixture):
    """The Dashboard's Backup and Trim health issues, and the alerts the
    "health" source raises from them."""
    BACKUP_KEY, TRIM_KEY = "health:Backup:nightly", "health:Trim:data"

    def setUp(self):
        super().setUp()
        # nightly's last run failed: a Backup issue.
        self.cluster.kube.add({"apiVersion": "batch/v1", "kind": "CronJob",
                               "metadata": {"namespace": "longhorn-system", "name": "nightly"},
                               "spec": {"schedule": "0 2 * * *"},
                               "status": {"lastScheduleTime": "2026-10-30T02:00:00Z",
                                          "lastSuccessfulTime": "2026-10-29T02:00:00Z"}})
        # A backup job needs a backup target that answers, or that is an issue of its own.
        self.enterContext(mock.patch.object(self.LH, "backup_target", lambda: {"configured": True, "available": True}))
        # The weekly trim failed on one volume: a Trim issue.
        self.TRIM = self.OV.WEEKLY_TRIM
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(self.TRIM, "DATA_DIR", folder))
        self.trim_file = Path(self.TRIM._path())
        self.trim_file.write_text('{"enabled": true, "last": {"failed": [{"pvc": "data", "message": "busy"}]}}')

    def overview(self):
        for store in (CACHE._cache, CACHE._running):
            store.clear()
        return self.OV.get_overview()

    def kinds(self, overview):
        return sorted({(i["kind"], i["name"]) for i in overview["current_issues"] if i["kind"] in ("Backup", "Trim")})

    def open_both(self):
        overview = self.overview()
        self.assertEqual([("Backup", "nightly"), ("Trim", "data")], self.kinds(overview))
        self.assertEqual({}, overview["protection_unread"])
        facts = ALERTS.health_facts(overview)
        ALERTS.observe({"health": facts}, now=1000)
        ALERTS.observe({"health": facts}, now=1000 + ALERTS.HOLD)
        active = ALERTS._load()["active"]
        self.assertIn(self.BACKUP_KEY, active)
        self.assertIn(self.TRIM_KEY, active)

    def after_four_holds(self, overview):
        facts = ALERTS.health_facts(overview)
        fresh = []
        for step in range(1, 5):
            fresh += ALERTS.observe({"health": facts}, now=1000 + ALERTS.HOLD * (step + 1))
        return {e["key"] for e in fresh if e["phase"] == "resolved"}, ALERTS._load()["active"]

    def assert_backup_held(self, broken, says):
        self.open_both()
        self.break_read(broken)
        overview = self.overview()
        self.assertRegex(overview["protection_unread"].get("Backup", ""), says)
        self.assertIn({"unknown_prefix": "health:Backup:"}, ALERTS.health_facts(overview))
        resolved, active = self.after_four_holds(overview)
        self.assertNotIn(self.BACKUP_KEY, resolved)
        self.assertIn(self.BACKUP_KEY, active)
        # The trim was read, and its issue is still there.
        self.assertIn(("Trim", "data"), self.kinds(overview))
        self.assertIn(self.TRIM_KEY, active)

    def test_backup_alerts_stay_open_while_the_recurring_jobs_cannot_be_read(self):
        self.assert_backup_held(RECURRING, "Backup protection could not be checked")

    def test_backup_alerts_stay_open_while_the_volumes_they_cover_cannot_be_read(self):
        # The job would otherwise cover 0 volumes, and protect nothing worth an issue.
        self.assert_backup_held(LH_VOLUMES, "volumes could not be read")

    def test_backup_alerts_stay_open_while_the_job_s_cronjob_cannot_be_read(self):
        # Its last run would otherwise read as not failed.
        self.assert_backup_held(CRONJOBS, "runs could not be read")

    def test_trim_alerts_stay_open_while_the_trim_state_cannot_be_read(self):
        self.open_both()
        self.trim_file.write_text("{not json")
        overview = self.overview()
        self.assertRegex(overview["protection_unread"].get("Trim", ""), "weekly-trim.json")
        self.assertNotIn("Backup", overview["protection_unread"])
        self.assertIn({"unknown_prefix": "health:Trim:"}, ALERTS.health_facts(overview))
        resolved, active = self.after_four_holds(overview)
        self.assertNotIn(self.TRIM_KEY, resolved)
        self.assertIn(self.TRIM_KEY, active)
        self.assertIn(self.BACKUP_KEY, active)

    def test_a_backup_or_trim_alert_held_for_over_an_hour_says_it_is_last_known(self):
        self.open_both()
        self.break_read(RECURRING)
        self.trim_file.write_text("{not json")
        facts = ALERTS.health_facts(self.overview())
        start = 1000 + 2 * ALERTS.HOLD
        ALERTS.observe({"health": facts}, now=start)
        ALERTS.observe({"health": facts}, now=start + ALERTS.STALE)
        rows = {row["key"]: row for row in ALERTS.active(now=start + ALERTS.STALE)}
        for key in (self.BACKUP_KEY, self.TRIM_KEY):
            self.assertTrue(rows[key]["stale"], key)
            self.assertTrue(rows[key]["body"].startswith("Last known, not current"), key)
        self.assertFalse(ALERTS.active(now=start + ALERTS.STALE - 1) and
                         any(r["stale"] for r in ALERTS.active(now=start + ALERTS.STALE - 1)
                             if r["key"] in (self.BACKUP_KEY, self.TRIM_KEY)))

    def test_list_jobs_is_lenient_unless_asked_strictly(self):
        # #944's convention: only the callers that must know read strictly.
        JOBS = sys.modules["homestead_longhorn_jobs"]
        for broken, says in ((RECURRING, "500"),
                             (LH_VOLUMES, "volumes could not be read"), (CRONJOBS, "runs could not be read")):
            with self.subTest(broken=broken):
                self.cluster.kube._failures.clear()
                self.break_read(broken)
                self.assertIsInstance(JOBS.list_jobs(), list)
                with self.assertRaisesRegex(Exception, says):
                    JOBS.list_jobs(strict=True)

    def test_last_state_is_lenient_unless_asked_strictly(self):
        self.trim_file.write_text("{not json")
        self.assertEqual({}, self.TRIM.last_state())
        with self.assertRaises(self.TRIM.SHARED.Unreadable):
            self.TRIM.last_state(strict=True)

    def test_the_dashboard_and_every_page_s_pill_say_unknown_with_both_reasons(self):
        # Nothing known is wrong (a fixed trim, no workloads, a hold that saw
        # no earlier test), but neither the jobs nor the trim could be read:
        # the verdict is unknown, not healthy, and says why (#1002's rule).
        self.trim_file.write_text('{"enabled": true, "last": {"failed": []}}')
        self.enterContext(mock.patch.object(self.OV.WORKLOADS, "get_workloads", lambda **_: []))
        self.enterContext(mock.patch.object(self.OV.CLUSTER, "HOLD", CLUSTER.HealthHold()))
        self.assertEqual("healthy", self.overview()["health_state"])
        self.break_read(RECURRING)
        self.trim_file.write_text("{not json")
        overview = self.overview()
        self.assertEqual("unknown", overview["health_state"])
        self.assertRegex(overview["health_summary"], r"^Backup protection could not be checked")
        self.assertIn("The weekly trim could not be checked", overview["health_summary"])
        with mock.patch.object(CLUSTER.homestead_routes, "page_kept", lambda key: (0.0, overview)):
            self.assertEqual("unknown", CLUSTER.health_brief()["state"])

    def test_with_everything_read_a_fixed_backup_and_trim_still_clear(self):
        self.open_both()
        cron = self.cluster.kube.object(f"{CRONJOBS}/nightly")
        cron["status"]["lastSuccessfulTime"] = "2026-10-30T02:05:00Z"
        self.cluster.kube.add(cron)
        self.trim_file.write_text('{"enabled": true, "last": {"failed": []}}')
        overview = self.overview()
        self.assertEqual([], self.kinds(overview))
        self.assertNotIn({"unknown_prefix": "health:Backup:"}, ALERTS.health_facts(overview))
        resolved, _ = self.after_four_holds(overview)
        self.assertEqual({self.BACKUP_KEY, self.TRIM_KEY}, resolved & {self.BACKUP_KEY, self.TRIM_KEY})

    def test_a_cluster_without_longhorn_has_no_backup_issue_and_nothing_unread(self):
        self.cluster.kube.absent_groups.add("longhorn.io")
        overview = self.overview()
        self.assertNotIn("Backup", overview["protection_unread"])
        self.assertNotIn(("Backup", "nightly"), self.kinds(overview))


class HealthCardTests(LonghornFixture):
    """Settings > Homestead's health: the backup storage, MQTT and node probe rows."""
    PROBE = "/apis/apps/v1/namespaces/lab/daemonsets/homestead-nodeprobe"

    def setUp(self):
        super().setUp()
        self.SELF_HEALTH = self.cluster.server.SELF_HEALTH
        S = self.SELF_HEALTH
        # The leader's shared beats live under homestead_shared.DIR (/data).
        self.enterContext(mock.patch.object(SHARED_MODULE, "DIR", self.enterContext(tempfile.TemporaryDirectory())))
        for name, value in (("kget", K.kget), ("replicas_now", lambda: {"desired": 1, "pods": []}),
                            ("node_temps", lambda: {}), ("samba_state", lambda: {"installed": False}),
                            ("heartbeats", lambda: {}), ("data_copies", lambda: {})):
            self.enterContext(mock.patch.object(S, name, value))
        self.enterContext(mock.patch.object(S.LEADER, "is_leader", lambda: True))
        self.enterContext(mock.patch.object(S.NETWORK, "inventory", lambda: {}))
        self.enterContext(mock.patch.object(S.NAMES, "NODEPROBE", "homestead-nodeprobe"))
        self.enterContext(mock.patch.object(S, "NAMESPACE", "lab"))

    def report(self):
        return self.SELF_HEALTH.report()

    def test_backup_storage_unread_says_so_rather_than_none(self):
        with mock.patch.object(self.SELF_HEALTH.OBJECTS, "status", side_effect=RuntimeError("timed out")):
            backups = self.report()["backups"]
        self.assertIn("timed out", backups["error"])
        self.assertIsNone(backups["deployed"])

    def test_backup_storage_read_is_shown_as_read(self):
        with mock.patch.object(self.SELF_HEALTH.OBJECTS, "status",
                               lambda: {"deployed": True, "ready": True, "endpoint": "http://192.0.2.5:9000"}):
            backups = self.report()["backups"]
        self.assertEqual((True, True, None), (backups["deployed"], backups["ready"], backups.get("error")))

    def test_mqtt_unread_is_shown_as_unknown_not_left_out(self):
        with mock.patch.object(self.SELF_HEALTH.OBJECTS, "status", lambda: {}), \
                mock.patch.object(self.SELF_HEALTH.MQTT, "shown_status", side_effect=RuntimeError("lock")):
            mqtt = self.report()["mqtt"]
        self.assertEqual("unknown", mqtt["state"])
        self.assertIn("lock", mqtt["error"])

    def test_the_node_probe_unread_is_unknown_not_not_installed(self):
        self.cluster.kube.fail(self.PROBE, 500, times=999)
        with mock.patch.object(self.SELF_HEALTH.OBJECTS, "status", lambda: {}):
            probe = self.report()["probe"]
        self.assertIsNone(probe["installed"])
        self.assertIn("500", probe["error"])

    def test_a_node_probe_that_is_not_there_is_not_installed(self):
        with mock.patch.object(self.SELF_HEALTH.OBJECTS, "status", lambda: {}):
            probe = self.report()["probe"]
        self.assertIs(False, probe["installed"])


if __name__ == "__main__":
    unittest.main()
