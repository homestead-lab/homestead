"""OS updates for the hosts a person chooses (#1174), and what stops a restart
nobody is watching.

A row or a selection runs the same engine as an update of every host: only the
named hosts, in the same order, with this run's own answer on restarts, and
automatic updates held on every Ready host until it ends. A read-only preview
says, per host, what the restart checks would say today. In any unattended
restart a drain blocker refuses it and is named; the host waits for a person.

Reads go through the real kget, the real review (homestead_power.plan) and the
real job store over the shared fake API server (tests/fakes/kube.py). The host
itself is the one stand-in, as nothing here can run a package manager."""
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import homestead_alerts as ALERTS
import homestead_drain_blockers as BLOCKERS
import homestead_history as HISTORY
import homestead_kube as K
import homestead_leader as LEADER
import homestead_os_rollout as ROLLOUT
import homestead_power as power
import homestead_power_jobs as POWER_JOBS
from fakes.kube import Kube
from test_drain_blockers import attachment, claim, engine, engine_orphan, instance_manager, pod
from rebind import keep_bound
from test_os_rollout import Hosts, facts as host_facts


def host(name, ready=True):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "uid": f"uid-{name}"},
            "spec": {},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "nodeInfo": {"bootID": f"boot-{name}"}}}


def orphans_on_node_a(count=3):
    """Engine orphans on node-a whose engines now run on node-b."""
    volumes = [f"vol-{'abcdefghijklmnopqrstuvwxyz'[n]}" for n in range(count)]
    return ([engine_orphan(volume, n) for n, volume in enumerate(volumes, 1)]
            + [engine(volume, "node-b") for volume in volumes]
            + [instance_manager("node-a", engines=[f"{v}-e-0" for v in volumes])])


def stuck_pod_on_node_a():
    return [pod(deleting_at=time.time() - 3600), claim(), attachment()]


class FakeCluster(unittest.TestCase):
    """Three hosts on the fake, the real review, and a stand-in for sending
    power: what would be restarted is recorded, nothing else is stubbed."""
    def setUp(self):
        self.kube = Kube(host("node-a"), host("node-b"), host("node-c"))
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        keep_bound(self, power)
        power.bind(K.kget, lambda node: {"workloads": [], "stranded": []},
                   lambda: {"members": ["node-a", "node-b", "node-c"], "can_lose": 1}, lambda: True)
        self.sent = []

        def send(plan, *args, **kwargs):
            self.sent.append(plan["node"])
            return {"operation": {"id": f"op-{plan['node']}"}}
        patcher = mock.patch.object(POWER_JOBS, "send_reviewed_power", side_effect=send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, *objects):
        for obj in objects:
            self.kube.add(obj)


class UnattendedRestartTests(FakeCluster):
    def test_a_host_with_nothing_in_the_way_still_restarts(self):
        self.assertEqual("op-node-a", POWER_JOBS.rollout_reboot("node-a"))
        self.assertEqual(["node-a"], self.sent)

    def test_a_drain_blocker_refuses_it_and_is_named(self):
        self.add(*orphans_on_node_a())
        with self.assertRaises(ValueError) as refused:
            POWER_JOBS.rollout_reboot("node-a")
        self.assertIn("3 Longhorn orphan records", str(refused.exception))
        self.assertEqual([], self.sent, "nothing is sent")
        self.assertEqual([], [w for w in self.kube.writes if w[1].startswith("/api/v1/nodes")], "nor cordoned")

    def test_every_blocker_is_named(self):
        self.add(*orphans_on_node_a(), *stuck_pod_on_node_a())
        with self.assertRaises(ValueError) as refused:
            POWER_JOBS.rollout_reboot("node-a")
        self.assertIn("orphan records", str(refused.exception))
        self.assertIn("apps/app-0", str(refused.exception))

    def test_a_check_that_could_not_read_refuses_too(self):
        # Not knowing is not "nothing in the way": a person looks.
        with mock.patch.object(BLOCKERS, "for_host", side_effect=KeyError("status")):
            with self.assertRaises(ValueError) as refused:
                POWER_JOBS.rollout_reboot("node-a")
        self.assertIn("could not check", str(refused.exception))
        self.assertEqual([], self.sent)

    def test_the_settings_accepting_an_only_copy_do_not_accept_a_blocker(self):
        self.add(*orphans_on_node_a())
        with self.assertRaises(ValueError):
            POWER_JOBS.rollout_reboot("node-a", allow_single_copy=True)
        self.assertEqual([], self.sent)

    def test_the_refusal_is_not_taken_for_copies_still_rebuilding(self):
        # The rollout waits 15 minutes on that phrase; a blocker waits for a person.
        self.add(*orphans_on_node_a())
        self.assertNotIn("only healthy copy", POWER_JOBS.rollout_check("node-a")[0])


class AskingTheChecksTests(FakeCluster):
    def test_it_answers_as_the_restart_would_and_changes_nothing(self):
        self.assertEqual("", POWER_JOBS.rollout_check("node-a")[0])
        self.add(*orphans_on_node_a())
        before = len(self.kube.writes)
        answer = POWER_JOBS.rollout_check("node-a")[0]
        self.assertIn("3 Longhorn orphan records", answer)
        with self.assertRaises(ValueError) as refused:
            POWER_JOBS.rollout_reboot("node-a")
        self.assertEqual(str(refused.exception), answer, "the same words as the restart itself")
        self.assertEqual([], self.sent)
        self.assertEqual(before, len(self.kube.writes))

    def test_the_answer_does_not_change_as_a_pod_stays_stuck(self):
        self.add(*stuck_pod_on_node_a())
        first = POWER_JOBS.rollout_check("node-a")[0]
        self.assertIn("apps/app-0", first)
        with mock.patch.object(BLOCKERS.time, "time", return_value=time.time() + 7200):
            self.assertEqual(first, POWER_JOBS.rollout_check("node-a")[0])

    def test_a_failed_read_is_not_an_answer(self):
        with mock.patch.object(POWER_JOBS.POWER, "plan", side_effect=OSError("no route to host")):
            with self.assertRaises(OSError):
                POWER_JOBS.rollout_check("node-a")[0]


