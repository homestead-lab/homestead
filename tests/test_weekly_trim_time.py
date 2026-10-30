"""Homestead's weekly trim at a time of the cluster's choosing, and in This
week (#927): the weekday and hour (UTC) are a setting, Saturday 04:00 by
default, and the trim joins the schedule clashes as a job of its own."""
import calendar
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as LH
import homestead_longhorn_jobs as LH_JOBS
import homestead_longhorn_target as BACKUP_TARGET
import homestead_longhorn_volumes as VOLUMES
import homestead_operations as OPS
import homestead_schedule_clashes as CLASHES
import homestead_weekly_trim as WT

SAT = calendar.timegm((2026, 10, 24, 4, 0, 0))          # Saturday 04:00 UTC
WED = calendar.timegm((2026, 10, 21, 22, 0, 0))         # the Wednesday before, 22:00 UTC
PVS = "/api/v1/persistentvolumes"


class StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        WT.bind(self.dir.name)

    def state(self):
        with open(os.path.join(self.dir.name, WT.STATE), encoding="utf-8") as handle:
            return json.load(handle)

    def save(self, state):
        with open(os.path.join(self.dir.name, WT.STATE), "w", encoding="utf-8") as handle:
            json.dump(state, handle)


class SlotTests(StateCase):
    def test_the_slot_follows_the_chosen_weekday_and_hour(self):
        self.assertEqual(WED, WT.last_slot(WED, 2, 22))
        self.assertEqual(WED, WT.last_slot(SAT, 2, 22))
        self.assertEqual(WED - 7 * 86400, WT.last_slot(WED - 1, 2, 22))
        self.assertEqual(SAT, WT.last_slot(SAT), "Saturday 04:00 stays the default")
        self.assertEqual((5, 4), WT.slot_of({}))
        self.assertEqual((2, 22), WT.slot_of({"weekday": 2, "hour": 22}))
        self.assertEqual((5, 4), WT.slot_of({"weekday": 9, "hour": "x"}), "a saved value out of range is the default")

    def test_due_and_start_use_the_saved_slot(self):
        moved = {"weekday": 2, "hour": 22}
        self.assertTrue(WT.due(moved, WED + 300))
        self.assertFalse(WT.due({**moved, "last": {"slot": WED}}, SAT + 300), "Saturday is not its slot any more")
        self.save(moved)
        with mock.patch.object(OPS, "start", return_value={"id": "j1"}) as start, \
                mock.patch.object(WT.time, "time", return_value=WED + 300):
            WT.start()
        self.assertEqual(WED, start.call_args[0][4]["slot"])
        self.assertEqual(WED, self.state()["tried"])

    def test_the_cron_says_the_slot_in_cron_words(self):
        self.assertEqual("0 4 * * 6", WT.cron({}))
        self.assertEqual("0 22 * * 3", WT.cron({"weekday": 2, "hour": 22}))
        self.assertEqual("0 0 * * 0", WT.cron({"weekday": 6, "hour": 0}))


