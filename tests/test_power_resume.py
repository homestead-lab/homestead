"""A host power job carries on when the drain evicts the Homestead replica
running it, or when that replica's container restarts in place.

Rebooting k3s-1 drained it; the drain evicted the Homestead pod on k3s-1,
which was the one running the job, and four minutes later the job was failed
with the host left cordoned and no command sent."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_node_actions as NODE_ACTIONS
import homestead_power as POWER
import homestead_lifecycle as LC
import homestead_maintenance as MAINTENANCE
from fakes.job_store import put


def job(phase="draining", age=60, worker="homestead-old", plan=True):
    return {"id": "job-1", "progress": 10, "message": "Evicting pods",
            "ref": {"node": "k3s-1", "action": "reboot", "phase": phase, "phase_at": time.time() - age,
                    **({"worker": worker} if worker else {}),
                    **({"plan": {"node": "k3s-1", "action": "reboot", "drain_pods": []}} if plan else {})}}


class ResolverTests(unittest.TestCase):
    def status(self, item, gone, resumes=True):
        with mock.patch.object(POWER, "WORKER_GONE", lambda pod, restarts=None: gone), \
             mock.patch.object(POWER, "RESUME", mock.Mock(return_value=resumes)) as resume:
            return POWER.status(item), resume

    def test_a_job_whose_replica_was_evicted_is_carried_on(self):
        (state, _, message), resume = self.status(job(), gone=True)
        self.assertEqual("running", state)
        self.assertIn("carrying on", message)
        resume.assert_called_once()

    def test_a_live_worker_may_wait_on_longhorn_for_longer_than_four_minutes(self):
        (state, _, _), resume = self.status(job(age=600), gone=False)
        self.assertEqual("running", state)
        resume.assert_not_called()
        (state, _, _), _ = self.status(job(age=2000), gone=False)
        self.assertEqual("failed", state, "a worker silent for half an hour has stopped")

    def test_the_resolver_asks_about_the_count_the_job_recorded(self):
        asked = []
        item = job()
        item["ref"]["worker_restarts"] = 4
        with mock.patch.object(POWER, "WORKER_GONE", lambda pod, restarts=None: asked.append((pod, restarts)) or False), \
             mock.patch.object(POWER, "RESUME", mock.Mock(return_value=True)):
            POWER.status(item)
        self.assertEqual([("homestead-old", 4)], asked)

    def test_older_jobs_without_a_named_worker_keep_the_old_limit(self):
        (state, _, _), resume = self.status(job(age=300, worker=None), gone=True)
        self.assertEqual("failed", state)
        resume.assert_not_called()


class ResumeTests(unittest.TestCase):
    def test_only_the_leader_resumes_and_only_once(self):
        started = []
        with mock.patch.object(server.LEADER, "is_leader", return_value=False):
            self.assertFalse(server.POWER_JOBS.resume_power_job(job()))
        with mock.patch.object(server.LEADER, "is_leader", return_value=True), \
             mock.patch.object(server.OPS, "record_phase") as claim, \
             mock.patch.object(server.POWER_JOBS, "_power_in_background", side_effect=lambda *a, **k: started.append((a, k))):
            server.POWER_JOBS._POWER_RESUMING.clear()
            self.assertTrue(server.POWER_JOBS.resume_power_job(job()))
            self.assertTrue(server.POWER_JOBS.resume_power_job(job()), "a second ask is the same resume")
            for _ in range(50):
                if started: break
                time.sleep(0.02)
        self.assertEqual(1, len(started))
        self.assertTrue(started[0][1]["resumed"])
        self.assertEqual(server.POWER_JOBS.POD_NAME, claim.call_args.kwargs["worker"], "the job now names this replica")
        server.POWER_JOBS._POWER_RESUMING.clear()

    def test_the_claim_records_the_new_count_and_only_takes_a_job_not_yet_sent(self):
        with mock.patch.object(server.LEADER, "is_leader", return_value=True), \
             mock.patch.object(server.POWER_JOBS, "own_restarts", return_value=6), \
             mock.patch.object(server.OPS, "record_phase") as claim, \
             mock.patch.object(server.POWER_JOBS, "_power_in_background"):
            server.POWER_JOBS._POWER_RESUMING.clear()
            server.POWER_JOBS.resume_power_job(job())
            for _ in range(50):
                if claim.called: break
                time.sleep(0.02)
        server.POWER_JOBS._POWER_RESUMING.clear()
        kwargs = claim.call_args.kwargs
        self.assertEqual(6, kwargs["worker_restarts"])
        self.assertEqual("homestead-old", kwargs["expect_worker"], "only while the job still names the worker that went")
        self.assertEqual(POWER.BEFORE_SEND, kwargs["before"], "and only before power was sent")

    def test_a_job_running_here_is_never_started_a_second_time_here(self):
        # An OS-update reboot runs its power job in the leader's own tick, not in the background.
        inside = []
        def node_power(*args, progress=None, **kwargs):
            inside.append(server.POWER_JOBS.resume_power_job(job()))
            raise server.POWER_JOBS.PowerNotSent("stop here")
        with mock.patch.object(server.LEADER, "is_leader", return_value=True), \
             mock.patch.object(server.OPS, "record_phase"), \
             mock.patch.object(NODE_ACTIONS, "node_power", side_effect=node_power), \
             mock.patch.object(server.POWER_JOBS, "_power_in_background") as again:
            server.POWER_JOBS._POWER_RESUMING.clear()
            with self.assertRaises(server.POWER_JOBS.PowerNotSent):
                server.POWER_JOBS.run_power_job("job-1", {"node": "k3s-1", "action": "reboot", "drain_pods": [],
                                                          "planned_outage": False})
        self.assertEqual([True], inside, "asked while it runs, it is the same run")
        again.assert_not_called()
        self.assertNotIn("job-1", server.POWER_JOBS._POWER_RESUMING, "and it is let go when the run ends")

    def test_a_job_saved_without_its_plan_is_not_resumed(self):
        with mock.patch.object(server.LEADER, "is_leader", return_value=True):
            self.assertFalse(server.POWER_JOBS.resume_power_job(job(plan=False)))


class OneOwnerTests(unittest.TestCase):
    """With two or three Homestead copies, an evicted replica can still be
    shutting down when another carries its job on: only one may send."""
    def setUp(self):
        import tempfile
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(server.OPS, "DATA_DIR", directory.name),
                      mock.patch.object(server.OPS, "WRITE_GUARD", None)):
            patch.start(); self.addCleanup(patch.stop)
        put(server.OPS, [{"id": "job-1", "kind": "node-power", "status": "running", "progress": 10, "message": "",
                            "history": [], "ref": {"node": "k3s-1", "phase": "draining", "worker": "homestead-new"}}])

    def test_a_claim_refuses_a_job_another_worker_took_or_that_has_sent(self):
        with self.assertRaises(server.OPS.Superseded):
            server.OPS.record_phase("job-1", "draining", 10, "carrying on", expect_worker="homestead-old",
                                    before=POWER.BEFORE_SEND)
        server.OPS.record_phase("job-1", "sending", 20, "Submitting power helper", owner="homestead-new")
        with self.assertRaises(server.OPS.Superseded):
            server.OPS.record_phase("job-1", "sending", 20, "carrying on", expect_worker="homestead-new",
                                    before=POWER.BEFORE_SEND)

    def test_a_superseded_worker_records_nothing_and_never_sends(self):
        plan = {"node": "k3s-1", "action": "reboot", "drain_pods": [], "planned_outage": False}
        sent = []
        def node_power(*args, progress=None, **kwargs):
            progress("sending", 20, "Submitting power helper")   # what precedes the send
            sent.append(True)
            return {}
        with mock.patch.object(server.POWER_JOBS, "POD_NAME", "homestead-old"),              mock.patch.object(NODE_ACTIONS, "node_power", side_effect=node_power):
            with self.assertRaises(server.OPS.Superseded):
                server.POWER_JOBS.run_power_job("job-1", plan)
        self.assertEqual([], sent)
        saved = server.OPS.stored()[0]
        self.assertEqual(("running", "draining"), (saved["status"], saved["ref"]["phase"]), "the job is left to its owner")

    def test_its_owner_carries_on(self):
        server.OPS.record_phase("job-1", "verifying", 15, "Rechecking", owner="homestead-new")
        self.assertEqual("verifying", server.OPS.stored()[0]["ref"]["phase"])


class WorkerGoneTests(unittest.TestCase):
    def gone(self, pod=None, node_ready=True, missing=False, restarts=None):
        import urllib.error
        def kget(path):
            if "/pods/" in path:
                if missing:
                    raise urllib.error.HTTPError(path, 404, "gone", None, None)
                return pod or {"metadata": {}, "spec": {"nodeName": "k3s-1"}, "status": {"phase": "Running"}}
            return {"status": {"conditions": [{"type": "Ready", "status": "True" if node_ready else "Unknown"}]}}
        with mock.patch.object(server.POWER_JOBS, "kget", kget):
            return server.POWER_JOBS.power_worker_gone("homestead-old", restarts)

    def test_evicted_deleted_or_on_a_host_that_stopped_answering(self):
        self.assertTrue(self.gone(missing=True))
        self.assertTrue(self.gone({"metadata": {"deletionTimestamp": "now"}, "spec": {}, "status": {}}))
        self.assertTrue(self.gone(node_ready=False), "a node shut down leaves its pods listed for minutes")
        self.assertFalse(self.gone())

    def test_a_worker_whose_container_restarted_has_gone(self):
        # Same pod, same name, Ready - but the process that ran the job died with its container.
        pod = {"metadata": {}, "spec": {"nodeName": "k3s-1"},
               "status": {"phase": "Running", "containerStatuses": [{"name": "homestead", "restartCount": 2}]}}
        self.assertTrue(self.gone(pod, restarts=1))
        self.assertFalse(self.gone(pod, restarts=2), "no restart since the job started")
        self.assertFalse(self.gone(pod), "a job that recorded no count keeps the old check")

    def test_only_homesteads_own_container_counts(self):
        pod = {"metadata": {}, "spec": {"nodeName": "k3s-1"},
               "status": {"phase": "Running", "containerStatuses": [{"name": "homestead", "restartCount": 1},
                                                                    {"name": "mesh-proxy", "restartCount": 7}]}}
        self.assertFalse(self.gone(pod, restarts=1), "a sidecar restarting is not the worker going")
        no_homestead = {"metadata": {}, "spec": {"nodeName": "k3s-1"},
                        "status": {"phase": "Running", "containerStatuses": [{"name": "other", "restartCount": 9}]}}
        self.assertFalse(self.gone(no_homestead, restarts=0), "no homestead container: not judged by the count")

    def test_the_worker_records_its_restart_count_when_it_starts(self):
        pod = {"status": {"containerStatuses": [{"name": "homestead", "restartCount": 3}]}}
        with mock.patch.object(server.POWER_JOBS, "kget", lambda path: pod), \
             mock.patch.object(server.POWER_JOBS, "POD_NAME", "homestead-old"):
            self.assertEqual(3, server.POWER_JOBS.own_restarts())
        def unreadable(path):
            raise OSError("no answer")
        with mock.patch.object(server.POWER_JOBS, "kget", unreadable), \
             mock.patch.object(server.POWER_JOBS, "POD_NAME", "homestead-old"):
            self.assertIsNone(server.POWER_JOBS.own_restarts(), "unknown: the job keeps the old check")


class ResumedDrainTests(unittest.TestCase):
    def pod(self, name):
        return {"metadata": {"namespace": "lab", "name": name, "uid": f"uid-{name}"},
                "spec": {"nodeName": "k3s-1", "containers": [{"name": "c"}]}}

    def drain(self, here, reviewed, resumed):
        with mock.patch.object(LC.MAINTENANCE, "items", return_value=here), \
             mock.patch.object(LC, "ksend"), mock.patch.object(LC, "kget", return_value={"items": []}):
            return NODE_ACTIONS.drain("k3s-1", reviewed_pods=reviewed, resumed=resumed)

    def test_pods_already_gone_are_fine_but_new_ones_are_not(self):
        reviewed = [list(row) for row in MAINTENANCE.pod_snapshot([self.pod("a"), self.pod("b")])]
        self.drain([self.pod("b")], reviewed, resumed=True)
        with self.assertRaisesRegex(ValueError, "changed after review"):
            self.drain([self.pod("b"), self.pod("new")], reviewed, resumed=True)
        with self.assertRaisesRegex(ValueError, "changed after review"):
            self.drain([self.pod("b")], reviewed, resumed=False)


if __name__ == "__main__":
    unittest.main()


class UnattendedRestartTests(unittest.TestCase):
    """An OS update restarts hosts with nobody watching: it stops nothing and
    moves no VM unasked, as before VMs could move or wait."""
    def plan(self, hold, **extra):
        return {"ready": True, "blockers": [], "stranded": [], "planned_outage": False, "requires_data_ack": False, "hold": hold,
                **extra}

    def test_scratch_space_alone_does_not_keep_a_host_from_its_restart(self):
        # Every k3s host runs pods with an emptyDir: counted as data at risk,
        # no host was ever restarted by an OS update.
        plan = self.plan([], requires_data_ack=True, volumes=[{"claim": "lab/app-data", "risk": "resync"}],
                         maintenance={"local_storage": [{"pod": "kube-system/metrics-server", "kind": "emptyDir (deleted by drain)",
                                                         "source": "tmp-dir"}]})
        with mock.patch.object(server.POWER, "plan", return_value=plan), \
             mock.patch.object(server.POWER_JOBS, "send_reviewed_power", return_value={"operation": {"id": "op"}}):
            self.assertEqual("op", server.POWER_JOBS.rollout_reboot("k3s-1"))

    def test_the_hosts_own_plumbing_is_not_data(self):
        # Longhorn's CSI attacher mounts kubelet's plugin directory: every
        # host has it, and it kept every host from its restart.
        plan = self.plan([], requires_data_ack=True, volumes=[],
                         maintenance={"local_storage": [
                             {"pod": "longhorn-system/csi-attacher-a", "kind": "host-local path (not moved)",
                              "source": "/var/lib/kubelet/plugins/driver.longhorn.io"},
                             {"pod": "kube-system/svclb-x", "kind": "host-local path (not moved)", "source": "/run/xtables.lock"},
                             {"pod": "longhorn-system/instance-manager-a", "kind": "host-local path (not moved)", "source": "/"},
                             {"pod": "longhorn-system/instance-manager-a", "kind": "host-local path (not moved)",
                              "source": "/var/lib/longhorn/engine-binaries/"}]})
        with mock.patch.object(server.POWER, "plan", return_value=plan), \
             mock.patch.object(server.POWER_JOBS, "send_reviewed_power", return_value={"operation": {"id": "op"}}):
            self.assertEqual("op", server.POWER_JOBS.rollout_reboot("k3s-1"))

    def test_what_keeps_a_host_from_its_restart_is_named(self):
        cases = (({"volumes": [{"claim": "lab/plex-config", "risk": "single-copy"}]},
                  "only healthy copy on this host: lab/plex-config"),
                 ({"maintenance": {"local_storage": [{"pod": "lab/frigate", "kind": "host-local path (not moved)", "source": "/mnt/media"}]}},
                  r"pods keep data on this host itself: lab/frigate \(/mnt/media\)"),
                 ({"storage_unknown": True}, "could not be read"))
        for extra, refusal in cases:
            with self.subTest(refusal=refusal), mock.patch.object(server.POWER, "plan", return_value=self.plan([], requires_data_ack=True, **extra)), \
                    mock.patch.object(server.POWER_JOBS, "send_reviewed_power") as send:
                with self.assertRaisesRegex(ValueError, refusal):
                    server.POWER_JOBS.rollout_reboot("k3s-1")
                send.assert_not_called()

    def test_vms_and_waiting_apps_keep_a_host_from_an_unattended_restart(self):
        vm = {"id": "VirtualMachine/vms/win", "kind": "VirtualMachine", "ns": "vms", "name": "win", "options": ["move"], "default": "move"}
        app = {"id": "Deployment/lab/plex", "kind": "Deployment", "ns": "lab", "name": "plex", "options": ["wait"], "default": "wait"}
        moves = {"id": "Deployment/lab/web", "kind": "Deployment", "ns": "lab", "name": "web", "options": ["move", "wait"], "default": "move"}
        for hold, refusal in (([vm], "Running VMs"), ([app], "stop and wait")):
            with mock.patch.object(server.POWER, "plan", return_value=self.plan(hold)), \
                 mock.patch.object(server.POWER_JOBS, "send_reviewed_power") as send:
                with self.assertRaisesRegex(ValueError, refusal):
                    server.POWER_JOBS.rollout_reboot("k3s-1")
                send.assert_not_called()
        with mock.patch.object(server.POWER, "plan", return_value=self.plan([moves])), \
             mock.patch.object(server.POWER_JOBS, "send_reviewed_power", return_value={"operation": {"id": "op"}}) as send:
            self.assertEqual("op", server.POWER_JOBS.rollout_reboot("k3s-1"))
            self.assertEqual({"Deployment/lab/web": "move"}, send.call_args.args[0]["choices"])


class FailedBeforePowerTests(OneOwnerTests):
    def test_what_was_stopped_starts_again_when_power_is_not_sent(self):
        server.OPS.record_phase("job-1", "holding", 8, "", held=[{"kind": "Deployment", "ns": "lab", "name": "plex", "was": "1"}])
        plan = {"node": "k3s-1", "action": "reboot", "drain_pods": [], "planned_outage": False}
        with mock.patch.object(server.POWER_JOBS, "POD_NAME", "homestead-new"), \
             mock.patch.object(NODE_ACTIONS, "node_power", side_effect=ValueError("drain timed out")), \
             mock.patch.object(server.HOLD, "restore", return_value=(["lab/plex"], [])) as restore:
            with self.assertRaises(server.POWER_JOBS.PowerNotSent):
                server.POWER_JOBS.run_power_job("job-1", plan)
        restore.assert_called_once()
        saved = server.OPS.stored()[0]
        self.assertEqual(["lab/plex"], saved["ref"]["restored"]["started"])
        self.assertIn("Started again: lab/plex", saved["message"])
