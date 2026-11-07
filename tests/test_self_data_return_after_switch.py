"""Going back to the original volume after the switch and before the start request
(#1214). Homestead has stayed at 0 copies, so nothing was written to the new volume."""
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_shared as SHARED
import homestead_self_data_admission as D
import homestead_self_data_anchor as A
import homestead_self_data_coordinator as C
import homestead_self_data_fence as F
import homestead_self_data_finish as FIN
import homestead_self_data_kube as HK
import homestead_self_data_launch as L
import homestead_self_data_recovery as R
import homestead_self_data_worker as W
from homestead_storage_journal import Held
from fakes.kube import Kube
from fakes.self_data_move import Move, NS

CUTOVER_WORDS = "Cutover has been requested"


class Base(unittest.TestCase):
    pins = None

    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.m = Move(self.kube, pins=self.pins)
        self.run_ = self.m.runner(real_admission=True)

    def load(self):
        return A.Anchor(self.m.read, self.m.send, NS, "homestead").load(**self.m.handle)

    def preview(self, action):
        return R.review(self.load(), self.m.read, lambda _: self.m.logs_text, action, clock=lambda: self.m.now)

    def confirm(self, action):
        fingerprint = self.preview(action)["fingerprint"]
        return R.confirm(self.load(), self.m.read, lambda _: self.m.logs_text, action, fingerprint, clock=lambda: self.m.now)

    def after_the_switch(self):
        m, run = self.m, self.run_
        run.tick(); m.stopped(); run.tick(); run.tick(); m.copy_finished(); run.tick(); run.tick(); m.copy_released(); run.tick(); run.tick()
        self.assertEqual("start", m.state()["phase"])
        self.assertIn("switch", m.steps())
        self.assertNotIn("start", m.steps())
        m.stopped()

    def stranger(self, claim):
        return self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "stranger", "namespace": NS},
                              "spec": {"nodeName": "node-b", "volumes": [{"persistentVolumeClaim": {"claimName": claim}}]}})

    def hold(self, claim="target"):
        """Held by something a person can clear: a pod outside Homestead on a volume."""
        path = self.stranger(claim)
        self.assertEqual("held", self.run_.tick()["status"])
        self.kube.discard(path)

    def puts(self):
        return len(self.m.writes_to(self.m.dep_path, "PUT"))

    def claim(self):
        return self.kube.object(self.m.dep_path)["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]

    def replicas(self):
        return self.kube.object(self.m.dep_path)["spec"]["replicas"]

    def journal(self, change):
        """Change the record as if it had been saved so: a state no run reaches by itself."""
        obj = self.kube.object(self.m.anchor.path)
        state = json.loads(obj["data"]["state.json"])
        change(state)
        obj["data"]["state.json"] = json.dumps(state)
        self.kube.add(obj)

    def both_claims_kept(self):
        for claim in ("source", "target"):
            self.assertIsNotNone(self.kube.object(f"/api/v1/namespaces/{NS}/persistentvolumeclaims/{claim}"))
        for volume in ("old-pv", "new-pv"):
            self.assertIsNotNone(self.kube.object("/api/v1/persistentvolumes/" + volume))


class GoingBackTests(Base):
    def test_held_after_the_switch_it_goes_back_with_both_volumes_kept(self):
        m = self.m
        self.after_the_switch()
        self.hold()
        self.assertEqual("target", self.claim())
        self.confirm("return-original")
        self.run_.tick()
        self.assertIn("recover-start", m.steps())
        self.assertEqual(("source", 1), (self.claim(), self.replicas()))
        m.started("source")
        view = self.run_.tick()
        self.assertEqual("done", m.state()["phase"])
        self.assertEqual("cancelled", view["status"])
        self.assertNotIn("start", m.steps())
        self.both_claims_kept()
        self.assertEqual("source", self.kube.object("/api/v1/namespaces/lab/pods/homestead-new")["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_a_copy_of_the_original_deployment_is_what_comes_back_at_the_reviewed_image(self):
        self.after_the_switch()
        self.hold()
        self.confirm("return-original")
        self.run_.tick()
        spec = self.kube.object(self.m.dep_path)["spec"]
        self.assertEqual(self.m.state()["plan"]["copy_image"], spec["template"]["spec"]["containers"][0]["image"])
        self.assertEqual("Recreate", spec["strategy"]["type"])

    def test_it_sends_one_write_to_go_back(self):
        self.after_the_switch()
        self.hold()
        self.confirm("return-original")
        puts = self.puts()
        self.run_.tick()
        self.run_.tick()
        self.m.started("source")
        self.run_.tick()
        self.run_.tick()
        self.assertEqual(puts + 1, self.puts())

    def test_a_start_request_with_no_recorded_outcome_refuses_both_ways(self):
        self.after_the_switch()
        self.hold()
        for outcome in ("intent", "uncertain"):
            with self.subTest(outcome=outcome):
                def started(state):
                    writes = state["journal"]["ref"]["storage_writes"]
                    entry = copy.deepcopy(next(e for e in writes if e["step"] == "switch"))
                    entry.update(step="start", state=outcome)
                    state["journal"]["ref"]["storage_writes"] = [e for e in writes if e["step"] != "start"] + [entry]
                self.journal(started)
                sent = len(self.kube.writes)
                for action in ("resume", "return-original"):
                    with self.assertRaisesRegex(Held, "known request outcomes"):
                        self.preview(action)
                self.assertEqual(sent, len(self.kube.writes))

    def test_a_switch_with_no_recorded_outcome_refuses_both_ways(self):
        self.after_the_switch()
        self.hold()
        def unknown(state):
            next(e for e in state["journal"]["ref"]["storage_writes"] if e["step"] == "switch")["state"] = "intent"
        self.journal(unknown)
        for action in ("resume", "return-original"):
            with self.assertRaisesRegex(Held, "known request outcomes"):
                self.preview(action)

    def test_after_a_start_request_it_still_refuses(self):
        m = self.m
        self.after_the_switch()
        self.run_.tick()
        self.assertIn("start", m.steps())
        m.started()
        self.hold()
        with self.assertRaisesRegex(Held, CUTOVER_WORDS):
            self.preview("return-original")
        self.assertEqual("resume", self.preview("resume")["action"])

    def test_a_pod_on_either_volume_stops_the_preview(self):
        self.after_the_switch()
        self.hold()
        for claim in ("target", "source"):
            with self.subTest(claim=claim):
                path = self.stranger(claim)
                with self.assertRaises(Held):
                    self.preview("return-original")
                self.kube.discard(path)

    def test_a_pod_on_either_volume_after_the_review_holds_and_writes_nothing(self):
        for claim in ("target", "source"):
            with self.subTest(claim=claim), Kube() as kube:
                self.kube = kube
                self.m = Move(kube)
                self.run_ = self.m.runner(real_admission=True)
                self.after_the_switch()
                self.hold()
                self.confirm("return-original")
                puts = self.puts()
                self.stranger(claim)
                self.assertEqual("held", self.run_.tick()["status"])
                self.assertEqual(puts, self.puts())
                self.assertNotIn("recover-start", self.m.steps())
                self.assertEqual("target", self.claim())

    def test_a_changed_mount_refuses(self):
        self.after_the_switch()
        self.hold()
        dep = self.kube.object(self.m.dep_path)
        dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "third"
        self.kube.add(dep)
        with self.assertRaises(Held):
            self.preview("return-original")

    def test_a_held_move_before_the_switch_goes_back_as_it_did(self):
        m = self.m
        run = self.run_
        run.tick(); m.stopped(); run.tick()
        self.assertEqual("copy", m.state()["phase"])
        self.hold("source")
        self.assertNotIn("switch", m.steps())
        self.confirm("return-original")
        puts = self.puts()
        run.tick()
        self.assertEqual(["stop", "recover-start"], m.steps())
        self.assertEqual(puts + 1, self.puts())
        self.assertEqual(("source", 1), (self.claim(), self.replicas()))

    def test_a_switch_with_no_copy_release_stays_a_refusal(self):
        """A switch can't be recorded without its release; a record that says so is not trusted."""
        m = self.m
        jobs, released = [], m.copy_released
        m.copy_released = lambda: (jobs.append(self.kube.object(m.copy_path)), released())
        self.after_the_switch()
        self.hold()
        def unreleased(state):
            writes = state["journal"]["ref"]["storage_writes"]
            state["journal"]["ref"]["storage_writes"] = [e for e in writes if e["step"] != "release-copy"]
        self.journal(unreleased)
        job = jobs[0]
        job["metadata"].pop("finalizers", None)
        job["metadata"].pop("deletionTimestamp", None)
        self.kube.add(job)
        with self.assertRaisesRegex(Held, "no completed release"):
            self.preview("return-original")

    def with_a_start_entry(self, state):
        entry = copy.deepcopy(next(e for e in state["journal"]["ref"]["storage_writes"] if e["step"] == "switch"))
        entry["step"] = "start"
        state["journal"]["ref"]["storage_writes"].append(entry)

    def test_the_coordinator_refuses_by_itself_after_a_start_request(self):
        """The record's own check refuses this first; the coordinator's line stands by itself too."""
        m = self.m
        self.after_the_switch()
        self.hold()
        self.confirm("return-original")
        self.journal(self.with_a_start_entry)
        with mock.patch.object(A, "_validate", lambda state, namespace: None):
            engine = C.Coordinator(self.load(), m.read, m.send, lambda _: m.logs_text, None,
                                   worker_uid=m.job["metadata"]["uid"], clock=lambda: m.now)
            with self.assertRaisesRegex(Held, "prohibited after cutover"):
                engine._return_original(self.kube.object(m.dep_path))

    def test_a_finished_return_with_a_start_entry_never_reads_as_cancelled(self):
        m = self.m
        self.after_the_switch()
        self.hold()
        self.confirm("return-original")
        self.run_.tick()
        m.started("source")
        self.assertEqual("cancelled", self.run_.tick()["status"])
        anchor = self.load()
        self.with_a_start_entry(anchor.state)
        self.assertNotEqual("cancelled", W.progress(anchor, m.now)["status"])

    def test_a_record_of_going_back_after_a_start_request_is_not_valid(self):
        self.after_the_switch()
        self.hold()
        self.confirm("return-original")
        state = json.loads(self.kube.object(self.m.anchor.path)["data"]["state.json"])
        A._validate(state, NS)
        entry = copy.deepcopy(next(e for e in state["journal"]["ref"]["storage_writes"] if e["step"] == "switch"))
        entry["step"] = "start"
        state["journal"]["ref"]["storage_writes"].append(entry)
        with self.assertRaisesRegex(Held, "prohibited after cutover"):
            A._validate(state, NS)


class TheProposalTests(Base):
    def state_and_dep(self):
        state = self.m.state()
        return state, self.kube.object(self.m.dep_path)

    def test_it_names_the_source_with_the_reviewed_count_and_image_and_leaves_its_input(self):
        self.after_the_switch()
        state, dep = self.state_and_dep()
        before = copy.deepcopy(dep)
        proposed = C.return_proposal(dep, state, state["plan"])
        self.assertEqual(before, dep)
        self.assertEqual(state["replicas"], proposed["spec"]["replicas"])
        spec = proposed["spec"]["template"]["spec"]
        self.assertEqual("source", spec["volumes"][0]["persistentVolumeClaim"]["claimName"])
        self.assertEqual(state["plan"]["copy_image"], spec["containers"][0]["image"])

    def test_it_pins_the_reviewed_image_whatever_the_deployment_runs(self):
        self.after_the_switch()
        state, dep = self.state_and_dep()
        dep["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.example.com/homestead:other"
        proposed = C.return_proposal(dep, state, state["plan"])
        self.assertEqual(state["plan"]["copy_image"], proposed["spec"]["template"]["spec"]["containers"][0]["image"])

    def test_it_refuses_the_new_volume_with_no_switch_on_record(self):
        run, m = self.run_, self.m
        run.tick(); m.stopped()
        state, dep = self.state_and_dep()
        dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "target"
        with self.assertRaisesRegex(Held, "reviewed data mount"):
            C.return_proposal(dep, state, state["plan"])

    def test_it_refuses_a_third_claim_and_two_data_volumes(self):
        self.after_the_switch()
        state, dep = self.state_and_dep()
        third = copy.deepcopy(dep)
        third["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "third"
        doubled = copy.deepcopy(dep)
        doubled["spec"]["template"]["spec"]["volumes"].append(copy.deepcopy(doubled["spec"]["template"]["spec"]["volumes"][0]))
        for wrong in (third, doubled):
            with self.assertRaisesRegex(Held, "reviewed data mount"):
                C.return_proposal(wrong, state, state["plan"])


class TheCapacityReviewTests(Base):
    pins = {"new-pv": "node-a"}

    def test_the_capacity_review_is_of_the_original_volume(self):
        m = self.m
        self.after_the_switch()
        self.hold()
        result = self.confirm("return-original")
        self.run_.tick()
        self.assertIn("recover-start", m.steps(), "the receipt must match what the PUT sends")
        sent = self.kube.object(m.dep_path)
        report = D.review(m.read, NS, "restart", sent, m.pins, 88, clock=lambda: m.now, boots=False, members=False)
        self.assertEqual(report["receipt"]["proposal"], result["restart"]["proposal"])
        self.assertEqual("source", sent["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_a_new_volume_pinned_to_a_dead_host_does_not_fail_it(self):
        m = self.m
        self.after_the_switch()
        self.hold()
        m.pass_time(0)
        node = self.kube.object("/api/v1/nodes/node-a")
        node["status"]["conditions"] = [{"type": "Ready", "status": "Unknown"}]
        self.kube.add(node)
        m.down.add("node-a")
        m.pass_time(60)
        self.confirm("return-original")
        self.run_.tick()
        self.assertIn("recover-start", m.steps())
        self.assertEqual(("source", 1), (self.claim(), self.replicas()))


class TheFenceTests(Base):
    """A new Homestead pod on the original volume, after a switch was recorded."""

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.after_the_switch()
        self.hold()
        self.confirm("return-original")
        self.run_.tick()
        self.m.started("source")
        pod = self.kube.object("/api/v1/namespaces/lab/pods/homestead-new")
        pod["spec"]["containers"][0]["volumeMounts"] = [{"name": "data", "mountPath": self.directory}]
        self.kube.add(pod)
        state = self.m.state()
        SHARED.write_json(os.path.join(self.directory, F.MARKER), A.pointer(NS, state, self.m.handle["uid"]), durable=True)
        self.fence = F.Fence(self.m.read, NS, "homestead", "homestead-new", "homestead", self.directory)

    def edit_entries(self, change):
        self.journal(lambda state: change(state["journal"]["ref"]["storage_writes"]))

    def test_the_fence_lets_the_original_volume_start_after_a_switch(self):
        before = self.fence.inspect()
        self.assertEqual(("recovery", False), (before["mode"], before["writable"]))
        self.run_.tick()
        self.assertEqual("done", self.m.state()["phase"])
        fence = F.Fence(self.m.read, NS, "homestead", "homestead-new", "homestead", self.directory)
        after = fence.inspect()
        self.assertEqual(("recovery", True), (after["mode"], after["writable"]))

    def test_the_fence_holds_with_no_recover_start(self):
        self.edit_entries(lambda writes: writes.remove(next(e for e in writes if e["step"] == "recover-start")))
        with self.assertRaisesRegex(Held, "no verified restart"):
            self.fence.inspect()

    def test_the_fence_holds_with_two_recover_starts(self):
        self.edit_entries(lambda writes: writes.append(copy.deepcopy(next(e for e in writes if e["step"] == "recover-start"))))
        with self.assertRaises(Held):
            self.fence.inspect()

    def test_a_start_too_never_opens_the_original_volume(self):
        def started(writes):
            entry = copy.deepcopy(next(e for e in writes if e["step"] == "switch"))
            entry["step"] = "start"
            writes.append(entry)
        self.edit_entries(started)
        with self.assertRaises(Held):
            self.fence.inspect()
        # The record's own check refuses this first; the fence's line stands by itself too.
        with mock.patch.object(A, "_validate", lambda state, namespace: None):
            with self.assertRaisesRegex(Held, "crossed cutover"):
                F.Fence(self.m.read, NS, "homestead", "homestead-new", "homestead", self.directory).inspect()

    def test_a_finished_return_after_a_switch_reads_as_complete(self):
        m = self.m
        self.run_.tick()
        view = W.progress(self.load(), m.now)
        self.assertEqual("cancelled", view["status"])
        obj = self.kube.object(m.anchor.path)
        state = m.state()
        binding = {"data_volume": "data", "destination_pvc": {"name": "source", "uid": state["source"]["uid"]},
                   "destination_pv": {k: state["plan"]["source_pv"][k] for k in ("name", "uid")}}
        FIN._save(self.directory, {"protocol": 1, "anchor": obj, "cleanup": {}, "source_binding": binding})
        self.assertIsNotNone(FIN.read(self.directory, NS, "homestead"))
        fence = F.Fence(m.read, NS, "homestead", "homestead-new", "homestead", self.directory)
        self.assertEqual(("done", True), (fence.inspect()["mode"], fence.inspect()["writable"]))


class ByHandTests(Base):
    """docs/data-move-recovery.md's settle.py, as written, on the going-back write."""

    @staticmethod
    def script(name):
        docs = (Path(__file__).resolve().parents[1] / "docs" / "data-move-recovery.md").read_text()
        return re.search(r"```python\n# " + re.escape(name) + r":.*?```", docs, re.S).group(0).split("\n", 1)[1].rsplit("```", 1)[0]

    def settle(self, outcome):
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
        env = {L.CONFIG_ENV: m.setup.config, L.UID_ENV: m.first_pod[0], L.JOB_UID_ENV: m.job["metadata"]["uid"]}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(HK, "Client", Client), \
                mock.patch.object(sys, "argv", ["settle.py", outcome]), mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(sys, "stdout", out):
            exec(compile(self.script("settle.py"), "settle.py", "exec"), {"__name__": "__main__"})
        return out.getvalue()

    def lost(self, *, applied):
        """Going back's one write loses its reply."""
        self.after_the_switch()
        self.hold()
        self.confirm("return-original")
        puts = self.puts()
        self.kube.fail(self.m.dep_path, 500, method="PUT", applied=applied, times=1)
        self.assertEqual("held", self.run_.tick()["status"])
        self.assertEqual(puts + 1, self.puts(), "one attempt, whatever became of it")
        self.assertNotEqual("accepted", next(e for e in self.m.state()["journal"]["ref"]["storage_writes"] if e["step"] == "recover-start")["state"])
        return puts

    def test_a_lost_reply_going_back_holds_and_doesnt_resend(self):
        puts = self.lost(applied=True)
        for _ in range(3):
            self.assertEqual("held", self.run_.tick()["status"])
        self.assertEqual(puts + 1, self.puts())
        self.assertEqual(("source", 1), (self.claim(), self.replicas()))
        for action in ("resume", "return-original"):
            with self.assertRaisesRegex(Held, "known request outcomes"):
                self.preview(action)

    def test_settled_applied_it_finishes_on_the_original_volume(self):
        self.lost(applied=True)
        self.assertIn("Recorded", self.settle("applied"))
        self.assertEqual("accepted", next(e for e in self.m.state()["journal"]["ref"]["storage_writes"] if e["step"] == "recover-start")["state"])
        self.m.started("source")
        self.confirm("return-original")
        puts = self.puts()
        self.run_.tick()
        self.assertEqual("done", self.m.state()["phase"])
        self.assertEqual(puts, self.puts())

    def test_settled_not_sent_it_can_go_back_again(self):
        self.lost(applied=False)
        self.assertEqual(("target", 0), (self.claim(), self.replicas()))
        self.settle("not-sent")
        self.assertNotIn("recover-start", self.m.steps())
        self.confirm("return-original")
        puts = self.puts()
        self.run_.tick()
        self.assertEqual(puts + 1, self.puts())
        self.assertEqual(("source", 1), (self.claim(), self.replicas()))

    def test_settling_refuses_the_opposite_answer(self):
        self.lost(applied=False)
        with self.assertRaises(SystemExit):
            self.settle("applied")


class WhatAPersonSeesTests(Base):
    def test_after_the_switch_the_hold_offers_both(self):
        self.after_the_switch()
        path = self.stranger("target")
        view = self.run_.tick()
        self.kube.discard(path)
        self.assertIn("--action resume", view["message"])
        self.assertIn("--action return-original", view["message"])

    def test_after_the_start_request_the_hold_offers_resume_only(self):
        self.after_the_switch()
        self.run_.tick()
        self.m.started()
        path = self.stranger("target")
        view = self.run_.tick()
        self.kube.discard(path)
        self.assertEqual("held", view["status"])
        self.assertIn("--action resume", view["message"])
        self.assertNotIn("return-original", view["message"])


if __name__ == "__main__":
    unittest.main()
