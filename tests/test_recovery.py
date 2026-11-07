"""Moving containers back after a host returns: noticing it, saying so, and
in the maintenance window doing it - but not under an app's backup."""
import calendar
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_audit as AUDIT
import homestead_data_copies as DATA_COPIES
import homestead_names as NAMES
import homestead_priority as PRIORITY
import homestead_recovery as recovery
import homestead_routes
import homestead_recovery_auto as AUTO
import homestead_recovery_marks as MARKS
from rebind import keep_bound, rebind
import server

WINDOW = {"days": [0, 1, 2, 3, 4, 5, 6], "start": "02:00", "duration_minutes": 120}
RESTARTED = NAMES.key("restartedAt")
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
        rebind(self, recovery, folder.name)   # every name its bind() sets comes back after the test


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


class TickCase(TempData):
    """One pass of the leader's loop, against a fake cluster."""
    def setUp(self):
        super().setUp()
        self.nodes = [node("a"), node("b")]
        self.settings = {"placement": {"rebalance": "window"}, "updates": {"maintenance": WINDOW}}
        self.plans, self.running = [], []
        api = {"/apis/batch/v1/cronjobs": {"items": [cronjob("backup-web", "15 2 * * *", "web-data")]},
               "/apis/apps/v1/deployments": {"items": [app("web", "web-data"), app("db", "db-data")]}}
        self.avoided = []

        self.restarts, self.backs = [], []

        def plan(exclude=(), avoid=(), restart=None, back=(), may_restart=False):
            self.plans.append(sorted(exclude))
            self.avoided.append(sorted(avoid))
            self.restarts.append(dict(restart or {}))
            self.backs.append(sorted(back))
            return {"moves": [{"id": "lab/db"}], "excluded": sorted(exclude)}
        self.start = mock.Mock(return_value={"id": "job"})
        jobs = mock.Mock(TERMINAL={"succeeded", "failed", "cancelled"}, snapshot=lambda: self.running)
        keep_bound(self, recovery)   # put back what recovery.bind() sets
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


class TickTests(TickCase):
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

class RestartMoveTests(TickCase):
    """Under Spread by capacity the window's run also moves apps Flux applies, by
    restarting them; and a tool that takes Homestead's restart mark off stops that."""
    TOOLS = {"db": {"by": "Flux", "source": "Kustomization flux-system/apps", "detected": True}}

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, recovery, "managed_by", recovery.managed_by)
        recovery.managed_by = lambda dep: self.TOOLS.get(dep["metadata"]["name"], {})
        self.settings["placement"]["mode"] = "spread"
        self.deployments = [app("web", "web-data"), app("db", "db-data")]
        real = recovery.read
        def read(path):
            if path == "/apis/apps/v1/deployments":
                return {"items": self.deployments}
            if "longhorn" in path:
                raise urllib.error.HTTPError(path, 404, "no Longhorn here", {}, None)
            return real(path)
        recovery.read = read
        self.addCleanup(setattr, recovery, "read", real)

    def stamped(self, name, value):
        dep = next(d for d in self.deployments if d["metadata"]["name"] == name)
        template = dep["spec"]["template"]
        template["metadata"] = {"annotations": {RESTARTED: value}} if value else {}

    def test_under_spread_flux_apps_move_by_restart_in_the_window(self):
        self.away_and_back()
        recovery.tick(at(2, 5))
        self.assertEqual({"lab/db": "Flux"}, self.restarts[-1])
        self.assertNotIn("lab/db", self.plans[-1])
        self.assertEqual(["b"], self.backs[-1], "a person's preference may be followed only to a host back")

    def test_under_default_and_hot_spare_flux_apps_stay_out(self):
        for mode in ("", "default", "standby"):
            self.settings["placement"]["mode"] = mode
            left_out, restart = recovery.split_gitops(self.deployments, mode, False)
            self.assertEqual(({"lab/db"}, {}), (left_out, restart), mode)
        self.settings["placement"]["mode"] = "default"
        self.away_and_back()
        recovery.tick(at(2, 5))
        self.assertIn("lab/db", self.plans[-1])
        self.assertEqual({}, self.restarts[-1])

    def test_argo_and_person_marked_apps_stay_out_under_spread(self):
        for tool in ({"by": "Argo CD", "source": "app", "detected": True}, {"by": "my script", "detected": False}, {"by": "Flux"}):
            with mock.patch.object(recovery, "managed_by", lambda dep: tool):
                left_out, restart = recovery.split_gitops([app("db", "d")], "spread", False)
            self.assertEqual(({"lab/db"}, {}), (left_out, restart), tool)

    def test_an_app_with_no_tool_is_neither(self):
        self.assertEqual((set(), {}), recovery.split_gitops([app("web", "d")], "spread", False))

    def test_a_removed_mark_stops_restart_moves_and_alerts(self):
        self.stamped("db", "2026-10-07T02:06:00Z")
        recovery.marked("lab/db", "2026-10-07T02:06:00Z", "Flux", at(2, 6))
        self.stamped("db", None)
        recovery.tick(at(2, 10))
        left_out, restart = recovery.split_gitops(self.deployments, "spread", recovery._load()["undone"])
        self.assertEqual(({"lab/db"}, {}), (left_out, restart))
        self.away_and_back()
        recovery.tick(at(2, 15))
        self.assertIn("lab/db", self.plans[-1])
        self.assertEqual({}, self.restarts[-1])
        fact = next(f for f in recovery.alert_facts("window", WINDOW) if f["key"] == "recovery:restart-undone")
        self.assertEqual("degraded", fact["severity"])
        self.assertIn("Flux removed Homestead's restart mark from lab/db", fact["title"] + fact["body"])

    def test_a_removed_mark_holds_in_every_mode(self):
        self.settings["placement"]["rebalance"] = "off"
        self.stamped("db", "2026-10-07T02:06:00Z")
        recovery.marked("lab/db", "2026-10-07T02:06:00Z", "Flux", at(2, 6))
        self.stamped("db", None)
        recovery.tick(at(2, 10))
        self.assertTrue(recovery._load()["undone"])
        self.assertTrue([f for f in recovery.alert_facts("off", WINDOW) if f["key"] == "recovery:restart-undone"])

    def test_a_newer_mark_is_a_person_not_a_fight(self):
        self.stamped("db", "2026-10-07T02:06:00Z")
        recovery.marked("lab/db", "2026-10-07T02:06:00Z", "Flux", at(2, 6))
        self.stamped("db", "2026-10-07T02:09:00Z")
        recovery.tick(at(2, 10))
        data = recovery._load()
        self.assertEqual({}, data["marks"])
        self.assertFalse(data["undone"])

    def test_a_mark_is_forgotten_after_the_watch(self):
        self.stamped("db", "2026-10-07T02:06:00Z")
        recovery.marked("lab/db", "2026-10-07T02:06:00Z", "Flux", at(2, 6))
        recovery.tick(at(3))
        self.assertIn("lab/db", recovery._load()["marks"], "still in its watch")
        recovery.tick(at(2, 6) + MARKS.MARK_WATCH + 1)
        self.assertEqual({}, recovery._load()["marks"])
        self.assertFalse(recovery._load()["undone"])

    def test_a_deployment_gone_is_forgotten_without_a_word(self):
        recovery.marked("lab/gone", "2026-10-07T02:06:00Z", "Flux", at(2, 6))
        recovery.tick(at(2, 10))
        self.assertEqual({}, recovery._load()["marks"])
        self.assertFalse(recovery._load()["undone"])

    def test_nothing_reads_deployments_for_marks_when_there_are_none(self):
        self.settings["placement"]["rebalance"] = "off"
        reads = []
        real = recovery.read
        recovery.read = lambda path: reads.append(path) or real(path)
        self.addCleanup(setattr, recovery, "read", real)
        recovery.tick(at(2, 10))
        self.assertNotIn("/apis/apps/v1/deployments", reads)

    def test_homestead_sends_no_kubectl_user_agent(self):
        # Flux's Kustomization removes fields a kubectl* field manager owns: that is
        # why Homestead's mark survives where `kubectl rollout restart`'s does not.
        offenders = [path.name for path in (Path(__file__).resolve().parents[1] / "server").glob("*.py")
                     if re.search(r"""(User-Agent|fieldManager)["'=:\s,]+[^\n]*kubectl""", path.read_text())]
        self.assertEqual([], offenders)


