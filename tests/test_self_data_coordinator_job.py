"""Homestead's own data move: the coordinator is a Job, so losing its pod does
not leave Homestead at 0 copies with nothing looking after it (stuck-jobs C4).

Each test drives the real setup, worker and coordinator on the shared fake API
(tests/fakes/kube.py, tests/fakes/self_data_move.py) with a clock the test moves.
A "new pod" is a new Runner, as a new process has nothing in memory."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_anchor as A
import homestead_self_data_bootstrap as B
import homestead_self_data_finish as FIN
import homestead_self_data_kube as HK
import homestead_self_data_launch as L
import homestead_self_data_recovery as R
import homestead_self_data_worker as W
from homestead_storage_journal import Held
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
        self.m.pod_terminating(self.m.first_pod, grace=0)
        self.m.pass_time(1)
        self.second.tick()
        self.first.hold = "The data handoff control write was not verified"
        self.first.tick()
        self.assertNotEqual("held", self.m.state()["runtime"]["state"])

    def test_two_pods_cannot_both_win_the_takeover(self):
        """The record's version decides: the loser's write is refused and it tries again next time."""
        self.m.pod_failed(self.m.first_pod)
        third = self.m.start_pod("node-a")
        self.kube.fail(self.m.anchor.path, 409, method="PUT")
        self.assertNotEqual("held", self.second.tick()["status"])
        self.assertEqual(self.m.first_pod[0], self.m.state()["runtime"]["pod_uid"])
        self.second.tick()
        self.assertEqual(self.second_pod[0], self.m.state()["runtime"]["pod_uid"])
        self.assertTrue(third)
        self.assertNotEqual("held", self.m.state()["runtime"]["state"])

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

    def test_identity_still_answers_with_the_job_after_a_new_pod(self):
        self.assertEqual(self.m.job["metadata"]["uid"], self.m.runner(self.m.start_pod("node-b")).worker_uid)


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

    def test_a_record_with_another_kind_of_worker_is_refused(self):
        state = self.m.state()
        state["setup"]["resources"][-2]["target"]["kind"] = "ConfigMap"
        with self.assertRaises(Held):
            B.validate(state["setup"], NS, "homestead", OP)


if __name__ == "__main__":
    unittest.main()
