"""Homestead's own data move: the coordinator is a Job, so losing its pod does
not leave Homestead at 0 copies with nothing looking after it (stuck-jobs C4).

Each test drives the real setup, worker and coordinator on the shared fake API
(tests/fakes/kube.py, tests/fakes/self_data_move.py) with a clock the test moves.
A "new pod" is a new Runner, as a new process has nothing in memory."""
import datetime
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
import homestead_self_data_admission as D
import homestead_self_data_bootstrap as B
import homestead_self_data_coordinator as C
import homestead_self_data_copy as COPY
import homestead_self_data_fence as F
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
    """The default: the coordinator and the copy on one host (node-a). Only hosts in use
    count for a reboot. These run production's real Admitter, whose capacity check is
    what a stand-in once hid: it compared every reviewed host's boot id."""

    def runner(self, pod=None):
        return self.m.runner(pod, real_admission=True)

    def to_the_copy(self):
        run = self.runner()
        run.tick(); self.m.stopped(); run.tick()
        self.assertEqual("copy", self.m.state()["phase"])
        return run

    def replaced(self):
        self.m.pod_lost(self.m.first_pod)
        return self.runner(self.m.start_pod("node-a"))

    def finish(self, run):
        m = self.m
        m.copy_finished(); run.tick(); run.tick(); m.copy_released(); run.tick(); run.tick(); m.stopped(); run.tick(); m.started()
        return run.tick()

    def test_a_reboot_before_the_copy_holds_nothing_and_the_move_finishes(self):
        m = self.m
        self.to_the_copy()
        m.rebooted("node-a")
        run = self.replaced()
        view = run.tick()
        self.assertNotEqual("held", view["status"], view)
        self.assertIn("copy-job", m.steps())
        m.copy_running()                            # on the host that rebooted, after it did
        self.assertNotEqual("held", run.tick()["status"])
        self.assertEqual("done", self.finish(run)["status"])

    def test_a_reboot_of_a_host_the_move_does_not_use_holds_nothing_at_any_admission(self):
        m = self.m
        run = self.to_the_copy()
        m.rebooted("node-b")
        self.assertNotEqual("held", run.tick()["status"])
        self.assertIn("copy-job", m.steps())
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

    def test_going_back_passes_the_real_capacity_check_after_a_reboot(self):
        m = self.m
        run = self.to_the_copy()
        path = self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "stranger", "namespace": NS},
                              "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "source"}}]}})
        self.assertEqual("held", run.tick()["status"])
        self.kube.discard(path)
        m.rebooted("node-b")
        load = lambda: A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
        review = R.review(load(), m.read, lambda _: m.logs_text, "return-original", clock=lambda: m.now)
        R.confirm(load(), m.read, lambda _: m.logs_text, "return-original", review["fingerprint"], clock=lambda: m.now)
        run.tick()
        self.assertIn("recover-start", m.steps())

    def test_a_replaced_or_removed_host_still_holds(self):
        """A reboot is not a replacement: a new uid, or a host gone, is not the reviewed cluster."""
        for change in ("replaced", "removed"):
            with self.subTest(change=change):
                kube = Kube()
                with kube:
                    m = Move(kube)
                    run = m.runner(real_admission=True)
                    run.tick(); m.stopped(); run.tick()
                    node = kube.object("/api/v1/nodes/node-b")
                    if change == "replaced":
                        node["metadata"]["uid"] = "a-new-machine"
                        kube.add(node)
                    else:
                        kube.discard("/api/v1/nodes/node-b")
                    self.assertEqual("held", run.tick()["status"])
                    self.assertNotIn("copy-job", m.steps())

    def test_the_capacity_check_still_compares_boot_ids_at_the_review_before_the_stop(self):
        m = self.m
        m.rebooted("node-b")
        with self.assertRaisesRegex(Held, "rebooted"):
            D.review(m.read, NS, "copy", COPY.job(NS, "homestead-data-copy-" + OP, "source", "target", IMAGE, OP, "node-a"),
                     m.pins, 88, clock=lambda: m.now)

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

    def test_a_reboot_during_the_copy_can_go_back_once_the_dead_copy_pod_is_removed(self):
        """The hand work the docs give: the boot id proves its process is dead."""
        m = self.m
        run = self.to_the_copy()
        run.tick(); m.copy_running(); run.tick()
        m.rebooted("node-a")
        self.assertEqual("held", run.tick()["status"])
        self.kube.discard(m.copy_pod)
        job = self.kube.object(m.copy_path)         # backoffLimit 0: the Job ends Failed
        job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
        self.kube.add(job)
        load = lambda: A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
        with self.assertRaisesRegex(Held, "failed"):
            R.review(load(), m.read, lambda _: m.logs_text, "resume", clock=lambda: m.now)
        review = R.review(load(), m.read, lambda _: m.logs_text, "return-original", clock=lambda: m.now)
        R.confirm(load(), m.read, lambda _: m.logs_text, "return-original", review["fingerprint"], clock=lambda: m.now)
        run.tick()
        self.assertIn("recover-release-copy", m.steps())
        self.kube.discard(m.copy_path)
        run.tick()
        self.assertIn("recover-start", m.steps())
        self.assertEqual("source", self.kube.object(m.dep_path)["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_a_reboot_while_the_finished_copy_pod_is_there_still_holds(self):
        m = self.m
        run = self.to_the_copy()
        run.tick(); m.copy_finished(); run.tick()
        m.rebooted("node-a")
        self.assertEqual("held", run.tick()["status"])

    def test_a_reboot_with_homesteads_old_pod_listed_holds_then_previews_pass_once_it_is_gone(self):
        """A writer from before the move: unchanged. The hand work: remove the dead pod."""
        m = self.m
        run = self.runner()
        run.tick()
        m.rebooted("node-a")
        self.assertEqual("held", run.tick()["status"])
        load = lambda: A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
        with self.assertRaisesRegex(Held, "rebooted"):
            R.review(load(), m.read, lambda _: m.logs_text, "resume", clock=lambda: m.now)
        m.stopped()
        for action in ("resume", "return-original"):
            self.assertEqual(action, R.review(load(), m.read, lambda _: m.logs_text, action, clock=lambda: m.now)["action"])


class HostBackWithoutMetricsTests(unittest.TestCase):
    """A host just back has no live memory figure for a minute or two: the normal case before
    the start request. The move waits up to 5 minutes for it, saying so, then holds as before.
    Only that: any other capacity change still holds at once."""

    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.reporting = ["node-a", "node-b"]
        self.extra = None
        self.kube.react("GET", "/apis/metrics.k8s.io/v1beta1/nodes", self.metrics)
        self.m = None
        self.m = Move(self.kube)
        self.run_ = self.m.runner(real_admission=True)
        self.run_.tick(); self.m.stopped(); self.run_.tick()
        self.assertEqual("copy", self.m.state()["phase"])

    def metrics(self, method, path, body):
        now = self.m.now if self.m else 100000
        stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"kind": "NodeMetricsList", "apiVersion": "metrics.k8s.io/v1beta1", "metadata": {},
                "items": [{"metadata": {"name": h, "uid": h}, "timestamp": stamp, "usage": {"memory": "1Gi", "cpu": "100m"}} for h in self.reporting]}

    def back_without_a_figure(self):
        self.m.rebooted("node-a")
        self.reporting.remove("node-a")

    def recover(self, action):
        m = self.m
        load = lambda: A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
        review = R.review(load(), m.read, lambda _: m.logs_text, action, clock=lambda: m.now)
        R.confirm(load(), m.read, lambda _: m.logs_text, action, review["fingerprint"], clock=lambda: m.now)

    def test_it_waits_saying_so_sends_nothing_and_carries_on_when_the_figure_comes(self):
        m = self.m
        self.back_without_a_figure()
        view = self.run_.tick()
        self.assertEqual("running", m.state()["runtime"]["state"])
        self.assertNotEqual("held", view["status"])
        self.assertIn("node-a", view["message"])
        self.assertIn("5 minutes", view["message"])
        self.assertFalse(view["requires_review"])
        self.assertNotIn("copy-job", m.steps())
        m.pass_time(120)
        self.assertNotEqual("held", self.run_.tick()["status"])
        self.reporting.append("node-a")
        view = self.run_.tick()
        self.assertIn("copy-job", m.steps())
        self.assertNotIn("figure", m.state().get("waits", {}))
        self.assertNotIn("5 minutes", self.run_.snapshot()["message"])

    def test_after_five_minutes_it_holds_as_before_and_resume_goes_on_once_the_figure_is_back(self):
        m = self.m
        self.back_without_a_figure()
        self.run_.tick()
        m.pass_time(299)
        self.assertNotEqual("held", self.run_.tick()["status"])
        m.pass_time(1)
        view = self.run_.tick()
        self.assertEqual("held", view["status"])
        self.assertIn("live memory usage is unavailable", view["message"])
        self.assertIn("held", m.state()["runtime"]["state"])
        self.reporting.append("node-a")
        self.recover("resume")
        self.run_.tick(); self.run_.tick()
        self.assertIn("copy-job", m.steps())

    def test_a_new_coordinator_keeps_the_same_five_minutes(self):
        m = self.m
        self.back_without_a_figure()
        self.run_.tick()
        m.pass_time(200)
        m.pod_lost(m.first_pod)
        run = m.runner(m.start_pod("node-b"), real_admission=True)
        self.assertNotEqual("held", run.tick()["status"])
        m.pass_time(100)
        self.assertEqual("held", run.tick()["status"])

    def test_a_host_that_did_not_reboot_with_no_figure_holds_at_once(self):
        self.reporting.remove("node-a")
        view = self.run_.tick()
        self.assertEqual("held", view["status"])
        self.assertIn("live memory usage is unavailable", view["message"])

    def test_another_host_with_no_figure_that_did_not_reboot_holds_at_once(self):
        self.back_without_a_figure()
        self.reporting.remove("node-b")
        self.assertEqual("held", self.run_.tick()["status"])

    def test_any_other_new_warning_holds_at_once_even_with_a_host_just_back(self):
        self.back_without_a_figure()
        real = D.review
        def with_another(*args, **kwargs):
            report = real(*args, **kwargs)
            report["warnings"] = report["warnings"] + ["projected RAM reaches 99%"]
            report["receipt"]["warnings"] = sorted({*report["receipt"]["warnings"], D.warning_key("projected RAM reaches 99%")})
            return report
        with mock.patch.object(D, "review", with_another):
            view = self.run_.tick()
        self.assertEqual("held", view["status"])
        self.assertEqual("held", self.m.state()["runtime"]["state"])

    def test_the_normal_case_before_the_start_request(self):
        m = self.m
        self.run_.tick(); m.copy_finished(); self.run_.tick(); self.run_.tick(); m.copy_released(); self.run_.tick(); self.run_.tick(); m.stopped()
        self.assertEqual("start", m.state()["phase"])
        self.back_without_a_figure()
        view = self.run_.tick()
        self.assertNotEqual("held", view["status"])
        self.assertNotIn("start", m.steps())
        self.reporting.append("node-a")
        self.run_.tick()
        self.assertIn("start", m.steps())


class HostNeverBackTests(unittest.TestCase):
    """Only the new volume on a host that dies: the original is healthy on shared storage."""

    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.m = Move(self.kube, pins={"new-pv": "node-a"})
        self.run_ = self.m.runner(real_admission=True)

    def load(self):
        return A.Anchor(self.m.read, self.m.send, NS, "homestead").load(**self.m.handle)

    def preview(self, action):
        return R.review(self.load(), self.m.read, lambda _: self.m.logs_text, action, clock=lambda: self.m.now)

    def confirm(self, action):
        fingerprint = self.preview(action)["fingerprint"]
        R.confirm(self.load(), self.m.read, lambda _: self.m.logs_text, action, fingerprint, clock=lambda: self.m.now)

    def to_the_copy(self):
        self.run_.tick(); self.m.stopped(); self.run_.tick()
        self.assertEqual("copy", self.m.state()["phase"])

    def down(self):
        """The host dies, taking the helper with it."""
        self.m.host_down("node-a")
        self.replaced()

    def replaced(self):
        m = self.m
        m.pod_lost(getattr(self, "current", m.first_pod))
        self.current = m.start_pod("node-b")
        self.run_ = m.runner(self.current, real_admission=True)

    def test_before_the_switch_it_waits_thirty_minutes_saying_so_then_holds(self):
        m = self.m
        self.to_the_copy()
        self.down()
        view = self.run_.tick()
        self.assertNotEqual("held", view["status"])
        self.assertIn("node-a", view["message"])
        self.assertIn("30 minutes", view["message"])
        self.assertFalse(view["requires_review"])
        started = m.state()["runtime"]["checked_at"]
        m.pass_time(1799)
        self.assertNotEqual("held", self.run_.tick()["status"])
        self.assertGreater(m.state()["runtime"]["checked_at"], started, "the heartbeat goes on while it waits")
        self.assertEqual("running", m.state()["runtime"]["state"])
        m.pass_time(1)
        view = self.run_.tick()
        self.assertEqual("held", view["status"])
        self.assertEqual("held", m.state()["runtime"]["state"])
        self.assertIn("node-a", view["message"])
        self.assertIn("recovery_command", view)
        self.assertIn("return-original", view["message"])
        self.assertEqual(["stop"], m.steps())

    def test_recovery_goes_back_to_the_original_volume_without_the_dead_host(self):
        m = self.m
        self.to_the_copy()
        self.down()
        self.run_.tick()
        m.pass_time(1800)
        self.run_.tick()
        with self.assertRaisesRegex(Held, "(?s)node-a.*return-original"):
            self.preview("resume")
        self.confirm("return-original")
        self.run_.tick()
        self.assertIn("recover-start", m.steps())
        dep = self.kube.object(m.dep_path)
        self.assertEqual((1, "source"), (dep["spec"]["replicas"], dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]))

    def test_a_new_coordinator_keeps_the_thirty_minutes(self):
        m = self.m
        self.to_the_copy()
        self.down()
        self.run_.tick()
        m.pass_time(1000)
        self.run_.tick()
        self.replaced()
        self.assertNotEqual("held", self.run_.tick()["status"])
        m.pass_time(800)
        self.assertEqual("held", self.run_.tick()["status"])

    def test_back_in_time_it_carries_on_and_the_message_and_clock_go(self):
        m = self.m
        self.to_the_copy()
        self.down()
        self.run_.tick()
        m.pass_time(1000)
        self.run_.tick()
        m.rebooted("node-a")
        view = self.run_.tick()
        self.assertNotEqual("held", view["status"])
        self.assertNotIn("node-a", view["message"])
        self.assertIn("copy-job", m.steps())
        self.assertNotIn("host", m.state().get("waits", {}))
        m.host_down("node-a")
        m.pass_time(1000)
        self.assertNotEqual("held", self.run_.tick()["status"], "a new outage has its own 30 minutes")

    def after_the_switch(self):
        m = self.m
        run = self.run_
        run.tick(); m.stopped(); run.tick(); run.tick(); m.copy_finished(); run.tick(); run.tick(); m.copy_released(); run.tick(); run.tick()
        self.assertEqual("start", m.state()["phase"])
        m.stopped()

    def test_after_the_switch_the_wait_has_no_limit_and_the_page_says_so(self):
        m = self.m
        self.after_the_switch()
        self.down()
        for _ in range(3):
            m.pass_time(86400)
            view = self.run_.tick()
        self.assertNotEqual("held", view["status"])
        self.assertEqual("running", m.state()["runtime"]["state"])
        self.assertIn("node-a", view["message"])
        self.assertIn("no limit", view["message"])
        self.assertIn("docs/data-move-recovery.md", view["message"])
        self.assertNotIn("start", m.steps())

    def test_after_the_start_request_likewise(self):
        m = self.m
        self.after_the_switch()
        self.run_.tick()
        self.assertIn("start", m.steps())
        self.down()
        m.pass_time(86400)
        view = self.run_.tick()
        self.assertNotEqual("held", view["status"])
        self.assertIn("no limit", view["message"])

    def test_the_original_on_the_same_dead_disk_cannot_go_back_and_says_so(self):
        kube = Kube()
        with kube:
            m = Move(kube, pinned_to="node-a")
            run = m.runner(real_admission=True)
            run.tick(); m.stopped(); run.tick()
            m.host_down("node-a")
            m.pod_lost(m.first_pod)
            run = m.runner(m.start_pod("node-b"), real_admission=True)
            run.tick()
            m.pass_time(1800)
            self.assertEqual("held", run.tick()["status"])
            load = lambda: A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
            for action in ("resume", "return-original"):
                with self.assertRaisesRegex(Held, "node-a"):
                    R.review(load(), m.read, lambda _: m.logs_text, action, clock=lambda: m.now)
            with self.assertRaisesRegex(Held, "(?i)original"):
                R.review(load(), m.read, lambda _: m.logs_text, "return-original", clock=lambda: m.now)
            m.rebooted("node-a")
            self.assertEqual("resume", R.review(load(), m.read, lambda _: m.logs_text, "resume", clock=lambda: m.now)["action"])

    def test_every_down_host_is_named(self):
        """A pinned volume's host, and the host of Homestead's own pod before any stop request."""
        kube = Kube()
        with kube:
            m = Move(kube, pins={"new-pv": "node-a"})
            pod = kube.object(m.old_pod); pod["spec"]["nodeName"] = "node-b"; kube.add(pod)
            run = m.runner()
            m.host_down("node-a"); m.host_down("node-b")
            view = run.tick()
            self.assertNotEqual("held", view["status"])
            self.assertIn("node-a, node-b", view["message"])

    def test_a_down_host_does_not_mask_a_measured_one(self):
        """Pinned node-a is down, and node-b rebooted with an old pod listed: a hold, at once."""
        kube = Kube()
        with kube:
            m = Move(kube, pins={"new-pv": "node-a"})
            run = m.runner()
            run.tick()
            pod = kube.object(m.old_pod); pod["spec"]["nodeName"] = "node-b"; kube.add(pod)
            m.host_down("node-a"); m.rebooted("node-b")
            self.assertEqual("held", run.tick()["status"])

    def test_the_ten_minute_clock_stops_during_the_wait(self):
        kube = Kube()
        with kube:
            m = Move(kube, pins={"new-pv": "node-a"})
            run = m.runner()
            run.tick()
            pod = kube.object(m.old_pod); pod["spec"]["nodeName"] = "node-b"; pod["metadata"]["deletionTimestamp"] = "2020-01-01T00:00:00Z"; kube.add(pod)
            dep = kube.object(m.dep_path)
            dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 0}; kube.add(dep)
            run.tick()
            self.assertIn("quiesce", m.state()["waits"])
            m.pass_time(300)
            m.host_down("node-a")
            run.tick()
            m.pass_time(1200)
            self.assertNotEqual("held", run.tick()["status"])
            m.rebooted("node-a")
            self.assertNotEqual("held", run.tick()["status"], "360 s of the 600 had gone before the outage was seen")
            m.pass_time(239)
            self.assertNotEqual("held", run.tick()["status"])
            m.pass_time(1)
            self.assertEqual("held", run.tick()["status"])