def tiered(name, claim, tier=""):
    dep = app(name, claim)
    if tier:
        dep["spec"]["template"]["spec"]["priorityClassName"] = tier
    return dep


class AutoCase(TickCase):
    """Mode auto: a host that went away at 11:00 and came back at 12:00, on a Wednesday
    whose window is 02:00-04:00 UTC; nothing else in the way."""
    LADDER = PRIORITY.ladder([{"metadata": {"name": "important"}, "value": 1000},
                              {"metadata": {"name": "everyday"}, "value": 0, "globalDefault": True}])

    def setUp(self):
        super().setUp()
        self.settings = {"placement": {"rebalance": "auto", "mode": "spread"}, "updates": {"maintenance": WINDOW}}
        self.deployments = [tiered("web", "web-data"), tiered("db", "db-data")]
        self.cronjobs, self.copies = [], ([], [], [])
        self.state = {"metrics": True, "metrics_state": {"state": "ok"}}
        self.exclude, self.would = [], ["lab/web", "lab/db"]
        self.job = {"id": "job-1", "status": "running", "message": "", "kind": "container-rebalance"}
        self.start.side_effect = self.started
        self.chosen = []

        self.kept, self.may = [], []

        def plan(exclude=(), avoid=(), restart=None, back=(), may_restart=False):
            self.exclude.append(sorted(exclude))
            self.may.append(may_restart)
            return {"moves": [{"id": a} for a in self.would if a not in exclude], "excluded": sorted(exclude), "kept": self.kept, **self.state}

        lists = {AUTO.VOLUMES: lambda: self.copies[0], AUTO.REPLICAS: lambda: self.copies[1], DATA_COPIES.ENGINES: lambda: self.copies[2],
                 "/apis/apps/v1/deployments": lambda: self.deployments, "/apis/batch/v1/cronjobs": lambda: self.cronjobs,
                 "/api/v1/nodes": lambda: self.nodes}
        recovery.read = lambda path: {"items": lists[path]()}
        recovery.plan = plan
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(PRIORITY, "ladder", lambda *a, **k: self.LADDER).start()

    def started(self, chosen, why="", unless=()):
        self.chosen.append(chosen)
        self.running.append(self.job)
        return self.job

    def back_at_noon(self):
        self.nodes = [node("a"), node("b")]
        recovery.tick(at(10))
        self.nodes = [node("a"), node("b", ready=False)]
        recovery.tick(at(11))
        self.nodes = [node("a"), node("b")]
        recovery.tick(at(12))
        self.start.reset_mock()
        recovery._planned[0] = 0.0

    def go(self, when=None):
        """A look with the plan's five-minute throttle out of the way."""
        recovery._planned[0] = 0.0
        return recovery.tick(when if when is not None else at(12, 16))

    def waiting(self):
        return " ".join(recovery._load()["auto"].get("waiting", {}).get("reasons", []))


