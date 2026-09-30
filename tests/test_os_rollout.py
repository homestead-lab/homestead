import datetime
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_os_rollout as ROLLOUT


def node(name, ready=True, cordoned=False):
    return {"metadata": {"name": name}, "spec": {"unschedulable": cordoned},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class Hosts:
    """Each host's OS as homestead_host_os would say, and what was done to it."""
    LOG = "/var/log/homestead-os-upgrade.log"

    def __init__(self, facts):
        self.facts, self.began, self.holds, self.codes = facts, [], [], {}

    def read(self, name, refresh=False):
        return self.facts[name]

    def report(self, name=None):
        return {"hosts": {k: v for k, v in self.facts.items() if name is None or k == name}}

    def stored(self, name):
        return self.facts.get(name)

    def upgrade_begin(self, name, facts=None):
        self.began.append(name)

    def upgrade_state(self, name):
        code = self.codes.get(name, "0")
        if code == "0":
            # Installed: nothing waits any more; a kernel asks for a restart.
            kernel = any(u["name"].startswith("linux-image") for u in self.facts[name]["updates"])
            self.facts[name] = {**self.facts[name], "updates": [], "security": 0, "reboot": kernel}
        return {"running": code is None, "code": code, "last": ""}

    def set_hold(self, name, hold):
        self.holds.append((name, hold))
        self.facts[name]["auto"]["held"] = hold


def facts(updates=(), reboot=False, auto=True):
    return {"updates": [{"name": u, "security": False} for u in updates], "security": 0, "reboot": reboot,
            "auto": {"tool": "unattended-upgrades" if auto else "", "on": auto, "reboots": False, "held": False}}


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.nodes = {"node-1": node("node-1"), "node-2": node("node-2"), "node-3": node("node-3")}
        self.hosts = Hosts({"node-1": facts(["curl", "linux-image-6.8"]), "node-2": facts(["curl"]), "node-3": facts()})
        self.restarts, self.cordons, self.ops, self.refuse = [], [], {}, {}
        self.bind()

    def bind(self, own="node-1"):
        def reboot(name, single_copy):
            if name in self.refuse:
                raise ValueError(self.refuse[name])
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

    def test_a_host_the_review_will_not_restart_keeps_its_updates_and_the_rollout_goes_on(self):
        self.refuse["node-1"] = "Running VMs are on this host; migrate or stop them and review again"
        self.bind(own="node-3")
        done = self.run_until_done() if ROLLOUT.start(now=1000) else None
        self.assertEqual("succeeded", done["status"])
        self.assertIn("needing a restart: node-1", done["message"])
        self.assertEqual({"node-1", "node-2"}, set(self.hosts.began))

    def test_a_host_cordoned_before_stays_cordoned(self):
        self.nodes["node-1"] = node("node-1", cordoned=True)
        self.bind(own="node-3")
        ROLLOUT.start(now=1000)
        self.run_until_done(on_step=self.finish_restarts)
        self.assertEqual(["node-1"], self.restarts)
        self.assertNotIn(("node-1", False), self.cordons)

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
        with self.assertRaisesRegex(ValueError, "running already"):
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
