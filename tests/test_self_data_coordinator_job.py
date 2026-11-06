"""Homestead's own data move: the coordinator is a Job, so losing its pod does
not leave Homestead at 0 copies with nothing looking after it (stuck-jobs C4).

Each test drives the real setup, worker and coordinator on the shared fake API
(tests/fakes/kube.py, tests/fakes/self_data_move.py) with a clock the test moves.
A "new pod" is a new Runner, as a new process has nothing in memory."""
import hashlib
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_anchor as A
import homestead_self_data_bootstrap as B
import homestead_self_data_coordinator as C
import homestead_self_data_finish as FIN
import homestead_self_data_kube as HK
import homestead_self_data_launch as L
import homestead_self_data_recovery as R
import homestead_self_data_worker as W
from homestead_storage_journal import Held, shape
from fakes.kube import Kube
from fakes.self_data_move import DIGEST, IMAGE, Move, NS, OP


def scope():
    return HK.Scope(NS, "homestead", OP, ["source", "target"], ["old-pv", "new-pv"], ["node-a", "node-b"])


class ManifestTests(unittest.TestCase):
    def job(self, **overrides):
        resources = L.resources(scope(), **{**dict(anchor_uid="anchor", status_digest=DIGEST, image=IMAGE, node="node-a"), **overrides})
        return next(r for r in resources if r["kind"] == "Job")

    def test_the_coordinator_is_a_job_with_three_retries_and_no_time_limit(self):
        spec = self.job()["spec"]
        self.assertEqual(3, spec["backoffLimit"])
        self.assertEqual("Never", spec["template"]["spec"]["restartPolicy"])
        # Replaced as soon as the old pod is ending: a dead host's pod ends only when collected.
        self.assertEqual("TerminatingOrFailed", spec["podReplacementPolicy"])
        self.assertNotIn("activeDeadlineSeconds", spec)
        self.assertNotIn("ttlSecondsAfterFinished", spec)

    def test_no_bare_pod_is_created(self):
        kinds = [r["kind"] for r in L.resources(scope(), anchor_uid="anchor", status_digest=DIGEST, image=IMAGE, node="node-a")]
        self.assertNotIn("Pod", kinds)
        self.assertEqual(["Job", "Service"], kinds[-2:])

    def test_a_retry_may_run_on_any_reviewed_host_and_prefers_the_chosen_one(self):
        affinity = self.job(node="node-b")["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]
        terms = affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        # The API refuses a field selector with more than one value: one term per host.
        self.assertEqual([["node-a"], ["node-b"]], sorted(t["matchFields"][0]["values"] for t in terms))
        preferred = affinity["preferredDuringSchedulingIgnoredDuringExecution"][0]["preference"]["matchFields"][0]
        self.assertEqual(["node-b"], preferred["values"])

    def test_the_job_mounts_neither_data_volume_and_stays_unprivileged(self):
        spec = self.job()["spec"]["template"]["spec"]
        self.assertEqual([HK.token_volume()], spec["volumes"])
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        self.assertNotIn("livenessProbe", spec["containers"][0])

    def test_the_scoped_account_can_read_its_job_and_cannot_change_it(self):
        s = scope()
        s.check("GET", s.worker_job)
        for method in ("DELETE", "PUT"):
            with self.assertRaises(Held):
                s.check(method, s.worker_job, {"metadata": {"name": "x"}, "preconditions": {"uid": "x"}, "propagationPolicy": "Foreground"})
        role = next(r for r in HK.access_resources(s) if r["kind"] == "Role")
        rules = [r for r in role["rules"] if r["resources"] == ["jobs"] and HK.access_name(NS, "homestead", OP) in r.get("resourceNames", [])]
        self.assertEqual([["get"]], [r["verbs"] for r in rules])

    def test_a_stop_signal_never_reports_success_to_the_job(self):
        """Exit 0 would mark the pod Succeeded and the Job Complete: a drained helper would not come back."""
        raw = L.configuration(scope(), "anchor", DIGEST)
        env = {L.CONFIG_ENV: raw, L.UID_ENV: "pod-1", L.JOB_UID_ENV: "job-1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["coordinator"]), \
                mock.patch.object(L.K, "Client"), mock.patch.object(L.W, "Runner") as runner, \
                mock.patch.object(L, "serve", lambda *a, **k: None):
            self.assertNotEqual(0, L.main())
        self.assertEqual("job-1", runner.call_args.kwargs["worker_uid"])
        self.assertEqual("pod-1", runner.call_args.kwargs["pod_uid"])
        self.assertEqual(raw, runner.call_args.kwargs["config"])


class MoveTest(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.m = Move(self.kube)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.m = Move(self.kube, handed_over=False)

    def test_the_records_worker_is_the_job_and_its_config_is_in_the_record(self):
        self.m.hand_over()
        state = self.m.state()
        job = self.kube.object(self.m.worker_path)
        self.assertEqual((job["metadata"]["name"], job["metadata"]["uid"]),
                         (state["plan"]["worker"]["name"], state["plan"]["worker"]["uid"]))
        value, parsed = L.parse_configuration(state["setup"]["config"])
        self.assertEqual((self.m.handle["uid"], DIGEST), (value["anchor_uid"], value["status_digest"]))
        self.assertEqual(("target", "homestead"), (parsed.claims[1], parsed.deployment))

    def test_setup_goes_on_with_a_new_pod_when_the_first_dies_early(self):
        before = self.m.setup.worker_fact()
        self.m.pod_failed(self.m.first_pod)
        with self.assertRaisesRegex(Held, "Waiting"):
            self.m.setup.worker_fact()
        self.m.start_pod("node-b")
        self.assertEqual(before, self.m.setup.worker_fact())

    def test_setup_stops_at_once_when_the_job_gave_up(self):
        self.m.pod_failed(self.m.first_pod)
        job = self.kube.object(self.m.worker_path)
        job["status"] = {"conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}
        self.kube.add(job)
        with self.assertRaises(Held) as held:
            self.m.setup.worker_fact()
        self.assertNotIn("Waiting", str(held.exception))
        self.assertIn("Homestead was not stopped", str(held.exception))

    def test_a_mutated_pod_cannot_become_the_reviewed_coordinator(self):
        pod = self.kube.object(self.m.first_pod[1])
        pod["spec"]["containers"][0]["volumeMounts"].append({"name": "source", "mountPath": "/source"})
        self.kube.add(pod)
        with self.assertRaises(Held):
            self.m.setup.worker_fact()

    def test_a_job_the_api_changed_is_held(self):
        target, body = self.m.setup.resources[-2]["target"], self.m.setup.bodies[-2]
        self.assertIsNone(B.admitted(body, self.kube.object(self.m.worker_path), target))
        job = self.kube.object(self.m.worker_path)
        job["spec"]["template"]["spec"]["hostNetwork"] = True
        with self.assertRaises(Held):
            B.admitted(body, job, target)
        job = self.kube.object(self.m.worker_path)
        job["spec"]["backoffLimit"] = 9
        with self.assertRaises(Held):
            B.admitted(body, job, target)
        job = self.kube.object(self.m.worker_path)
        job["spec"]["template"]["metadata"]["labels"]["homestead.io/extra"] = "x"
        with self.assertRaises(Held):
            B.admitted(body, job, target)


class Died(BaseException):
    """The process is gone: nothing after this point of a tick ran."""


def _fails_after_intent():
    """The first checkpoint (the intent) lands; the process is gone before the second."""
    real, calls = A.Anchor.checkpoint, []
    def fatal(anchor, job):
        calls.append(job)
        if len(calls) > 1:
            raise Died()
        return real(anchor, job)
    return fatal


class ReplacementStartingTests(MoveTest):
    """C1: a normal replacement is not a hold while the API catches up."""

    def stopped_and_copying(self):
        run = self.m.runner()
        run.tick(); self.m.stopped(); run.tick()
        return run

    def test_a_new_pod_the_api_still_shows_pending_carries_on(self):
        self.stopped_and_copying()
        self.m.pod_failed(self.m.first_pod)
        pending = self.m.start_pod("node-b", phase="Pending")
        view = self.m.runner(pending).tick()
        self.assertNotEqual("held", view["status"], view)
        self.assertEqual(("running", pending[0]), (self.m.state()["runtime"]["state"], self.m.state()["runtime"]["pod_uid"]))
        self.assertIn("copy-job", self.m.steps())

    def test_a_pod_with_no_host_yet_in_the_api_carries_on(self):
        self.stopped_and_copying()
        self.m.pod_failed(self.m.first_pod)
        pending = self.m.start_pod("node-b", phase="Pending")
        pod = self.kube.object(pending[1]); pod["spec"].pop("nodeName"); self.kube.add(pod)
        self.assertNotEqual("held", self.m.runner(pending).tick()["status"])

    def test_the_acting_pod_being_deleted_writes_no_hold_for_its_replacement_to_inherit(self):
        """kubectl delete or a drain, or a kubelet cut off so the signal never comes."""
        m = self.m
        run = self.stopped_and_copying()
        m.pod_terminating(m.first_pod, grace=60)
        self.assertNotEqual("held", run.tick()["status"])
        self.assertEqual("running", m.state()["runtime"]["state"])
        second = m.start_pod("node-b")
        m.pass_time(61)
        self.assertNotEqual("held", m.runner(second).tick()["status"])
        self.assertEqual(second[0], m.state()["runtime"]["pod_uid"])

    def test_recovery_previews_with_the_new_pod_still_pending(self):
        m = self.m
        run = self.stopped_and_copying()
        path = self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "stranger", "namespace": NS},
                              "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "source"}}]}})
        self.assertEqual("held", run.tick()["status"])
        self.kube.discard(path)
        m.pod_failed(m.first_pod)
        m.start_pod("node-b", phase="Pending")
        anchor = A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
        self.assertEqual("resume", R.review(anchor, m.read, lambda _: m.logs_text, "resume", clock=lambda: m.now)["action"])

    def test_a_pod_of_the_job_that_mounts_a_data_volume_holds(self):
        """The check that stays: whichever try it is, it mounts neither volume."""
        for claim in ("source", "target"):
            with self.subTest(claim=claim):
                kube = Kube()
                with kube:
                    m = Move(kube)
                    run = m.runner()
                    run.tick(); m.stopped()
                    pod = kube.object(m.first_pod[1])
                    pod["spec"]["volumes"].append({"name": "data", "persistentVolumeClaim": {"claimName": claim}})
                    kube.add(pod)
                    view = run.tick()
                    self.assertEqual("held", view["status"])
                    # This check's own words: the wait for mounts would also hold, later and vaguer.
                    self.assertIn("independently of both data volumes", view["message"])

    def test_a_pod_of_the_job_on_an_unreviewed_host_holds(self):
        run = self.m.runner()
        run.tick()
        pod = self.kube.object(self.m.first_pod[1]); pod["spec"]["nodeName"] = "node-z"; self.kube.add(pod)
        self.assertEqual("held", run.tick()["status"])


class HostRebootTests(MoveTest):
    """The default: the coordinator and the copy on one host (node-a). A reboot counts
    only while a copy pod is on it; before or after the copy it holds nothing."""

    def to_the_copy(self):
        run = self.m.runner()
        run.tick(); self.m.stopped(); run.tick()
        self.assertEqual("copy", self.m.state()["phase"])
        return run

    def replaced(self):
        self.m.pod_lost(self.m.first_pod)
        return self.m.runner(self.m.start_pod("node-a"))

    def finish(self, run):
        m = self.m
        m.copy_finished(); run.tick(); run.tick(); m.copy_released(); run.tick(); run.tick(); m.stopped(); run.tick(); m.started()
        return run.tick()

    def test_a_reboot_before_the_copy_holds_nothing_and_the_move_finishes(self):
        m = self.m
        self.to_the_copy()
        m.rebooted("node-a")
        run = self.replaced()
        self.assertNotEqual("held", run.tick()["status"])
        self.assertIn("copy-job", m.steps())
        m.copy_running()                            # on the host that rebooted, after it did
        self.assertNotEqual("held", run.tick()["status"])
        self.assertEqual("done", self.finish(run)["status"])

    def test_a_reboot_after_the_copy_holds_nothing_and_the_move_finishes(self):
        m = self.m
        run = self.to_the_copy()
        run.tick(); m.copy_finished(); run.tick(); run.tick(); m.copy_released()
        m.rebooted("node-a")
        run = self.replaced()
        self.assertEqual("switch", run.tick()["phase"])
        self.assertEqual("start", run.tick()["phase"])
        m.stopped(); run.tick(); m.started()        # Homestead's new pods are on that host
        self.assertEqual("done", run.tick()["status"])

    def test_a_reboot_during_the_copy_still_holds_and_recovery_refuses(self):
        m = self.m
        run = self.to_the_copy()
        run.tick(); m.copy_running()
        self.assertNotEqual("held", run.tick()["status"])
        m.rebooted("node-a")
        self.assertIn("rebooted", run.tick()["message"])
        anchor = A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
        with self.assertRaisesRegex(Held, "rebooted"):
            R.review(anchor, m.read, lambda _: m.logs_text, "resume", clock=lambda: m.now)

    def test_a_reboot_while_the_finished_copy_pod_is_there_still_holds(self):
        m = self.m
        run = self.to_the_copy()
        run.tick(); m.copy_finished(); run.tick()
        m.rebooted("node-a")
        self.assertEqual("held", run.tick()["status"])

    def test_a_reboot_while_homesteads_own_old_pod_is_there_still_holds(self):
        """A writer from before the move, on the host that rebooted: unchanged."""
        run = self.m.runner()
        run.tick()
        self.m.rebooted("node-a")
        self.assertEqual("held", run.tick()["status"])


class ByHandTests(MoveTest):
    """docs/data-move-recovery.md's own scripts, run as written."""

    @staticmethod
    def script(name):
        docs = (Path(__file__).resolve().parents[1] / "docs" / "data-move-recovery.md").read_text()
        return re.search(r"```python\n# " + re.escape(name) + r":.*?```", docs, re.S).group(0).split("\n", 1)[1].rsplit("```", 1)[0]

    def new_try(self):
        """new-try.py over the Job as kubectl prints it, then kubectl create."""
        job = self.kube.object(self.m.worker_path)
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(job))), mock.patch.object(sys, "stdout", out):
            exec(compile(self.script("new-try.py"), "new-try.py", "exec"), {"__name__": "__main__"})
        pod = json.loads(out.getvalue())
        pod["metadata"]["name"] = pod["metadata"].pop("generateName") + "abcde"
        pod["status"] = {"phase": "Running"}
        path = self.kube.add(pod)
        return self.kube.object(path)["metadata"]["uid"], path

    def settle(self, outcome):
        m = self.m
        client = mock.Mock(read=m.read, send=m.send)
        env = {L.CONFIG_ENV: m.setup.config}
        with mock.patch.dict(os.environ, env), mock.patch.object(HK, "Client", lambda scope: client), \
                mock.patch.object(sys, "argv", ["settle.py", outcome]), mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch("builtins.print"):
            exec(compile(self.script("settle.py"), "settle.py", "exec"), {"__name__": "__main__"})

    def load(self):
        return A.Anchor(self.m.read, self.m.send, NS, "homestead").load(**self.m.handle)

    def recover(self, action):
        m = self.m
        review = R.review(self.load(), m.read, lambda _: m.logs_text, action, clock=lambda: m.now)
        R.confirm(self.load(), m.read, lambda _: m.logs_text, action, review["fingerprint"], clock=lambda: m.now)

    def job_gave_up(self):
        job = self.kube.object(self.m.worker_path)
        job["status"] = {"failed": 4, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}
        self.kube.add(job)

    def test_after_the_last_try_a_pod_made_from_the_failed_job_finishes_the_move(self):
        m = self.m
        run = m.runner()
        run.tick(); m.stopped(); run.tick(); run.tick()
        m.pod_failed(m.first_pod)
        self.job_gave_up()
        run = m.runner(self.new_try())
        self.assertNotEqual("held", run.tick()["status"])
        m.copy_finished(); run.tick(); run.tick(); m.copy_released(); run.tick(); run.tick(); m.stopped(); run.tick(); m.started()
        self.assertEqual("done", run.tick()["status"])
        self.assertEqual(["stop", "copy-job", "release-copy", "switch", "start"], m.steps())

    def test_the_new_try_carries_the_jobs_labels_owner_and_no_tracking_finalizer(self):
        pod = self.kube.object(self.new_try()[1])
        self.assertEqual(self.m.job["metadata"]["uid"], pod["metadata"]["labels"]["batch.kubernetes.io/controller-uid"])
        owner = pod["metadata"]["ownerReferences"][0]
        self.assertEqual(("Job", self.m.job["metadata"]["uid"], True), (owner["kind"], owner["uid"], owner["controller"]))
        self.assertNotIn("finalizers", pod["metadata"])

    def unknown_outcome(self):
        m = self.m
        with mock.patch.object(A.Anchor, "checkpoint", _fails_after_intent()), self.assertRaises(Died):
            m.runner().tick()
        m.pod_lost(m.first_pod)
        run = m.runner(m.start_pod("node-b"))
        self.assertEqual("held", run.tick()["status"])
        return run

    def test_a_request_found_applied_is_recorded_and_recovery_goes_on_without_sending_it(self):
        m = self.m
        run = self.unknown_outcome()
        self.assertEqual(0, self.kube.object(m.dep_path)["spec"]["replicas"])
        self.settle("applied")
        m.stopped()
        self.recover("resume")
        puts = len(m.writes_to(m.dep_path, "PUT"))
        run.tick(); run.tick()
        self.assertEqual("copy", m.state()["phase"])
        self.assertEqual(puts, len(m.writes_to(m.dep_path, "PUT")))

    def test_a_request_found_not_sent_can_go_back_to_the_original_volume(self):
        """It never reached the API: the record forgets it, and Homestead was never stopped."""
        m = self.m
        self.kube.fail(m.dep_path, 500, method="PUT")
        with mock.patch.object(A.Anchor, "checkpoint", _fails_after_intent()), self.assertRaises(Died):
            m.runner().tick()
        m.pod_lost(m.first_pod)
        run = m.runner(m.start_pod("node-b"))
        self.assertEqual("held", run.tick()["status"])
        self.assertEqual(1, self.kube.object(m.dep_path)["spec"]["replicas"])
        self.settle("not-sent")
        self.assertEqual([], m.steps())
        self.recover("return-original")
        self.assertEqual("cancelled", run.tick()["status"])

    def test_settling_refuses_a_request_whose_outcome_is_known(self):
        m = self.m
        m.runner().tick()
        with self.assertRaises(SystemExit):
            self.settle("applied")
        self.assertEqual("accepted", m.state()["journal"]["ref"]["storage_writes"][-1]["state"])

    def test_settling_refuses_anything_but_the_two_outcomes(self):
        self.unknown_outcome()
        with self.assertRaises(SystemExit):
            self.settle("maybe")
        self.assertEqual("intent", self.m.state()["journal"]["ref"]["storage_writes"][-1]["state"])

    def test_a_recovery_clears_a_new_pods_hold_when_the_old_pod_had_already_held(self):
        """The old pod's hold and an open request: the new pod keeps the record's message,
        which is what recovery compares when it clears the hold."""
        m = self.m
        with mock.patch.object(A.Anchor, "checkpoint", _fails_after_intent()), self.assertRaises(Died):
            m.runner().tick()
        anchor = self.load()
        anchor.report(m.job["metadata"]["uid"], m.now, "held", "An earlier hold")
        m.pod_lost(m.first_pod)
        run = m.runner(m.start_pod("node-b"))
        self.assertEqual("held", run.tick()["status"])
        self.settle("applied")
        m.stopped()
        self.recover("resume")
        self.assertNotEqual("held", run.tick()["status"])


class LostPodTests(MoveTest):
    """A new process at each phase: nothing already done is done again."""

    def setUp(self):
        super().setUp()
        self.current = self.m.first_pod

    def replaced(self):
        """The pod is lost; the Job starts another, on the other host. A new
        process, so a new Runner with nothing in memory."""
        self.m.pod_lost(self.current)
        self.current = self.m.start_pod("node-b")
        return self.m.runner(self.current)

    def counts(self):
        posted = [w for w in self.m.writes_to(self.m.copy_path.rsplit("/", 1)[0], "POST") if w[2]["metadata"]["name"].startswith("homestead-data-copy")]
        return {"deployment": len(self.m.writes_to(self.m.dep_path, "PUT")), "copy-post": len(posted),
                "copy-delete": len(self.m.writes_to(self.m.copy_path, "DELETE"))}

    def claim(self):
        return self.kube.object(self.m.dep_path)["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]

    def test_lost_at_every_phase_the_move_finishes_and_each_request_went_once(self):
        m = self.m
        self.assertEqual("quiesce", m.runner(self.current).tick()["phase"])          # stop sent
        view = self.replaced().tick()                                                # still stopping; no second stop
        self.assertEqual(("quiesce", 1), (view["phase"], self.counts()["deployment"]))
        m.stopped()
        self.assertEqual("copy", self.replaced().tick()["phase"])
        self.replaced().tick()                                                       # the copy Job is posted
        self.assertEqual(1, self.counts()["copy-post"])
        self.assertEqual("copy", self.replaced().tick()["phase"])                    # running with no coordinator: not posted again
        self.assertEqual(1, self.counts()["copy-post"])
        m.copy_finished()                                                            # finished while no coordinator was there
        self.assertEqual("verify", self.replaced().tick()["phase"])
        self.assertEqual("verified", m.state()["copy_receipt"]["state"])
        self.replaced().tick()                                                       # the release is sent
        self.assertEqual(1, self.counts()["copy-delete"])
        self.assertEqual("verify", self.replaced().tick()["phase"])                  # waiting for it; not sent again
        self.assertEqual(1, self.counts()["copy-delete"])
        m.copy_released()
        self.assertEqual("switch", self.replaced().tick()["phase"])
        self.assertEqual("source", self.claim())
        self.assertEqual("start", self.replaced().tick()["phase"])                   # data switched
        self.assertEqual("target", self.claim())
        m.stopped()
        self.replaced().tick()                                                       # forward only: the start request
        self.assertEqual(("target", 3), (self.claim(), self.counts()["deployment"]))
        view = self.replaced().tick()
        self.assertEqual(("start", 3), (view["phase"], self.counts()["deployment"]))
        m.started()
        self.assertEqual("done", self.replaced().tick()["status"])
        self.assertEqual(["stop", "copy-job", "release-copy", "switch", "start"], m.steps())
        self.assertEqual({"deployment": 3, "copy-post": 1, "copy-delete": 1}, self.counts())
        self.assertEqual("done", m.state()["runtime"]["state"])

    def died_in(self, method, run, *, after=0):
        """The process dies inside an Anchor method, after that many calls went through."""
        real = getattr(A.Anchor, method)
        calls = {"n": 0}
        def fatal(anchor, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] > after:
                raise Died()
            return real(anchor, *args, **kwargs)
        with mock.patch.object(A.Anchor, method, fatal), self.assertRaises(Died):
            run.tick()

    def test_died_after_an_accepted_request_before_the_next_phase_sends_nothing_again(self):
        """stop: accepted and recorded, gone before the phase moved on."""
        m = self.m
        self.died_in("advance", m.runner(self.current))
        self.assertEqual((["stop"], "prepare", 1), (m.steps(), m.state()["phase"], self.counts()["deployment"]))
        self.assertEqual("quiesce", self.replaced().tick()["phase"])
        self.assertEqual(1, self.counts()["deployment"])

    def test_died_at_the_copy_receipt_starts_no_second_copy(self):
        m = self.m
        run = m.runner(self.current)
        run.tick(); m.stopped(); run.tick()
        self.died_in("copy_started", run)
        self.assertEqual(0, self.counts()["copy-post"])
        self.replaced().tick()
        self.assertEqual(1, self.counts()["copy-post"])
        m.copy_finished()
        self.died_in("copy_finished", self.replaced())
        self.assertEqual("intent", m.state()["copy_receipt"]["state"])
        self.assertEqual("verify", self.replaced().tick()["phase"])
        self.assertEqual((1, "verified"), (self.counts()["copy-post"], m.state()["copy_receipt"]["state"]))

    def test_died_after_the_switch_goes_forward_and_never_back(self):
        m = self.m
        run = m.runner(self.current)
        run.tick(); m.stopped(); run.tick(); run.tick(); m.copy_finished(); run.tick(); run.tick(); m.copy_released(); run.tick()
        self.assertEqual("switch", m.state()["phase"])
        self.died_in("advance", run)
        self.assertEqual("target", self.claim())
        self.assertEqual("switch", m.state()["phase"])
        m.stopped()
        puts = self.counts()["deployment"]
        self.assertEqual("start", self.replaced().tick()["phase"])
        self.assertEqual(("target", puts), (self.claim(), self.counts()["deployment"]))

    def test_copy_receipt_survives_a_new_pod(self):
        m = self.m
        run = m.runner(self.current)
        run.tick(); m.stopped(); run.tick(); run.tick(); m.copy_finished()
        self.replaced().tick()
        self.assertEqual(m.job["metadata"]["uid"], m.state()["copy_receipt"]["worker_uid"])

    def test_lost_before_the_stop_keeps_homestead_as_it_was(self):
        """Later than the copy preflight's minute, the new pod holds with Homestead running."""
        m = self.m
        m.pass_time(61)
        view = self.replaced().tick()
        self.assertEqual("held", view["status"])
        self.assertEqual([], m.steps())
        self.assertEqual(1, self.kube.object(m.dep_path)["spec"]["replicas"])
        self.assertIn("recovery_command", view)

    def test_request_with_no_known_outcome_holds_and_is_not_sent_again(self):
        """It died between saying it would send and recording the answer."""
        m = self.m
        self.died_in("checkpoint", m.runner(self.current), after=1)           # the intent is saved, the answer is not
        self.assertEqual(("stop", "intent"), tuple(m.state()["journal"]["ref"]["storage_writes"][-1][k] for k in ("step", "state")))
        self.assertEqual(1, self.counts()["deployment"])
        view = self.replaced().tick()
        self.assertEqual("held", view["status"])
        self.assertIn("will not be sent again", view["message"])
        self.assertIn("(stop)", view["message"])
        self.assertEqual(1, self.counts()["deployment"])
        self.assertEqual("held", self.m.runner(self.current).tick()["status"])
        self.assertEqual(1, self.counts()["deployment"])
        anchor = A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
        with self.assertRaisesRegex(Held, "known request outcomes"):
            R.review(anchor, m.read, lambda _: m.logs_text, "resume", clock=lambda: m.now)

    def test_a_rebooted_coordinator_host_does_not_hold_the_new_pod(self):
        m = self.m
        run = m.runner(self.current)
        run.tick(); m.stopped(); run.tick(); run.tick()
        # node-b only ever ran the coordinator; it rebooted and the new pod is on it.
        m.pod_lost(self.current)
        self.current = m.start_pod("node-b")
        node = self.kube.object("/api/v1/nodes/node-b")
        node["status"]["nodeInfo"]["bootID"] = "another-boot"
        self.kube.add(node)
        self.assertNotEqual("held", m.runner(self.current).tick()["status"])

    def test_a_hold_stays_held_through_a_new_pod_and_recovery_still_works(self):
        m = self.m
        run = m.runner(self.current)
        run.tick(); m.stopped()
        stranger = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "stranger", "namespace": NS},
                    "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "source"}}]}}
        path = self.kube.add(stranger)
        self.assertEqual("held", run.tick()["status"])
        replacement = self.replaced()
        self.assertEqual("held", replacement.tick()["status"])
        self.kube.discard(path)
        load = lambda: A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
        review = R.review(load(), m.read, lambda _: m.logs_text, "resume", clock=lambda: m.now)
        R.confirm(load(), m.read, lambda _: m.logs_text, "resume", review["fingerprint"], clock=lambda: m.now)
        replacement.tick()
        self.assertEqual("copy", replacement.tick()["phase"])
        self.assertEqual(self.current[0], m.state()["runtime"]["pod_uid"])

    def test_a_hold_is_cleared_only_by_recovery_even_with_a_new_pod_on_a_clean_cluster(self):
        m = self.m
        run = m.runner(self.current)
        run.tick(); m.stopped()
        path = self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "stranger", "namespace": NS},
                              "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "source"}}]}})
        run.tick()
        self.kube.discard(path)
        puts = self.counts()
        self.assertEqual("held", self.replaced().tick()["status"])
        self.assertEqual(puts, self.counts())