class AutoTickTests(AutoCase):
    def test_auto_waits_fifteen_minutes(self):
        self.back_at_noon()
        self.go(at(12, 14))
        self.start.assert_not_called()
        self.assertIn("b steady in 1 min", self.waiting())
        self.go(at(12, 15))
        self.start.assert_called_once()

    def test_it_says_who_for(self):
        self.back_at_noon()
        self.go()
        self.assertEqual(" after b came back", self.start.call_args[0][1])
        self.assertTrue(self.chosen[0]["automatic"])
        self.assertIn("container-rebalance", self.start.call_args.kwargs["unless"])

    def test_a_flap_restarts_the_clock(self):
        self.back_at_noon()
        self.nodes = [node("a"), node("b", ready=False)]
        self.go(at(12, 10))
        self.nodes = [node("a"), node("b")]
        self.go(at(12, 11))
        self.go(at(12, 16))
        self.start.assert_not_called()
        self.go(at(12, 26))
        self.start.assert_called_once()

    def test_a_short_flap_between_looks_is_seen_from_ready_time(self):
        self.back_at_noon()
        ready = node("b")
        ready["status"]["conditions"][0]["lastTransitionTime"] = "2026-10-07T12:10:00Z"
        self.nodes = [node("a"), ready]
        self.go(at(12, 16))
        self.start.assert_not_called()
        self.go(at(12, 25))
        self.start.assert_called_once()

    def test_auto_waits_for_copies_on_that_host(self):
        self.back_at_noon()
        self.copies = ([], [{"metadata": {"name": "r1"}, "spec": {"volumeName": "v1", "nodeID": "b", "failedAt": ""}}],
                       [{"spec": {"volumeName": "v1"}, "status": {"replicaModeMap": {"r1": "WO"}}}])
        self.go()
        self.start.assert_not_called()
        self.assertIn("1 copy rebuilding on b", self.waiting())
        self.copies = ([], [], [])
        self.go(at(12, 17))
        self.start.assert_called_once()

    def test_longhorn_unreadable_holds_and_says_so_in_health(self):
        self.back_at_noon()
        def broken(path):
            raise urllib.error.HTTPError(path, 503, "no", {}, None)
        lists = recovery.read
        recovery.read = lambda path: broken(path) if "longhorn" in path else lists(path)
        with self.assertRaises(urllib.error.HTTPError):
            self.go()
        self.start.assert_not_called()

    def test_auto_holds_while_another_host_is_away(self):
        self.nodes = [node("a"), node("b"), node("c")]
        recovery.tick(at(10))
        self.nodes = [node("a"), node("b", ready=False), node("c", ready=False)]
        recovery.tick(at(11))
        self.nodes = [node("a"), node("b"), node("c", ready=False)]
        recovery.tick(at(12))
        self.go()
        self.start.assert_not_called()
        self.assertIn("c is away", self.waiting())

    def test_a_host_away_for_days_still_holds_it(self):
        # Andreas, 7 Nov: everything holds, even for a long-dead host, and says which.
        self.nodes = [node("a"), node("b"), node("c")]
        recovery.tick(at(10))
        self.nodes = [node("a"), node("b"), node("c", ready=False)]
        recovery.tick(at(11))
        self.nodes = [node("a"), node("b", ready=False), node("c", ready=False)]
        recovery.tick(at(11, 30))
        self.nodes = [node("a"), node("b"), node("c", ready=False)]
        recovery.tick(at(12))
        self.go(at(12) + 6 * 86400)
        self.start.assert_not_called()
        self.assertIn("c is away", self.waiting())

    def test_auto_holds_beside_each_job_kind(self):
        self.back_at_noon()
        for kind in recovery.WAITS_FOR:
            with self.subTest(kind):
                self.running[:] = [{"id": "other", "kind": kind, "status": "running"}]
                self.go()
                self.start.assert_not_called()
        self.running[:] = [{"id": "other", "kind": "os-rollout", "status": "succeeded"}]
        self.go()
        self.start.assert_called_once()

    def test_auto_holds_when_metrics_are_stale(self):
        self.back_at_noon()
        for state in ({"metrics": False, "metrics_state": {"state": "ok"}}, {"metrics": True, "metrics_state": {"state": "stale"}}):
            self.state = state
            self.go()
            self.start.assert_not_called()
            self.assertTrue(recovery._load()["returned"], "the host stays remembered")
        self.assertIn("figures", self.waiting())

    def test_critical_apps_wait_outside_the_window(self):
        self.deployments = [tiered("web", "web-data", "important"), tiered("db", "db-data", "everyday")]
        self.back_at_noon()
        self.go()
        self.assertEqual(["lab/db"], [m["id"] for m in self.chosen[0]["moves"]])
        self.assertEqual(1, recovery._load()["auto"]["waiting"]["later"])

    def test_an_unknown_tier_waits_too(self):
        self.deployments = [tiered("web", "web-data", "gone"), tiered("db", "db-data")]
        self.back_at_noon()
        self.go()
        self.assertEqual(["lab/db"], [m["id"] for m in self.chosen[0]["moves"]])

    def test_only_critical_apps_left_outside_the_window_keeps_the_host_remembered(self):
        self.deployments = [tiered("web", "web-data", "important"), tiered("db", "db-data", "important")]
        self.back_at_noon()
        self.go()
        self.start.assert_not_called()
        self.assertTrue(recovery._load()["returned"])
        self.assertEqual(2, recovery._load()["auto"]["waiting"]["later"])

    def test_critical_apps_move_once_in_the_opening(self):
        self.deployments = [tiered("web", "web-data", "important"), tiered("db", "db-data")]
        self.back_at_noon()
        self.go()
        recovery.moved_back("lab/db", at(12, 16))
        self.chosen.clear(); self.start.reset_mock()
        self.job["status"] = "succeeded"
        self.go(at(12, 17))               # sees it end
        self.go(at(2, 30) + 86400)        # the next day's window: db has moved, cooled off
        self.assertEqual(["lab/web"], [m["id"] for m in self.chosen[0]["moves"]])
        self.assertTrue(recovery.already_ran(recovery.window_key(WINDOW, at(2, 30) + 86400)))
        self.chosen.clear(); self.start.reset_mock()
        self.job["status"] = "succeeded"
        self.go(at(2, 31) + 86400)        # sees it end
        self.go(at(3, 30) + 86400)
        self.assertEqual([], [m for c in self.chosen for m in c["moves"] if m["id"] == "lab/web"], "once an opening")

    def test_a_window_with_no_days_never_moves_critical_apps(self):
        self.settings["updates"]["maintenance"] = {**WINDOW, "days": []}
        self.deployments = [tiered("web", "web-data", "important")]
        self.would = ["lab/web"]
        self.back_at_noon()
        self.go()
        self.start.assert_not_called()

    def test_a_backup_due_within_the_run_keeps_the_app(self):
        self.cronjobs = [cronjob("backup-web", "20 12 * * *", "web-data", tz="UTC")]
        self.back_at_noon()
        self.go()
        self.assertEqual(["lab/db"], [m["id"] for m in self.chosen[0]["moves"]])

    def test_an_app_moves_once_a_day(self):
        self.back_at_noon()
        recovery.moved_back("lab/web", at(12, 16))
        self.go(at(12, 40))
        self.assertEqual(["lab/db"], [m["id"] for m in self.chosen[0]["moves"]])
        self.chosen.clear(); self.job["status"] = "succeeded"; self.start.reset_mock()
        self.go(at(12, 41))               # sees it end
        self.go(at(12, 16) + recovery.APP_COOLDOWN + 1)
        self.assertEqual(["lab/db", "lab/web"], sorted(m["id"] for m in self.chosen[0]["moves"]))

    def test_the_cooldown_is_dropped_when_old(self):
        recovery.moved_back("lab/web", at(1))
        self.nodes = [node("a")]
        recovery.tick(at(1) + recovery.APP_COOLDOWN + 1)
        self.assertEqual({}, recovery._load()["auto"].get("moved", {}))

    def test_runs_are_fifteen_minutes_apart(self):
        self.back_at_noon()
        self.go()
        self.assertEqual(1, self.start.call_count)
        self.job["status"] = "succeeded"       # still in the job list
        recovery.moved_back("lab/web", at(12, 16)); recovery.moved_back("lab/db", at(12, 16))
        self.would.append("lab/other")
        self.go(at(12, 17))                 # sees it ended now
        self.go(at(12, 31))
        self.assertEqual(1, self.start.call_count)
        self.go(at(12, 33))
        self.assertEqual(2, self.start.call_count)

    def test_steady_time_survives_a_leader_change(self):
        # Clocks live in recovery.json: nothing runs early, and time isn't lost.
        self.back_at_noon()
        self.go(at(12, 10))
        fresh = [0.0]
        with mock.patch.object(recovery, "_planned", fresh):
            recovery.tick(at(12, 11))
            self.start.assert_not_called()
            fresh[0] = 0.0
            recovery.tick(at(12, 15))
            self.start.assert_called_once()

    def test_a_new_host_is_not_back(self):
        self.nodes = [node("a")]
        recovery.tick(at(10))
        self.nodes = [node("a"), node("b")]
        self.go(at(12))
        self.go(at(13))
        self.start.assert_not_called()

    def test_nothing_to_move_forgets_the_host_only_once_it_passed(self):
        self.back_at_noon()
        self.would = []
        self.go(at(12, 5))
        self.assertTrue(recovery._load()["returned"], "a just-returned host often has no figures yet")
        self.go(at(12, 16))
        self.assertEqual({}, recovery._load()["returned"])

    def test_a_flux_app_moves_by_restart_only_under_spread(self):
        recovery.managed_by = lambda dep: {"by": "Flux", "detected": True} if dep["metadata"]["name"] == "db" else {}
        self.addCleanup(setattr, recovery, "managed_by", lambda dep: {})
        self.settings["placement"]["mode"] = "default"
        self.back_at_noon()
        self.go()
        self.assertEqual(["lab/web"], [m["id"] for m in self.chosen[0]["moves"]])

    def test_auto_is_accepted_and_nonsense_refused_by_settings(self):
        self.assertEqual("auto", server.APP_SETTINGS.validate({"placement": {"rebalance": "auto"}})["placement"]["rebalance"])
        with self.assertRaises(ValueError):
            server.APP_SETTINGS.validate({"placement": {"rebalance": "whenever"}})


