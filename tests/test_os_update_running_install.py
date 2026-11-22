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
        self.installing, self.refreshed = set(), []

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
        self.enterContext(Kube(*map(node, NAMES)))
        self.enterContext(mock.patch.object(CANARY, "pick", return_value=None))
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


if __name__ == "__main__":
    unittest.main()