class TwoPodsTests(MoveTest):
    def setUp(self):
        super().setUp()
        self.first = self.m.runner(self.m.first_pod)
        self.first.tick()
        self.second_pod = self.m.start_pod("node-b")
        self.second = self.m.runner(self.second_pod)

    def test_the_new_pod_writes_nothing_while_the_old_one_lives(self):
        before = len(self.kube.writes)
        view = self.second.tick()
        self.assertEqual(before, len(self.kube.writes))
        self.assertIn("previous", view["message"])
        self.assertEqual(self.m.first_pod[0], self.m.state()["runtime"]["pod_uid"])

    def test_the_new_pod_waits_out_a_terminating_pods_grace(self):
        self.m.pod_terminating(self.m.first_pod, grace=60)
        before = len(self.kube.writes)
        self.second.tick()
        self.assertEqual(before, len(self.kube.writes))
        self.m.pass_time(61)                        # a dead host: it stays Terminating for ever
        self.second.tick()
        self.assertEqual(self.second_pod[0], self.m.state()["runtime"]["pod_uid"])

    def test_the_new_pod_takes_over_from_a_failed_or_gone_pod(self):
        for ending in (self.m.pod_failed, self.m.pod_lost):
            with self.subTest(ending=ending.__name__):
                kube = Kube()
                with kube:
                    m = Move(kube)
                    m.runner(m.first_pod).tick()
                    second = m.start_pod("node-b")
                    getattr(m, ending.__name__)(m.first_pod)
                    m.runner(second).tick()
                    self.assertEqual(second[0], m.state()["runtime"]["pod_uid"])

    def test_a_stale_pod_that_comes_back_writes_nothing_and_leaves_no_hold(self):
        self.m.pod_terminating(self.m.first_pod, grace=0)
        self.m.pass_time(1)
        self.second.tick()
        before = len(self.m.writes_to(self.m.anchor.path, "PUT"))
        self.m.pass_time(5)
        view = self.first.tick()                    # it was only cut off; it sees it has been replaced
        self.assertEqual(before, len(self.m.writes_to(self.m.anchor.path, "PUT")))
        self.assertNotEqual("held", self.m.state()["runtime"]["state"], view)

    def test_a_stale_pods_hold_is_not_written_over_the_new_pods_record(self):
        """It is mid-tick when the new pod takes over: its heartbeat is refused, it latches
        a hold of its own, and the record the new pod owns stays as it was."""
        read, raced = self.first.read, []
        def read_then_be_replaced(path):
            answer = read(path)
            if path == self.m.dep_path and not raced:
                raced.append(True)
                self.m.pod_terminating(self.m.first_pod, grace=0)
                self.m.pass_time(1)
                self.second.tick()
            return answer
        self.first.read = read_then_be_replaced
        self.first.tick()
        self.assertTrue(raced)
        self.assertIsNotNone(self.first.hold, "its own write was refused")
        self.assertEqual(self.second_pod[0], self.m.state()["runtime"]["pod_uid"])
        self.assertNotEqual("held", self.m.state()["runtime"]["state"])
        self.first.tick()
        self.assertNotEqual("held", self.m.state()["runtime"]["state"])

    def test_a_pod_being_deleted_does_not_take_over_even_when_the_last_one_has_ended(self):
        self.m.pod_failed(self.m.first_pod)
        self.m.pod_terminating(self.second_pod, grace=60)
        before = len(self.m.writes_to(self.m.anchor.path, "PUT"))
        self.second.tick()
        self.assertEqual(before, len(self.m.writes_to(self.m.anchor.path, "PUT")))
        self.assertEqual(self.m.first_pod[0], self.m.state()["runtime"]["pod_uid"])

    def test_a_pod_the_api_says_has_failed_does_not_take_back_the_move(self):
        self.second.tick()                          # waits: the first is alive
        self.m.pod_failed(self.m.first_pod)
        self.second.tick()                          # took over
        self.m.pod_failed(self.second_pod)
        self.m.pod_failed(self.m.first_pod)
        before = len(self.m.writes_to(self.m.anchor.path, "PUT"))
        self.first.tick()
        self.assertEqual(self.second_pod[0], self.m.state()["runtime"]["pod_uid"])
        self.assertEqual(before, len(self.m.writes_to(self.m.anchor.path, "PUT")))

    def test_a_pod_that_has_not_taken_over_writes_no_hold_into_a_record_with_no_owner(self):
        kube = Kube()
        with kube:
            m = Move(kube)
            changed = L.configuration(m.scope, m.handle["uid"], "e" * 64)
            before = len(m.writes_to(m.anchor.path, "PUT"))
            self.assertEqual("held", m.runner(config=changed).tick()["status"])
            self.assertEqual(before, len(m.writes_to(m.anchor.path, "PUT")))
            self.assertNotIn("runtime", m.state())

    def test_two_pods_cannot_both_win_the_takeover(self):
        """Both read the record; the third writes first. The record's version decides:
        the second is refused, holds nothing, and waits for the winner."""
        self.m.pod_failed(self.m.first_pod)
        third_pod = self.m.start_pod("node-a")
        third = self.m.runner(third_pod)
        read, raced = self.second.read, []
        def read_then_lose_the_race(path):
            answer = read(path)
            if path == self.m.pods_path and not raced:   # the second is about to take over
                raced.append(True)
                third.tick()
            return answer
        self.second.read = read_then_lose_the_race
        self.assertNotEqual("held", self.second.tick()["status"])
        self.assertTrue(raced)
        self.assertEqual(third_pod[0], self.m.state()["runtime"]["pod_uid"])
        self.assertNotEqual("held", self.m.state()["runtime"]["state"])
        before = len(self.m.writes_to(self.m.anchor.path, "PUT"))
        self.assertIn("previous", self.second.tick()["message"])
        self.assertEqual(before, len(self.m.writes_to(self.m.anchor.path, "PUT")))

    def test_a_pod_of_another_job_writes_nothing_to_the_record(self):
        pod = self.m.start_pod("node-b")
        other = self.m.runner(pod, worker_uid="another-job")
        before = len(self.m.writes_to(self.m.anchor.path, "PUT"))
        self.assertEqual("held", other.tick()["status"])
        self.assertEqual(before, len(self.m.writes_to(self.m.anchor.path, "PUT")))

    def test_a_pod_whose_config_differs_from_the_record_holds(self):
        self.m.pod_failed(self.m.first_pod)
        changed = L.configuration(self.m.scope, self.m.handle["uid"], "e" * 64)
        view = self.m.runner(self.second_pod, config=changed).tick()
        self.assertEqual("held", view["status"])
        self.assertIn("configuration", view["message"])

    def test_a_clock_behind_the_last_heartbeat_does_not_hold(self):
        """Hosts' clocks differ by a little; the record only moves forward."""
        self.m.pod_failed(self.m.first_pod)
        behind = self.m.runner(self.second_pod, clock=lambda: self.m.now - 3)
        self.assertNotEqual("held", behind.tick()["status"])
        self.assertNotEqual("held", behind.tick()["status"])
        self.assertEqual(self.second_pod[0], self.m.state()["runtime"]["pod_uid"])