class AutoKeptTests(AutoCase):
    KEPT = [{"id": "lab/web", "why": "its host preference keeps it on a, so a restart would not move it"}]

    def test_nothing_to_move_settles_the_host_with_no_more_plans_or_runs(self):
        # Remembering it is for Settings' list; it is no leave to rebalance for a week.
        self.kept, self.would = self.KEPT, []
        self.back_at_noon()
        self.go()
        plans = len(self.exclude)
        self.would = ["lab/db"]           # day 5: the figures say something could move now
        self.go(at(12) + 5 * 86400)
        self.start.assert_not_called()
        self.assertEqual(plans, len(self.exclude), "no more plans")
        self.assertEqual(self.KEPT, recovery.status()["kept"], "Settings keeps the list for the week")

    def test_a_settled_host_leaves_no_alert(self):
        self.kept, self.would = self.KEPT, []
        self.back_at_noon()
        self.go()
        self.assertEqual([], recovery.alert_facts("auto", WINDOW))

    def test_a_hold_does_not_settle_it(self):
        self.kept, self.would = self.KEPT, []
        self.back_at_noon()
        self.go(at(12, 5))
        self.would = ["lab/db"]
        self.go()
        self.start.assert_called_once()

    def test_critical_apps_waiting_for_the_window_stay_active(self):
        self.deployments = [tiered("web", "web-data", "important"), tiered("db", "db-data", "important")]
        self.kept = self.KEPT
        self.back_at_noon()
        self.go()
        self.start.assert_not_called()
        self.go(at(2, 30) + 86400)
        self.start.assert_called_once()

    def test_the_host_returning_again_is_a_new_return(self):
        self.kept, self.would = self.KEPT, []
        self.back_at_noon()
        self.go()
        self.nodes = [node("a"), node("b", ready=False)]
        self.go(at(13))
        self.nodes = [node("a"), node("b")]
        self.go(at(14))
        self.would = ["lab/db"]
        self.go(at(14, 16))
        self.start.assert_called_once()

    def test_a_second_host_returning_runs_for_that_host_alone(self):
        self.kept, self.would = self.KEPT, []
        self.nodes = [node("a"), node("b"), node("c")]
        recovery.tick(at(10))
        self.nodes = [node("a"), node("b", ready=False), node("c")]
        recovery.tick(at(11))
        self.nodes = [node("a"), node("b"), node("c")]
        recovery.tick(at(11, 30))
        self.go(at(11, 50))                # b settled
        self.nodes = [node("a"), node("b"), node("c", ready=False)]
        recovery.tick(at(12))
        self.nodes = [node("a"), node("b"), node("c")]
        recovery.tick(at(12, 5))
        self.would = ["lab/db"]
        self.go(at(12, 25))
        self.assertEqual(" after c came back", self.start.call_args[0][1])

    def test_the_true_count_is_kept_when_the_list_is_cut(self):
        self.kept = [{"id": f"lab/app-{i}", "why": "no"} for i in range(recovery.KEPT_SHOWN + 10)]
        self.would = ["lab/db"]
        self.back_at_noon()
        self.go()
        status = recovery.status()
        self.assertEqual((recovery.KEPT_SHOWN, recovery.KEPT_SHOWN + 10), (len(status["kept"]), status["kept_total"]))

    def test_an_app_left_out_with_why_is_stored_for_settings(self):
        self.kept, self.would = self.KEPT, []
        self.back_at_noon()
        self.go()
        self.assertEqual(self.KEPT, recovery._load()["auto"]["waiting"]["kept"])

    def test_an_app_left_out_keeps_the_host_remembered_for_settings(self):
        # Nothing to move, but only because of them: not "nothing needs to move".
        self.kept, self.would = self.KEPT, []
        self.back_at_noon()
        self.go()
        self.assertEqual(["b"], list(recovery._load()["returned"]))

    def test_restarts_are_on_only_under_spread(self):
        self.back_at_noon()
        self.go()
        self.assertTrue(self.may[-1])
        self.settings["placement"]["mode"] = "default"
        self.job["status"] = "succeeded"
        self.go(at(12, 17))
        self.go(at(12, 33))
        self.assertFalse(self.may[-1])

    def test_each_of_the_moving_kinds_is_waited_for(self):
        for kind in ("restore-swap", "host-move", "set-restart", "volume-rebalance", "volume-copy-move"):
            self.assertIn(kind, recovery.MOVES_OR_RESTARTS)

    def test_reasons_use_a_jobs_title_not_its_kind(self):
        self.back_at_noon()
        self.running[:] = [{"id": "x", "kind": "os-rollout", "title": "Update 3 hosts", "status": "running"}]
        self.go()
        self.assertEqual("Update 3 hosts is running", self.waiting())
        self.assertEqual("Update 3 hosts started", recovery.may_go_on())


