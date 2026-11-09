import datetime
import functools
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_drain_blockers as DRAIN_BLOCKERS
import homestead_history as HISTORY
import homestead_os_rollout as ROLLOUT
from rebind import keep_bound


def node(name, ready=True, cordoned=False):
    return {"metadata": {"name": name}, "spec": {"unschedulable": cordoned},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class Hosts:
    """Each host's OS as homestead_host_os would say, and what was done to it."""
    LOG = "/var/log/homestead-os-upgrade.log"

    def __init__(self, facts):
        self.facts, self.began, self.holds, self.codes, self.held = facts, [], [], {}, set()
        # What set_hold raises per host: the real one takes the host's lease
        # through hostrun, so it can be Busy or time out.
        self.hold_errors = {}

    def read(self, name, refresh=False):
        return self.facts[name]

    def report(self, name=None):
        return {"hosts": {k: v for k, v in self.facts.items() if name is None or k == name}}

    def stored(self, name):
        return self.facts.get(name)

    def upgrade_begin(self, name, facts=None, ref=None):
        self.began.append(name)
        if ref is not None:
            ref["lease"] = f"lease-{name}"
        self.held.add(name)

    def upgrade_done(self, name, ref):
        if ref.pop("lease", "") == f"lease-{name}":
            self.held.discard(name)

    def upgrade_state(self, name, run=None):
        self.asked_run = getattr(self, "asked_run", [])
        self.asked_run.append((name, run))
        code = self.codes.get(name, "0")
        if code == "0":
            # Installed: nothing waits any more; a kernel asks for a restart.
            kernel = any(u["name"].startswith("linux-image") for u in self.facts[name]["updates"])
            self.facts[name] = {**self.facts[name], "updates": [], "security": 0, "reboot": kernel}
        return {"running": code is None, "code": code, "last": ""}

    def set_hold(self, name, hold):
        if name in self.hold_errors:
            raise self.hold_errors[name]
        self.holds.append((name, hold))
        self.facts[name]["auto"]["held"] = hold


def facts(updates=(), reboot=False, auto=True):
    return {"updates": [{"name": u, "security": False} for u in updates], "security": 0, "reboot": reboot,
            "auto": {"tool": "unattended-upgrades" if auto else "", "on": auto, "reboots": False, "held": False}}


def own_history(test):
    """The test's own history file, for declaring maintenance (#535)."""
    @functools.wraps(test)
    def run(self):
        with mock.patch.object(HISTORY, "DATA_DIR", self.data):
            return test(self)
    return run


class RolloutTests(unittest.TestCase):
    def setUp(self):
        keep_bound(self, ROLLOUT)
        self.data = tempfile.mkdtemp()
        self.nodes = {"node-1": node("node-1"), "node-2": node("node-2"), "node-3": node("node-3")}
        self.hosts = Hosts({"node-1": facts(["curl", "linux-image-6.8"]), "node-2": facts(["curl"]), "node-3": facts()})
        self.restarts, self.cordons, self.ops, self.refuse = [], [], {}, {}
        self.bind()

    def bind(self, own="node-1"):
        def reboot(name, single_copy):
            if name in self.refuse:
                # As the real one: a volume's only copy may clear, the rest won't.
                may_clear = "only healthy copy" in self.refuse[name]
                raise (DRAIN_BLOCKERS.MayClear if may_clear else ValueError)(self.refuse[name])
            self.restarts.append(name)
            self.ops[f"op-{name}-{len(self.restarts)}"] = {"id": f"op-{name}-{len(self.restarts)}", "status": "running"}
            return f"op-{name}-{len(self.restarts)}"
        ROLLOUT.bind(lambda path: {"items": list(self.nodes.values())},
                     lambda force=False: {"distribution": "k3s"}, self.hosts, reboot, lambda op: self.ops.get(op),
                     lambda name, on: self.cordons.append((name, on)), lambda: own, self.data)

    def run_until_done(self, limit=60, on_step=None):
        for _ in range(limit):
            ROLLOUT.tick(now=1000)
            if on_step:
                on_step()
            rollout = ROLLOUT._load().get("rollout") or {}
            if rollout.get("status") != "running":
                return rollout
        self.fail("the rollout did not finish")

    def finish_restarts(self):
        for op in self.ops.values():
            op["status"] = "succeeded"

    def test_a_scheduled_rollout_waits_while_containers_are_rebalanced(self):
        with mock.patch.object(ROLLOUT, "due", return_value=True), \
                mock.patch.object(ROLLOUT.OPS, "stored", return_value=[{"kind": "container-rebalance", "status": "running"}]), \
                mock.patch.object(ROLLOUT, "start") as start:
            ROLLOUT.tick()
            start.assert_not_called()
        with mock.patch.object(ROLLOUT, "due", return_value=True), \
                mock.patch.object(ROLLOUT.OPS, "stored", return_value=[{"kind": "container-rebalance", "status": "succeeded"}]), \
                mock.patch.object(ROLLOUT, "start") as start:
            ROLLOUT.tick(now=time.time() + ROLLOUT.WINDOW_RETRY)   # its next look
            start.assert_called_once()

    @own_history
    def test_rollout_leaves_a_host_in_maintenance_alone(self):
        # A person declared it: nobody wants a reboot in the middle of a drive swap (#535).
        HISTORY.maintenance_declare("node-2", "Drive swap", "op-9", 9_999_999_999, cordoned_before=False, node_uid="u")
        ROLLOUT.start(now=1000)
        done = self.run_until_done(on_step=self.finish_restarts)
        row = next(r for r in done["results"] if r["node"] == "node-2")
        self.assertEqual((False, "in maintenance, so left alone"), (row["ok"], row["note"]))
        self.assertNotIn("node-2", self.restarts)
        self.assertNotIn("node-2", [name for name, _ in self.cordons])
        self.assertTrue(any(r["node"] == "node-3" and r["ok"] for r in done["results"]), "the others carry on")

    def declared_on_node_2(self):
        self.hosts = Hosts({"node-1": facts(["curl"]), "node-2": facts(["linux-image-6.8"]), "node-3": facts()})
        self.bind()

    def declare_node_2(self):
        if not HISTORY.declared_for("node-2"):
            HISTORY.maintenance_declare("node-2", "Drive swap", "op-9", 9_999_999_999, cordoned_before=False, node_uid="u")

    @own_history
    def test_a_host_declared_before_its_restart_is_not_restarted_or_uncordoned(self):
        self.declared_on_node_2()
        ROLLOUT.start(now=1000)

        def when_it_reached_it():
            rollout = ROLLOUT._load().get("rollout") or {}
            if (rollout.get("node") or {}).get("name") == "node-2" and rollout.get("phase") == "restart":
                self.declare_node_2()
            self.finish_restarts()
        done = self.run_until_done(on_step=when_it_reached_it)
        row = next(r for r in done["results"] if r["node"] == "node-2")
        self.assertEqual([], [n for n in self.restarts if n == "node-2"])
        self.assertNotIn(("node-2", False), self.cordons)
        self.assertEqual("in maintenance, so not restarted", row["note"], "it may have installed nothing")
        self.assertTrue(row["restart_needed"])

    @own_history
    def test_a_host_declared_during_its_restart_keeps_the_cordon(self):
        self.declared_on_node_2()
        ROLLOUT.start(now=1000)

        def during():
            rollout = ROLLOUT._load().get("rollout") or {}
            if (rollout.get("node") or {}).get("name") == "node-2" and rollout.get("phase") == "restarting":
                self.declare_node_2()
                self.finish_restarts()
        done = self.run_until_done(on_step=during)
        self.assertIn("node-2", self.restarts)
        self.assertNotIn(("node-2", False), self.cordons, "the maintenance's cordon stays")
        self.assertEqual("succeeded", done["status"])

    def spoil_history(self):
        with open(HISTORY._path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")

    def tick_past_the_error_limit(self, start=1000):
        for at in range(start, start + ROLLOUT.ERROR_LIMIT + 2 * ROLLOUT.ERROR_GAP, ROLLOUT.ERROR_GAP // 2):
            ROLLOUT.tick(now=at)
            if ROLLOUT._load()["rollout"].get("status") != "running":
                break
        return ROLLOUT._load()["rollout"]

    @own_history
    def test_history_that_cannot_be_read_waits_before_a_host_and_goes_on_once_it_can(self):
        self.declared_on_node_2()
        ROLLOUT.start(now=1000)
        self.spoil_history()
        for _ in range(4):
            ROLLOUT.tick(now=1000)
        rollout = ROLLOUT._load()["rollout"]
        self.assertEqual(("next", 0, []), (rollout["phase"], rollout["index"], rollout["results"]), "it doesn't assume")
        self.assertIn("could not be read", rollout["message"])
        os.remove(HISTORY._path())
        ROLLOUT.tick(now=1060)
        self.assertNotEqual("next", ROLLOUT._load()["rollout"]["phase"], "and goes on once it can")

    @own_history
    def test_history_that_stays_unreadable_ends_the_rollout_and_lets_go_of_the_holds(self):
        self.declared_on_node_2()
        ROLLOUT.start(now=1000)
        self.spoil_history()
        rollout = self.tick_past_the_error_limit()
        self.assertNotEqual("running", rollout.get("status"), "not for ever")
        self.assertTrue(rollout.get("failed"))
        self.assertIn("could not be read", rollout["results"][0]["note"])
        self.assertEqual([], self.restarts)
        held = {name for name, on in self.hosts.holds if on}
        self.assertTrue(held)
        self.assertEqual(held, {name for name, on in self.hosts.holds if not on}, "automatic updates are let go")

    def to_restart_on_node_2(self):
        self.declared_on_node_2()
        ROLLOUT.start(now=1000)
        for _ in range(30):
            ROLLOUT.tick(now=1000)
            rollout = ROLLOUT._load()["rollout"]
            if (rollout.get("node") or {}).get("name") == "node-2" and rollout.get("phase") == "restart":
                return
        self.fail("it never reached node-2's restart")

    @own_history
    def test_history_that_stays_unreadable_at_a_restart_ends_it_with_the_host_cordoned(self):
        self.to_restart_on_node_2()
        self.spoil_history()
        ROLLOUT.tick(now=1000)
        self.assertEqual("restart", ROLLOUT._load()["rollout"]["phase"], "a wait first")
        rollout = self.tick_past_the_error_limit()
        self.assertNotEqual("running", rollout.get("status"))
        self.assertEqual([], self.restarts)
        self.assertNotIn(("node-2", False), self.cordons)
        self.assertIn("left cordoned", next(r for r in rollout["results"] if r["node"] == "node-2")["note"])

    @own_history
    def test_stop_does_not_wait_longer_than_the_error_limit(self):
        self.to_restart_on_node_2()
        self.spoil_history()
        state = ROLLOUT._load()
        state["rollout"]["stop"] = True
        ROLLOUT._save(state)
        self.assertNotEqual("running", self.tick_past_the_error_limit().get("status"))

    @own_history
    def test_a_cordon_it_cannot_tell_about_stays_after_the_restart(self):
        self.declared_on_node_2()
        ROLLOUT.start(now=1000)
        for _ in range(30):
            ROLLOUT.tick(now=1000)
            rollout = ROLLOUT._load()["rollout"]
            if (rollout.get("node") or {}).get("name") == "node-2" and rollout.get("phase") == "restarting":
                break
        self.assertEqual("restarting", rollout["phase"])
        with open(HISTORY._path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.finish_restarts()
        ROLLOUT.tick(now=1000)
        self.assertEqual("node-2", ROLLOUT._load()["rollout"]["results"][0]["node"], "the restart finished")
        self.assertNotIn(("node-2", False), self.cordons, "and an unread history is not a no")

    def test_hosts_are_updated_one_at_a_time_with_its_own_host_last(self):
        rollout = ROLLOUT.start(now=1000)
        self.assertEqual(["node-2", "node-3", "node-1"], rollout["nodes"], "the leader's host goes last")
        done = self.run_until_done(on_step=self.finish_restarts)
        self.assertEqual("succeeded", done["status"])
        self.assertEqual(["node-2", "node-1"], self.hosts.began, "an up-to-date host installs nothing")
        self.assertEqual(["node-1"], self.restarts, "only a host whose update asks for it restarts")
        self.assertIn(("node-1", False), self.cordons, "uncordoned once back")
        notes = {r["node"]: r["note"] for r in done["results"]}
        self.assertEqual(("updates installed", "up to date", "updates installed and restarted"),
                         (notes["node-2"], notes["node-3"], notes["node-1"]))

    def test_each_host_is_let_go_of_once_its_updates_are_installed(self):
        ROLLOUT.start(now=1000)
        done = self.run_until_done(on_step=self.finish_restarts)
        self.assertEqual("succeeded", done["status"])
        self.assertEqual(["node-2", "node-1"], self.hosts.began)
        self.assertEqual(set(), self.hosts.held, "no host is left held for another change")

    def test_a_host_the_review_will_not_restart_keeps_its_updates_and_the_rollout_goes_on(self):
        self.refuse["node-1"] = "Running VMs are on this host; migrate or stop them and review again"
        self.bind(own="node-3")
        done = self.run_until_done() if ROLLOUT.start(now=1000) else None
        self.assertEqual("succeeded", done["status"])
        self.assertIn("needing a restart: node-1", done["message"])
        self.assertIn("(node-1: Running VMs are on this host", done["message"], "the job says why")
        self.assertEqual({"node-1", "node-2"}, set(self.hosts.began))

    def test_a_host_waits_for_copies_still_rebuilding_then_restarts(self):
        self.refuse["node-1"] = "a volume has its only healthy copy on this host (the settings do not accept that)"
        self.bind(own="node-3")
        ROLLOUT.start(now=1000)
        clock, waited = 1000, False
        for _ in range(80):
            clock += 30
            ROLLOUT.tick(now=clock)
            rollout = ROLLOUT._load().get("rollout") or {}
            if "waiting for volume copies" in rollout.get("message", ""):
                waited = True
                self.refuse.pop("node-1", None)        # the copies are whole again
            self.finish_restarts()
            if rollout.get("status") != "running":
                break
        self.assertTrue(waited, "it waited for the copies")
        self.assertIn("node-1", self.restarts, "and then restarted the host")
        self.assertNotIn("needing a restart", rollout["message"])

    def test_a_busy_api_server_is_waited_out_not_taken_as_a_refusal(self):
        # Right after a host rejoins, the API server can answer 429 for a while.
        busy = urllib.error.HTTPError("/api/v1/nodes/node-1", 429, "Too Many Requests", {}, None)
        self.bind(own="node-3")
        calls = []
        original = ROLLOUT.reboot
        def reboot(name, single_copy):
            calls.append(name)
            if len(calls) < 3:
                raise busy
            return original(name, single_copy)
        ROLLOUT.reboot = reboot
        ROLLOUT.start(now=1000)
        clock = 1000
        for _ in range(80):
            clock += 30
            ROLLOUT.tick(now=clock)
            self.finish_restarts()
            rollout = ROLLOUT._load().get("rollout") or {}
            if rollout.get("status") != "running":
                break
        self.assertEqual(["node-1"], self.restarts, "restarted once the API server answered")
        self.assertNotIn("needing a restart", rollout["message"])

    def tick_for(self, minutes, clock, every=30):
        end = clock + minutes * 60
        while clock < end and (ROLLOUT._load().get("rollout") or {}).get("status") == "running":
            clock += every
            ROLLOUT.tick(now=clock)
            self.finish_restarts()
        return clock

    def test_a_host_that_stops_answering_ends_the_rollout_and_lets_go_of_the_holds(self):
        # node-2 (first: the leader's node-1 goes last) goes away while its
        # updates install: every look at it fails.
        original = self.hosts.upgrade_state
        self.hosts.upgrade_state = lambda name: (_ for _ in ()).throw(OSError("node-2 is not answering")) \
            if name == "node-2" else original(name)
        ROLLOUT.start(now=1000)
        # Installing, it is given its install time (INSTALL_LIMIT), as a host-os job is.
        clock = self.tick_for(ROLLOUT.INSTALL_LIMIT / 60 - 2, 1000)
        rollout = ROLLOUT._load()["rollout"]
        self.assertEqual("running", rollout["status"], "errors are waited out for a while")
        self.assertIn("not answering", rollout["message"])
        self.tick_for(4, clock)
        rollout = ROLLOUT._load()["rollout"]
        self.assertEqual("failed", rollout["status"])
        self.assertIn("node-2", rollout["message"])
        self.assertIn("not answering", rollout["message"], "the last error is kept")
        held = {name for name, on in self.hosts.holds if on}
        self.assertTrue(held)
        self.assertEqual(held, {name for name, on in self.hosts.holds if not on},
                         "every host's automatic updates are let go of")
        self.assertIn("node-2", self.hosts.held, "the install may still run there: its own hold lapses by itself")
        self.assertEqual(["node-2"], self.hosts.began, "no other host was started")
        ROLLOUT.start(now=clock + 600)   # a new rollout is no longer refused

    def test_a_host_whose_check_keeps_failing_ends_the_rollout_after_30_min(self):
        original = self.hosts.read
        self.hosts.read = lambda name, refresh=False: (_ for _ in ()).throw(OSError("node-2 is not answering")) \
            if name == "node-2" else original(name, refresh)
        ROLLOUT.start(now=1000)
        clock = self.tick_for(ROLLOUT.ERROR_LIMIT / 60 - 2, 1000)
        self.assertEqual("running", ROLLOUT._load()["rollout"]["status"])
        clock = self.tick_for(4, clock)
        self.assertEqual("failed", ROLLOUT._load()["rollout"]["status"])
        self.assertEqual([], self.hosts.began)

    def test_a_gap_in_ticks_starts_the_count_again(self):
        original = self.hosts.read
        self.hosts.read = lambda name, refresh=False: (_ for _ in ()).throw(OSError("busy")) \
            if name == "node-2" else original(name, refresh)
        ROLLOUT.start(now=1000)
        clock = self.tick_for(5, 1000)
        clock = self.tick_for(ROLLOUT.ERROR_LIMIT / 60 - 2, clock + 3600)   # Homestead was down an hour
        self.assertEqual("running", ROLLOUT._load()["rollout"]["status"])

    def test_errors_that_clear_start_the_count_again(self):
        self.bind(own="node-3")
        broken = {"on": True}
        original = self.hosts.read
        self.hosts.read = lambda name, refresh=False: (_ for _ in ()).throw(OSError("busy")) \
            if broken["on"] else original(name, refresh)
        ROLLOUT.start(now=1000)
        clock = self.tick_for(ROLLOUT.ERROR_LIMIT / 60 - 5, 1000)
        broken["on"] = False
        clock = self.tick_for(0.5, clock)                     # one round: node-1 moves on
        broken["on"] = True
        clock = self.tick_for(ROLLOUT.ERROR_LIMIT / 60 - 5, clock)
        self.assertEqual("running", ROLLOUT._load()["rollout"]["status"])
        broken["on"] = False
        self.assertEqual("succeeded", self.tick_for(30, clock) and ROLLOUT._load()["rollout"]["status"])

    def test_a_restart_a_drained_leader_left_running_is_taken_up_not_asked_again(self):
        # The leader ran node-1's drain and was itself drained off; the job it
        # started carries on elsewhere. The new leader adopts it.
        self.bind(own="node-3")
        self.ops["op-left"] = {"id": "op-left", "status": "running"}
        ROLLOUT.power_job = lambda name, since: "op-left" if name == "node-1" else ""
        ROLLOUT.start(now=1000)
        done = self.run_until_done(on_step=self.finish_restarts)
        self.assertEqual([], self.restarts, "no second restart was asked for")
        self.assertIn("updates installed and restarted", {r["node"]: r["note"] for r in done["results"]}["node-1"])

    def test_a_host_still_holding_the_only_copy_after_the_wait_is_left_for_someone(self):
        self.refuse["node-1"] = "a volume has its only healthy copy on this host (the settings do not accept that)"
        self.bind(own="node-3")
        ROLLOUT.start(now=1000)
        clock = 1000
        for _ in range(200):
            clock += 30
            ROLLOUT.tick(now=clock)
            self.finish_restarts()
            rollout = ROLLOUT._load().get("rollout") or {}
            if rollout.get("status") != "running":
                break
        self.assertIn("needing a restart: node-1", rollout["message"])
        self.assertNotIn("node-1", self.restarts)

    def test_a_host_cordoned_before_stays_cordoned(self):
        self.nodes["node-1"] = node("node-1", cordoned=True)
        self.bind(own="node-3")
        ROLLOUT.start(now=1000)
        self.run_until_done(on_step=self.finish_restarts)
        self.assertEqual(["node-1"], self.restarts)
        self.assertNotIn(("node-1", False), self.cordons)

    def test_an_unconfirmed_launch_is_read_only_by_its_own_id(self):
        def begin(name, facts=None, ref=None):
            self.hosts.began.append(name)
            ref.update(lease=f"lease-{name}", run="run-1", unconfirmed=60)
            self.hosts.held.add(name)
        self.hosts.upgrade_begin = begin
        ROLLOUT.start(now=1000)
        self.run_until_done(on_step=self.finish_restarts)
        asked = {run for name, run in self.hosts.asked_run}
        self.assertEqual({"run-1"}, asked, "every unconfirmed launch's result must carry its own id")

    def test_an_unconfirmed_launch_does_not_show_an_old_log_line_as_progress(self):
        def begin(name, facts=None, ref=None):
            self.hosts.began.append(name)
            ref.update(lease=f"lease-{name}", run="run-1", unconfirmed=60)
            self.hosts.held.add(name)
        self.hosts.upgrade_begin = begin
        self.hosts.upgrade_state = lambda name, run=None: {"running": True, "code": None, "active": False,
                                                           "last": "Setting up curl (an earlier install)"}
        ROLLOUT.start(now=1000)
        for _ in range(6):
            ROLLOUT.tick(now=1000)
        message = (ROLLOUT._load().get("rollout") or {}).get("message", "")
        self.assertNotIn("an earlier install", message)
        self.assertIn("needs attention", message)
        self.assertIn("cannot tell whether", message)

    def test_a_failed_install_stops_before_the_next_host(self):
        self.hosts.codes["node-2"] = "100"
        ROLLOUT.start(now=1000)
        done = self.run_until_done()
        self.assertEqual("failed", done["status"])
        self.assertEqual(["node-2"], self.hosts.began, "nothing after the host that failed")
        self.assertIn("code 100", done["message"])

    def test_a_restart_cut_off_before_power_was_sent_is_tried_once_more(self):
        self.bind(own="node-3")
        ROLLOUT.start(now=1000)

        def interrupt_first():
            ops = list(self.ops.values())
            if len(ops) == 1:
                ops[0].update(status="failed", message="Maintenance stopped before power was sent; inspect the host")
            elif len(ops) == 2:
                ops[1]["status"] = "succeeded"
        done = self.run_until_done(on_step=interrupt_first)
        self.assertEqual(["node-1", "node-1"], self.restarts)
        self.assertEqual("succeeded", done["status"])

    def test_ubuntus_own_updates_are_held_off_while_it_runs_and_let_go_after(self):
        ROLLOUT.start(now=1000)
        self.run_until_done(on_step=self.finish_restarts)
        held = {name for name, on in self.hosts.holds if on}
        released = {name for name, on in self.hosts.holds if not on}
        self.assertEqual({"node-1", "node-2", "node-3"}, held)
        self.assertEqual({"node-1", "node-2", "node-3"}, released)

    def test_when_homestead_manages_updates_the_hold_stays(self):
        ROLLOUT.save_settings({"manage": "homestead"})
        ROLLOUT.tick(now=1000)
        self.assertEqual({("node-1", True), ("node-2", True), ("node-3", True)}, set(self.hosts.holds))
        ROLLOUT.save_settings({"manage": "ubuntu"})
        ROLLOUT.tick(now=1000)
        self.assertIn(("node-2", False), self.hosts.holds)

    def test_only_one_rollout_at_a_time_and_harvester_is_left_alone(self):
        ROLLOUT.start(now=1000)
        with self.assertRaisesRegex(ValueError, "already running"):
            ROLLOUT.start(now=1000)
        ROLLOUT.bind(lambda path: {"items": []}, lambda force=False: {"harvester": True, "distribution": "rke2"},
                     self.hosts, None, None, None, lambda: "", self.data)
        with self.assertRaisesRegex(ValueError, "Harvester"):
            ROLLOUT.start(now=1000)

    def test_stopping_finishes_the_host_in_hand_first(self):
        ROLLOUT.start(now=1000)
        ROLLOUT.tick(now=1000)          # hold, then node-2 checked and installing
        ROLLOUT.stop()
        done = self.run_until_done(on_step=self.finish_restarts)
        self.assertEqual("stopped", done["status"])
        self.assertEqual(["node-2"], [r["node"] for r in done["results"]])


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        keep_bound(self, ROLLOUT)
        self.data = tempfile.mkdtemp()
        ROLLOUT.bind(None, None, None, None, None, None, lambda: "", self.data)

    def at(self, text):
        return datetime.datetime.fromisoformat(text).replace(tzinfo=datetime.timezone.utc).timestamp()

    def test_the_window_opens_on_its_day_and_hour_in_its_own_clock_once(self):
        ROLLOUT.save_settings({"schedule": {"enabled": True, "days": ["sun"], "hour": 3, "offset_min": 60}})
        state = ROLLOUT._load()
        # 2026-10-04 is a Sunday: 02:30 UTC is 03:30 at UTC+1.
        self.assertTrue(ROLLOUT.due(state, self.at("2026-10-04T02:30:00")))
        self.assertFalse(ROLLOUT.due(state, self.at("2026-10-04T03:30:00")), "04:30 there")
        self.assertFalse(ROLLOUT.due(state, self.at("2026-10-05T02:30:00")), "a Monday")
        state["last_scheduled"] = self.at("2026-10-04T02:05:00")
        self.assertFalse(ROLLOUT.due(state, self.at("2026-10-04T02:40:00")), "once per window")

    def test_settings_are_checked(self):
        for bad, words in (({"schedule": {"enabled": True, "days": []}}, "at least one day"),
                           ({"schedule": {"hour": 24}}, "0 to 23"), ({"reboot": "always"}, "when-needed"),
                           ({"manage": "nobody"}, "homestead or ubuntu")):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words):
                ROLLOUT.save_settings(bad)
        self.assertEqual("ubuntu", ROLLOUT.settings()["manage"], "the defaults leave Ubuntu's own updates alone")


if __name__ == "__main__":
    unittest.main()
