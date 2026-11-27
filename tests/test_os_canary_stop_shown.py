"""A host OS update its canary's check stopped, as a person sees it (#1527)."""
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


def node(name):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name}, "spec": {},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


def pod(name, on):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "apps"},
            "spec": {"nodeName": on}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


class CanaryStopShown(unittest.TestCase):
    """Three hosts, Homestead's leader on node-a: node-b has the fewest pods and
    goes first as the canary; a degraded alert raised in its soak stops the run."""

    def setUp(self):
        keep_bound(self, ROLLOUT)
        keep_bound(self, CANARY)
        keep_bound(self, ALERTS)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = tmp.name
        self.enterContext(mock.patch.object(HISTORY, "DATA_DIR", self.data))
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", self.data))
        self.kube = self.enterContext(Kube(*map(node, NAMES), pod("web-1", "node-a"), pod("web-2", "node-a"),
                                           pod("db-1", "node-c")))
        ALERTS.bind(self.data)
        CANARY.alerts = None          # the real alert store (homestead_alerts.active)
        self.hosts = Hosts({"node-a": facts(["curl"]), "node-b": facts(["curl"]), "node-c": facts(["curl"])})
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

    def stopped_by_the_canary(self, then=lambda later: None):
        """The run, stopped by an alert raised two hours into the canary's soak."""
        ALERTS.observe({"jobs": [], "os-updates": []}, T - 60)   # sources watched long before the run
        ROLLOUT.start(now=T)
        rollout = self.tick_until(T, lambda r: r.get("phase") == "canary")
        self.assertEqual(("node-b", ["node-b", "node-c", "node-a"]), (rollout["canary"], rollout["nodes"]))
        later = T + 2 * 3600
        fact = {"key": "address:192.0.2.50", "category": "degraded", "severity": "degraded",
                "title": "Service address 192.0.2.50 has no route", "body": "-"}
        ALERTS.observe({"addresses": []}, later - 120)
        ALERTS.observe({"addresses": [fact]}, later - 90)
        ALERTS.observe({"addresses": [fact]}, later)            # held a minute: raised
        rollout = self.tick_until(later, lambda r: r.get("status") != "running")
        then(later)
        OPS.list_operations()                                    # the jobs loop reads the run's state
        job = next(j for j in OPS.snapshot() if j["kind"] == "os-rollout" and j["status"] != "running")
        return rollout, job, later

    def look(self, now):
        """One look of the alert loop's own sources (homestead_overview_server), the two
        this is about kept: a source the loop does not take answers None, held."""
        # The other sources read what they read and fail: no request leaves the test (a release check reads GitHub).
        with mock.patch("builtins.print"), mock.patch.object(FETCH, "get", side_effect=OSError("no network here")):
            answers = OV._alert_sources()
        return ALERTS.observe({name: answers.get(name) for name in ("jobs", "os-updates")}, now)

    def test_the_stop_shows_what_happened_to_which_host_not_a_failure_of_every_host(self):
        rollout, job, _ = self.stopped_by_the_canary()
        self.assertEqual(["node-b"], self.hosts.began, "only the canary was touched")
        self.assertEqual([("node-b", True, "updates installed")],
                         [(r["node"], r["ok"], r["note"]) for r in rollout["results"]])
        self.assertEqual("stopped", rollout["status"], "the run's own record, shown under Last run")
        # A stopped run's job state, as a person's Stop gives it: not "failed", not a full bar.
        self.assertEqual(("cancelled", 33, "Update every host's OS (3 hosts)"), (job["status"], job["progress"], job["title"]))
        self.assertEqual("node-b was updated first, then its check stopped the update. An alert was raised while the canary "
                         "soaked: Service address 192.0.2.50 has no route (see the Timeline). The other hosts were left as they are", job["message"])

    def test_a_job_read_after_a_new_run_began_keeps_the_share_it_reached(self):
        # The jobs loop had not read the stopped run before a new one replaced it.
        _, job, _ = self.stopped_by_the_canary(then=lambda later: ROLLOUT.start(now=later + 60))
        self.assertEqual(("cancelled", 33), (job["status"], job["progress"]))

    def test_the_stop_raises_an_alert_with_the_reason_and_a_link_to_the_job(self):
        _, job, later = self.stopped_by_the_canary()
        self.assertEqual([], [e["title"] for e in self.look(later + 20)], "nothing at once: no 'Job failed', and a condition is held a minute")
        raised = self.look(later + 90)
        self.assertEqual([("raised", "degraded", "degraded", "An OS update stopped after its first host", f"/?job={job['id']}")],
                         [(e["phase"], e["category"], e["severity"], e["title"], e["href"]) for e in raised],
                         "this alert, and no 'Job failed' one")
        self.assertIn("Service address 192.0.2.50 has no route", raised[0]["body"])

    def test_dismissing_the_job_clears_the_alert(self):
        _, job, later = self.stopped_by_the_canary()
        self.look(later + 20)
        self.look(later + 90)
        self.assertTrue(any(r["key"].startswith("os-update-stopped:") for r in ALERTS.active()), "raised before the dismiss")
        OPS.dismiss(job["id"])
        self.look(later + 120)
        resolved = self.look(later + 200)
        self.assertEqual([("resolved", "The stopped OS update no longer needs attention")],
                         [(e["phase"], e["title"]) for e in resolved])

    def test_a_new_run_clears_the_alert_and_its_canary_is_not_stopped_by_it(self):
        _, _, later = self.stopped_by_the_canary()
        self.look(later + 20)
        self.look(later + 90)
        again = later + 3600
        ROLLOUT.start(now=again)
        rollout = self.tick_until(again, lambda r: r.get("phase") == "canary")
        since = rollout["soak"]["since"]
        self.assertTrue(any(r["key"].startswith("os-update-stopped:") for r in ALERTS.active()), "open until a look misses it")
        self.look(since + 10)
        self.assertEqual([("resolved", "The stopped OS update no longer needs attention")],
                         [(e["phase"], e["title"]) for e in self.look(since + 80)], "gone once the new run began")
        self.look(since + 3600)
        rollout = self.tick_until(since + CANARY.SOAK, lambda r: r.get("canary_passed") or r.get("status") != "running")
        self.assertEqual(("running", True), (rollout["status"], rollout.get("canary_passed")), rollout.get("message"))


if __name__ == "__main__":
    unittest.main()