class WindowRestartGates(AutoCase):
    """The window's run keeps its behaviour, but its restarts get the copies gate."""
    def setUp(self):
        super().setUp()
        self.settings["placement"]["rebalance"] = "window"
        recovery.managed_by = lambda dep: {"by": "Flux", "detected": True} if dep["metadata"]["name"] == "db" else {}
        self.addCleanup(setattr, recovery, "managed_by", lambda dep: {})
        self.restarted = []
        real = recovery.plan
        recovery.plan = lambda *a, **k: (self.restarted.append((dict(k.get("restart") or {}), k.get("may_restart"))), real(*a, **k))[1]

    def rebuilding(self):
        self.copies = ([], [{"metadata": {"name": "r1"}, "spec": {"volumeName": "v1", "nodeID": "b", "failedAt": ""}}],
                       [{"spec": {"volumeName": "v1"}, "status": {"replicaModeMap": {"r1": "WO"}}}])

    def test_restarts_wait_while_a_copy_is_rebuilt_on_the_returned_host(self):
        self.back_at_noon()
        self.rebuilding()
        self.go(at(2, 30) + 86400)
        self.assertEqual(({}, False), self.restarted[-1])
        self.assertIn("lab/db", self.exclude[-1])
        self.start.assert_called_once()

    def test_restarts_go_once_the_copies_are_done(self):
        self.back_at_noon()
        self.go(at(2, 30) + 86400)
        self.assertEqual(({"lab/db": "Flux"}, True), self.restarted[-1])

    def test_a_held_window_says_so(self):
        self.back_at_noon()
        self.rebuilding()
        self.go(at(2, 30) + 86400)
        self.assertIn("rebuilding on b, so apps Flux applies wait for the next opening", " ".join(recovery.status()["waiting"]))

    def test_restarts_are_left_out_at_plan_time_while_a_host_is_away(self):
        # A restart in the job would stop it on the spot, placement moves and all.
        self.nodes = [node("a"), node("b"), node("c")]
        recovery.tick(at(10))
        self.nodes = [node("a"), node("b", ready=False), node("c", ready=False)]
        recovery.tick(at(11))
        self.nodes = [node("a"), node("b"), node("c", ready=False)]
        recovery.tick(at(12))
        self.go(at(2, 30) + 86400)
        self.assertEqual(({}, False), self.restarted[-1])
        self.assertIn("lab/db", self.exclude[-1])
        self.start.assert_called_once()
        self.assertIn("c is away", " ".join(recovery.status()["waiting"]))

    def test_the_window_stores_what_it_keeps_for_settings(self):
        self.kept = [{"id": "lab/web", "why": "its host preference keeps it on a"}]
        self.back_at_noon()
        self.go(at(2, 30) + 86400)
        status = recovery.status()
        self.assertEqual(self.kept, status["kept"])
        self.assertEqual(1, status["kept_total"])

    def test_the_window_alert_promises_only_what_it_can(self):
        self.back_at_noon()
        recovery.suggest(["lab/db"], at(12, 5))
        self.assertIn("Those it can move in the next update window", recovery.alert_facts("window", WINDOW)[0]["body"])

    def test_outside_spread_longhorn_is_not_read_for_it(self):
        self.settings["placement"]["mode"] = "default"
        self.back_at_noon()
        real = recovery.read
        recovery.read = lambda path: (_ for _ in ()).throw(TimeoutError("longhorn")) if "longhorn" in path else real(path)
        self.go(at(2, 30) + 86400)
        self.start.assert_called_once()


