"""A host OS update whose install does not finish in time, as a person sees it (#1530)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_alerts as ALERTS
import homestead_fetch as FETCH
import homestead_history as HISTORY
import homestead_kube as K
import homestead_operations as OPS
import homestead_os_canary as CANARY
import homestead_os_rollout as ROLLOUT
import homestead_overview_server as OV
from fakes.kube import Kube
from rebind import keep_bound
from test_os_rollout import Hosts, facts

T = 1_800_000_000
NAMES = ("node-a", "node-b", "node-c")
TITLE = "Update every host's OS (3 hosts)"


def node(name):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name}, "spec": {},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


def pod(name, on):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "apps"},
            "spec": {"nodeName": on}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


class InstallTimeout(unittest.TestCase):
    """Three hosts, Homestead's leader on node-a, so it goes last; node-b has the
    fewest pods, so it goes first (as the canary when one is picked)."""

    def setUp(self):
        keep_bound(self, ROLLOUT)
        keep_bound(self, CANARY)
        keep_bound(self, ALERTS)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = tmp.name
        self.enterContext(mock.patch.object(HISTORY, "DATA_DIR", self.data))
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.data))
        self.kube = self.enterContext(Kube(*map(node, NAMES), pod("web-1", "node-a"), pod("web-2", "node-a"), pod("db-1", "node-c")))
        ALERTS.bind(self.data)
        CANARY.alerts = None
        self.hosts = Hosts({name: facts(["curl"]) for name in NAMES})
        ROLLOUT.bind(K.kget, lambda force=False: {"distribution": "k3s"}, self.hosts, None, lambda op: None,
                     lambda name, on: None, lambda: "node-a", self.data,
                     # As server.py binds it: the rollout's job in the job store.
                     lambda rollout: OPS.start("os-rollout", ROLLOUT.title(rollout),
                                               {"kind": "Node", "name": ", ".join(rollout["nodes"])[:200]}, "/nodes",
                                               {"rollout": rollout["id"]}, "Starting"))
        self.enterContext(mock.patch.dict(OPS.RESOLVERS, {"os-rollout": ROLLOUT.status}))

    def tick_until(self, now, done):
        for _ in range(30):
            ROLLOUT.tick(now=now)
            rollout = ROLLOUT._load().get("rollout") or {}
            if done(rollout):
                return rollout
        self.fail(f"not reached: {rollout.get('phase')} {rollout.get('message')}")

    def timed_out(self, name):
        """The run, with name's install still going two hours and a minute after it began."""
        self.hosts.codes[name] = None            # no exit code written: still installing
        ALERTS.observe({"jobs": [], "os-updates": []}, T - 60)
        ROLLOUT.start(now=T)
        self.tick_until(T, lambda r: r.get("phase") == "installing" and r["node"].get("name") == name)
        later = T + ROLLOUT.INSTALL_LIMIT + 60
        rollout = self.tick_until(later, lambda r: r.get("status") != "running")
        OPS.list_operations()                    # the jobs loop reads the run's state
        job = next(j for j in OPS.snapshot() if j["kind"] == "os-rollout")
        return rollout, job, later

    def raised(self, later):
        """What the alert loop raises from its own sources in the first two minutes."""
        titles = []
        for now in (later + 20, later + 90):
            with mock.patch("builtins.print"), mock.patch.object(FETCH, "get", side_effect=OSError("no network here")):
                answers = OV._alert_sources()
            titles += [e["title"] for e in ALERTS.observe({k: answers.get(k) for k in ("jobs", "os-updates")}, now)
                       if e["phase"] == "raised"]
        return titles

    def test_the_last_host_timing_out_fails_the_run_and_is_not_counted_done(self):
        with mock.patch.object(CANARY, "pick", return_value=None):
            rollout, job, later = self.timed_out("node-a")
        self.assertEqual(["node-b", "node-c", "node-a"], rollout["nodes"])
        self.assertEqual("failed", rollout["status"], "the run's own record, shown under Last run")
        self.assertEqual(("failed", 100, TITLE), (job["status"], job["progress"], job["title"]), "under Needs attention")
        self.assertEqual("2 hosts done; 0 restarted; node-a: updates did not finish within two hours. "
                         f"See {Hosts.LOG} there", job["message"])
        self.assertEqual([f"Job failed: {TITLE}"], self.raised(later))

    def test_the_first_host_timing_out_fails_the_run_not_stops_it(self):
        rollout, job, later = self.timed_out("node-b")
        self.assertEqual("node-b", rollout["canary"])
        self.assertEqual(["node-b"], self.hosts.began, "nothing after the host that did not finish")
        self.assertEqual("failed", rollout["status"])
        self.assertEqual(("failed", 100), (job["status"], job["progress"]), "not cancelled, as a Stop is")
        self.assertEqual("0 hosts done; 0 restarted; node-b: updates did not finish within two hours. "
                         f"See {Hosts.LOG} there", job["message"])
        self.assertEqual([f"Job failed: {TITLE}"], self.raised(later), "not the canary's stop alert")

    def test_a_middle_host_timing_out_fails_the_run_and_leaves_the_rest(self):
        with mock.patch.object(CANARY, "pick", return_value=None):
            rollout, job, later = self.timed_out("node-c")
        self.assertEqual(["node-b", "node-c"], self.hosts.began, "node-a, after it, is not touched")
        self.assertEqual(("failed", "failed", 100), (rollout["status"], job["status"], job["progress"]))
        self.assertEqual("1 host done; 0 restarted; node-c: updates did not finish within two hours. "
                         f"See {Hosts.LOG} there", job["message"])
        self.assertEqual([f"Job failed: {TITLE}"], self.raised(later))

    def test_a_host_left_alone_is_not_counted_done(self):
        # Not Ready by its turn: left alone, and the run goes on (not a failure).
        with mock.patch.object(CANARY, "pick", return_value=None):
            ROLLOUT.start(now=T)
            down = node("node-c")
            down["status"]["conditions"][0]["status"] = "False"
            self.kube.add(down)
            rollout = self.tick_until(T, lambda r: r.get("status") != "running")
        OPS.list_operations()
        job = next(j for j in OPS.snapshot() if j["kind"] == "os-rollout")
        self.assertEqual(["node-b", "node-a"], self.hosts.began)
        self.assertEqual(("succeeded", "succeeded"), (rollout["status"], job["status"]))
        self.assertEqual("2 hosts done; 0 restarted; node-c: not Ready, so left alone", job["message"])


if __name__ == "__main__":
    unittest.main()