class WhatAPersonSeesTests(MoveTest):
    def test_the_recovery_command_uses_the_job(self):
        command = W.recovery_command(NS, self.m.state())
        self.assertIn(f"exec job/{self.m.job['metadata']['name']} --", command)

    def test_identity_answers_with_the_job_on_every_try(self):
        """Setup's handshake and the private Service: the real status server, a new pod."""
        token = "f" * 64
        answers = []
        for pod in (self.m.first_pod, self.m.start_pod("node-b")):
            server = W.status_server(("127.0.0.1", 0), self.m.runner(pod), hashlib.sha256(token.encode()).hexdigest())
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                connection.request("GET", "/identity", headers={"Authorization": "Bearer " + token})
                answers.append(json.load(connection.getresponse()))
            finally:
                server.shutdown(); server.server_close()
        self.assertEqual([self.m.job["metadata"]["uid"]] * 2, [a["worker_uid"] for a in answers])

    def test_a_hold_with_an_unknown_outcome_does_not_offer_a_command_that_refuses(self):
        m = self.m
        run = m.runner()
        with mock.patch.object(A.Anchor, "checkpoint", _fails_after_intent()), self.assertRaises(Died):
            run.tick()
        m.pod_lost(m.first_pod)
        view = m.runner(m.start_pod("node-b")).tick()
        self.assertEqual("held", view["status"])
        self.assertNotIn("recovery_command", view)
        self.assertNotIn("--action", view["message"])
        self.assertIn("(stop)", view["message"])
        self.assertIn("docs/data-move-recovery.md", view["message"])


