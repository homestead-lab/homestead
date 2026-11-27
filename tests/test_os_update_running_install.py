"""A host whose install may still be running after its OS update ended (#1540):
its automatic updates stay held, and a new OS update does not start it."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_history as HISTORY
import homestead_kube as K
import homestead_operations as OPS
import homestead_os_canary as CANARY
import homestead_os_rollout as ROLLOUT
from fakes.kube import Kube
from rebind import keep_bound
from test_os_rollout import Hosts, facts

T = 1_800_000_000
NAMES = ("node-a", "node-b", "node-c")


def node(name):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name}, "spec": {},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


class Installing(Hosts):
    """The rollout tests' host double, with what the real host reports while its
    install unit is active: the read's "upgrading", upgrade_state's "active", and
    upgrade_begin's refusal (homestead_host_os)."""

    def __init__(self, facts_by_host):
        super().__init__(facts_by_host)
        self.installing, self.refreshed, self.looked = set(), [], []
        # What upgrade_state raises per host: the real one runs a command there through hostrun.
        self.state_errors = {}
        self.during_look = lambda name: None

    def read(self, name, refresh=False):
        if refresh:
            self.refreshed.append(name)
        return {**self.facts[name], "upgrading": name in self.installing}

    def upgrade_begin(self, name, facts=None, ref=None):
        if (facts or self.read(name)).get("upgrading"):
            raise ValueError(f"{name} is installing updates already")
        super().upgrade_begin(name, facts, ref)
        if self.codes.get(name, "0") is None:
            self.installing.add(name)        # an install that does not end by itself

    def upgrade_state(self, name, run=None):
        self.looked.append(name)
        self.during_look(name)
        if name in self.state_errors:
            raise self.state_errors[name]
        return {**super().upgrade_state(name, run), "active": name in self.installing}

    def install_ends(self, name):
        self.installing.discard(name)
        self.codes[name] = "0"