class AutoStaysStopped(AutoCase):
    def ran_and_ended(self, status, message="Stopped after moving 1 container"):
        self.back_at_noon()
        self.go()
        self.start.assert_called_once()
        self.job.update(status=status, message=message)
        self.running.clear()
        self.running.append(self.job)
        self.start.reset_mock()

    def test_after_a_cancel_no_new_run_starts(self):
        self.ran_and_ended("cancelled")
        for later in (at(12, 17), at(13), at(13) + 86400):
            self.go(later)
        self.start.assert_not_called()
        self.assertIn("Stopped after moving 1", recovery._load()["auto"]["paused"]["why"])

    def test_after_a_failed_run_no_new_run_starts(self):
        self.ran_and_ended("failed", "lab/web restarted on b, not a; the rest were not moved")
        self.go(at(13))
        self.start.assert_not_called()
        self.assertIn("restarted on b", recovery._load()["auto"]["paused"]["why"])

    def test_the_stop_outlasts_the_job_leaving_the_list(self):
        self.ran_and_ended("cancelled")
        self.go(at(12, 20))
        self.running.clear()
        self.go(at(13))
        self.start.assert_not_called()
        self.assertTrue(recovery._load()["auto"]["paused"])

    def test_a_new_return_does_not_lift_it(self):
        self.ran_and_ended("failed")
        self.go(at(12, 20))
        self.nodes = [node("a"), node("b", ready=False)]
        self.go(at(13))
        self.nodes = [node("a"), node("b")]
        self.go(at(14))
        self.go(at(14, 30))
        self.start.assert_not_called()

    def test_a_run_whose_ending_was_not_seen_stays_stopped(self):
        # Its job gone from the list, dismissed say: it may have failed or been stopped.
        self.back_at_noon()
        self.go()
        self.running.clear()
        self.start.reset_mock()
        self.go(at(12, 17))
        self.start.assert_not_called()
        self.assertIn("could not see", recovery._load()["auto"]["paused"]["why"])

    def test_a_finished_run_leaves_no_pause(self):
        self.ran_and_ended("succeeded", "Moved 2 containers")
        self.go(at(12, 17))
        self.assertEqual("", recovery._load()["auto"]["job"])
        self.assertFalse(recovery._load()["auto"].get("paused"))

    def test_choosing_no_clears_the_pause(self):
        self.ran_and_ended("cancelled")
        self.go(at(12, 20))
        self.assertTrue(recovery._load()["auto"]["paused"])
        self.settings["placement"]["rebalance"] = "off"
        self.go(at(12, 21))
        self.assertFalse(recovery._load()["auto"].get("paused"))
        self.assertFalse(recovery._load()["auto"].get("job"))

    def test_changing_the_choice_stops_the_run_before_the_next_move(self):
        self.back_at_noon()
        self.assertEqual("", recovery.may_go_on())
        for mode in ("off", "suggest"):
            self.settings["placement"]["rebalance"] = mode
            self.assertIn("choice", recovery.may_go_on(), mode)
        self.settings["placement"]["rebalance"] = "window"
        self.assertEqual("", recovery.may_go_on())

    def test_a_host_leaving_mid_run_stops_it(self):
        self.back_at_noon()
        self.nodes = [node("a"), node("b", ready=False)]
        self.assertIn("b", recovery.may_go_on())

    def test_another_job_starting_mid_run_stops_it(self):
        self.back_at_noon()
        self.running[:] = [{"id": "me", "kind": "container-rebalance", "status": "running"}]
        self.assertEqual("", recovery.may_go_on(), "its own kind")
        self.running.append({"id": "other", "kind": "os-rollout", "title": "Update 3 hosts", "status": "running"})
        self.assertIn("Update 3 hosts", recovery.may_go_on())

    def test_it_stops_if_it_cannot_tell(self):
        self.back_at_noon()
        def broken(path):
            raise TimeoutError("no")
        recovery.read = broken
        self.assertIn("could not read", recovery.may_go_on())