class Runs(FakeCluster):
    """The engine as server.py binds it: the real review, the real job store."""
    def setUp(self):
        super().setUp()
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        for patcher in (mock.patch.object(ROLLOUT.OPS, "DATA_DIR", self.data.name),
                        mock.patch.object(ROLLOUT.OPS, "WRITE_GUARD", None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        keep_bound(self, ROLLOUT)
        keep_bound(self, ALERTS)
        ALERTS.bind(self.data.name)
        self.hosts = Hosts({"node-a": host_facts(["curl", "linux-image-6.8"]), "node-b": host_facts(["curl"]),
                            "node-c": host_facts(["vim"])})
        self.own = "node-a"
        self.bind()

    def bind(self):
        ROLLOUT.bind(K.kget, lambda force=False: {"distribution": "k3s"}, self.hosts, POWER_JOBS.rollout_reboot,
                     lambda op: {"id": op, "status": "succeeded"} if op else None, lambda name, on: None,
                     lambda: self.own, self.data.name,
                     lambda rollout: ROLLOUT.OPS.start("os-rollout", ROLLOUT.title(rollout), {"kind": "Node", "name": ", ".join(rollout["nodes"])[:200]},
                                                       "/nodes", {"rollout": rollout["id"]}, "Starting"),
                     POWER_JOBS.rollout_power_job, POWER_JOBS.rollout_check)

    def run_it(self, limit=80):
        for _ in range(limit):
            ROLLOUT.tick(now=1000)
            rollout = ROLLOUT._load().get("rollout") or {}
            if rollout.get("status") != "running":
                return rollout
        self.fail("the run did not finish")

    def job(self, kind, node="", status="running", **ref):
        """A job in the real store, as another feature would have left it."""
        items = ROLLOUT.OPS.stored()
        items.append({"id": f"{kind}-{len(items)}", "kind": kind, "title": f"The {kind} job", "status": status, "progress": 1,
                      "message": "", "history": [], "resource": {"kind": "Node" if node else "", "name": node, "namespace": ""},
                      "ref": {"node": node, **ref} if node else ref})
        ROLLOUT.OPS._write(items)

    def holds(self, on):
        return sorted(name for name, hold in self.hosts.holds if hold is on)


class ChosenHostsTests(Runs):
    def test_only_the_named_hosts_run_with_its_own_host_last(self):
        rollout = ROLLOUT.start("asked-hosts", only=["node-a", "node-c"])
        self.assertEqual(["node-c", "node-a"], rollout["nodes"])
        done = self.run_it()
        self.assertEqual("succeeded", done["status"])
        self.assertEqual(["node-c", "node-a"], self.hosts.began)
        self.assertEqual(["node-a"], self.sent, "the kernel update restarts its host")

    def test_a_host_that_is_not_ready_refuses_the_whole_run(self):
        self.kube.discard("/api/v1/nodes/node-b")
        self.add(host("node-b", ready=False))
        with self.assertRaises(ValueError) as refused:
            ROLLOUT.start("asked-hosts", only=["node-a", "node-b"])
        self.assertIn("node-b", str(refused.exception))
        self.assertEqual({}, ROLLOUT._load().get("rollout") or {})

    def test_a_host_no_cluster_knows_refuses_it(self):
        with self.assertRaises(ValueError):
            ROLLOUT.start("asked-hosts", only=["node-z"])

    def test_this_runs_answer_can_say_do_not_restart(self):
        ROLLOUT.start("asked-hosts", only=["node-a"], restart="never")
        done = self.run_it()
        self.assertEqual([], self.sent)
        self.assertEqual("updates installed, but the restart was not done: this run does not restart hosts",
                         done["results"][0]["note"])
        self.assertTrue(done["results"][0]["restart_needed"])
        self.assertIn("needing a restart: node-a (node-a: this run does not restart hosts)", done["message"])
        self.assertEqual("when-needed", ROLLOUT.settings()["reboot"], "the setting is not touched")

    def test_this_runs_answer_can_restart_when_the_setting_says_never(self):
        ROLLOUT.save_settings({"reboot": "never"})
        ROLLOUT.start("asked-hosts", only=["node-a"], restart="when-needed")
        self.run_it()
        self.assertEqual(["node-a"], self.sent)
        self.assertEqual("never", ROLLOUT.settings()["reboot"])

    def test_with_no_answer_it_follows_the_setting(self):
        ROLLOUT.save_settings({"reboot": "never"})
        ROLLOUT.start("asked-hosts", only=["node-a"])
        self.run_it()
        self.assertEqual([], self.sent)

    def test_a_host_with_nothing_to_install_that_needs_a_restart_is_not_called_up_to_date(self):
        # #1319: with restarts off it was "up to date", and the summary left it out.
        self.hosts.facts["node-b"] = host_facts([], reboot=True)
        ROLLOUT.save_settings({"reboot": "never"})
        ROLLOUT.start("asked-hosts", only=["node-b"])
        done = self.run_it()
        self.assertEqual([], self.sent)
        row = done["results"][0]
        self.assertEqual("up to date, but the restart was not done: this run does not restart hosts", row["note"])
        self.assertTrue(row["restart_needed"])
        self.assertIn("needing a restart: node-b", done["message"])

    def test_the_leaders_host_goes_last_whichever_replica_answers(self):
        # #1319: the order followed the pod that answered, not the leader.
        self.kube.add(pod("homestead-0", ns=LEADER.NS, node="node-a", phase="Running", claim="homestead-data"))
        self.kube.add(pod("homestead-1", ns=LEADER.NS, node="node-c", phase="Running", claim="homestead-data"))
        keep_bound(self, LEADER)
        LEADER.bind(K.kget, K.ksend, LEADER.NS)
        self.addCleanup(LEADER._state.update, dict(LEADER._state))
        LEADER._state.update(leader=False, holder="homestead-1")   # this replica, on node-a, does not lead
        self.own = "node-a"
        self.bind()
        mock.patch.object(ROLLOUT, "own_node", LEADER.leader_node).start()
        self.addCleanup(mock.patch.stopall)
        self.assertEqual(["node-a", "node-b", "node-c"], ROLLOUT.start("asked-hosts", only=["node-a", "node-b", "node-c"])["nodes"])
        self.assertEqual([False, False, True], [h["homestead_here"] for h in ROLLOUT.preview(["node-a", "node-b", "node-c"])["hosts"]])

    def test_with_no_leader_known_no_host_is_called_homesteads(self):
        keep_bound(self, LEADER)
        LEADER.bind(K.kget, K.ksend, LEADER.NS)
        self.addCleanup(LEADER._state.update, dict(LEADER._state))
        LEADER._state.update(leader=False, holder="")
        self.assertEqual("", LEADER.leader_node())

    def test_server_binds_the_leaders_host(self):
        import ast
        tree = ast.parse((Path(server.__file__)).read_text())
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "OS_ROLLOUT.bind")
        self.assertEqual("LEADER.leader_node", ast.unparse(call.args[6]))

    def test_only_the_two_answers_are_taken(self):
        with self.assertRaises(ValueError):
            ROLLOUT.start("asked-hosts", only=["node-a"], restart="always")

    def test_a_second_run_is_refused_in_words_for_any_run(self):
        ROLLOUT.start("asked-hosts", only=["node-b"])
        with self.assertRaises(ValueError) as refused:
            ROLLOUT.start()
        self.assertIn("An OS update is already running", str(refused.exception))
        self.run_it()
        with self.assertRaises(ValueError) as refused:
            ROLLOUT.stop()
        self.assertIn("No OS update is running", str(refused.exception))

    def test_the_job_says_which_hosts(self):
        ROLLOUT.start("asked-hosts", only=["node-b"])
        titles = [i["title"] for i in ROLLOUT.OPS.stored() if i["kind"] == "os-rollout"]
        self.assertEqual(["Update node-b's OS"], titles)
        self.run_it()
        ROLLOUT.start("asked-hosts", only=["node-a", "node-c"], now=2000)
        self.assertEqual("Update the OS of 2 hosts", ROLLOUT.title(ROLLOUT._load()["rollout"]))
        self.assertEqual("Update every host's OS (3 hosts)", ROLLOUT.title({"nodes": ["a", "b", "c"], "reason": "asked"}))
        self.assertEqual("Update every host's OS (3 hosts)", ROLLOUT.title({"nodes": ["a", "b", "c"], "reason": "schedule"}))

    def test_last_run_keeps_which_hosts_and_the_kind_of_run(self):
        ROLLOUT.start("asked-hosts", only=["node-b"])
        self.run_it()
        last = ROLLOUT._load()["last_rollout"]
        self.assertEqual((["node-b"], "asked-hosts", "succeeded"), (last["nodes"], last["reason"], last["status"]))


class HoldsTests(Runs):
    def test_every_ready_host_is_held_for_a_smaller_run_and_let_go_at_its_end(self):
        self.kube.discard("/api/v1/nodes/node-c")
        self.add(host("node-c", ready=False))
        ROLLOUT.start("asked-hosts", only=["node-b"])
        ROLLOUT.tick(now=1000)
        self.assertEqual(["node-a", "node-b"], self.holds(True), "node-c is not Ready, so it can't be reached")
        self.assertEqual([], self.holds(False))
        self.run_it()
        self.assertEqual(["node-a", "node-b"], self.holds(False))

    def test_a_host_that_becomes_ready_during_a_run_is_held_and_let_go_too(self):
        self.kube.discard("/api/v1/nodes/node-c")
        self.add(host("node-c", ready=False))
        ROLLOUT.start("asked-hosts", only=["node-a", "node-b"])
        ROLLOUT.tick(now=1000)
        self.assertNotIn("node-c", self.holds(True))
        self.kube.discard("/api/v1/nodes/node-c")
        self.add(host("node-c"))
        self.run_it()
        self.assertIn("node-c", self.holds(True))
        self.assertIn("node-c", self.holds(False))

    def test_a_stopped_run_lets_go_of_the_hosts_outside_it_too(self):
        ROLLOUT.start("asked-hosts", only=["node-b", "node-c"])
        ROLLOUT.tick(now=1000)
        ROLLOUT.stop()
        self.assertEqual("stopped", self.run_it()["status"])
        self.assertEqual(["node-a", "node-b", "node-c"], self.holds(False))

    def test_a_failed_run_lets_go_of_the_hosts_outside_it_too(self):
        self.hosts.codes["node-b"] = "100"
        ROLLOUT.start("asked-hosts", only=["node-b"])
        self.assertEqual("failed", self.run_it()["status"])
        self.assertEqual(["node-a", "node-b", "node-c"], self.holds(False))

    def test_when_homestead_manages_updates_the_holds_stay(self):
        ROLLOUT.save_settings({"manage": "homestead"})
        ROLLOUT.start("asked-hosts", only=["node-b"])
        self.run_it()
        self.assertEqual(["node-a", "node-b", "node-c"], self.holds(True))
        self.assertEqual([], self.holds(False))

    def test_a_run_saved_by_the_version_before_keeps_its_own_hosts(self):
        # An upgrade of Homestead during a run: its saved run has no held_hosts yet.
        ROLLOUT.start()
        with ROLLOUT._lock:
            state = ROLLOUT._load()
            state["rollout"].pop("held_hosts")
            ROLLOUT._save(state)
        self.run_it()
        self.assertEqual(["node-a", "node-b", "node-c"], self.holds(False))


class NoJobNoRunTests(Runs):
    def refusing_job(self):
        self.job("disk-allocation", "node-b")

    def test_a_run_whose_job_is_refused_does_not_start(self):
        for start in (lambda: ROLLOUT.start(), lambda: ROLLOUT.start("asked-hosts", only=["node-c"])):
            self.refusing_job()
            with self.assertRaises(ValueError) as refused:
                start()
            self.assertIn("Finish disk preparation", str(refused.exception))
            self.assertEqual({}, ROLLOUT._load().get("rollout") or {})
            self.assertIsNone(ROLLOUT.tick(now=1000), "nothing moves")
            self.assertEqual([], self.hosts.began)
            self.assertEqual([], self.holds(True))
            ROLLOUT.OPS._write([])

    def test_the_last_run_stays_as_it_was(self):
        ROLLOUT.start("asked-hosts", only=["node-b"])
        self.run_it()
        before = ROLLOUT._load()["rollout"]
        self.refusing_job()
        with self.assertRaises(ValueError):
            ROLLOUT.start(now=5000)
        self.assertEqual(before, ROLLOUT._load()["rollout"])

    def test_any_failure_making_the_job_counts(self):
        ROLLOUT.announce = mock.Mock(side_effect=OSError("disk full"))
        with self.assertRaises(ValueError) as refused:
            ROLLOUT.start()
        self.assertIn("disk full", str(refused.exception))
        self.assertEqual({}, ROLLOUT._load().get("rollout") or {})

    def test_a_window_whose_job_is_refused_can_start_later_in_its_hour(self):
        self.refusing_job()
        with self.assertRaises(ValueError):
            ROLLOUT.start("schedule", now=5000)
        self.assertNotIn("last_scheduled", ROLLOUT._load())
        ROLLOUT.OPS._write([])
        ROLLOUT.start("schedule", now=5001)
        self.assertEqual(5001, ROLLOUT._load()["last_scheduled"])

    def test_the_job_finds_its_run_saved(self):
        # The job tray may read the job the moment it exists.
        seen = []

        def announce(rollout):
            seen.append(ROLLOUT._load()["rollout"]["id"])
            return {"id": "op"}
        ROLLOUT.announce = announce
        rollout = ROLLOUT.start()
        self.assertEqual([rollout["id"]], seen)
        self.assertEqual({"id": "op"}, rollout["operation"])


class RefusedRestartInARunTests(Runs):
    def test_every_host_update_refuses_a_drain_blocker_and_goes_on(self):
        self.own = "node-c"
        self.add(*orphans_on_node_a())
        ROLLOUT.start()
        done = self.run_it()
        self.assertEqual("succeeded", done["status"], "a refused restart does not stop the run")
        self.assertEqual(["node-a", "node-b", "node-c"], self.hosts.began, "the next hosts still update")
        self.assertEqual([], self.sent)
        row = done["results"][0]
        self.assertEqual("node-a", row["node"])
        self.assertTrue(row["restart_needed"])
        self.assertIn("3 Longhorn orphan records", row["note"])
        self.assertIn("needing a restart: node-a", done["message"])
        self.assertIn("3 Longhorn orphan records", done["message"], "the summary says why")

    def test_a_chosen_host_waits_the_same_way(self):
        self.add(*orphans_on_node_a())
        ROLLOUT.start("asked-hosts", only=["node-a"])
        done = self.run_it()
        self.assertEqual("succeeded", done["status"])
        self.assertEqual([], self.sent)
        self.assertTrue(done["results"][0]["restart_needed"])

    def test_a_long_list_of_blockers_is_not_cut_short(self):
        self.add(*orphans_on_node_a(count=20), *stuck_pod_on_node_a())
        ROLLOUT.start("asked-hosts", only=["node-a"])
        note = self.run_it()["results"][0]["note"]
        self.assertIn("apps/app-0", note, "the last blocker is still named")


class PreviewTests(Runs):
    def preview(self, *names):
        return ROLLOUT.ROUTES[("GET", "/api/os-updates/preview")][1](types.SimpleNamespace(query={"node": list(names)}, body={}))

    def post(self, **body):
        return ROLLOUT.ROUTES[("POST", "/api/os-updates/start")][1](types.SimpleNamespace(query={}, body=body))

    def row(self, answer, name):
        return next(h for h in answer["hosts"] if h["node"] == name)

    def test_it_is_for_admins_like_the_start_it_reviews(self):
        self.assertEqual("admin", ROLLOUT.ROUTES[("GET", "/api/os-updates/preview")][0])

    def test_it_says_what_would_install_in_the_order_it_would_run(self):
        self.hosts.facts["node-a"]["security"] = 1
        answer = self.preview("node-a", "node-c")
        self.assertEqual(["node-c", "node-a"], [h["node"] for h in answer["hosts"]], "its own host last")
        mine = self.row(answer, "node-a")
        self.assertEqual(["curl", "linux-image-6.8"], [u["name"] for u in mine["updates"]])
        self.assertEqual(1, mine["security"])
        self.assertTrue(mine["homestead_here"])
        self.assertFalse(self.row(answer, "node-c")["homestead_here"])
        self.assertEqual("", mine["restart_refusal"])
        self.assertEqual("when-needed", answer["restart_policy"])
        self.assertEqual("", answer["blocked"])

    def test_it_starts_from_the_setting(self):
        ROLLOUT.save_settings({"reboot": "never"})
        self.assertEqual("never", self.preview("node-a")["restart_policy"])

    def test_it_says_what_the_checks_would_refuse_today(self):
        self.add(*orphans_on_node_a())
        answer = self.preview("node-a", "node-b")
        self.assertIn("3 Longhorn orphan records", self.row(answer, "node-a")["restart_refusal"])
        self.assertEqual("", self.row(answer, "node-b")["restart_refusal"])
        self.assertEqual("", answer["blocked"], "the update still goes ahead; only the restart waits")

    def test_it_changes_nothing(self):
        self.add(*orphans_on_node_a())
        writes, saved, jobs = len(self.kube.writes), ROLLOUT._load(), ROLLOUT.OPS.stored()
        self.preview("node-a", "node-b", "node-c")
        self.assertEqual(writes, len(self.kube.writes))
        self.assertEqual(saved, ROLLOUT._load())
        self.assertEqual(jobs, ROLLOUT.OPS.stored())
        self.assertEqual(([], []), (self.hosts.began, self.hosts.holds))
        self.assertEqual([], self.sent)

    def test_checks_that_could_not_be_read_are_said_apart_from_a_refusal(self):
        with mock.patch.object(POWER_JOBS.POWER, "plan", side_effect=OSError("no route to host")):
            answer = self.preview("node-a")
        self.assertEqual("", self.row(answer, "node-a")["restart_refusal"])
        self.assertIn("no route to host", self.row(answer, "node-a")["restart_unread"])
        self.assertEqual("", answer["blocked"], "the install can go ahead; the restart is checked when it is due")

    def assert_blocked(self, words, *names):
        answer = self.preview(*names)
        self.assertIn(words, answer["blocked"])
        token = answer["review_token"]
        with self.assertRaises(ValueError) as refused:
            self.post(nodes=list(names), review_token=token)
        self.assertIn(words, str(refused.exception))
        self.assertEqual({}, ROLLOUT._load().get("rollout") or {})

    def test_a_job_on_the_host_blocks_it_and_is_named(self):
        self.job("smart-test", "node-b")
        self.assert_blocked("node-b has a job running: The smart-test job", "node-a", "node-b")

    def test_a_job_on_another_host_does_not(self):
        self.job("smart-test", "node-c")
        self.assertEqual("", self.preview("node-a", "node-b")["blocked"])

    def test_a_job_naming_the_host_in_a_list_of_rows_blocks_it(self):
        # Longhorn's V2 clean-up keeps its hosts as rows.
        self.job("some-kind", nodes=[{"node": "node-b", "phase": "x"}])
        self.assert_blocked("node-b has a job running", "node-b")

    def test_a_job_whose_list_is_not_a_list_is_not_matched_by_letters(self):
        self.job("some-kind", nodes="not-node-b")
        self.assertEqual("", self.preview("node-b")["blocked"])

    def test_a_kind_nobody_listed_blocks_it(self):
        self.job("a-kind-added-next-year", "node-b")
        self.assert_blocked("a-kind-added-next-year", "node-b")

    def test_an_ended_job_does_not(self):
        self.job("smart-test", "node-b", status="succeeded")
        self.assertEqual("", self.preview("node-b")["blocked"])

    def test_a_job_across_the_cluster_blocks_every_host(self):
        self.job("platform-upgrade")
        self.assert_blocked("The platform-upgrade job is running", "node-b")

    def test_balance_hosts_blocks_every_host(self):
        self.job("container-rebalance")
        self.assert_blocked("rebalanced", "node-b")

    def test_a_host_that_is_not_ready_blocks_it(self):
        self.kube.discard("/api/v1/nodes/node-b")
        self.add(host("node-b", ready=False))
        self.assert_blocked("node-b", "node-b")
        self.assertFalse(self.row(self.preview("node-b"), "node-b")["ready"])

    def test_a_run_going_blocks_it(self):
        ROLLOUT.start("asked-hosts", only=["node-c"])
        answer = self.preview("node-b")
        self.assertIn("An OS update is already running", answer["blocked"])
        with self.assertRaises(ValueError):
            self.post(nodes=["node-b"], review_token=answer["review_token"])
        self.assertEqual(["node-c"], ROLLOUT._load()["rollout"]["nodes"])

    def test_it_needs_hosts(self):
        with self.assertRaises(ValueError):
            self.preview()

    def test_harvester_updates_its_own(self):
        ROLLOUT.platform = lambda force=False: {"harvester": True, "distribution": "k3s"}
        with self.assertRaises(ValueError):
            self.preview("node-a")

    # Starting from the review ---------------------------------------------

    def test_the_reviewed_hosts_start_with_this_runs_answer(self):
        token = self.preview("node-a", "node-b")["review_token"]
        answer = self.post(nodes=["node-a", "node-b"], restart="never", review_token=token)
        self.assertTrue(answer["ok"])
        self.assertEqual(["node-b", "node-a"], answer["rollout"]["nodes"])
        self.assertEqual("never", answer["rollout"]["restart"])
        self.assertEqual("asked-hosts", answer["rollout"]["reason"])
        self.assertEqual("os-rollout", answer["operation"]["kind"])
        self.assertIn("2 hosts", answer["detail"])
        self.run_it()
        self.assertEqual([], self.sent)

    def test_the_detail_names_one_host(self):
        token = self.preview("node-b")["review_token"]
        self.assertIn("node-b", self.post(nodes=["node-b"], review_token=token)["detail"])

    def test_a_host_named_twice_runs_once(self):
        token = self.preview("node-b")["review_token"]
        answer = self.post(nodes=["node-b", "node-b"], review_token=token)
        self.assertEqual(["node-b"], answer["rollout"]["nodes"])

    def test_an_empty_or_bad_list_is_never_every_host(self):
        for nodes in ([], None, "node-a", [""], [None], [1], {"node-a": True}):
            with self.assertRaises(ValueError, msg=repr(nodes)):
                self.post(nodes=nodes, review_token="any")
        self.assertEqual({}, ROLLOUT._load().get("rollout") or {})

    def test_with_no_review_it_is_refused_like_a_stale_one(self):
        reply = self.post(nodes=["node-b"])
        self.assertEqual(409, reply.code)
        self.assertEqual({}, ROLLOUT._load().get("rollout") or {})

    def test_a_stale_review_is_refused_with_a_fresh_one(self):
        token = self.preview("node-b")["review_token"]
        self.hosts.facts["node-b"] = host_facts(["curl", "openssl"])
        reply = self.post(nodes=["node-b"], review_token=token)
        self.assertEqual(409, reply.code)
        self.assertIn("review", reply.body["error"])
        self.assertEqual(["curl", "openssl"], [u["name"] for u in reply.body["preview"]["hosts"][0]["updates"]])
        self.assertEqual({}, ROLLOUT._load().get("rollout") or {})

    def test_a_verdict_that_changed_makes_the_review_stale(self):
        token = self.preview("node-a")["review_token"]
        self.add(*orphans_on_node_a())
        self.assertEqual(409, self.post(nodes=["node-a"], review_token=token).code)

    def test_a_setting_that_changed_makes_the_review_stale(self):
        token = self.preview("node-a")["review_token"]
        ROLLOUT.save_settings({"reboot": "never"})
        self.assertEqual(409, self.post(nodes=["node-a"], review_token=token).code)

    def test_the_time_of_a_read_does_not(self):
        self.hosts.facts["node-b"]["at"] = 1000
        token = self.preview("node-b")["review_token"]
        self.hosts.facts["node-b"]["at"] = 2000
        self.assertTrue(self.post(nodes=["node-b"], review_token=token)["ok"])

    def test_another_replica_answering_does_not(self):
        token = self.preview("node-a", "node-b")["review_token"]
        self.own = "node-b"
        self.assertEqual(token, self.preview("node-a", "node-b")["review_token"])

    def test_a_pod_staying_stuck_does_not(self):
        self.add(*stuck_pod_on_node_a())
        token = self.preview("node-a")["review_token"]
        with mock.patch.object(BLOCKERS.time, "time", return_value=time.time() + 7200):
            self.assertTrue(self.post(nodes=["node-a"], review_token=token)["ok"])

    def test_a_job_starting_between_the_check_and_the_lock_still_refuses(self):
        self.job("smart-test", "node-b")
        with self.assertRaises(ValueError) as refused:
            ROLLOUT.start("asked-hosts", only=["node-b"])
        self.assertIn("node-b has a job running", str(refused.exception))

    def test_an_unreadable_job_list_starts_nothing(self):
        token = self.preview("node-b")["review_token"]
        with mock.patch.object(ROLLOUT.OPS, "stored", side_effect=OSError("damaged")):
            with self.assertRaises(OSError):
                self.post(nodes=["node-b"], review_token=token)
        self.assertEqual({}, ROLLOUT._load().get("rollout") or {})

    def test_an_empty_body_is_still_every_host(self):
        answer = self.post()
        self.assertEqual(["node-b", "node-c", "node-a"], answer["rollout"]["nodes"])
        self.assertEqual("asked", answer["rollout"]["reason"])
        self.assertIn("3 hosts", answer["detail"])

    def test_every_host_can_take_this_runs_answer_too(self):
        answer = self.post(restart="never")
        self.assertEqual("never", answer["rollout"]["restart"])

    def test_every_host_now_still_starts_past_a_job_on_a_host(self):
        # Refusing busy hosts is for the chosen hosts; the gap for the rest is listed.
        self.job("smart-test", "node-b")
        self.assertEqual(3, len(self.post()["rollout"]["nodes"]))


class HeldEveryStepTests(Runs):
    """Decision 7: every Ready host, until the run ends. Asserted again each step,
    so a host that comes Ready at any point, and a hold that failed, are caught."""
    def make_not_ready(self, name):
        self.kube.discard(f"/api/v1/nodes/{name}")
        self.add(host(name, ready=False))

    def make_ready(self, name):
        self.kube.discard(f"/api/v1/nodes/{name}")
        self.add(host(name))

    def test_a_host_ready_during_a_one_host_run_is_held(self):
        self.make_not_ready("node-c")
        self.hosts.codes["node-b"] = None    # still installing
        ROLLOUT.start("asked-hosts", only=["node-b"])
        ROLLOUT.tick(now=1000)
        self.assertEqual("installing", ROLLOUT._load()["rollout"]["phase"])
        self.assertNotIn("node-c", self.holds(True))
        self.make_ready("node-c")
        ROLLOUT.tick(now=1020)
        self.assertIn("node-c", self.holds(True), "held while the only host installs")
        self.hosts.codes["node-b"] = "0"
        self.run_it()
        self.assertIn("node-c", self.holds(False), "let go at the end")

    def test_a_host_ready_during_the_last_host_of_a_run_is_held(self):
        self.make_not_ready("node-c")
        self.hosts.codes["node-b"] = None
        ROLLOUT.start("asked-hosts", only=["node-a", "node-b"])
        for now in range(1000, 1100, 20):
            ROLLOUT.tick(now=now)
        self.assertEqual("node-b", ROLLOUT._load()["rollout"]["node"]["name"], "the last host")
        self.make_ready("node-c")
        ROLLOUT.tick(now=1200)
        self.assertIn("node-c", self.holds(True))

    def test_a_failed_hold_is_tried_again_and_the_run_goes_on(self):
        self.hosts.hold_errors["node-c"] = TimeoutError("timed out")
        self.hosts.codes["node-b"] = None
        ROLLOUT.start("asked-hosts", only=["node-b"])
        ROLLOUT.tick(now=1000)
        self.assertNotIn("node-c", self.holds(True))
        self.assertEqual("installing", ROLLOUT._load()["rollout"]["phase"], "a hold on another host doesn't stop the run")
        del self.hosts.hold_errors["node-c"]
        ROLLOUT.tick(now=1000 + ROLLOUT.HOLD_RETRY)
        self.assertIn("node-c", self.holds(True))
        self.assertEqual({}, ROLLOUT._load()["rollout"].get("hold_failed") or {}, "no longer failing")

    def test_a_host_that_keeps_failing_is_not_asked_every_step(self):
        # set_hold can take 45 s of the rollout's lock each time.
        calls = []
        real = self.hosts.set_hold

        def counted(name, hold):
            if name == "node-c":
                calls.append(hold)
            return real(name, hold)
        self.hosts.set_hold = counted
        self.hosts.hold_errors["node-c"] = ValueError("Busy")
        self.hosts.codes["node-b"] = None
        ROLLOUT.start("asked-hosts", only=["node-b"])
        for now in range(1000, 1000 + ROLLOUT.HOLD_RETRY, 20):
            ROLLOUT.tick(now=now)
        self.assertEqual(1, len(calls))
        ROLLOUT.tick(now=1000 + ROLLOUT.HOLD_RETRY)
        self.assertEqual(2, len(calls))

    def test_a_host_never_read_is_read_and_then_held(self):
        fresh = self.hosts.facts.pop("node-c")
        self.hosts.codes["node-b"] = None
        ROLLOUT.start("asked-hosts", only=["node-b"])
        ROLLOUT.tick(now=1000)
        self.assertNotIn("node-c", self.holds(True))
        self.assertIn("node-c", ROLLOUT._load()["rollout"]["hold_failed"], "not known to be held is said")
        self.hosts.facts["node-c"] = fresh
        ROLLOUT.tick(now=1000 + ROLLOUT.HOLD_RETRY)
        self.assertIn("node-c", self.holds(True))

    def test_a_host_with_nothing_to_hold_is_left_alone(self):
        self.hosts.facts["node-c"] = host_facts(["vim"], auto=False)
        ROLLOUT.start("asked-hosts", only=["node-b"])
        self.run_it()
        self.assertNotIn("node-c", self.holds(True))
        self.assertEqual({}, ROLLOUT._load()["rollout"].get("hold_failed") or {})

    def test_a_host_held_already_is_not_asked_again(self):
        self.hosts.codes["node-b"] = None
        ROLLOUT.start("asked-hosts", only=["node-b"])
        for now in range(1000, 1200, 20):
            ROLLOUT.tick(now=now)
        self.assertEqual(1, self.holds(True).count("node-c"))

    def test_a_nodes_read_that_fails_does_not_fail_the_step(self):
        self.hosts.codes["node-b"] = None
        ROLLOUT.start("asked-hosts", only=["node-b"])
        ROLLOUT.tick(now=1000)
        self.kube.fail("/api/v1/nodes", 500, times=1)
        ROLLOUT.tick(now=1020)
        self.assertEqual("running", ROLLOUT._load()["rollout"]["status"])
        self.assertNotIn("error_since", ROLLOUT._load()["rollout"])

    def test_a_hold_that_stays_failed_reaches_a_person(self):
        self.hosts.hold_errors["node-c"] = ValueError("Busy: a host change is running")
        self.hosts.codes["node-b"] = None
        ROLLOUT.start("asked-hosts", only=["node-b"])
        ROLLOUT.tick(now=1000)
        self.assertEqual([], ALERTS.log(categories=["degraded"])["alerts"], "a moment's Busy is no news")
        for now in range(1000, 1000 + ROLLOUT.HOLD_NOTICE + 100, 20):
            ROLLOUT.tick(now=now)
        row = ROLLOUT._load()["rollout"]["hold_failed"]["node-c"]
        self.assertTrue(row["noted"], "the dialog shows it from here")
        self.assertIn("Busy", row["error"])
        alerts = ALERTS.log(categories=["degraded"])["alerts"]
        self.assertEqual(1, len(alerts), "one alert, not one per retry")
        self.assertIn("node-c", alerts[0]["title"])
        self.hosts.codes["node-b"] = "0"
        done = self.run_it()
        self.assertIn("could not be held on node-c", done["message"], "Last run says so")
        self.assertIn("could not be held on node-c", ROLLOUT._load()["last_rollout"]["message"])

    def test_a_hold_that_failed_only_at_the_end_is_said_too(self):
        self.hosts.hold_errors["node-c"] = TimeoutError("timed out")
        ROLLOUT.start("asked-hosts", only=["node-b"])
        self.assertIn("could not be held on node-c", self.run_it()["message"])

    def test_a_host_that_went_away_is_not_reported_as_failing(self):
        self.hosts.hold_errors["node-c"] = TimeoutError("timed out")
        self.hosts.codes["node-b"] = None
        ROLLOUT.start("asked-hosts", only=["node-b"])
        ROLLOUT.tick(now=1000)
        self.make_not_ready("node-c")
        ROLLOUT.tick(now=1020)
        self.assertEqual({}, ROLLOUT._load()["rollout"].get("hold_failed") or {})


class RealWiringTests(unittest.TestCase):
    """What server.py binds, not a copy of it."""
    def test_the_review_asks_the_real_restart_checks(self):
        with mock.patch.object(server.POWER_JOBS, "rollout_check", return_value=("refused", False)) as asked:
            self.assertEqual(("refused", False), server.OS_ROLLOUT.restart_check("node-a", True))
        asked.assert_called_once_with("node-a", True)

    def test_a_run_restarts_through_the_real_unattended_restart(self):
        self.assertIs(server.POWER_JOBS.rollout_reboot, server.OS_ROLLOUT.reboot)

    def test_the_job_gets_the_runs_own_title(self):
        with mock.patch.object(server.OPS, "start", return_value={"id": "op"}) as start:
            server.OS_ROLLOUT.announce({"id": "os-1", "nodes": ["node-a"], "reason": "asked-hosts"})
        self.assertEqual(("os-rollout", "Update node-a's OS"), start.call_args.args[:2])


class ChecksNotWiredTests(Runs):
    def test_no_restart_check_is_unread_not_go_ahead(self):
        ROLLOUT.restart_check = None
        row = ROLLOUT.preview(["node-a"])["hosts"][0]
        self.assertEqual("", row["restart_refusal"])
        self.assertTrue(row["restart_unread"])


def engines_elsewhere_on_node_a():
    """node-a's instance-manager still lists an engine that now runs on node-b,
    with no orphan record yet: Longhorn records these by itself within minutes."""
    return [engine("vol-x", "node-b"), instance_manager("node-a", engines=["vol-x-e-0"])]


def removing_orphan_on_node_a():
    orphan = engine_orphan("vol-y", 9)
    orphan["metadata"]["deletionTimestamp"] = "2026-11-05T10:00:00Z"
    return [orphan, instance_manager("node-a", engines=[])]


class ClearsByItselfTests(Runs):
    """Andreas, 5 Nov: a read that failed, or a blocker Longhorn clears by itself,
    gets the same 15 minutes as copies rebuilding. Still there: refused and named,
    and the host waits for a person. What won't clear is refused at once."""
    def setUp(self):
        super().setUp()
        self.own = "node-c"
        ROLLOUT.save_settings({})

    def until_restart_phase(self, now=1000):
        ROLLOUT.start("asked-hosts", only=["node-a"], now=now)
        for _ in range(12):
            ROLLOUT.tick(now=now)
            rollout = ROLLOUT._load()["rollout"]
            if rollout.get("status") != "running" or rollout["node"].get("copies_since"):
                break
        return ROLLOUT._load()["rollout"]

    def assert_waits_then_refuses(self, words):
        rollout = self.until_restart_phase()
        self.assertEqual("running", rollout["status"], "waits, not refused")
        self.assertEqual("restart", rollout["phase"])
        self.assertIn("waiting", rollout["message"])
        self.assertEqual([], self.sent)
        ROLLOUT.tick(now=1000 + ROLLOUT.COPIES_WAIT - 60)
        self.assertEqual("running", ROLLOUT._load()["rollout"]["status"])
        for now in (1000 + ROLLOUT.COPIES_WAIT, 1000 + ROLLOUT.COPIES_WAIT + 20):
            ROLLOUT.tick(now=now)
        done = ROLLOUT._load()["rollout"]
        self.assertEqual("succeeded", done["status"], "the host waits for a person; the run ends")
        self.assertTrue(done["results"][0]["restart_needed"])
        self.assertIn(words, done["results"][0]["note"])
        self.assertEqual([], self.sent)

    def test_a_drain_check_read_that_keeps_failing_waits_then_refuses_naming_it(self):
        self.kube.fail(BLOCKERS.ORPHANS, 503, times=100000)
        self.assert_waits_then_refuses("could not check")

    def test_a_drain_check_read_that_recovers_restarts_the_host(self):
        self.kube.fail(BLOCKERS.ORPHANS, 500, times=100000)
        rollout = self.until_restart_phase()
        self.assertEqual("restart", rollout["phase"])
        self.kube._failures.clear()
        ROLLOUT.tick(now=1300)
        ROLLOUT.tick(now=1320)
        self.assertEqual(["node-a"], self.sent, "the check answered: on with the restart")

    def test_engines_that_run_elsewhere_wait(self):
        self.add(*engines_elsewhere_on_node_a())
        self.assert_waits_then_refuses("still lists engine")

    def test_engines_elsewhere_that_clear_restart_the_host(self):
        self.add(*engines_elsewhere_on_node_a())
        self.until_restart_phase()
        self.assertEqual([], self.sent)
        for obj in engines_elsewhere_on_node_a():
            self.kube.discard(self.kube.path_of(obj))
        ROLLOUT.tick(now=1300)
        self.assertEqual(["node-a"], self.sent)

    def test_orphans_being_removed_wait(self):
        self.add(*removing_orphan_on_node_a())
        self.assert_waits_then_refuses("being removed")

    def test_a_blocker_that_will_not_clear_is_refused_at_once(self):
        self.add(*orphans_on_node_a())
        rollout = self.until_restart_phase()
        self.assertEqual("succeeded", rollout["status"])
        self.assertTrue(rollout["results"][0]["restart_needed"])
        self.assertNotIn("copies_since", rollout["results"][0])

    def test_one_that_will_not_clear_beside_one_that_would_is_refused_at_once(self):
        self.add(*stuck_pod_on_node_a(), *engines_elsewhere_on_node_a())
        rollout = self.until_restart_phase()
        self.assertEqual("succeeded", rollout["status"])
        self.assertIn("apps/app-0", rollout["results"][0]["note"])
        self.assertIn("still lists engine", rollout["results"][0]["note"], "both named")

    def test_longhorn_volumes_that_could_not_be_read_wait_too(self):
        plan = power.plan("node-a", "reboot")
        unknown = {**plan, "requires_data_ack": True, "storage_unknown": True, "volumes": [], "maintenance": {"local_storage": []}}
        with mock.patch.object(POWER_JOBS.POWER, "plan", return_value=unknown):
            with self.assertRaises(BLOCKERS.MayClear):
                POWER_JOBS.rollout_reboot("node-a")
        kept = {**unknown, "maintenance": {"local_storage": [{"pod": "apps/db", "kind": "host-local path", "source": "/srv/db"}]}}
        with mock.patch.object(POWER_JOBS.POWER, "plan", return_value=kept):
            with self.assertRaises(ValueError) as refused:
                POWER_JOBS.rollout_reboot("node-a")
        self.assertNotIsInstance(refused.exception, BLOCKERS.MayClear, "data on the host won't clear")

    def test_waiting_does_not_read_the_review_four_times_a_tick(self):
        self.add(*engines_elsewhere_on_node_a())
        self.until_restart_phase()
        with mock.patch.object(POWER_JOBS.POWER, "plan", wraps=POWER_JOBS.POWER.plan) as plan:
            ROLLOUT.tick(now=1020)
        self.assertEqual(1, plan.call_count)

    def test_the_review_says_it_today_as_the_restart_would(self):
        self.add(*engines_elsewhere_on_node_a())
        self.assertIn("still lists engine", POWER_JOBS.rollout_check("node-a")[0])


SUNDAY = 1794106800   # 2026-11-08 03:00 UTC, a Sunday
WEEK = 7 * 24 * 3600


class WindowSkippedTests(Runs):
    """Andreas, 5 Nov: a window whose job can't be made doesn't run, and says why:
    an alert, and a line beside Last run naming the job. It tries again now and
    then, not at every tick, and without a forced detection or two saves each time."""
    def setUp(self):
        super().setUp()
        ROLLOUT.save_settings({"schedule": {"enabled": True, "days": ["sun"], "hour": 3, "tz": "UTC"}})
        self.job("disk-allocation", "node-b")

    def alerts(self):
        return ALERTS.log(categories=["degraded"])["alerts"]

    def line(self):
        return (ROLLOUT.report().get("window_skip") or {}).get("why", "")

    def rollout(self):
        return ROLLOUT._load().get("rollout") or {}

    def test_it_does_not_run_and_names_the_job(self):
        ROLLOUT.tick(now=SUNDAY)
        self.assertEqual({}, self.rollout())
        self.assertIn("The disk-allocation job", self.line())
        self.assertIn("Finish disk preparation", self.line())
        self.assertEqual([], self.hosts.holds)

    def test_it_raises_one_alert_for_the_window(self):
        for now in range(SUNDAY, SUNDAY + 3000, 20):
            ROLLOUT.tick(now=now)
        alerts = self.alerts()
        self.assertEqual(1, len(alerts))
        self.assertIn("The disk-allocation job", alerts[0]["body"])

    def test_it_tries_again_every_few_minutes_not_every_tick(self):
        ROLLOUT.tick(now=SUNDAY)
        ROLLOUT.OPS._write([])
        ROLLOUT.tick(now=SUNDAY + 20)
        ROLLOUT.tick(now=SUNDAY + ROLLOUT.WINDOW_RETRY - 20)
        self.assertEqual({}, self.rollout(), "not yet")
        ROLLOUT.tick(now=SUNDAY + ROLLOUT.WINDOW_RETRY)
        self.assertEqual("schedule", self.rollout()["reason"], "the job has ended, so the window runs")
        self.assertEqual("", self.line())
        self.assertEqual(SUNDAY + ROLLOUT.WINDOW_RETRY, ROLLOUT._load()["last_scheduled"])

    def test_retries_force_no_detection_and_no_extra_saves(self):
        forced = []
        ROLLOUT.platform = lambda force=False: forced.append(force) or {"distribution": "k3s"}
        with mock.patch.object(ROLLOUT, "_save", wraps=ROLLOUT._save) as saves:
            ROLLOUT.tick(now=SUNDAY - 3600)     # the hour before: an idle tick
            idle = saves.call_count
            saves.reset_mock()
            ticks = int(ROLLOUT.WINDOW_RETRY / 20) * 3     # three retries' worth
            for n in range(ticks):
                ROLLOUT.tick(now=SUNDAY + n * 20)
        self.assertNotIn(True, forced)
        self.assertEqual(idle * ticks + 3, saves.call_count, "one more save per retry, none per tick")

    def test_the_line_stays_after_the_hour_with_no_more_tries(self):
        ROLLOUT.tick(now=SUNDAY)
        first = ROLLOUT.report()["window_skip"]["last_try"]
        ROLLOUT.tick(now=SUNDAY + 3600 + 600)
        self.assertEqual(first, ROLLOUT.report()["window_skip"]["last_try"])
        self.assertIn("The disk-allocation job", self.line())

    def test_a_skip_carries_on_into_the_next_week_and_ends_when_a_window_runs(self):
        ROLLOUT.tick(now=SUNDAY)
        self.assertEqual(1, len(self.alerts()))
        ROLLOUT.tick(now=SUNDAY + WEEK)
        self.assertEqual({}, self.rollout(), "still in the way a week on")
        self.assertEqual(2, len(self.alerts()), "a new window, a new alert")
        self.assertEqual(SUNDAY + WEEK, ROLLOUT.report()["window_skip"]["first"])
        ROLLOUT.OPS._write([])
        ROLLOUT.tick(now=SUNDAY + WEEK + ROLLOUT.WINDOW_RETRY)
        self.assertEqual("schedule", self.rollout()["reason"])
        self.assertEqual("", self.line())

    def test_the_line_names_whichever_job_is_in_the_way_now(self):
        ROLLOUT.tick(now=SUNDAY)
        ROLLOUT.OPS._write([])
        self.job("container-rebalance")
        ROLLOUT.tick(now=SUNDAY + ROLLOUT.WINDOW_RETRY)
        self.assertIn("The container-rebalance job", self.line())
        self.assertEqual({}, self.rollout())

    def test_another_refusal_is_said_the_same_way(self):
        ROLLOUT.OPS._write([])
        ROLLOUT.announce = mock.Mock(side_effect=ValueError("the job store said no"))
        ROLLOUT.tick(now=SUNDAY)
        self.assertEqual({}, self.rollout())
        self.assertIn("the job store said no", self.line())

    def test_an_unreadable_job_list_skips_and_says_so(self):
        with mock.patch.object(ROLLOUT.OPS, "stored", side_effect=OSError("damaged")):
            ROLLOUT.tick(now=SUNDAY)
        self.assertEqual({}, self.rollout())
        self.assertIn("damaged", self.line())

    def test_update_every_host_ends_the_line_and_a_smaller_run_does_not(self):
        ROLLOUT.tick(now=SUNDAY)
        ROLLOUT.OPS._write([])
        ROLLOUT.start("asked-hosts", only=["node-b"], now=SUNDAY + 60)
        self.assertIn("disk-allocation", self.line(), "the other hosts still missed their window")
        self.run_it()
        ROLLOUT.start(now=SUNDAY + 120)
        self.assertEqual("", self.line())

    def test_a_refused_start_keeps_the_line(self):
        ROLLOUT.tick(now=SUNDAY)
        with self.assertRaises(ValueError):
            ROLLOUT.start(now=SUNDAY + 60)
        self.assertIn("disk-allocation", self.line())

    def test_with_the_window_off_there_is_no_line(self):
        ROLLOUT.tick(now=SUNDAY)
        ROLLOUT.save_settings({"schedule": {"enabled": False}})
        self.assertEqual("", self.line())

    def test_asking_for_a_run_still_refuses_with_the_reason(self):
        with self.assertRaises(ValueError) as refused:
            ROLLOUT.ROUTES[("POST", "/api/os-updates/start")][1](types.SimpleNamespace(query={}, body={}))
        self.assertIn("Finish disk preparation", str(refused.exception))
        self.assertEqual([], self.alerts(), "someone is looking at this one")

    def test_a_run_going_when_the_window_opens_is_no_skip(self):
        ROLLOUT.OPS._write([])
        self.hosts.codes["node-b"] = None
        ROLLOUT.start("asked-hosts", only=["node-b"], now=SUNDAY - 60)
        ROLLOUT.tick(now=SUNDAY)
        self.assertEqual("", self.line())


class HoldTimingTests(Runs):
    """A real set_hold can take well over a minute (hostrun: up to 60 s for its
    helper, 45 s for the script, more on a refused exec). The stand-in moves the
    clock on as a real try would."""
    def setUp(self):
        super().setUp()
        self.t = 0
        patcher = mock.patch.object(ROLLOUT, "clock", lambda: self.t)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tries = []
        self.hosts.codes["node-b"] = None    # still installing: the run stays on one step a tick

    def slow_failure(self, seconds=150, *names):
        real = self.hosts.set_hold

        def set_hold(name, hold):
            if hold and name in names:
                self.tries.append(name)
                self.t += seconds
                raise TimeoutError("timed out")
            return real(name, hold)
        self.hosts.set_hold = set_hold

    def test_the_wait_counts_from_when_the_try_ended(self):
        self.slow_failure(150, "node-c")
        ROLLOUT.start("asked-hosts", only=["node-b"], now=1000)
        ROLLOUT.tick(now=1000)
        self.assertEqual(["node-c"], self.tries)
        self.assertEqual(1000 + 150 + ROLLOUT.HOLD_RETRY, ROLLOUT._load()["rollout"]["hold_failed"]["node-c"]["retry_at"])
        for now in (1150, 1170, 1209):
            ROLLOUT.tick(now=now)
        self.assertEqual(1, len(self.tries), "not tried again as soon as the last try ended")
        ROLLOUT.tick(now=1210)
        self.assertEqual(2, len(self.tries))

    def test_at_most_one_host_fails_per_step(self):
        self.slow_failure(150, "node-a", "node-c")
        log = self.tries
        real_step = ROLLOUT.step
        with mock.patch.object(ROLLOUT, "step", side_effect=lambda *a, **k: log.append("step") or real_step(*a, **k)):
            ROLLOUT.start("asked-hosts", only=["node-b"], now=1000)
            for now in range(1000, 2000, 20):
                ROLLOUT.tick(now=now)
        for one, two in zip(log, log[1:]):
            self.assertFalse(one != "step" and two != "step", log[:20])
        self.assertIn("node-a", log)
        self.assertIn("node-c", log, "one failing host doesn't keep the next from being tried")

    def test_a_host_read_for_an_unread_host_is_spaced_the_same(self):
        self.hosts.facts.pop("node-c")
        real = self.hosts.read

        def read(name, refresh=False):
            if name == "node-c":
                self.tries.append(name)
                self.t += 150
            return real(name, refresh)
        self.hosts.read = read
        ROLLOUT.start("asked-hosts", only=["node-b"], now=1000)
        ROLLOUT.tick(now=1000)
        ROLLOUT.tick(now=1150)
        self.assertEqual(1, len(self.tries))
        ROLLOUT.tick(now=1210)
        self.assertEqual(2, len(self.tries))

    def test_a_try_does_not_hold_the_lock_stop_needs(self):
        real, stopped = self.hosts.set_hold, []

        def set_hold(name, hold):
            if hold and not stopped:
                # Another thread, as a request on another replica: the lock is
                # re-entrant for this one.
                other = threading.Thread(target=lambda: stopped.append(ROLLOUT.stop()), daemon=True)
                other.start()
                other.join(5)
                stopped.append(None) if not stopped else None
            return real(name, hold)
        self.hosts.set_hold = set_hold
        ROLLOUT.start("asked-hosts", only=["node-b"], now=1000)
        ROLLOUT.tick(now=1000)
        self.assertTrue(stopped[0], "Stop got through while a hold was being tried")
        self.assertTrue(ROLLOUT._load()["rollout"]["stop"], "and what the try wrote afterwards kept it")

    def test_a_try_does_not_hold_the_lock_settings_need_and_keeps_them(self):
        real, saved = self.hosts.set_hold, []

        def set_hold(name, hold):
            if hold and not saved:
                other = threading.Thread(target=lambda: saved.append(ROLLOUT.save_settings({"single_copy": True})), daemon=True)
                other.start()
                other.join(5)
                saved.append(None) if not saved else None
            raise TimeoutError("timed out")
        self.hosts.set_hold = set_hold
        ROLLOUT.start("asked-hosts", only=["node-b"], now=1000)
        ROLLOUT.tick(now=1000)
        self.assertTrue(saved[0])
        self.assertTrue(ROLLOUT.settings()["single_copy"], "the failure's save didn't put the old settings back")

    def test_a_try_that_outlives_its_run_writes_nothing_to_the_next_one(self):
        real = self.hosts.set_hold

        def set_hold(name, hold):
            if hold and name == "node-c":
                self.hosts.set_hold = real
                with ROLLOUT._lock:
                    state = ROLLOUT._load()
                    state["rollout"]["id"] = "os-another"
                    ROLLOUT._save(state)
                raise TimeoutError("timed out")
            return real(name, hold)
        self.hosts.set_hold = set_hold
        ROLLOUT.start("asked-hosts", only=["node-b"], now=1000)
        ROLLOUT.tick(now=1000)
        rollout = ROLLOUT._load()["rollout"]
        self.assertEqual("os-another", rollout["id"])
        self.assertEqual({}, rollout.get("hold_failed") or {})

    def test_slow_tries_do_not_reset_the_failure_streak(self):
        # A step that keeps raising ends the run after ERROR_LIMIT. Time spent in a
        # hold's try between two errors must not read as "Homestead was down".
        self.assertGreater(400, ROLLOUT.ERROR_GAP)
        self.slow_failure(400, "node-c")
        real = self.hosts.read
        self.hosts.read = lambda name, refresh=False: (_ for _ in ()).throw(ConnectionError("no answer")) if name == "node-b" else real(name, refresh)
        ROLLOUT.start("asked-hosts", only=["node-b"], now=1000)
        # The leader's tick reads the real time at each step: it passes no now.
        with mock.patch.object(ROLLOUT, "time", types.SimpleNamespace(time=lambda: 1000 + self.t)):
            for _ in range(200):
                ROLLOUT.tick()
                self.t += 20
                if ROLLOUT._load()["rollout"]["status"] != "running":
                    break
        rollout = ROLLOUT._load()["rollout"]
        self.assertEqual("failed", rollout["status"])
        self.assertIn("no answer", rollout["results"][0]["note"])


class Waiting(Runs):
    def setUp(self):
        super().setUp()
        self.own = "node-c"

    def waiting(self, only=("node-a",), now=1000):
        ROLLOUT.start("asked-hosts", only=list(only), now=now)
        for _ in range(12):
            ROLLOUT.tick(now=now)
            rollout = ROLLOUT._load()["rollout"]
            if rollout.get("node", {}).get("waiting"):
                return rollout
        self.fail(f"it did not wait: {rollout.get('phase')} {rollout.get('message')}")


class StopDuringAWaitTests(Waiting):
    """Andreas, 5 Nov: Stop during any wait for a restart ends the run at once,
    without restarting that host. It stays installed and waits for a person."""
    def assert_stopped_without_restart(self, now=1020):
        ended = ROLLOUT._load()["rollout"]
        self.assertEqual("stopped", ended["status"], ended["message"])
        self.assertEqual([], self.sent)
        row = ended["results"][0]
        self.assertEqual("node-a", row["node"])
        self.assertTrue(row["restart_needed"], "its row offers Restart…")
        self.assertIn("It stopped while waiting", row["note"])
        self.assertIn("needing a restart: node-a", ended["message"])
        self.assertIn("node-a", self.hosts.began, "it stays installed")
        return ended

    def test_stop_during_longhorns_wait_ends_the_run_and_a_blocker_that_clears_does_not_restart(self):
        self.add(*engines_elsewhere_on_node_a())
        self.waiting()
        self.assertIn("Stopping after node-a", ROLLOUT.stop()["message"])
        for obj in engines_elsewhere_on_node_a():
            self.kube.discard(self.kube.path_of(obj))
        ROLLOUT.tick(now=1020)
        self.assert_stopped_without_restart()
        ROLLOUT.tick(now=1040)
        self.assertEqual([], self.sent)

    def test_stop_during_a_read_error_wait(self):
        self.kube.fail(BLOCKERS.ORPHANS, 503, times=100000)
        self.waiting()
        ROLLOUT.stop()
        self.kube._failures.clear()
        ROLLOUT.tick(now=1020)
        self.assert_stopped_without_restart()

    def test_stop_during_the_wait_for_copies(self):
        ROLLOUT.reboot = mock.Mock(side_effect=BLOCKERS.MayClear("a volume has its only healthy copy on this host: lab/data (the settings do not accept that)"))
        rollout = self.waiting()
        self.assertIn("volume copies", rollout["message"])
        ROLLOUT.stop()
        calls = ROLLOUT.reboot.call_count
        ROLLOUT.tick(now=1020)
        self.assertEqual(calls, ROLLOUT.reboot.call_count, "not asked again")
        self.assert_stopped_without_restart()

    def test_stop_during_a_busy_api_wait(self):
        ROLLOUT.reboot = mock.Mock(side_effect=urllib.error.HTTPError("/x", 503, "busy", {}, None))
        self.waiting()
        ROLLOUT.stop()
        ROLLOUT.reboot = mock.Mock(return_value="op-too-late")
        ROLLOUT.tick(now=1020)
        ROLLOUT.reboot.assert_not_called()
        self.assert_stopped_without_restart()

    def test_stopping_is_not_overwritten_by_the_waiting_message(self):
        self.add(*engines_elsewhere_on_node_a())
        rollout = self.waiting()
        self.assertIn("waiting up to 15 minutes", rollout["message"])
        ROLLOUT.stop()
        with mock.patch.object(ROLLOUT, "_finish", side_effect=lambda state, roll, now: None) as finish:
            ROLLOUT.tick(now=1020)
        self.assertTrue(finish.called)
        self.assertIn("Stopping after node-a", ROLLOUT._load()["rollout"]["message"])

    def test_the_hosts_after_it_are_not_touched(self):
        self.add(*engines_elsewhere_on_node_a())
        self.waiting(only=("node-a", "node-b"))
        ROLLOUT.stop()
        ROLLOUT.tick(now=1020)
        ended = ROLLOUT._load()["rollout"]
        self.assertEqual("stopped", ended["status"])
        self.assertEqual(["node-a"], self.hosts.began)
        self.assertEqual(["node-a", "node-b", "node-c"], self.holds(False), "holds let go")

    def test_its_cordon_is_given_back(self):
        cordons = []
        ROLLOUT.set_cordon = lambda name, on: cordons.append((name, on))
        self.add(*engines_elsewhere_on_node_a())
        self.waiting()
        ROLLOUT.stop()
        ROLLOUT.tick(now=1020)
        self.assertEqual([("node-a", False)], cordons)

    def test_stop_set_when_a_refusal_would_start_a_wait_does_not_wait(self):
        ROLLOUT.reboot = mock.Mock(side_effect=lambda *_: (_ for _ in ()).throw(BLOCKERS.MayClear("engines still listed")))
        self.hosts.codes["node-a"] = None
        ROLLOUT.start("asked-hosts", only=["node-a"], now=1000)
        ROLLOUT.tick(now=1000)
        ROLLOUT.stop()
        self.hosts.codes["node-a"] = "0"
        for now in (1020, 1040):
            ROLLOUT.tick(now=now)
        ended = ROLLOUT._load()["rollout"]
        self.assertEqual("stopped", ended["status"])
        self.assertEqual(1, ROLLOUT.reboot.call_count)
        self.assertIn("engines still listed", ended["results"][0]["note"])

    def test_stop_with_no_wait_still_finishes_the_host_in_hand(self):
        # As before: only a wait ends at once.
        self.hosts.codes["node-a"] = None
        ROLLOUT.start("asked-hosts", only=["node-a"], now=1000)
        ROLLOUT.tick(now=1000)
        ROLLOUT.stop()
        self.hosts.codes["node-a"] = "0"
        for now in (1020, 1040):
            ROLLOUT.tick(now=now)
        self.assertEqual(["node-a"], self.sent)

    def test_a_restart_already_under_way_is_followed_not_abandoned(self):
        self.add(*engines_elsewhere_on_node_a())
        self.waiting()
        ROLLOUT.power_job = lambda name, since: "op-adopted"
        ROLLOUT.stop()
        ROLLOUT.tick(now=1020)
        self.assertNotIn("It stopped while waiting", str(ROLLOUT._load()["rollout"]["results"]))


class StopDuringAWaitInMaintenanceTests(Waiting):
    """Stop during a wait is checked before #535's maintenance check: a host a
    person put in maintenance meanwhile, or whose maintenance can't be read,
    still ends the run as stopped, and the maintenance's cordon stays."""
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(HISTORY, "DATA_DIR", self.data.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cordons = []
        ROLLOUT.set_cordon = lambda name, on: self.cordons.append((name, on))
        self.add(*engines_elsewhere_on_node_a())
        self.waiting()
        ROLLOUT.stop()

    def assert_stopped_keeping_the_cordon(self):
        ROLLOUT.tick(now=1020)
        ended = ROLLOUT._load()["rollout"]
        self.assertEqual("stopped", ended["status"], ended["message"])
        self.assertIn("It stopped while waiting", ended["results"][0]["note"])
        self.assertEqual([], self.sent)
        self.assertNotIn(("node-a", False), self.cordons, "the maintenance's cordon stays")

    def test_a_host_put_in_maintenance_during_the_wait(self):
        HISTORY.maintenance_declare("node-a", "Drive swap", "op-9", 9_999_999_999, cordoned_before=False, node_uid="u")
        self.assert_stopped_keeping_the_cordon()

    def test_maintenance_that_cannot_be_read_during_the_wait(self):
        with open(HISTORY._path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assert_stopped_keeping_the_cordon()
        self.assertFalse(ROLLOUT._load()["rollout"].get("failed"), "not left failing")


class MixedRefusalTests(FakeCluster):
    """A blocker that may clear beside anything that won't: refused at once, all named."""
    def plan(self, **extra):
        return {**power.plan("node-a", "reboot"), **extra}

    def refused(self, plan):
        with mock.patch.object(POWER_JOBS.POWER, "plan", return_value=plan):
            with self.assertRaises(ValueError) as refused:
                POWER_JOBS.rollout_reboot("node-a")
        return refused.exception

    def setUp(self):
        super().setUp()
        self.add(*engines_elsewhere_on_node_a())

    def test_alone_it_may_clear(self):
        self.assertIsInstance(self.refused(self.plan()), BLOCKERS.MayClear)

    def test_beside_workloads_with_nowhere_to_go(self):
        error = self.refused(self.plan(stranded=[{"ns": "apps", "name": "db"}]))
        self.assertNotIsInstance(error, BLOCKERS.MayClear)
        self.assertIn("still lists engine", str(error))
        self.assertIn("no other host", str(error))

    def test_beside_running_vms(self):
        error = self.refused(self.plan(hold=[{"kind": "VirtualMachine", "ns": "vms", "name": "win", "default": "move", "id": "v"}]))
        self.assertNotIsInstance(error, BLOCKERS.MayClear)
        self.assertIn("still lists engine", str(error))
        self.assertIn("Running VMs", str(error))

    def test_beside_apps_that_would_wait(self):
        error = self.refused(self.plan(hold=[{"kind": "Deployment", "ns": "apps", "name": "web", "default": "wait", "id": "w"}]))
        self.assertNotIsInstance(error, BLOCKERS.MayClear)
        self.assertIn("apps/web", str(error))
        self.assertIn("still lists engine", str(error))

    def test_beside_data_kept_on_the_host(self):
        error = self.refused(self.plan(requires_data_ack=True, volumes=[], maintenance={"local_storage": [
            {"pod": "apps/db", "kind": "host-local path", "source": "/srv/db"}]}))
        self.assertNotIsInstance(error, BLOCKERS.MayClear)
        self.assertIn("apps/db", str(error))
        self.assertIn("still lists engine", str(error))

    def test_beside_a_stop_the_review_itself_has(self):
        error = self.refused(self.plan(ready=False, blockers=["The host boot ID is unavailable"]))
        self.assertNotIsInstance(error, BLOCKERS.MayClear)
        self.assertIn("boot ID", str(error))
        self.assertIn("still lists engine", str(error))

    def test_an_only_copy_beside_data_kept_is_refused_at_once(self):
        error = self.refused(self.plan(requires_data_ack=True, drain_blockers=[], requires_blocker_ack=False,
                                       volumes=[{"claim": "lab/data", "risk": "single-copy"}],
                                       maintenance={"local_storage": [{"pod": "apps/db", "kind": "host-local path", "source": "/srv/db"}]}))
        self.assertNotIsInstance(error, BLOCKERS.MayClear)
        self.assertIn("lab/data", str(error))

    def test_a_person_accepting_only_copies_in_settings_leaves_the_blocker(self):
        error = self.refused(self.plan(requires_data_ack=True, volumes=[{"claim": "lab/data", "risk": "single-copy"}], maintenance={}))
        self.assertIsInstance(error, BLOCKERS.MayClear)
        self.assertIn("lab/data", str(error))
        self.assertIn("still lists engine", str(error))


class MayWaitInTheReviewTests(Runs):
    def test_the_review_says_what_may_clear_apart_from_what_wont(self):
        self.add(*engines_elsewhere_on_node_a())
        row = ROLLOUT.preview(["node-a"])["hosts"][0]
        self.assertIn("still lists engine", row["restart_refusal"])
        self.assertTrue(row["restart_may_clear"])
        self.add(*stuck_pod_on_node_a())
        row = ROLLOUT.preview(["node-a"])["hosts"][0]
        self.assertFalse(row["restart_may_clear"])

    def test_nothing_in_the_way_is_not_may_clear(self):
        row = ROLLOUT.preview(["node-a"])["hosts"][0]
        self.assertEqual(("", False), (row["restart_refusal"], row["restart_may_clear"]))

    def test_may_clear_changing_makes_the_review_stale(self):
        self.add(*engines_elsewhere_on_node_a())
        with mock.patch.object(ROLLOUT, "restart_check", return_value=("same words", True)):
            first = ROLLOUT.preview(["node-a"])["review_token"]
        with mock.patch.object(ROLLOUT, "restart_check", return_value=("same words", False)):
            self.assertNotEqual(first, ROLLOUT.preview(["node-a"])["review_token"])


class WindowBlipTests(Runs):
    """A read that failed hasn't lost the window: it says so, looks again, and only
    raises the alert if the hour ends with the window lost."""
    def setUp(self):
        super().setUp()
        ROLLOUT.save_settings({"schedule": {"enabled": True, "days": ["sun"], "hour": 3, "tz": "UTC"}})

    def alerts(self):
        return ALERTS.log(categories=["degraded"])["alerts"]

    def skip(self):
        return ROLLOUT.report().get("window_skip") or {}

    def blip(self, now=SUNDAY):
        # The tick's own node read passes; start's fails.
        self.kube.fail("/api/v1/nodes", 503, times=1, skip=1)
        ROLLOUT.tick(now=now)
        self.kube._failures.clear()

    def test_a_503_from_start_says_what_it_was_not_the_job_list(self):
        self.blip()
        self.assertNotIn("job list", self.skip()["why"])
        self.assertIn("503", self.skip()["why"])
        self.assertTrue(self.skip()["blip"])
        self.assertEqual({}, ROLLOUT._load().get("rollout") or {})

    def test_it_raises_no_alert_and_runs_at_its_next_look(self):
        self.blip()
        self.assertEqual([], self.alerts())
        ROLLOUT.tick(now=SUNDAY + ROLLOUT.WINDOW_RETRY)
        self.assertEqual("schedule", ROLLOUT._load()["rollout"]["reason"])
        self.assertEqual({}, self.skip())
        self.assertEqual([], self.alerts(), "the window was never lost")

    def test_a_job_list_that_cant_be_read_says_that_and_raises_no_alert_yet(self):
        with mock.patch.object(ROLLOUT.OPS, "stored", side_effect=OSError("damaged")):
            ROLLOUT.tick(now=SUNDAY)
        self.assertIn("job list could not be read", self.skip()["why"])
        self.assertTrue(self.skip()["blip"])
        self.assertEqual([], self.alerts())

    def test_an_hour_that_ends_on_a_blip_has_lost_the_window_so_says_so_once(self):
        self.blip()
        ROLLOUT.tick(now=SUNDAY + 3600 + 60)
        alerts = self.alerts()
        self.assertEqual(1, len(alerts))
        self.assertIn("503", alerts[0]["body"])
        self.assertFalse(self.skip()["blip"], "no longer 'looking again'")
        ROLLOUT.tick(now=SUNDAY + 3600 + 120)
        self.assertEqual(1, len(self.alerts()))

    def test_a_job_in_the_way_after_a_blip_alerts_at_once(self):
        self.blip()
        self.job("disk-allocation", "node-b")
        ROLLOUT.tick(now=SUNDAY + ROLLOUT.WINDOW_RETRY)
        self.assertFalse(self.skip()["blip"])
        self.assertEqual(1, len(self.alerts()))

    def test_a_blip_after_a_job_in_the_way_does_not_alert_twice(self):
        self.job("disk-allocation", "node-b")
        ROLLOUT.tick(now=SUNDAY)
        ROLLOUT.OPS._write([])
        self.blip(now=SUNDAY + ROLLOUT.WINDOW_RETRY)
        ROLLOUT.tick(now=SUNDAY + 3600 + 60)
        self.assertEqual(1, len(self.alerts()))


class AfterAWaitTests(Waiting):
    """Once a wait is over the host isn't waiting: the dialog's Stop goes back to
    what it was, on the state the server really leaves."""
    def test_a_cleared_wait_leaves_no_trace_in_restarting(self):
        self.add(*engines_elsewhere_on_node_a())
        ROLLOUT.operation = lambda op: {"id": op, "status": "running", "message": "draining"}
        self.waiting()
        for obj in engines_elsewhere_on_node_a():
            self.kube.discard(self.kube.path_of(obj))
        ROLLOUT.tick(now=1020)
        rollout = ROLLOUT._load()["rollout"]
        self.assertEqual("restarting", rollout["phase"])
        self.assertEqual(["node-a"], self.sent)
        self.assertNotIn("waiting", rollout["node"])
        self.assertNotIn("wait_why", rollout["node"])
        # tests/os-updates-hosts-view.test.js paints this state.
        self.assertEqual(["attempts", "copies_since", "cordoned_before", "name", "op", "security", "since", "updates"],
                         sorted(k for k in rollout["node"] if k != "lease"))

    def test_an_adopted_job_clears_it_too(self):
        self.add(*engines_elsewhere_on_node_a())
        ROLLOUT.operation = lambda op: {"id": op, "status": "running"}
        self.waiting()
        ROLLOUT.power_job = lambda name, since: "op-adopted"
        ROLLOUT.tick(now=1020)
        rollout = ROLLOUT._load()["rollout"]
        self.assertEqual("restarting", rollout["phase"])
        self.assertNotIn("waiting", rollout["node"])
        self.assertNotIn("wait_why", rollout["node"])


class StoppedJobEndsTests(Runs):
    """A stopped run is over: its job has to end too, or it holds up what waits
    for os-rollout jobs (a host's promotion, a disk conversion)."""
    def setUp(self):
        super().setUp()
        keep = dict(ROLLOUT.OPS.RESOLVERS)
        self.addCleanup(lambda: (ROLLOUT.OPS.RESOLVERS.clear(), ROLLOUT.OPS.RESOLVERS.update(keep)))
        ROLLOUT.register(ROLLOUT.OPS)

    def stopped_run(self):
        ROLLOUT.start("asked-hosts", only=["node-b", "node-c"], now=1000)
        ROLLOUT.stop()
        done = self.run_it()
        self.assertEqual("stopped", done["status"], "the run itself still says stopped")
        return done

    def job(self):
        return next(i for i in ROLLOUT.OPS.list_operations() if i["kind"] == "os-rollout")

    def test_its_job_ends_with_the_runs_message(self):
        done = self.stopped_run()
        item = next(i for i in ROLLOUT.OPS.list_operations() if i["kind"] == "os-rollout")
        self.assertIn(item["status"], ROLLOUT.OPS.TERMINAL)
        self.assertEqual("cancelled", item["status"])
        self.assertEqual(done["message"], item["message"])
        self.assertTrue(next(i for i in ROLLOUT.OPS.stored() if i["kind"] == "os-rollout")["finished_at"])

    def test_nothing_waiting_on_os_rollout_jobs_sees_it_running(self):
        import homestead_storage_conflicts as CONFLICTS
        self.stopped_run()
        self.job()   # the job loop's round
        self.assertEqual([], [o for o in ROLLOUT.OPS.snapshot() if o.get("status") not in ROLLOUT.OPS.TERMINAL],
                         "homestead_promote's list of jobs still running")
        CONFLICTS.require_clear(ROLLOUT.OPS.stored(), "disk-allocation", {"node": "node-b"}, {"kind": "Node", "name": "node-b"})

    def test_a_run_replaced_since_still_ends_its_job(self):
        self.stopped_run()
        first = self.job()["id"]
        ROLLOUT.start("asked-hosts", only=["node-b"], now=2000)
        item = next(i for i in ROLLOUT.OPS.list_operations() if i["id"] == first)
        self.assertEqual("cancelled", item["status"])

    def test_a_job_read_while_its_run_goes_is_not_ended(self):
        ROLLOUT.start("asked-hosts", only=["node-b", "node-c"], now=1000)
        self.assertNotIn(self.job()["status"], ROLLOUT.OPS.TERMINAL)

    def test_a_finished_run_keeps_succeeded_and_a_failed_one_failed(self):
        ROLLOUT.start("asked-hosts", only=["node-b"], now=1000)
        self.run_it()
        self.assertEqual("succeeded", self.job()["status"])
        ROLLOUT.OPS._write([])
        self.hosts.codes["node-c"] = "100"
        ROLLOUT.start("asked-hosts", only=["node-c"], now=2000)
        self.run_it()
        self.assertEqual("failed", self.job()["status"])


class StopSaysWhatHappensTests(Waiting):
    def stop_route(self):
        return ROLLOUT.ROUTES[("POST", "/api/os-updates/stop")][1](types.SimpleNamespace(query={}, body={}))["detail"]

    def test_stopping_during_a_wait_says_the_host_wont_restart(self):
        self.add(*engines_elsewhere_on_node_a())
        self.waiting()
        said = self.stop_route()
        self.assertIn("node-a", said)
        self.assertIn("does not restart", said)
        self.assertNotIn("once the host being updated is done", said)

    def test_stopping_with_no_wait_says_what_it_did(self):
        self.hosts.codes["node-a"] = None
        ROLLOUT.start("asked-hosts", only=["node-a"], now=1000)
        ROLLOUT.tick(now=1000)
        self.assertIn("once the host being updated is done", self.stop_route())


class StopBeforeAnyStepTests(Runs):
    """Rounds of holds with no step, Stop before the first tick, then a tick: holds
    land and are let go, and no host is asked to install, refresh or restart anything."""
    def test_it_holds_and_lets_go_and_touches_no_package(self):
        reads = []
        real = self.hosts.read
        self.hosts.read = lambda name, refresh=False: reads.append((name, refresh)) or real(name, refresh)
        self.hosts.facts.pop("node-c")
        self.hosts.facts["node-c"] = host_facts(["vim"])
        ROLLOUT.start("asked-hosts", only=["node-a"], restart="never", now=1000)
        for now in (1000, 1060):
            ROLLOUT._hold_round(now)
        self.assertEqual(["node-a", "node-b", "node-c"], self.holds(True))
        self.assertEqual("hold", ROLLOUT._load()["rollout"]["phase"], "no step ran")
        ROLLOUT.stop()
        ROLLOUT.tick(now=1120)
        ended = ROLLOUT._load()["rollout"]
        self.assertEqual("stopped", ended["status"])
        self.assertEqual(["node-a", "node-b", "node-c"], self.holds(False))
        self.assertEqual([], self.hosts.began, "nothing installed")
        self.assertEqual([], self.sent, "nothing restarted")
        self.assertEqual([], [r for r in reads if r[1]], "no package lists refreshed")
        self.assertEqual([], ended["results"])


if __name__ == "__main__":
    unittest.main()
