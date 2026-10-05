"""A host power job carries on when the drain evicts the Homestead replica
running it.

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
import homestead_power as POWER
import homestead_lifecycle as LC
import homestead_maintenance as MAINTENANCE


def job(phase="draining", age=60, worker="homestead-old", plan=True):
    return {"id": "job-1", "progress": 10, "message": "Evicting pods",
            "ref": {"node": "k3s-1", "action": "reboot", "phase": phase, "phase_at": time.time() - age,
                    **({"worker": worker} if worker else {}),
                    **({"plan": {"node": "k3s-1", "action": "reboot", "drain_pods": []}} if plan else {})}}


class ResolverTests(unittest.TestCase):
    def status(self, item, gone, resumes=True):
        with mock.patch.object(POWER, "WORKER_GONE", lambda pod: gone), \
             mock.patch.object(POWER, "RESUME", mock.Mock(return_value=resumes)) as resume:
            return POWER.status(item), resume

    def test_a_job_whose_replica_was_evicted_is_carried_on(self):
        (state, _, message), resume = self.status(job(), gone=True)
        self.assertEqual("running", state)
        self.assertIn("carrying on from another replica", message)
        resume.assert_called_once()

    def test_a_live_worker_may_wait_on_longhorn_for_longer_than_four_minutes(self):
        (state, _, _), resume = self.status(job(age=600), gone=False)
        self.assertEqual("running", state)
        resume.assert_not_called()
        (state, _, _), _ = self.status(job(age=2000), gone=False)
        self.assertEqual("failed", state, "a worker silent for half an hour has stopped")

    def test_older_jobs_without_a_named_worker_keep_the_old_limit(self):
        (state, _, _), resume = self.status(job(age=300, worker=None), gone=True)
        self.assertEqual("failed", state)
        resume.assert_not_called()


class ResumeTests(unittest.TestCase):
    def test_only_the_leader_resumes_and_only_once(self):
        started = []
        with mock.patch.object(server.LEADER, "is_leader", return_value=False):
            self.assertFalse(server.resume_power_job(job()))
        with mock.patch.object(server.LEADER, "is_leader", return_value=True), \
             mock.patch.object(server.OPS, "record_phase") as claim, \
             mock.patch.object(server, "_power_in_background", side_effect=lambda *a, **k: started.append((a, k))):
            server._POWER_RESUMING.clear()
            self.assertTrue(server.resume_power_job(job()))
            self.assertTrue(server.resume_power_job(job()), "a second ask is the same resume")
            for _ in range(50):
                if started: break
                time.sleep(0.02)
        self.assertEqual(1, len(started))
        self.assertTrue(started[0][1]["resumed"])
        self.assertEqual(server.POD_NAME, claim.call_args.kwargs["worker"], "the job now names this replica")
        server._POWER_RESUMING.clear()

    def test_a_job_saved_without_its_plan_is_not_resumed(self):
        with mock.patch.object(server.LEADER, "is_leader", return_value=True):
            self.assertFalse(server.resume_power_job(job(plan=False)))


class OneOwnerTests(unittest.TestCase):
    """With two or three Homestead copies, an evicted replica can still be
    shutting down when another carries its job on: only one may send."""
    def setUp(self):
        import tempfile
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(server.OPS, "DATA_DIR", directory.name),
                      mock.patch.object(server.OPS, "WRITE_GUARD", None)):
            patch.start(); self.addCleanup(patch.stop)
        server.OPS._write([{"id": "job-1", "kind": "node-power", "status": "running", "progress": 10, "message": "",
                            "history": [], "ref": {"node": "k3s-1", "phase": "draining", "worker": "homestead-new"}}])

    def test_a_superseded_worker_records_nothing_and_never_sends(self):
        plan = {"node": "k3s-1", "action": "reboot", "drain_pods": [], "planned_outage": False}
        sent = []
        def node_power(*args, progress=None, **kwargs):
            progress("sending", 20, "Submitting power helper")   # what precedes the send
            sent.append(True)
            return {}
        with mock.patch.object(server, "POD_NAME", "homestead-old"),              mock.patch.object(server.LC, "node_power", side_effect=node_power):
            with self.assertRaises(server.OPS.Superseded):
                server.run_power_job("job-1", plan)
        self.assertEqual([], sent)
        saved = server.OPS._read()[0]
        self.assertEqual(("running", "draining"), (saved["status"], saved["ref"]["phase"]), "the job is left to its owner")

    def test_its_owner_carries_on(self):
        server.OPS.record_phase("job-1", "verifying", 15, "Rechecking", owner="homestead-new")
        self.assertEqual("verifying", server.OPS._read()[0]["ref"]["phase"])


class WorkerGoneTests(unittest.TestCase):
    def gone(self, pod=None, node_ready=True, missing=False):
        import urllib.error
        def kget(path):
            if "/pods/" in path:
                if missing:
                    raise urllib.error.HTTPError(path, 404, "gone", None, None)
                return pod or {"metadata": {}, "spec": {"nodeName": "k3s-1"}, "status": {"phase": "Running"}}
            return {"status": {"conditions": [{"type": "Ready", "status": "True" if node_ready else "Unknown"}]}}
        with mock.patch.object(server, "kget", kget):
            return server.power_worker_gone("homestead-old")

    def test_evicted_deleted_or_on_a_host_that_stopped_answering(self):
        self.assertTrue(self.gone(missing=True))
        self.assertTrue(self.gone({"metadata": {"deletionTimestamp": "now"}, "spec": {}, "status": {}}))
        self.assertTrue(self.gone(node_ready=False), "a node shut down leaves its pods listed for minutes")
        self.assertFalse(self.gone())


class ResumedDrainTests(unittest.TestCase):
    def pod(self, name):
        return {"metadata": {"namespace": "lab", "name": name, "uid": f"uid-{name}"},
                "spec": {"nodeName": "k3s-1", "containers": [{"name": "c"}]}}

    def drain(self, here, reviewed, resumed):
        with mock.patch.object(LC.MAINTENANCE, "items", return_value=here), \
             mock.patch.object(LC, "ksend"), mock.patch.object(LC, "kget", return_value={"items": []}):
            return LC.drain("k3s-1", reviewed_pods=reviewed, resumed=resumed)

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
             mock.patch.object(server, "send_reviewed_power", return_value={"operation": {"id": "op"}}):
            self.assertEqual("op", server.rollout_reboot("k3s-1"))

    def test_what_keeps_a_host_from_its_restart_is_named(self):
        cases = (({"volumes": [{"claim": "lab/plex-config", "risk": "single-copy"}]},
                  "only healthy copy on this host: lab/plex-config"),
                 ({"maintenance": {"local_storage": [{"pod": "lab/frigate", "kind": "host-local path (not moved)", "source": "/mnt/media"}]}},
                  r"pods keep data on this host itself: lab/frigate \(/mnt/media\)"),
                 ({"storage_unknown": True}, "could not be read"))
        for extra, refusal in cases:
            with self.subTest(refusal=refusal), mock.patch.object(server.POWER, "plan", return_value=self.plan([], requires_data_ack=True, **extra)), \
                    mock.patch.object(server, "send_reviewed_power") as send:
                with self.assertRaisesRegex(ValueError, refusal):
                    server.rollout_reboot("k3s-1")
                send.assert_not_called()

    def test_vms_and_waiting_apps_keep_a_host_from_an_unattended_restart(self):
        vm = {"id": "VirtualMachine/vms/win", "kind": "VirtualMachine", "ns": "vms", "name": "win", "options": ["move"], "default": "move"}
        app = {"id": "Deployment/lab/plex", "kind": "Deployment", "ns": "lab", "name": "plex", "options": ["wait"], "default": "wait"}
        moves = {"id": "Deployment/lab/web", "kind": "Deployment", "ns": "lab", "name": "web", "options": ["move", "wait"], "default": "move"}
        for hold, refusal in (([vm], "Running VMs"), ([app], "stop and wait")):
            with mock.patch.object(server.POWER, "plan", return_value=self.plan(hold)), \
                 mock.patch.object(server, "send_reviewed_power") as send:
                with self.assertRaisesRegex(ValueError, refusal):
                    server.rollout_reboot("k3s-1")
                send.assert_not_called()
        with mock.patch.object(server.POWER, "plan", return_value=self.plan([moves])), \
             mock.patch.object(server, "send_reviewed_power", return_value={"operation": {"id": "op"}}) as send:
            self.assertEqual("op", server.rollout_reboot("k3s-1"))
            self.assertEqual({"Deployment/lab/web": "move"}, send.call_args.args[0]["choices"])


class FailedBeforePowerTests(OneOwnerTests):
    def test_what_was_stopped_starts_again_when_power_is_not_sent(self):
        server.OPS.record_phase("job-1", "holding", 8, "", held=[{"kind": "Deployment", "ns": "lab", "name": "plex", "was": "1"}])
        plan = {"node": "k3s-1", "action": "reboot", "drain_pods": [], "planned_outage": False}
        with mock.patch.object(server, "POD_NAME", "homestead-new"), \
             mock.patch.object(server.LC, "node_power", side_effect=ValueError("drain timed out")), \
             mock.patch.object(server.HOLD, "restore", return_value=(["lab/plex"], [])) as restore:
            with self.assertRaises(server.PowerNotSent):
                server.run_power_job("job-1", plan)
        restore.assert_called_once()
        saved = server.OPS._read()[0]
        self.assertEqual(["lab/plex"], saved["ref"]["restored"]["started"])
        self.assertIn("Started again: lab/plex", saved["message"])