class CleanupTests(MoveTest):
    """After the move, cleanup removes the Job and every try's pod, and neither volume."""

    def setUp(self):
        super().setUp()
        m = self.m
        run = m.runner()
        run.tick(); m.stopped(); run.tick(); run.tick(); m.copy_finished(); run.tick(); run.tick()
        m.copy_released(); run.tick(); run.tick(); m.stopped(); run.tick(); m.started()
        self.assertEqual("done", run.tick()["status"])
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.fence = mock.Mock(directory=temp.name, namespace=NS, deployment="homestead")
        self.fence.inspect.return_value = {"mode": "done", "writable": True}

    def save(self):
        obj = self.kube.object(self.m.anchor.path)
        FIN._save(self.directory, {"protocol": 1, "anchor": obj, "cleanup": {}})

    def finish(self):
        return FIN.finish(self.fence, self.m.read, self.m.send)

    def test_the_job_goes_first_in_the_foreground_and_its_pods_with_it(self):
        self.m.pod_failed(self.m.first_pod)
        retry = self.m.start_pod("node-b")
        self.save()
        self.assertFalse(self.finish()["done"], "waits for the foreground delete")
        deletes = [w for w in self.kube.writes if w[0] == "DELETE" and "/jobs/homestead-handoff-" in w[1]]
        self.assertEqual("Foreground", deletes[0][2]["propagationPolicy"])
        self.assertEqual(self.m.job["metadata"]["uid"], deletes[0][2]["preconditions"]["uid"])
        self.assertIsNotNone(self.kube.object(self.m.worker_path), "foreground: kept until its pods are gone")
        for pod in (self.m.first_pod, retry):
            self.kube.discard(pod[1])
        self.kube.discard(self.m.worker_path)
        self.assertTrue(self.finish()["done"])
        self.assertIsNone(self.kube.object(self.m.anchor.path))
        for name in ("source", "target"):
            self.assertIsNotNone(self.kube.object(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{name}"))

    def test_a_job_that_used_up_its_tries_is_still_cleaned_up(self):
        job = self.kube.object(self.m.worker_path)
        job["status"] = {"failed": 4, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}
        self.kube.add(job)
        self.save()
        self.finish()
        self.assertTrue([w for w in self.kube.writes if w[0] == "DELETE" and w[1] == self.m.worker_path])

    def test_a_finished_move_of_the_earlier_release_cleans_up_its_bare_pod(self):
        """Its record has a Pod target and no configuration; its pod has the receipt's uid."""
        obj = self.kube.object(self.m.anchor.path)
        state = json.loads(obj["data"]["state.json"])
        name = HK.access_name(NS, "homestead", OP)
        path = f"/api/v1/namespaces/{NS}/pods/{name}"
        self.kube.discard(self.m.worker_path)
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS}, "spec": {"nodeName": "node-a"}}
        self.kube.add(pod)
        live = self.kube.object(path)
        state["setup"]["resources"][-2]["target"] = {"kind": "Pod", "apiVersion": "v1", "namespace": NS, "name": name, "path": path}
        state["setup"]["receipts"][-2].update(after={"uid": live["metadata"]["uid"], "resourceVersion": "1"}, fingerprint=B.fingerprint(live))
        state["setup"].pop("config")
        state["plan"]["worker"].update(uid=live["metadata"]["uid"])
        state["runtime"]["worker_uid"] = live["metadata"]["uid"]
        state["runtime"].pop("pod_uid")
        state["copy_receipt"]["worker_uid"] = live["metadata"]["uid"]
        obj["data"]["state.json"] = json.dumps(state)
        self.kube.add(obj)
        self.save()
        self.finish()
        deleted = [w for w in self.kube.writes if w[0] == "DELETE" and w[1] == path]
        self.assertEqual(live["metadata"]["uid"], deleted[0][2]["preconditions"]["uid"])
        self.assertIsNone(self.kube.object(path))


