"""The canary's day-long check counts alerts that were raised, not conditions
still in their hold (#1526: a Service address Homestead itself recorded again
within seconds stopped an OS update, and no alert was ever raised).

Runs the real alert store (homestead_alerts.observe and note, in a temporary
data folder) with the Service-address facts homestead_vips gives, and the
canary as it is wired in production: no alerts hook, so it reads the store."""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_alerts as ALERTS
import homestead_os_canary as CANARY
import homestead_vips as VIPS
from fakes.kube import no_cluster
from rebind import keep_bound

T = 1_800_000_000
LOOK = 20     # the alert loop's look (homestead_overview_server.LOOK_SECONDS)
TICK = 60     # the OS update's step while the canary is checked
END = T + CANARY.SOAK
SETTLE = 3600  # the wait after the day (homestead_os_canary.SETTLE): twice a stalled job's 30 min hold


def setUpModule():
    unittest.enterModuleContext(no_cluster())


def ready(name):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


def kget(path):
    if path.startswith("/api/v1/nodes/"):
        return ready(path.rsplit("/", 1)[1])
    return {"items": []}


def unrouted(ip="192.0.2.50"):
    """What homestead_vips.address_map says while a Service has lost its address
    from status and kube-vip still answers for it."""
    return VIPS.alert_facts({"addresses": [{"ip": ip, "state": "unrouted",
                                             "reason": f"node-a answers for {ip}, but lab/web does not carry it"}]})


def volume(severity):
    return ALERTS.health_facts({"current_issues": [{"kind": "Volume", "name": "web-data", "severity": severity,
                                                    "reason": "one replica of three is rebuilding"}]})


class RaisedAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        keep_bound(self, ALERTS, CANARY)
        ALERTS.bind(self.temp.name)
        CANARY.bind(kget, None)           # as in production: CANARY.alerts is never bound
        self.restart()
        self.soak = {"name": "node-b", "since": T}
        self.look(T - 600, [])            # the alert loop was running before the check began
        self.routine(2, T - 600)          # and the log has entries from before it, as a running cluster's has

    def restart(self):
        """What a Homestead restart or a leader change gives the canary: the
        module as a new process loads it, the alert store as the data volume
        keeps it, and the OS update's state as it saved it (homestead_os_rollout._save)."""
        importlib.reload(CANARY)
        CANARY.bind(kget, None)
        if hasattr(self, "soak"):
            self.soak = json.loads(json.dumps(self.soak))

    def look(self, at, facts, source="addresses"):
        return ALERTS.observe({source: facts}, now=at)

    def looks(self, start, stop, facts, source="addresses"):
        for at in range(start, stop, LOOK):
            self.look(at, facts, source)

    def watch(self, at):
        return CANARY.watch(self.soak, now=at)

    def routine(self, count, at):
        """Notes a busy cluster writes to the alert log: updates available."""
        for n in range(count):
            ALERTS.note({"key": f"updates:lab/app-{n}:{at}", "category": "updates", "severity": "info", "event": True,
                         "title": f"Update available: app-{n}", "href": "/containers?panel=updates"}, now=at)

    # ---- #1526 as seen: a condition repaired inside its hold

    def test_a_condition_repaired_inside_its_hold_does_not_stop_the_check(self):
        self.look(T + 7300, unrouted())          # one look sees the address lost
        row = ALERTS.active()[0]
        # What the store hands the canary for a condition nobody was told about.
        self.assertEqual((0, T + 7300, "degraded"), (row["announced"], row["since"], row["severity"]))
        self.assertEqual([], [a for a in ALERTS.active() if a.get("announced", 0) > 0], "the bell shows nothing")
        self.assertEqual("wait", self.watch(T + 7310)[0], "held, not raised: not news")
        self.look(T + 7300 + LOOK, [])            # Homestead recorded it again
        self.assertEqual("wait", self.watch(T + 7300 + LOOK + 10)[0],
                         "repaired before its hold, still in the store's 60 s grace")
        self.looks(T + 7300 + 2 * LOOK, T + 7500, [])
        self.assertEqual([], ALERTS.active())
        self.assertEqual("wait", self.watch(T + 7600)[0])
        self.assertEqual("go", self.watch(END)[0])

    def test_a_condition_from_before_the_check_that_flaps_does_not_stop_it(self):
        self.look(T - 200, unrouted())            # held before the check began
        self.look(T - 180, [])
        self.look(T + 100, unrouted())            # back within the grace: its since moves on
        self.assertGreaterEqual(ALERTS.active()[0]["since"], T)
        self.assertEqual("wait", self.watch(T + 110)[0])

    # ---- what does stop it

    def test_a_condition_that_outlasts_its_hold_stops_it_and_names_the_alert(self):
        self.looks(T + 100, T + 100 + VIPS.ADDRESS_HOLD + LOOK, unrouted())
        self.assertGreater(ALERTS.active()[0]["announced"], 0)
        verdict, words = self.watch(T + 100 + VIPS.ADDRESS_HOLD + LOOK)
        self.assertEqual("stop", verdict)
        self.assertIn("an alert was raised while the canary soaked: Service address 192.0.2.50 has no route", words)
        self.assertIn("(see the Timeline)", words, "says where a person finds it, also once it cleared")
        self.assertNotIn("All alerts", words, "All alerts lists only what is still open")
        titles = [e["title"] for e in ALERTS.log(limit=ALERTS.LOG_SIZE)["alerts"]]
        self.assertIn("Service address 192.0.2.50 has no route", titles, "the alert it names is in the Timeline's source")

    def test_a_fault_raised_and_cleared_between_two_looks_still_stops_it(self):
        self.looks(T + 100, T + 100 + VIPS.ADDRESS_HOLD + LOOK, unrouted())
        self.looks(T + 420, T + 560, [])
        self.assertEqual([], ALERTS.active(), "resolved and gone from the store")
        verdict, words = self.watch(T + 600)
        self.assertEqual("stop", verdict, "it was raised during the check: a host that flapped is news")
        self.assertIn("Service address 192.0.2.50 has no route", words)

    def test_a_condition_from_before_the_check_raised_during_it_stops_it(self):
        self.looks(T - 200, T + 120, unrouted())  # began while the canary restarted
        self.assertLess(ALERTS.active()[0]["since"], T)
        self.assertEqual("stop", self.watch(T + 130)[0])

    def test_an_alert_raised_before_the_check_that_worsens_during_it_stops_it(self):
        self.looks(T - 300, T - 200, volume("degraded"), "health")
        self.assertGreater(ALERTS.active()[0]["announced"], 0, "raised before the check")
        self.looks(T - 200, T + 1000, volume("degraded"), "health")
        self.assertEqual("wait", self.watch(T + 1000)[0], "raised before the check, unchanged since")
        self.looks(T + 1000, T + 1100, volume("critical"), "health")
        verdict, words = self.watch(T + 1100)
        self.assertEqual("stop", verdict)
        self.assertIn("an alert got worse while the canary soaked: Volume web-data is faulted", words)

    def test_an_acknowledged_alert_still_stops_it(self):
        self.looks(T + 100, T + 200, volume("degraded"), "health")
        row = ALERTS.active(user="owner")[0]
        ALERTS.acknowledge("owner", row["key"], row["version"], now=T + 200)
        self.assertTrue(ALERTS.active(user="owner")[0]["acknowledged"])
        self.assertEqual("stop", self.watch(T + 210)[0],
                         "seen by one person is not the same as unrelated to the update")

    def test_an_alert_raised_in_the_check_s_first_second_counts(self):
        self.soak = {"name": "node-b", "since": T + 0.5}
        self.look(T + 0.9, [{"key": "logs:la-0123456789ab", "category": "logs", "severity": "degraded", "urgent": True,
                             "title": "Errors: 12 matching lines in a minute"}], "logs")
        self.assertEqual(T, ALERTS.log(limit=1)["alerts"][-1]["at"], "the log keeps whole seconds")
        self.assertEqual("stop", self.watch(T + 20)[0])

    # ---- what does not

    def test_a_failed_job_does_not_stop_it(self):
        self.look(T - 300, [], "jobs")           # the jobs source was already watched
        self.look(T + 100, ALERTS.job_facts([{"id": "op-7", "status": "failed", "title": "Update web",
                                              "message": "the registry did not answer"}]), "jobs")
        entry = ALERTS.log(limit=1)["alerts"][-1]
        self.assertEqual(("Job failed: Update web", "degraded", True), (entry["title"], entry["severity"], entry["event"]))
        self.assertEqual("wait", self.watch(T + 110)[0], "an event, not a condition: as before #1526")

    def test_the_run_s_own_notes_and_routine_notes_do_not_stop_it(self):
        # The OS update's own alert (homestead_os_rollout._alert: "it never stops what raised it").
        ALERTS.note({"key": "os-update-hold:os-1:node-a", "category": "degraded", "severity": "degraded", "event": True,
                     "title": "Automatic updates could not be held on node-a during an OS update",
                     "body": "x. Homestead keeps trying.", "href": "/nodes"}, now=T + 100)
        # A restore's note (homestead_restore_swap), a log alert paused, a test push, a summary.
        ALERTS.note({"key": "previous-volume:lab/web-data:deleted:1", "category": "degraded", "severity": "info",
                     "event": True, "title": "Deleted web's previous volume web-data (kept 7 days)"}, now=T + 110)
        ALERTS.note({"key": "logs:la-0123456789ab:paused", "category": "logs", "severity": "info", "event": True,
                     "title": "Errors: paused by Homestead"}, now=T + 120)
        ALERTS.note({"key": "test:1", "category": "test", "severity": "info", "title": "Homestead test notification",
                     "to": "device"}, now=T + 130)
        ALERTS.note({"key": "summary:owner:1", "category": "summary", "severity": "info", "event": True,
                     "title": "Your summary", "for": "owner"}, now=T + 140)
        self.routine(20, T + 150)
        self.assertEqual("wait", self.watch(T + 200)[0])
        self.assertEqual("go", self.watch(END)[0])

    # ---- the end of the day

    def test_the_day_ends_while_a_condition_is_in_its_hold_the_others_wait(self):
        self.look(END - 30, unrouted())
        verdict, words = self.watch(END + 5)
        self.assertEqual("wait", verdict, "not news yet, but it may become news")
        self.assertIn("a day passed. Waiting up to 1 h for Service address 192.0.2.50 has no route to clear", words)
        self.assertNotIn(";", words)
        self.looks(END - 10, END + 200, [])
        self.assertEqual("go", self.watch(END + 210)[0], "it cleared: the day is not counted again")

    def test_the_day_ends_while_a_condition_is_in_its_hold_then_it_is_raised(self):
        self.look(END - 30, unrouted())
        self.assertEqual("wait", self.watch(END + 5)[0])
        self.looks(END - 10, END + VIPS.ADDRESS_HOLD, unrouted())
        self.assertEqual("stop", self.watch(END + VIPS.ADDRESS_HOLD)[0])

    def test_a_condition_whose_source_cannot_be_read_stops_it_after_an_hour(self):
        self.look(END - 30, unrouted())
        self.looks(END - 10, END + SETTLE + 2 * LOOK, None)   # the addresses could not be read
        row = ALERTS.active(now=END + SETTLE + LOOK)[0]
        self.assertEqual((0, True), (row["announced"], row["since"] >= END), "never raised, and always new")
        self.assertEqual("wait", self.watch(END + SETTLE - 60)[0])
        verdict, words = self.watch(END + SETTLE + LOOK)
        self.assertEqual("stop", verdict, "an hour is twice the longest hold (a stalled job's 30 minutes)")
        self.assertIn("Service address 192.0.2.50 has no route neither cleared nor was raised as an alert", words)
        self.assertIn("Homestead could not check it", words, "the row is stale (homestead_alerts.STALE)")

    def test_a_condition_that_keeps_coming_back_stops_it_after_an_hour(self):
        at = END - 30
        while at < END + SETTLE + LOOK:   # seen, gone for 40 s, back: within the store's 60 s grace
            self.look(at, unrouted())
            self.look(at + LOOK, [])
            self.look(at + 2 * LOOK, [])
            at += 3 * LOOK
        self.assertEqual(0, ALERTS.active()[0]["announced"])
        verdict, words = self.watch(END + SETTLE + LOOK)
        self.assertEqual("stop", verdict)
        self.assertIn("Service address 192.0.2.50 has no route neither cleared nor was raised as an alert", words)

    # ---- restarts and the log's length

    def test_a_restart_during_the_check_keeps_what_was_raised(self):
        self.assertEqual("wait", self.watch(T + 60)[0])
        self.looks(T + 100, T + 100 + VIPS.ADDRESS_HOLD + LOOK, unrouted())
        self.looks(T + 420, T + 560, [])
        self.restart()                        # the alert log is on the shared data volume
        self.assertEqual("stop", self.watch(T + 900)[0])

    def test_a_restart_on_a_quiet_cluster_goes_on(self):
        self.assertEqual("wait", self.watch(T + 60)[0])
        self.routine(5, T + 100)
        self.restart()
        self.assertEqual("wait", self.watch(T + 900)[0])
        self.assertEqual("go", self.watch(END)[0])

    def test_a_busy_log_looked_at_every_minute_does_not_stop_it_across_restarts(self):
        for n, at in enumerate(range(T + TICK, T + 40 * TICK, TICK)):
            self.routine(12, at)              # 468 entries: more than the log keeps
            if n % 10 == 9:
                self.restart()                # what the canary read is kept in the OS update's state
            self.assertEqual("wait", self.watch(at + 1)[0], n)
        self.assertGreater(ALERTS.log(limit=ALERTS.LOG_SIZE)["alerts"][0]["at"], T, "the start has left the log")
        self.assertEqual("go", self.watch(END)[0])

    def test_a_raised_alert_pushed_out_of_the_log_before_the_canary_looked_stops_it(self):
        self.routine(3, T - 100)
        self.assertEqual("wait", self.watch(T + 60)[0])
        self.looks(T + 100, T + 100 + VIPS.ADDRESS_HOLD + LOOK, unrouted())
        self.looks(T + 420, T + 560, [])
        self.routine(ALERTS.LOG_SIZE, T + 570)
        self.assertNotIn("Service address 192.0.2.50 has no route",
                         [e["title"] for e in ALERTS.log(limit=ALERTS.LOG_SIZE)["alerts"]])
        verdict, words = self.watch(T + 600)
        self.assertEqual("stop", verdict, "it cannot tell what was raised: it does not go on")
        self.assertIn("cannot tell whether an alert was raised", words)

    def test_a_restart_while_300_entries_arrived_unread_stops_it(self):
        self.assertEqual("wait", self.watch(T + 60)[0])
        self.routine(ALERTS.LOG_SIZE + 10, T + 3600)
        self.restart()
        verdict, words = self.watch(T + 3700)
        self.assertEqual("stop", verdict)
        self.assertIn("cannot tell whether an alert was raised", words)

    def test_a_run_started_by_the_previous_release_goes_on_while_the_log_reaches_back(self):
        old = {"name": "node-b", "since": T}       # a soak saved before this change: no log fields
        self.routine(5, T + 100)
        self.assertEqual("wait", CANARY.watch(old, now=T + 7200)[0])
        self.routine(ALERTS.LOG_SIZE - 10, T + 7210)   # the start of the day leaves the log
        self.assertEqual("wait", CANARY.watch(old, now=T + 7220)[0], "a later look trusts what the first one read")
        self.looks(T + 7300, T + 7300 + VIPS.ADDRESS_HOLD + LOOK, unrouted())
        self.assertEqual("stop", CANARY.watch(old, now=T + 7300 + VIPS.ADDRESS_HOLD + LOOK)[0])

    def test_a_person_s_summary_replaced_after_the_canary_read_it_does_not_stop_it(self):
        summary = {"key": "summary:owner:1", "category": "summary", "severity": "info", "event": True,
                   "title": "Your summary", "for": "owner"}
        ALERTS.note(summary, now=T + 100)
        self.assertEqual("wait", self.watch(T + 110)[0])        # the last entry it read is that summary
        ALERTS.note({**summary, "key": "summary:owner:2"}, now=T + 3600)   # the next one replaces it
        self.assertEqual("wait", self.watch(T + 3610)[0])
        self.assertEqual("go", self.watch(END)[0])

    def test_an_alert_with_a_time_before_the_check_counts_once_the_check_began(self):
        self.assertEqual("wait", self.watch(T + 60)[0])
        # The clock set back: the alert log says it was raised before the check.
        self.looks(T - 3000, T - 3000 + VIPS.ADDRESS_HOLD + LOOK, unrouted())
        self.assertEqual("stop", self.watch(T + 120)[0])

    def test_a_damaged_record_of_what_was_read_stops_it(self):
        for bad in ([None, 3, T], [1, 5, "abc"], [1, "5", T], "x", [1, 2], {"id": 5}, [True, 5, T]):
            soak = {"name": "node-b", "since": T, "log_read": bad}
            verdict, words = CANARY.watch(soak, now=T + 600)
            self.assertEqual("stop", verdict, bad)
            self.assertIn("cannot tell whether an alert was raised", words, bad)

    def test_a_run_started_by_the_previous_release_stops_when_the_log_no_longer_reaches_back(self):
        self.routine(ALERTS.LOG_SIZE + 10, T + 3600)
        verdict, words = CANARY.watch({"name": "node-b", "since": T}, now=T + 7200)
        self.assertEqual("stop", verdict)
        self.assertIn("cannot tell whether an alert was raised", words)

    def test_a_lost_alert_log_stops_it_also_after_a_restart(self):
        self.routine(5, T + 100)
        self.assertEqual("wait", self.watch(T + 300)[0])
        os.remove(os.path.join(self.temp.name, "alerts.json"))
        self.routine(8, T + 400)                # a fresh log, counted from 1 again, past where it was
        self.restart()
        verdict, words = self.watch(T + 500)
        self.assertEqual("stop", verdict)
        self.assertIn("cannot tell whether an alert was raised", words)

    def test_a_corrupt_log_begun_again_stops_it_after_a_restart(self):
        self.assertEqual("wait", self.watch(T + 60)[0])
        with open(os.path.join(self.temp.name, "alerts.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.look(T + 100, [], "jobs")          # the alert loop reads it leniently: a fresh log
        self.routine(3, T + 120)
        self.assertEqual(1, ALERTS.log(limit=ALERTS.LOG_SIZE)["alerts"][0]["id"])
        self.restart()
        verdict, words = self.watch(T + 200)
        self.assertEqual("stop", verdict)
        self.assertIn("cannot tell whether an alert was raised", words)

    def test_an_unreadable_alert_log_makes_it_wait(self):
        with open(os.path.join(self.temp.name, "alerts.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        verdict, words = self.watch(END)
        self.assertEqual("wait", verdict, "a day passed, but what was raised in it cannot be read")
        self.assertIn("alert log", words)


if __name__ == "__main__":
    unittest.main()