class AutoStatusTests(AutoCase):
    def test_status_says_what_it_waits_for(self):
        self.back_at_noon()
        self.go(at(12, 9))
        status = recovery.status()
        self.assertEqual("auto", status["mode"])
        self.assertEqual([{"name": "b", "steady_in": 360, "copies_left": 0}], status["hosts"])
        self.assertIn("b steady in 6 min", status["waiting"])
        self.assertIsNone(status["paused"])
        self.assertIsNone(status["undone"])

    def test_status_reads_nothing_from_the_cluster(self):
        self.back_at_noon()
        self.go(at(12, 9))
        recovery.read = mock.Mock(side_effect=AssertionError("no cluster reads"))
        self.assertEqual(1, len(recovery.status()["hosts"]))

    def test_status_counts_what_waits_and_what_is_left_out(self):
        self.deployments = [tiered("web", "web-data", "important"), tiered("db", "db-data")]
        recovery.managed_by = lambda dep: {"by": "Argo CD", "detected": True} if dep["metadata"]["name"] == "db" else {}
        self.addCleanup(setattr, recovery, "managed_by", lambda dep: {})
        self.back_at_noon()
        self.go()
        status = recovery.status()
        self.assertEqual((1, 1), (status["later"], status["left_out"]))

    def test_status_shows_a_stop_and_its_job(self):
        self.back_at_noon()
        self.go()
        self.assertEqual("job-1", recovery.status()["job"])
        self.job.update(status="cancelled", message="Stopped after moving 1 container")
        self.go(at(12, 17))
        self.assertIn("Stopped after moving 1", recovery.status()["paused"]["why"])

    def test_resume_needs_an_admin(self):
        self.assertEqual("viewer", recovery.ROUTES[("GET", "/api/recovery")][0])
        self.assertEqual("admin", recovery.ROUTES[("POST", "/api/recovery/resume")][0])

    def test_resume_clears_a_pause_and_a_removed_mark(self):
        self.back_at_noon()
        self.go()
        self.job.update(status="cancelled", message="Stopped")
        self.go(at(12, 17))
        recovery.undone_by("lab/db", "Flux", at(12, 18))
        self.start.reset_mock()
        answer = recovery.ROUTES[("POST", "/api/recovery/resume")][1](mock.Mock())
        self.assertIsNone(answer["paused"])
        self.assertIsNone(answer["undone"])
        self.go(at(13))
        self.start.assert_called_once()

    def test_the_routes_are_registered(self):
        self.assertIn("homestead_recovery", homestead_routes.MODULES)


