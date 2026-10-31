"""Moving containers back after a host returns: noticing it, saying so, and
in the maintenance window doing it - but not under an app's backup."""
import calendar
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_recovery as recovery
import server

WINDOW = {"days": [0, 1, 2, 3, 4, 5, 6], "start": "02:00", "duration_minutes": 120}
# Wednesday 2026-10-07, at the given UTC time.
at = lambda h, m=0: calendar.timegm((2026, 10, 7, h, m, 0))


def node(name, ready=True, cordoned=False):
    return {"metadata": {"name": name}, "spec": {"unschedulable": cordoned},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def app(name, claim):
    return {"metadata": {"name": name, "namespace": "lab"},
            "spec": {"template": {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}}}


def cronjob(name, schedule, claim, active=False, suspend=False, tz=""):
    spec = {"schedule": schedule, "suspend": suspend,
            "jobTemplate": {"spec": {"template": {"spec": {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": claim}}]}}}}}
    if tz:
        spec["timeZone"] = tz
    return {"metadata": {"name": name, "namespace": "lab"}, "spec": spec, "status": {"active": [{}] if active else []}}


class TempData(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        recovery.bind(folder.name)
        self.addCleanup(recovery.bind, "/data")


class WindowTests(unittest.TestCase):
    def test_the_window_is_open_inside_it_and_says_when_it_closes(self):
        self.assertEqual((True, at(4)), recovery.window(WINDOW, at(2, 30)))
        self.assertEqual((False, 0), recovery.window(WINDOW, at(4)))

    def test_a_window_across_midnight_closes_on_the_next_day(self):
        late = {**WINDOW, "start": "23:00"}
        self.assertEqual((True, at(1)), recovery.window(late, at(0, 30)))

    def test_each_opening_is_its_own(self):
        self.assertEqual(recovery.window_key(WINDOW, at(2)), recovery.window_key(WINDOW, at(3, 59)))
        self.assertNotEqual(recovery.window_key(WINDOW, at(2)), recovery.window_key(WINDOW, at(2) + 86400))
        self.assertEqual("", recovery.window_key(WINDOW, at(12)))


class CronTests(unittest.TestCase):
    def test_a_nightly_job_fires_inside_the_window_and_not_outside(self):
        self.assertTrue(recovery.fires_between("15 2 * * *", at(2), at(4), "UTC"))
        self.assertFalse(recovery.fires_between("15 5 * * *", at(2), at(4), "UTC"))
        self.assertTrue(recovery.fires_between("@daily", at(23) - 86400, at(1), "UTC"))

    def test_days_of_the_week_by_number_or_name_and_steps(self):
        self.assertFalse(recovery.fires_between("30 2 * * 1", at(2), at(4), "UTC"))      # Mondays; this is a Wednesday
        self.assertTrue(recovery.fires_between("30 2 * * 3", at(2), at(4), "UTC"))
        self.assertTrue(recovery.fires_between("30 2 * * WED", at(2), at(4), "UTC"))
        self.assertFalse(recovery.fires_between("30 2 * OCT mon", at(2), at(4), "UTC"))
        self.assertTrue(recovery.fires_between("*/20 * * * *", at(2, 1), at(2, 21), "UTC"))

    def test_a_last_run_from_before_the_clocks_changed_still_guards_the_job(self):
        # 03:00 on a London host: 02:00 UTC in summer, 03:00 UTC after the
        # clocks go back on 25 October.
        start, end = calendar.timegm((2026, 10, 25, 2, 30, 0)), calendar.timegm((2026, 10, 25, 3, 30, 0))
        self.assertTrue(recovery.fires_between("0 3 * * *", start, end, last="2026-10-24T02:00:00Z"))

    def test_a_job_without_a_time_zone_is_read_on_the_clock_its_last_run_shows(self):
        # 04:30 on a host in Berlin (CEST) is 02:30 UTC - inside the window.
        self.assertTrue(recovery.fires_between("30 4 * * *", at(2), at(4), last="2026-10-06T02:30:00Z"))
        # The same job on a host on UTC last ran at 04:30 UTC - after it,
        # even allowing for a change of the clocks since.
        self.assertFalse(recovery.fires_between("30 4 * * *", at(0), at(3), last="2026-10-06T04:30:00Z"))
        # Never run yet: any offset may be the controller's, so it may run.
        self.assertTrue(recovery.fires_between("30 4 * * *", at(2), at(4)))
        # A last run the schedule cannot have made: the clock is unknown.
        self.assertIsNone(recovery.fires_between("30 4 * * *", at(2), at(4), last="2026-10-06T04:31:00Z"))

    def test_a_job_in_its_own_time_zone(self):
        # 03:15 in London is 02:15 UTC in October (BST).
        self.assertTrue(recovery.fires_between("15 3 * * *", at(2), at(2, 30), "Europe/London"))
        self.assertFalse(recovery.fires_between("15 3 * * *", at(2, 30), at(4), "Europe/London"))

    def test_an_unreadable_schedule_is_not_known(self):
        self.assertIsNone(recovery.fires_between("every night", at(2), at(4)))
        self.assertIsNone(recovery.fires_between("0 2 * * *", at(2), at(4), "Mars/Olympus"))


class GuardTests(unittest.TestCase):
    def test_an_app_whose_backup_runs_in_the_window_stays(self):
        kept = recovery.guarded([cronjob("backup-web", "15 2 * * *", "web-data")],
                                [app("web", "web-data"), app("db", "db-data")], at(2), at(4))
        self.assertEqual({"lab/web"}, set(kept))
        self.assertIn("backup-web", kept["lab/web"])

    def test_a_running_or_unreadable_job_keeps_its_app_and_a_paused_one_does_not(self):
        deps = [app("web", "web-data")]
        self.assertIn("lab/web", recovery.guarded([cronjob("b", "0 12 * * *", "web-data", active=True)], deps, at(2), at(4)))
        self.assertIn("lab/web", recovery.guarded([cronjob("b", "nonsense", "web-data")], deps, at(2), at(4)))
        self.assertFalse(recovery.guarded([cronjob("b", "15 2 * * *", "web-data", suspend=True)], deps, at(2), at(4)))


class HostTests(TempData):
    def test_a_minute_with_nothing_new_writes_nothing(self):
        recovery.observe([node("a")], at(0))
        with mock.patch.object(recovery, "_save") as save:
            recovery.observe([node("a")], at(0, 1))
        save.assert_not_called()

    def test_a_standby_host_coming_back_is_no_reason_to_move_anything(self):
        recovery.observe([node("a"), node("spare")], at(0))
        recovery.observe([node("a"), node("spare", ready=False)], at(1))
        self.assertEqual({}, recovery.observe([node("a"), node("spare")], at(2), standby=["spare"]))

    def test_a_host_seen_for_the_first_time_has_not_come_back(self):
        self.assertEqual({}, recovery.observe([node("a"), node("b")], at(1)))

    def test_a_host_ready_again_or_uncordoned_has_come_back(self):
        recovery.observe([node("a"), node("b"), node("c")], at(0))
        recovery.observe([node("a"), node("b", ready=False), node("c", cordoned=True)], at(1))
        self.assertEqual({"b", "c"}, set(recovery.observe([node("a"), node("b"), node("c")], at(1, 5))))

    def test_a_new_host_finishing_joining_has_not_come_back(self):
        recovery.observe([node("a"), node("new", ready=False)], at(1))
        self.assertEqual({}, recovery.observe([node("a"), node("new")], at(1, 5)))

    def test_a_host_down_again_or_a_week_later_is_forgotten(self):
        recovery.observe([node("b")], at(0))
        recovery.observe([node("b", ready=False)], at(1))
        recovery.observe([node("b")], at(1, 5))
        self.assertEqual({}, recovery.observe([node("b", ready=False)], at(1, 10)))
        recovery.observe([node("b")], at(1, 15))
        self.assertEqual({}, recovery.observe([node("b")], at(1, 15) + recovery.RETURN_KEEP))


class AlertTests(TempData):
    def back(self, moves):
        recovery.observe([node("b")], at(0))
        recovery.observe([node("b", ready=False)], at(1))
        recovery.observe([node("b")], at(1, 5))
        recovery.suggest([{}] * moves, at(1, 5))

    def test_a_host_back_with_containers_to_move_raises_an_info_alert(self):
        self.back(3)
        fact = recovery.alert_facts("suggest")[0]
        self.assertEqual(("joins", "info", "/containers?panel=rebalance"), (fact["category"], fact["severity"], fact["href"]))
        self.assertIn("b is back: 3 containers", fact["title"])
        self.assertIn("Nothing moves until you review it", fact["body"])

    def test_in_window_mode_it_says_when_they_move_or_that_they_will_not(self):
        self.back(1)
        self.assertIn("next update window (02:00 UTC)", recovery.alert_facts("window", WINDOW)[0]["body"])
        self.assertIn("no days set", recovery.alert_facts("window", {**WINDOW, "days": []})[0]["body"])

    def test_nothing_to_move_forgets_the_host_and_off_says_nothing(self):
        self.back(2)
        self.assertEqual([], recovery.alert_facts("off"))
        recovery.suggest([], at(1, 10))
        self.assertEqual([], recovery.alert_facts("suggest"))
        self.assertEqual({}, recovery.observe_returned())


class TickTests(TempData):
    """One pass of the leader's loop, against a fake cluster."""
    def setUp(self):
        super().setUp()
        self.nodes = [node("a"), node("b")]
        self.settings = {"placement": {"rebalance": "window"}, "updates": {"maintenance": WINDOW}}
        self.plans, self.running = [], []
        api = {"/apis/batch/v1/cronjobs": {"items": [cronjob("backup-web", "15 2 * * *", "web-data")]},
               "/apis/apps/v1/deployments": {"items": [app("web", "web-data"), app("db", "db-data")]}}
        self.avoided = []

        def plan(exclude=(), avoid=()):
            self.plans.append(sorted(exclude))
            self.avoided.append(sorted(avoid))
            return {"moves": [{"id": "lab/db"}], "excluded": sorted(exclude)}
        self.start = mock.Mock(return_value={"id": "job"})
        jobs = mock.Mock(TERMINAL={"succeeded", "failed", "cancelled"}, snapshot=lambda: self.running)
        bound = (recovery.read, recovery.settings, recovery.operations, recovery.plan, recovery.start, recovery.WAITS_FOR)
        self.addCleanup(lambda: recovery.bind(recovery.DATA_DIR, *bound[:5], waits_for=bound[5]))
        recovery.bind(recovery.DATA_DIR, lambda p: {"items": self.nodes} if p == "/api/v1/nodes" else api[p],
                      lambda: self.settings, jobs, plan, self.start, ("container-rebalance", "os-rollout"))
        patcher = mock.patch.object(recovery, "_planned", [0.0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def away_and_back(self):
        recovery.tick(at(0))
        self.nodes = [node("a"), node("b", ready=False)]
        recovery.tick(at(1))
        self.nodes = [node("a"), node("b")]

    def test_an_app_flux_or_argo_applies_is_left_out_of_the_window_run(self):
        self.addCleanup(setattr, recovery, "managed_by", recovery.managed_by)
        recovery.managed_by = lambda dep: {"by": "Flux"} if dep["metadata"]["name"] == "db" else {}
        self.away_and_back()
        recovery.tick(at(2, 5))
        self.assertIn("lab/db", self.plans[-1])

    def test_a_backup_due_after_the_window_closes_but_while_the_run_lasts_is_guarded(self):
        api_cron = cronjob("backup-db", "5 4 * * *", "db-data", tz="UTC")    # just after the 04:00 close
        reads = recovery.read
        self.addCleanup(setattr, recovery, "read", reads)
        recovery.read = lambda p: {"items": [api_cron]} if p == "/apis/batch/v1/cronjobs" else reads(p)
        self.away_and_back()
        recovery.tick(at(3, 58))                                  # one move: it may run until 04:08
        self.assertIn("lab/db", self.plans[-1])

    def test_the_start_is_checked_under_the_job_lists_lock(self):
        self.away_and_back()
        recovery.tick(at(2, 5))
        self.assertEqual(recovery.WAITS_FOR, self.start.call_args.kwargs["unless"])

    def test_a_start_refused_because_other_work_began_is_tried_again(self):
        self.away_and_back()
        self.start.side_effect = [ValueError("Update web automatically is running"), {"id": "job"}]
        self.assertIsNone(recovery.tick(at(2, 5)))
        self.assertEqual({"id": "job"}, recovery.tick(at(2, 6)))

    def test_in_the_window_it_rebalances_once_leaving_apps_whose_backup_is_due(self):
        self.away_and_back()
        self.assertIsNone(recovery.tick(at(1, 30)))            # back, but outside the window
        self.assertEqual({"id": "job"}, recovery.tick(at(2, 5)))
        self.assertEqual(["lab/web"], self.plans[-1])
        self.start.assert_called_once()
        self.assertIn("after b came back", self.start.call_args[0][1])
        self.assertIsNone(recovery.tick(at(2, 30)))            # once per opening
        # Remembered, so what the backup held back moves at the next opening.
        self.assertEqual({"b"}, set(recovery.observe_returned()))
        self.assertEqual({"id": "job"}, recovery.tick(at(2, 5) + 86400))

    def test_a_failed_start_is_tried_again_in_the_same_window(self):
        self.away_and_back()
        self.start.side_effect = [RuntimeError("API down"), {"id": "job"}]
        with self.assertRaises(RuntimeError):
            recovery.tick(at(2, 5))
        self.assertEqual({"id": "job"}, recovery.tick(at(2, 6)))

    def test_suggest_mode_never_moves_anything_itself(self):
        self.settings["placement"]["rebalance"] = "suggest"
        self.away_and_back()
        recovery.tick(at(2, 5))
        self.start.assert_not_called()
        self.assertTrue(recovery.alert_facts("suggest"))

    def test_other_work_moving_apps_or_hosts_is_left_to_finish(self):
        self.away_and_back()
        for kind in ("container-rebalance", "os-rollout"):
            self.running = [{"kind": kind, "status": "running"}]
            self.assertIsNone(recovery.tick(at(2, 5)), kind)
        self.start.assert_not_called()

class BindingTests(unittest.TestCase):
    def test_homestead_waits_for_every_job_that_moves_or_restarts_apps_or_hosts(self):
        waits = set(server.RECOVERY.WAITS_FOR)
        self.assertLessEqual({server.CREBALANCE.KIND, server.RESERVE.KIND, server.AUTOUPDATE.KIND, "os-rollout",
                              "node-power", "k3s-cluster", "host-os", "deployment", "move", "reclass", "volume-restore",
                              "cluster-shutdown", "self-data-move", "vm-migration", "image-update"}, waits)

    def test_every_kind_waited_for_is_one_homestead_starts(self):
        source = "".join(path.read_text(encoding="utf-8") for path in (Path(__file__).resolve().parents[1] / "server").glob("*.py"))
        for kind in server.RECOVERY.MOVES_OR_RESTARTS:
            # Named once in the list itself, and at least once where its job is made.
            self.assertTrue(source.count(f'"{kind}"') >= 2 or kind.startswith("image-"), kind)


class AutoUpdateTests(unittest.TestCase):
    def test_no_automatic_update_starts_while_a_rebalance_runs(self):
        running = [{"kind": server.CREBALANCE.KIND, "status": "running"}]
        with mock.patch.object(server.OPS, "snapshot", return_value=running), \
                mock.patch.object(server.SETTINGS_HELPERS, "update_policy_status", return_value={"window_open": True, "policy": "maintenance_window"}), \
                mock.patch.object(server.CACHE, "cached", return_value=[]), \
                mock.patch.object(server.UPTIME, "report", return_value={}), \
                mock.patch.object(server.AUTOUPDATE, "tidy"), \
                mock.patch.object(server.AUTOUPDATE, "candidates", return_value=[{"ns": "lab", "name": "web"}]), \
                mock.patch.object(server.OPS, "start") as start:
            self.assertIsNone(server.CLUSTER_ROUTES.autoupdate_tick())
        start.assert_not_called()

    def test_an_update_waits_for_a_rebalance_but_not_for_a_finished_one(self):
        apps = [{"ns": "lab", "name": "web"}]
        with mock.patch.object(server.AUTOUPDATE, "candidates", return_value=apps):
            for status, expected in (("running", None), ("cancelling", None), ("succeeded", apps[0])):
                operations = [{"kind": server.CREBALANCE.KIND, "status": status}]
                self.assertEqual(expected, server.AUTOUPDATE.pick([], {}, {}, operations, True, "maintenance_window",
                                                                  (server.CREBALANCE.KIND,)), status)


if __name__ == "__main__":
    unittest.main()