class SettingsTests(StateCase):
    def test_moving_the_trim_saves_the_slot_and_does_not_run_a_passed_one_at_once(self):
        self.save({"retired": True, "enabled": True, "last": {"slot": SAT - 7 * 86400}})
        with mock.patch.object(WT.time, "time", return_value=SAT + 86400):     # Sunday
            reply = WT.settings({"weekday": 2, "hour": 22})
            self.assertFalse(WT.due(self.state(), SAT + 86400), "Wednesday has passed this week: next week's")
        self.assertEqual({"retired": True, "enabled": True, "last": {"slot": SAT - 7 * 86400},
                          "weekday": 2, "hour": 22, "tried": WED}, self.state())
        self.assertEqual((True, 2, 22), (reply["ok"], reply["weekday"], reply["hour"]))
        self.assertIn("Wednesday at 22:00 UTC", reply["detail"])
        self.assertTrue(WT.due(self.state(), WED + 7 * 86400 + 60))

    def test_a_slot_later_this_week_still_runs_this_week(self):
        self.save({"enabled": True, "last": {"slot": SAT - 7 * 86400}})
        with mock.patch.object(WT.time, "time", return_value=WED - 86400):   # Tuesday
            WT.settings({"weekday": 2, "hour": 22})
        self.assertTrue(WT.due(self.state(), WED + 60))

    def test_only_a_weekday_0_to_6_and_an_hour_0_to_23(self):
        for bad in ({"weekday": 7, "hour": 4}, {"weekday": -1, "hour": 4}, {"weekday": 2, "hour": 24},
                    {"weekday": "2", "hour": 4}, {"weekday": True, "hour": 4}, {"weekday": 2}, {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                WT.settings(bad)

    def test_switching_on_and_off_still_works_and_says_the_slot(self):
        self.save({"weekday": 0, "hour": 1})
        self.assertFalse(WT.settings({"enabled": False})["enabled"])
        self.assertIn("Monday at 01:00 UTC", WT.settings({"enabled": True})["detail"])
        self.assertEqual({"weekday": 0, "hour": 1, "enabled": True}, self.state())

    def test_the_route_takes_the_whole_body(self):
        role, handler = WT.ROUTES[("POST", "/api/weekly-trim/settings")]
        self.assertEqual("admin", role)
        with mock.patch.object(WT, "settings", return_value={"ok": True}) as settings:
            handler(type("Request", (), {"body": {"weekday": 1, "hour": 2}})())
        settings.assert_called_once_with({"weekday": 1, "hour": 2})


class ViewAndHealthTests(StateCase):
    def test_the_view_names_the_slot_and_the_next_run_on_it(self):
        self.save({"enabled": True, "weekday": 2, "hour": 22})
        LH.bind(lambda path, **_: {"items": []}, None, {}, "longhorn")
        with mock.patch.object(OPS, "snapshot", return_value=[]), \
                mock.patch.object(WT.time, "time", return_value=SAT):
            view = WT.view()
        self.assertEqual((2, 22, "2026-10-28T22:00:00Z"), (view["weekday"], view["hour"], view["next"]))

    def test_a_failed_trim_says_when_it_is_tried_again(self):
        issues = WT.issues({"weekday": 2, "hour": 22, "last": {"failed": [{"volume": "pvc-a", "message": "x"}]}})
        self.assertIn("next Wednesday", issues[0]["reason"])


def row(name, state="attached", pvc=None):
    return {"name": name, "pvc": pvc or f"data-{name}", "state": state, "groups": ["default"], "labels": {}}


class ScheduleTests(StateCase):
    def schedule(self, volumes, jobs=(), pvs=()):
        items = [{"metadata": {"name": n}, "spec": {"volumeMode": mode}} for n, mode in pvs]
        LH.bind(lambda path, **_: {"items": items} if path == PVS else {"items": []}, None, {}, "longhorn")
        return WT.schedule(volumes, list(jobs))

    def test_the_trim_is_a_job_on_its_slot_over_the_attached_filesystem_volumes(self):
        self.save({"enabled": True, "weekday": 2, "hour": 22})
        job = self.schedule([row("pvc-a"), row("pvc-b", "detached"), row("pvc-vm")], pvs=[("pvc-vm", "Block")])
        self.assertEqual({"name": WT.SCHEDULE_NAME, "task": "filesystem-trim", "cron": "0 22 * * 3",
                          "volumes": ["pvc-a"], "homestead": True}, job)

    def test_none_while_it_is_off_or_a_trim_job_of_theirs_keeps_it_off(self):
        self.save({"enabled": False})
        self.assertIsNone(self.schedule([row("pvc-a")]))
        self.save({})
        theirs = {"name": "weekly-trim", "task": "filesystem-trim", "groups": ["default"]}
        self.assertIsNone(self.schedule([row("pvc-a")], [theirs]))
        self.assertIsNotNone(self.schedule([row("pvc-a")], [{**theirs, "groups": ["media"]}]))

    def test_unreadable_pvs_count_every_attached_volume(self):
        self.save({})
        def kget(path, **_):
            raise OSError("down")
        LH.bind(kget, None, {}, "longhorn")
        self.assertEqual(["pvc-a"], WT.schedule([row("pvc-a")], [])["volumes"])

    def test_the_overview_lists_it_and_finds_its_clash_with_a_backup(self):
        self.save({"weekday": 5, "hour": 4})
        backup = {"name": "nightly", "task": "backup", "cron": "0 4 * * *", "volumes": ["pvc-a"], "groups": ["default"]}
        LH.bind(lambda path, **_: {"items": []}, None, {}, "longhorn")
        with mock.patch.object(LH_JOBS, "list_jobs", return_value=[backup]), \
                mock.patch.object(VOLUMES, "_volumes", return_value=[row("pvc-a")]), \
                mock.patch.object(BACKUP_TARGET, "backup_target", return_value={"configured": True}), \
                mock.patch.object(CLASHES.time, "time", return_value=SAT - 3600):
            overview = LH.overview()
        self.assertEqual([WT.SCHEDULE_NAME], [j["name"] for j in overview["homestead_jobs"]])
        self.assertEqual(["nightly"], [j["name"] for j in overview["jobs"]], "not one of Longhorn's jobs")
        self.assertEqual([("clash", {"nightly", WT.SCHEDULE_NAME})],
                         [(c["severity"], {j["name"] for j in c["jobs"]}) for c in overview["clashes"]])


if __name__ == "__main__":
    unittest.main()