class PartitionedWriterTests(unittest.TestCase):
    """A host with a pod listed on it that stops answering may have a writer still running: a
    hold, never a wait, even when a volume is pinned there too."""

    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.m = Move(self.kube, pinned_to="node-a")
        self.run_ = self.m.runner()

    def copying(self):
        self.run_.tick(); self.m.stopped(); self.run_.tick(); self.run_.tick(); self.m.copy_running()
        self.assertNotEqual("held", self.run_.tick()["status"])

    def not_ready(self):
        node = self.kube.object("/api/v1/nodes/node-a")
        node["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        self.kube.add(node)

    def stale(self):
        self.m.down.add("node-a")
        self.m.pass_time(60)

    def test_a_copy_pod_on_a_not_ready_host_holds(self):
        self.copying()
        self.not_ready()
        self.assertIn("unavailable", self.run_.tick()["message"])
        self.assertEqual("held", self.m.state()["runtime"]["state"])

    def test_a_copy_pod_on_a_host_with_a_stale_heartbeat_holds(self):
        self.copying()
        self.stale()
        self.assertIn("heartbeat is stale", self.run_.tick()["message"])
        self.assertEqual("held", self.m.state()["runtime"]["state"])

    def terminating_old_pod(self):
        self.run_.tick()
        pod = self.kube.object(self.m.old_pod)
        pod["metadata"]["deletionTimestamp"] = "2020-01-01T00:00:00Z"
        self.kube.add(pod)

    def test_an_old_homestead_pod_terminating_on_a_not_ready_host_holds(self):
        self.terminating_old_pod()
        self.not_ready()
        self.assertIn("unavailable", self.run_.tick()["message"])

    def test_an_old_homestead_pod_terminating_on_a_host_with_a_stale_heartbeat_holds(self):
        self.terminating_old_pod()
        self.stale()
        self.assertIn("heartbeat is stale", self.run_.tick()["message"])


class HeldBeforeTheStopTests(MoveTest):
    """Before any stop request Homestead's pods are fenced by the marker and going back writes
    nothing, so a reboot of their host holds nothing either: deleting the pod could not help,
    as its ReplicaSet starts another on the same host."""

    def load(self):
        return A.Anchor(self.m.read, self.m.send, NS, "homestead").load(**self.m.handle)

    def held(self):
        m = self.m
        m.pass_time(61)
        self.run_ = m.runner()
        self.assertEqual("held", self.run_.tick()["status"])

    def test_both_previews_pass_after_a_reboot(self):
        self.held()
        self.m.rebooted("node-a")
        for action in ("resume", "return-original"):
            self.assertEqual(action, R.review(self.load(), self.m.read, lambda _: self.m.logs_text, action, clock=lambda: self.m.now)["action"])

    def test_going_back_writes_nothing_and_homestead_keeps_running(self):
        m = self.m
        self.held()
        m.rebooted("node-a")
        review = R.review(self.load(), m.read, lambda _: m.logs_text, "return-original", clock=lambda: m.now)
        R.confirm(self.load(), m.read, lambda _: m.logs_text, "return-original", review["fingerprint"], clock=lambda: m.now)
        self.assertEqual("cancelled", self.run_.tick()["status"])
        self.assertEqual([], m.writes_to(m.dep_path))
        self.assertEqual(1, self.kube.object(m.dep_path)["spec"]["replicas"])

    def test_resume_after_a_reboot_sends_no_stop_on_an_expired_check_and_holds_again(self):
        """The copy check's one-minute receipt: resume can't get past it, and nothing is sent."""
        m = self.m
        self.held()
        m.rebooted("node-a")
        review = R.review(self.load(), m.read, lambda _: m.logs_text, "resume", clock=lambda: m.now)
        R.confirm(self.load(), m.read, lambda _: m.logs_text, "resume", review["fingerprint"], clock=lambda: m.now)
        self.run_.tick()
        view = self.run_.tick()
        self.assertEqual("held", view["status"])
        self.assertIn("expired", view["message"])
        self.assertEqual([], m.writes_to(m.dep_path))

    def test_homesteads_host_down_before_the_stop_is_a_wait_with_nothing_sent(self):
        m = self.m
        run = m.runner()
        m.host_down("node-a")
        view = run.tick()
        self.assertNotEqual("held", view["status"])
        self.assertIn("node-a", view["message"])
        self.assertEqual([], m.writes_to(m.dep_path))

    def test_after_the_stop_request_its_pods_are_measured_again(self):
        m = self.m
        run = m.runner()
        run.tick()
        self.assertEqual(["stop"], m.steps())
        m.rebooted("node-a")
        self.assertEqual("held", run.tick()["status"])


class VolumePinnedToAHostTests(unittest.TestCase):
    """Both volumes on node-a's disk, as local-path has them. Only a pod listed in the API can
    write, and those are still measured; the copy fsyncs before its receipt. So the host must
    be up with a fresh heartbeat, and its boot id alone holds nothing: the move carries on with
    no person, at every phase. While it is down the move waits and sends nothing."""

    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.m = Move(self.kube, pinned_to="node-a")
        self.run_ = self.m.runner(real_admission=True)

    def tick(self):
        return self.run_.tick()

    def reboot_then_carry_on(self):
        """Down first, as a real reboot is seen: no hold, no request, says so. Then back."""
        m = self.m
        before = [w for w in self.kube.writes if w[1] != m.anchor.path]
        m.host_down("node-a")
        view = self.tick()
        self.assertNotEqual("held", view["status"], view)
        self.assertNotEqual("held", m.state()["runtime"]["state"])
        self.assertIn("node-a", view["message"])
        self.assertEqual(before, [w for w in self.kube.writes if w[1] != m.anchor.path], "nothing is sent while it is down")
        m.rebooted("node-a")
        return self.tick()

    def test_before_the_copy(self):
        m = self.m
        self.tick(); m.stopped(); self.tick()
        self.assertEqual("copy", m.state()["phase"])
        self.assertNotEqual("held", self.reboot_then_carry_on()["status"])
        self.assertIn("copy-job", m.steps())

    def test_after_the_copy_released(self):
        m = self.m
        self.tick(); m.stopped(); self.tick(); self.tick(); m.copy_finished(); self.tick(); self.tick(); m.copy_released()
        self.assertEqual("switch", self.reboot_then_carry_on()["phase"])

    def test_before_the_switch_request(self):
        m = self.m
        self.tick(); m.stopped(); self.tick(); self.tick(); m.copy_finished(); self.tick(); self.tick(); m.copy_released(); self.tick()
        self.assertEqual("switch", m.state()["phase"])
        self.assertEqual("start", self.reboot_then_carry_on()["phase"])
        self.assertIn("switch", m.steps())

    def test_after_the_switch_before_the_start_request(self):
        m = self.m
        self.tick(); m.stopped(); self.tick(); self.tick(); m.copy_finished(); self.tick(); self.tick(); m.copy_released(); self.tick(); self.tick()
        m.stopped()
        self.assertEqual("start", self.reboot_then_carry_on()["phase"])
        self.assertIn("start", m.steps())

    def test_after_the_start_request_where_a_hold_would_keep_the_new_pods_unready(self):
        m = self.m
        self.tick(); m.stopped(); self.tick(); self.tick(); m.copy_finished(); self.tick(); self.tick(); m.copy_released(); self.tick(); self.tick()
        m.stopped(); self.tick()
        m.started()                                 # on the pinned host
        self.assertNotEqual("held", self.reboot_then_carry_on()["status"])
        self.assertEqual("done", m.state()["runtime"]["state"])

    def waits_then_carries_on(self):
        m = self.m
        before = [w for w in self.kube.writes if w[1] != m.anchor.path]
        view = self.tick()
        self.assertNotEqual("held", view["status"], view)
        self.assertIn("node-a", view["message"])
        self.assertEqual(before, [w for w in self.kube.writes if w[1] != m.anchor.path])
        m.rebooted("node-a")
        self.assertNotEqual("held", self.tick()["status"])
        self.assertIn("copy-job", m.steps())

    def test_a_stale_heartbeat_alone_is_a_wait(self):
        """Ready stays True for 40-50 s after the last heartbeat: this is seen first."""
        m = self.m
        self.tick(); m.stopped(); self.tick()
        m.down.add("node-a")
        m.pass_time(60)
        self.assertTrue(any(c["status"] == "True" for c in self.kube.object("/api/v1/nodes/node-a")["status"]["conditions"]))
        self.waits_then_carries_on()

    def test_not_ready_alone_is_a_wait(self):
        m = self.m
        self.tick(); m.stopped(); self.tick()
        node = self.kube.object("/api/v1/nodes/node-a")
        node["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        self.kube.add(node)
        self.waits_then_carries_on()

    def test_a_copy_pod_listed_on_it_is_still_measured(self):
        m = self.m
        self.tick(); m.stopped(); self.tick(); self.tick(); m.copy_running(); self.tick()
        m.rebooted("node-a")
        self.assertIn("rebooted", self.tick()["message"])

    def test_a_homestead_pod_listed_on_it_is_still_measured(self):
        m = self.m
        self.tick()
        pod = self.kube.object(m.old_pod); pod["spec"]["nodeName"] = "node-a"; self.kube.add(pod)
        m.rebooted("node-a")
        self.assertEqual("held", self.tick()["status"])

    def test_a_replaced_host_still_holds(self):
        m = self.m
        self.tick(); m.stopped(); self.tick()
        node = self.kube.object("/api/v1/nodes/node-a"); node["metadata"]["uid"] = "a-new-machine"; self.kube.add(node)
        self.assertEqual("held", self.tick()["status"])
        self.assertNotIn("copy-job", m.steps())

    def test_recovery_refuses_while_it_is_down_and_previews_once_it_is_back(self):
        m = self.m
        self.tick(); m.stopped()
        path = self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "stranger", "namespace": NS},
                              "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "source"}}]}})
        self.assertEqual("held", self.tick()["status"])
        self.kube.discard(path)
        m.host_down("node-a")
        load = lambda: A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle)
        for action in ("resume", "return-original"):
            with self.assertRaises(Held) as refused:
                R.review(load(), m.read, lambda _: m.logs_text, action, clock=lambda: m.now)
            self.assertIn("node-a", str(refused.exception))
            self.assertNotIn("carries on", str(refused.exception), "a held move does not carry on by itself")
        m.rebooted("node-a")
        self.assertEqual("resume", R.review(load(), m.read, lambda _: m.logs_text, "resume", clock=lambda: m.now)["action"])

    def test_a_volume_on_a_host_the_fence_cannot_map_still_measures_every_host(self):
        kube = Kube()
        with kube:
            m = Move(kube)
            pv = kube.object("/api/v1/persistentvolumes/old-pv")
            pv["spec"]["nodeAffinity"] = {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                {"key": "topology.kubernetes.io/zone", "operator": "In", "values": ["zone-a"]}]}]}}
            kube.add(pv)
            engine = C.Coordinator(A.Anchor(m.read, m.send, NS, "homestead").load(**m.handle), m.read, m.send,
                                   lambda _: "", lambda *_: True, worker_uid=m.job["metadata"]["uid"], clock=lambda: m.now)
            self.assertEqual({p["name"]: p["boot_id"] for p in m.pins}, engine._involved())