class AutoAlertTests(AutoCase):
    def facts(self, key="recovery:rebalance"):
        return [f for f in recovery.alert_facts("auto", self.settings["updates"]["maintenance"]) if f["key"] == key]

    KEPT = [{"id": "lab/web", "why": "its host preference keeps it on a, so a restart would not move it"},
            {"id": "lab/db", "why": "the scheduler would put it back on a: reservations favour that host"}]

    def test_apps_left_out_are_said_with_why_while_it_holds(self):
        self.kept, self.would = self.KEPT, []
        self.back_at_noon()
        self.go(at(12, 5))
        fact, = self.facts()
        self.assertIn("2 containers stay where they are", fact["body"])
        self.assertIn("lab/web: its host preference keeps it on a", fact["body"])
        self.assertIn("Balance hosts can still move them", fact["body"])
        self.assertEqual(2, fact["signals"]["kept"])
        self.assertEqual(2, len(recovery.status()["kept"]))

    def test_the_alert_says_the_true_count(self):
        self.kept = [{"id": f"lab/app-{i}", "why": "no"} for i in range(recovery.KEPT_SHOWN + 10)]
        self.would = []
        self.back_at_noon()
        self.go(at(12, 5))
        self.assertIn(f"{recovery.KEPT_SHOWN + 10} containers stay", self.facts()[0]["body"])

    def test_no_title_says_nothing_could_move(self):
        self.deployments = [tiered("web", "web-data", "important"), tiered("db", "db-data", "important")]
        self.back_at_noon()
        self.go()
        fact, = self.facts()
        self.assertNotIn("0 containers", fact["title"])
        self.assertIn("2 critical apps wait", fact["title"])

    def test_the_clearing_line_is_true_in_every_mode_and_path(self):
        self.back_at_noon()
        recovery.suggest(["lab/db"], at(12, 5))
        for mode in ("suggest", "window", "auto"):
            line = recovery.alert_facts(mode, WINDOW)[0]["resolved"]
            self.assertEqual("This alert has cleared; Balance hosts shows anything that could still move.", line, mode)

    def test_a_hold_with_apps_kept_doesnt_say_they_started_elsewhere_when_nothing_else_is_said(self):
        self.kept, self.would = self.KEPT, []
        self.back_at_noon()
        recovery._auto_wait(["b steady in 6 min"], [], 0, 0, self.KEPT, [], 0, kept_total=2)
        recovery.suggest([], at(12, 5), forget=False)
        self.assertNotIn("started elsewhere", self.facts()[0]["body"])

    def test_resume_is_audited(self):
        self.assertTrue(AUDIT.audited("POST", "/api/recovery/resume"))

    def test_the_alert_says_what_it_waits_for(self):
        self.back_at_noon()
        self.go(at(12, 9))
        fact, = self.facts()
        self.assertEqual("joins", fact["category"])
        self.assertIn("b steady in 6 min", fact["body"])

    def test_the_alert_names_a_host_away_in_its_title(self):
        # Andreas, 7 Nov: everything holds, with an alert naming it.
        self.nodes = [node("a"), node("b"), node("c")]
        recovery.tick(at(10))
        self.nodes = [node("a"), node("b", ready=False), node("c", ready=False)]
        recovery.tick(at(11))
        self.nodes = [node("a"), node("b"), node("c", ready=False)]
        recovery.tick(at(12))
        self.go()
        fact, = self.facts()
        self.assertIn("c is away", fact["title"])
        self.assertEqual(1, fact["signals"]["away"])

    def test_the_alert_says_what_waits_for_the_window(self):
        self.deployments = [tiered("web", "web-data", "important"), tiered("db", "db-data")]
        self.back_at_noon()
        self.go()
        self.assertIn("1 critical app waits for the update window (02:00 UTC)", self.facts()[0]["body"])

    def test_the_alert_says_when_the_window_has_no_days(self):
        self.settings["updates"]["maintenance"] = {**WINDOW, "days": []}
        self.deployments = [tiered("web", "web-data", "important"), tiered("db", "db-data", "important")]
        self.back_at_noon()
        self.go()
        self.assertIn("The update window has no days set, so 2 critical apps stay until you move them", self.facts()[0]["body"])

    def test_the_alert_says_flux_apps_are_left_out_outside_spread(self):
        recovery.managed_by = lambda dep: {"by": "Flux", "detected": True} if dep["metadata"]["name"] == "db" else {}
        self.addCleanup(setattr, recovery, "managed_by", lambda dep: {})
        self.settings["placement"]["mode"] = "default"
        self.back_at_noon()
        self.go()
        self.assertIn("1 app Flux applies is left out: choose Spread by capacity to include it", self.facts()[0]["body"])

    def test_under_spread_flux_apps_are_not_called_left_out(self):
        recovery.managed_by = lambda dep: {"by": "Flux", "detected": True} if dep["metadata"]["name"] == "db" else {}
        self.addCleanup(setattr, recovery, "managed_by", lambda dep: {})
        self.back_at_noon()
        self.go()
        self.assertNotIn("left out", self.facts()[0]["body"])

    def test_the_alert_says_a_run_is_moving_them(self):
        self.back_at_noon()
        self.go()
        fact, = self.facts()
        self.assertIn("2 containers", fact["title"])
        self.assertIn("one at a time", fact["body"])

    def test_a_stop_has_its_own_alert_linking_to_settings(self):
        self.back_at_noon()
        self.go()
        self.job.update(status="failed", message="lab/web restarted on b, not a")
        self.go(at(12, 17))
        fact, = self.facts("recovery:stopped")
        self.assertEqual("Moving containers back is stopped: lab/web restarted on b, not a", fact["title"])
        self.assertEqual("/settings?tab=placement", fact["href"])
        recovery.resume()
        self.assertEqual([], self.facts("recovery:stopped"))

    def test_the_alert_clears_when_nothing_is_left(self):
        self.back_at_noon()
        self.would = []
        self.go()
        self.assertEqual([], recovery.alert_facts("auto", WINDOW))


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