class RunningInstall(unittest.TestCase):
    """Three hosts, Ubuntu managing updates, Homestead's leader on node-a (last),
    no canary: node-b, node-c, node-a."""

    def setUp(self):
        keep_bound(self, ROLLOUT)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = tmp.name
        self.enterContext(mock.patch.object(HISTORY, "DATA_DIR", self.data))
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.data))
        self.kube = self.enterContext(Kube(*map(node, NAMES)))
        self.pick = self.enterContext(mock.patch.object(CANARY, "pick", return_value=None))
        self.enterContext(mock.patch("builtins.print"))
        self.hosts = Installing({name: facts(["curl"]) for name in NAMES})
        ROLLOUT.bind(K.kget, lambda force=False: {"distribution": "k3s"}, self.hosts, None, lambda op: None,
                     lambda name, on: None, lambda: "node-a", self.data)

    def tick_until(self, now, done):
        for _ in range(30):
            ROLLOUT.tick(now=now)
            rollout = ROLLOUT._load().get("rollout") or {}
            if done(rollout):
                return rollout
        self.fail(f"not reached: {rollout.get('phase')} {rollout.get('message')}")

    def node_c_timed_out(self):
        """A run whose install on node-c is still going two hours and a minute after it began."""
        self.hosts.codes["node-c"] = None
        ROLLOUT.start(now=T)
        self.tick_until(T, lambda r: r.get("phase") == "installing" and r["node"].get("name") == "node-c")
        later = T + ROLLOUT.INSTALL_LIMIT + 60
        rollout = self.tick_until(later, lambda r: r.get("status") != "running")
        self.assertEqual("failed", rollout["status"])
        self.assertIn("node-c", self.hosts.installing, "its install goes on")
        return later

    def released(self):
        return {name for name, on in self.hosts.holds if not on}

    def test_a_host_still_installing_keeps_its_automatic_updates_held_until_its_install_ends(self):
        later = self.node_c_timed_out()
        for at in range(later, later + 600, 20):     # idle ticks, ten minutes
            ROLLOUT.tick(now=at)
        self.assertEqual({"node-a", "node-b"}, self.released(), "the other hosts are let go")
        self.assertTrue(self.hosts.facts["node-c"]["auto"]["held"], "node-c's install may still be running")
        self.hosts.install_ends("node-c")
        for at in range(later + 600, later + 900, 20):     # five minutes more
            ROLLOUT.tick(now=at)
        self.assertIn("node-c", self.released(), "let go once its install has ended")
        self.assertFalse(self.hosts.facts["node-c"]["auto"]["held"])

    def test_a_new_run_does_not_start_a_host_whose_install_may_still_be_running(self):
        later = self.node_c_timed_out()
        ROLLOUT.tick(now=later + 20)
        with self.assertRaises(ValueError) as refused:
            ROLLOUT.start(now=later + 60)
        words = str(refused.exception)
        self.assertIn("node-c may still be installing updates", words)
        self.assertIn(Hosts.LOG, words, "where to look")
        self.assertEqual("failed", ROLLOUT._load()["rollout"]["status"], "no new run was saved")
        review = ROLLOUT.preview(["node-b", "node-c"])
        self.assertIn("node-c may still be installing updates", review["blocked"], "the review says so before anyone agrees")
        self.assertEqual("", ROLLOUT.preview(["node-b"])["blocked"], "the other hosts can still be updated")
        self.hosts.install_ends("node-c")
        for at in range(later + 40, later + 340, 20):     # five minutes of idle ticks
            ROLLOUT.tick(now=at)
        self.assertEqual(["node-b", "node-c", "node-a"], ROLLOUT.start(now=later + 360)["nodes"],
                         "once the install has ended, a new run starts it")

    def test_a_host_found_installing_at_its_turn_is_left_alone_and_not_waited_on(self):
        # An install Homestead did not start, or one it stopped following (a host-os job forgotten).
        self.hosts.installing.add("node-c")
        ROLLOUT.start(now=T)
        rollout = self.tick_until(T, lambda r: r.get("status") != "running")
        notes = {r["node"]: (r["ok"], r["note"]) for r in rollout["results"]}
        self.assertEqual((False, f"an earlier install is still running, so left alone. See {Hosts.LOG} there"),
                         notes["node-c"])
        self.assertNotIn("no answer", rollout["message"])
        self.assertEqual(1, self.hosts.refreshed.count("node-c"), "its package lists are refreshed once, not on every look")
        self.assertEqual(["node-b", "node-a"], self.hosts.began, "the run goes on to the next host")
        self.assertNotIn("node-c", self.released(), "and its automatic updates stay held")
        with self.assertRaisesRegex(ValueError, "node-c may still be installing updates"):
            ROLLOUT.start(now=T + 60)

    def node_c_not_answering_past_its_install_time(self):
        """node-c's install began, then node-c stopped answering for over two hours."""
        self.hosts.codes["node-c"] = None
        ROLLOUT.start(now=T)
        self.tick_until(T, lambda r: r.get("phase") == "installing" and r["node"].get("name") == "node-c")
        self.hosts.state_errors["node-c"] = OSError("node-c is not answering")
        at = T
        while (ROLLOUT._load().get("rollout") or {}).get("status") == "running":
            at += 60
            self.assertLess(at, T + ROLLOUT.INSTALL_LIMIT + 3600, "it never ended")
            ROLLOUT.tick(now=at)
        self.assertIn("no answer for 120 min", ROLLOUT._load()["rollout"]["message"])
        return at

    def test_a_host_that_stopped_answering_while_installing_stays_held_and_refused(self):
        at = self.node_c_not_answering_past_its_install_time()
        for later in range(at, at + 600, 20):
            ROLLOUT.tick(now=later)
        self.assertEqual({"node-a", "node-b"}, self.released(), "its install was started, so it may still run")
        self.assertTrue(self.hosts.facts["node-c"]["auto"]["held"])
        with self.assertRaisesRegex(ValueError, "node-c may still be installing updates"):
            ROLLOUT.start(now=at + 660)

    def test_a_recorded_host_whose_look_fails_stays_held_and_says_how_to_go_on(self):
        later = self.node_c_timed_out()
        self.hosts.state_errors["node-c"] = OSError("node-c is not answering")
        self.hosts.install_ends("node-c")
        for at in range(later, later + 600, 20):
            ROLLOUT.tick(now=at)
        self.assertGreater(self.hosts.looked.count("node-c"), 1, "it is looked at again")
        self.assertNotIn("node-c", self.released(), "an unanswered look is no proof the install ended")
        blocked = ROLLOUT.preview(["node-c"])["blocked"]
        self.assertIn("cannot reach it", blocked)
        self.assertIn("Make sure node-c answers", blocked, "the way on")

    def set_ready(self, name, ready):
        item = node(name)
        item["status"]["conditions"][0]["status"] = "True" if ready else "False"
        self.kube.add(item)

    def test_a_recorded_host_not_ready_is_not_looked_at_until_it_is_ready_again(self):
        later = self.node_c_timed_out()
        self.set_ready("node-c", False)
        # Before any look: the review's row for node-c says what is true of a host not Ready.
        busy = ROLLOUT.preview(["node-c"])["hosts"][0]["busy"]
        self.assertIn("once node-c is Ready and no OS update runs", busy)
        self.assertNotIn("about once a minute", busy, "no promise it cannot keep")
        looked = self.hosts.looked.count("node-c")
        for at in range(later, later + 600, 20):
            ROLLOUT.tick(now=at)
        self.assertEqual(looked, self.hosts.looked.count("node-c"), "no command on a host that is not Ready")
        busy = ROLLOUT.preview(["node-c"])["hosts"][0]["busy"]
        self.assertIn("once node-c is Ready and no OS update runs", busy)
        self.assertNotIn("about once a minute", busy)
        self.hosts.install_ends("node-c")
        self.set_ready("node-c", True)
        for at in range(later + 600, later + 900, 20):
            ROLLOUT.tick(now=at)
        self.assertGreater(self.hosts.looked.count("node-c"), looked, "looked at again once Ready")
        self.assertIn("node-c", self.released())

    def test_a_refusal_whose_node_read_fails_still_says_the_host_may_be_installing(self):
        self.node_c_timed_out()
        self.set_ready("node-c", False)
        # The review's own read of the hosts works; the refusal's read of them fails.
        self.kube.fail("/api/v1/nodes", OSError("the API server is busy"), skip=1)
        busy = ROLLOUT.preview(["node-c"])["hosts"][0]["busy"]
        self.assertTrue(busy.startswith("node-c may still be installing updates from an earlier OS update."), busy)
        self.assertIn("about once a minute", busy, "without the read, the text the record gives")

    def test_a_host_that_does_not_answer_is_looked_at_less_and_less_often(self):
        later = self.node_c_timed_out()
        self.hosts.state_errors["node-c"] = OSError("node-c is not answering")
        looked = self.hosts.looked.count("node-c")
        for at in range(later, later + 3600, 20):     # an hour of idle ticks
            ROLLOUT.tick(now=at)
        looks = self.hosts.looked.count("node-c") - looked
        # Waits of 1, 2, 4, 8, then 15 minutes: seven looks in the hour, not sixty.
        self.assertLessEqual(looks, 8, "each failed look doubles the wait, up to 15 minutes")
        self.assertGreaterEqual(looks, 5, "but it keeps looking")
        busy = ROLLOUT.preview(["node-c"])["hosts"][0]["busy"]
        self.assertIn("Make sure node-c answers", busy)
        self.assertIn("within 15 minutes", busy)
        self.assertNotIn("once a minute", busy, "no promise it cannot keep")
        del self.hosts.state_errors["node-c"]
        self.hosts.install_ends("node-c")
        for at in range(later + 3600, later + 3600 + 15 * 60 + 40, 20):
            ROLLOUT.tick(now=at)
        self.assertIn("node-c", self.released(), "the first good look still clears it")
        self.assertNotIn("node-c", ROLLOUT._load().get("installing") or {})

    def test_a_host_recorded_again_during_a_look_keeps_its_new_record(self):
        later = self.node_c_timed_out()
        self.hosts.install_ends("node-c")

        def recorded_again(name):
            # Another replica's run recorded node-c anew while this look was out.
            state = ROLLOUT._load()
            state["installing"]["node-c"] = {"since": later + 30, "run": "os-other"}
            ROLLOUT._save(state)
            self.hosts.during_look = lambda name: None
        self.hosts.during_look = recorded_again
        ROLLOUT.tick(now=later + 20)
        self.assertIn("node-c", ROLLOUT._load().get("installing") or {}, "the newer record is not cleared by an older look")

    def test_a_canary_found_installing_is_left_alone_and_the_others_get_no_soak(self):
        # As for a canary not Ready by its turn: the others go on without a day of watching it.
        self.pick.return_value = "node-c"
        self.hosts.installing.add("node-c")
        rollout = ROLLOUT.start(now=T)
        self.assertEqual("node-c", rollout["nodes"][0])
        phases = []
        for _ in range(30):
            ROLLOUT.tick(now=T)
            rollout = ROLLOUT._load()["rollout"]
            phases.append(rollout.get("phase"))
            if rollout.get("status") != "running":
                break
        self.assertNotIn("canary", phases)
        self.assertEqual(["node-b", "node-a"], self.hosts.began)
        self.assertEqual((False, f"an earlier install is still running, so left alone. See {Hosts.LOG} there"),
                         (rollout["results"][0]["ok"], rollout["results"][0]["note"]))


if __name__ == "__main__":
    unittest.main()