class OldMoveTests(unittest.TestCase):
    """A move started by the previous release: a bare pod in the record, no config."""

    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.m = Move(self.kube)
        anchor = A.Anchor(self.m.read, self.m.send, NS, "homestead").load(**self.m.handle)
        state = json.loads(anchor.obj["data"]["state.json"])
        name = HK.access_name(NS, "homestead", OP)
        path = f"/api/v1/namespaces/{NS}/pods/{name}"
        state["setup"]["resources"][-2]["target"] = {"kind": "Pod", "apiVersion": "v1", "namespace": NS, "name": name, "path": path}
        state["setup"].pop("config")
        obj = self.kube.object(anchor.path)
        obj["data"]["state.json"] = json.dumps(state)
        self.kube.add(obj)

    def test_the_record_still_loads(self):
        self.assertEqual("Pod", self.m.state()["setup"]["resources"][-2]["target"]["kind"])

    def test_the_recovery_command_still_names_the_pod(self):
        command = W.recovery_command(NS, self.m.state())
        self.assertIn(f"exec {self.m.state()['plan']['worker']['name']} --", command)
        self.assertNotIn("job/", command)

    def test_a_bare_coordinator_must_still_be_running_and_mount_nothing(self):
        """Only a Job's next try makes "not Running" normal; an earlier release's pod is alone."""
        m = self.m
        obj = self.kube.object(m.anchor.path)
        state = json.loads(obj["data"]["state.json"])
        name = state["setup"]["resources"][-2]["target"]["name"]
        path = state["setup"]["resources"][-2]["target"]["path"]
        self.kube.discard(m.worker_path)
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS},
                       "spec": {"nodeName": "node-a", "volumes": []}, "status": {"phase": "Running"}})
        live = self.kube.object(path)
        uid = live["metadata"]["uid"]
        state["setup"]["receipts"][-2]["after"]["uid"] = uid
        state["plan"]["worker"].update(uid=uid, shape=shape(live))
        obj["data"]["state.json"] = json.dumps(state)
        self.kube.add(obj)
        engine = lambda: C.Coordinator(A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle), m.read, m.send,
                                       lambda _: "", lambda *_: True, worker_uid=uid, clock=lambda: m.now)
        self.assertEqual(1, engine()._environment()["spec"]["replicas"])
        live["status"]["phase"] = "Pending"
        self.kube.add(live)
        with self.assertRaisesRegex(Held, "remain running"):
            engine()._environment()

    def test_a_record_with_another_kind_of_worker_is_refused(self):
        state = self.m.state()
        state["setup"]["resources"][-2]["target"]["kind"] = "ConfigMap"
        with self.assertRaises(Held):
            B.validate(state["setup"], NS, "homestead", OP)


if __name__ == "__main__":
    unittest.main()