class ByHandTests(MoveTest):
    """docs/data-move-recovery.md's own scripts, run as written."""

    @staticmethod
    def script(name):
        docs = (Path(__file__).resolve().parents[1] / "docs" / "data-move-recovery.md").read_text()
        return re.search(r"```python\n# " + re.escape(name) + r":.*?```", docs, re.S).group(0).split("\n", 1)[1].rsplit("```", 1)[0]

    def new_try(self):
        """new-try.py over the Job as kubectl prints it, then kubectl create."""
        self.job_gave_up()
        job = self.kube.object(self.m.worker_path)
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(job))), mock.patch.object(sys, "stdout", out):
            exec(compile(self.script("new-try.py"), "new-try.py", "exec"), {"__name__": "__main__"})
        pod = json.loads(out.getvalue())
        pod["metadata"]["name"] = pod["metadata"].pop("generateName") + "abcde"
        pod["status"] = {"phase": "Running"}
        path = self.kube.add(pod)
        return self.kube.object(path)["metadata"]["uid"], path

    def settle(self, outcome, pod=None):
        """settle.py as the docs say, inside this pod, on a client held to the real Scope."""
        m = self.m
        scope = m.scope
        class Client:
            def __init__(self, _scope): pass
            def read(self, path):
                scope.check("GET", path)
                return m.read(path)
            def send(self, method, path, body=None):
                scope.check(method, path, body)
                return m.send(method, path, body)
        env = {L.CONFIG_ENV: m.setup.config, L.UID_ENV: (pod or self.acting)[0], L.JOB_UID_ENV: m.job["metadata"]["uid"]}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(HK, "Client", Client), \
                mock.patch.object(sys, "argv", ["settle.py", outcome]), mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(sys, "stdout", out):
            exec(compile(self.script("settle.py"), "settle.py", "exec"), {"__name__": "__main__"})
        return out.getvalue()

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

    def unknown_outcome(self, *, applied=True):
        """The stop request's outcome is unknown; a new pod holds on it."""
        m = self.m
        if not applied:
            self.kube.fail(m.dep_path, 500, method="PUT")
        with mock.patch.object(A.Anchor, "checkpoint", _fails_after_intent()), self.assertRaises(Died):
            m.runner().tick()
        m.pod_lost(m.first_pod)
        self.acting = m.start_pod("node-b")
        run = m.runner(self.acting)
        self.assertEqual("held", run.tick()["status"])
        return run

    def refused(self, outcome, words):
        before = self.m.state()
        with self.assertRaisesRegex(SystemExit, words):
            self.settle(outcome)
        self.assertEqual(before, self.m.state(), "a refusal writes nothing")

    def test_a_request_found_applied_is_recorded_and_recovery_goes_on_without_sending_it(self):
        m = self.m
        run = self.unknown_outcome()
        self.assertEqual(0, self.kube.object(m.dep_path)["spec"]["replicas"])
        self.assertIn("Recorded", self.settle("applied"))
        m.stopped()
        self.recover("resume")
        puts = len(m.writes_to(m.dep_path, "PUT"))
        run.tick(); run.tick()
        self.assertEqual("copy", m.state()["phase"])
        self.assertEqual(puts, len(m.writes_to(m.dep_path, "PUT")))

    def test_a_request_found_not_sent_can_go_back_to_the_original_volume(self):
        """It never reached the API: the record forgets it, and Homestead was never stopped."""
        m = self.m
        run = self.unknown_outcome(applied=False)
        self.assertEqual(1, self.kube.object(m.dep_path)["spec"]["replicas"])
        self.settle("not-sent")
        self.assertEqual([], m.steps())
        self.recover("return-original")
        self.assertEqual("cancelled", run.tick()["status"])

    def test_not_sent_is_refused_when_the_request_reached_the_deployment(self):
        self.unknown_outcome()
        self.refused("not-sent", "stop.*applied")

    def test_applied_is_refused_when_the_deployment_still_has_its_replicas(self):
        self.unknown_outcome(applied=False)
        self.refused("applied", "stop.*not-sent")

    def test_settling_refuses_while_the_move_is_not_held(self):
        """A live request on the wire: the outcome is about to be known."""
        m = self.m
        with mock.patch.object(A.Anchor, "checkpoint", _fails_after_intent()), self.assertRaises(Died):
            m.runner().tick()
        self.acting = m.first_pod
        self.refused("applied", "not held")
        self.refused("not-sent", "not held")

    def test_settling_refuses_while_another_pod_of_the_job_has_not_finished(self):
        m = self.m
        self.unknown_outcome()
        other = m.start_pod("node-a")
        self.refused("applied", other[1].rsplit("/", 1)[1])
        m.pod_terminating(other, grace=60)
        self.refused("applied", other[1].rsplit("/", 1)[1])
        m.pod_failed(other)
        self.assertIn("Recorded", self.settle("applied"))

    def test_settling_refuses_a_request_whose_outcome_is_known(self):
        m = self.m
        m.runner().tick()
        anchor = self.load()
        anchor.report(m.job["metadata"]["uid"], m.now, "held", "Held for another reason")
        self.acting = m.first_pod
        self.refused("applied", "Nothing to settle")

    def test_settling_refuses_anything_but_the_two_outcomes(self):
        self.unknown_outcome()
        self.refused("maybe", "applied or not-sent")

    def test_a_conflict_gets_words_not_a_traceback(self):
        m = self.m
        self.unknown_outcome()
        self.kube.fail(m.anchor.path, 409, method="PUT")
        with self.assertRaisesRegex(SystemExit, "changed.*again"):
            self.settle("applied")
        self.assertIn("Recorded", self.settle("applied"))

    def settle_step(self, step, outcome, change):
        """Each row of the docs' table, for the step named, on a record that says it is open."""
        m = self.m
        obj = self.kube.object(m.anchor.path)
        state = json.loads(obj["data"]["state.json"])
        paths = {"copy-job": (m.copy_path, "POST"), "release-copy": (m.copy_path, "DELETE"), "recover-release-copy": (m.copy_path, "DELETE")}
        path, method = paths.get(step, (m.dep_path, "PUT"))
        target = {"apiVersion": "batch/v1" if "job" in path else "apps/v1", "kind": "Job" if "job" in path else "Deployment",
                  "namespace": NS, "name": path.rsplit("/", 1)[1], "path": path}
        before = None if method == "POST" else {"uid": "u", "resourceVersion": "1"}
        state["journal"]["ref"]["storage_writes"] = [{"step": step, "method": method, "target": target, "payload": "a" * 64, "before": before, "state": "intent"}]
        state["runtime"] = {"worker_uid": m.job["metadata"]["uid"], "checked_at": m.now, "state": "held", "message": "Held", "pod_uid": m.first_pod[0]}
        obj["data"]["state.json"] = json.dumps(state)
        self.kube.add(obj)
        self.acting = m.first_pod
        change()
        return self.settle(outcome)

    def deployment(self, replicas, claim):
        def change():
            dep = self.kube.object(self.m.dep_path)
            dep["spec"]["replicas"] = replicas
            dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = claim
            self.kube.add(dep)
        return change

    def copy_job(self, *, deleting=False):
        def change():
            self.kube.add({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": self.m.copy_path.rsplit("/", 1)[1], "namespace": NS,
                           **({"deletionTimestamp": "2020-01-01T00:00:00Z", "finalizers": ["foregroundDeletion"]} if deleting else {})}, "spec": {}})
        return change

    def test_every_row_of_the_table_is_checked_both_ways(self):
        none = lambda: None
        cases = [("stop", self.deployment(0, "source"), self.deployment(1, "source")),
                 ("switch", self.deployment(0, "target"), self.deployment(0, "source")),
                 ("start", self.deployment(1, "target"), self.deployment(0, "target")),
                 ("recover-start", self.deployment(1, "source"), self.deployment(0, "source")),
                 ("copy-job", self.copy_job(), none),
                 ("release-copy", self.copy_job(deleting=True), self.copy_job()),
                 ("recover-release-copy", none, self.copy_job())]
        for step, applied, not_sent in cases:
            for outcome, world, other in (("applied", applied, "not-sent"), ("not-sent", not_sent, "applied")):
                with self.subTest(step=step, outcome=outcome):
                    self.kube.discard(self.m.copy_path)
                    self.assertIn("Recorded", self.settle_step(step, outcome, world))
                    self.kube.discard(self.m.copy_path)
                    with self.assertRaisesRegex(SystemExit, step):
                        self.settle_step(step, other, world)

    def test_new_try_refuses_a_job_that_has_not_failed(self):
        job = self.kube.object(self.m.worker_path)
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(job))), self.assertRaisesRegex(SystemExit, "has not failed"):
            exec(compile(self.script("new-try.py"), "new-try.py", "exec"), {"__name__": "__main__"})

    def test_a_recovery_clears_a_new_pods_hold_when_the_old_pod_had_already_held(self):
        """The old pod's hold and an open request: the new pod keeps the record's message,
        which is what recovery compares when it clears the hold."""
        m = self.m
        with mock.patch.object(A.Anchor, "checkpoint", _fails_after_intent()), self.assertRaises(Died):
            m.runner().tick()
        anchor = self.load()
        anchor.report(m.job["metadata"]["uid"], m.now, "held", "An earlier hold")
        m.pod_lost(m.first_pod)
        self.acting = m.start_pod("node-b")
        run = m.runner(self.acting)
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

    def test_going_back_the_receipt_can_be_removed_once_the_helpers_are_gone_and_startup_is_normal(self):
        """The docs' way back: no marker, no receipt, no control record is a normal start."""
        m = self.m
        self.save()
        self.finish()
        for pod in (m.first_pod,):
            self.kube.discard(pod[1])
        self.kube.discard(m.worker_path)
        self.assertTrue(self.finish()["done"])
        self.assertIsNone(self.kube.object(m.anchor.path))
        receipt = os.path.join(self.directory, FIN.FILE)
        self.assertTrue(os.path.exists(receipt))
        docs = (Path(__file__).resolve().parents[1] / "docs" / "data-move-recovery.md").read_text()
        self.assertIn("rm /data/" + FIN.FILE, docs)
        os.unlink(receipt)
        fence = F.Fence(m.read, NS, "homestead", "homestead-new", "homestead", self.directory)
        self.assertEqual({"mode": "normal", "writable": True}, fence.inspect())

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
