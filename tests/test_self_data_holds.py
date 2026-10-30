"""Homestead's own data move: holds that had no way out (stuck-jobs sweep, 30 Oct).

Each test drives the real coordinator, worker, recovery, fence or cleanup code
on the self-data Cluster fake from test_self_data_coordinator (or the shared
fake API in tests/fakes/kube.py) with a fake clock. Nothing sleeps."""
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import datetime
import tempfile

import homestead_self_data_anchor as A
import homestead_self_data_fence as F
import homestead_self_data_finish as FIN
import homestead_self_data_recovery as R
import homestead_self_data_worker as W
import homestead_shared as SHARED
from homestead_storage_journal import Held
from test_self_data_coordinator import Cluster


def refused(*_args, **_kwargs):
    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


class TransientApiErrorTests(unittest.TestCase):
    """C3: an API blip while Homestead is stopped is not a durable hold."""

    def setUp(self):
        self.c = Cluster()
        self.now = 1000
        self.runner = W.Runner(self.c.read, self.c.send, lambda _: self.c.logs_text, self.c.admit,
                               namespace="lab", deployment="homestead", operation=self.c.handle["operation"],
                               anchor_uid=self.c.handle["uid"], worker_uid="coordinator-uid", clock=lambda: self.now)

    def state(self):
        return self.c.fresh().load(**self.c.handle).state

    def test_unreachable_api_is_retried_not_latched(self):
        read = self.runner.read
        self.runner.read = refused
        view = self.runner.tick()
        self.assertNotEqual("held", view["status"])
        self.assertIsNone(self.runner.hold)
        self.runner.read = read
        self.assertEqual("quiesce", self.runner.tick()["phase"])
        self.assertNotEqual("held", self.state()["runtime"]["state"])

    def test_lost_heartbeat_reply_is_retried_from_the_durable_record(self):
        c = self.c
        c.lost = lambda m, p, b: p == c.anchor.path and json.loads(b["data"]["state.json"]).get("runtime", {}).get("state") == "running"
        self.assertNotEqual("held", self.runner.tick()["status"])
        c.lost = None
        stops = sum(p == c.dep_path for _, p, _ in c.sent)
        self.assertEqual("quiesce", self.runner.tick()["phase"])
        self.assertEqual(stops, sum(p == c.dep_path for _, p, _ in c.sent))
        self.assertEqual("running", self.state()["runtime"]["state"])

    def test_outage_longer_than_the_bound_holds_with_its_reason(self):
        self.runner.read = refused
        self.runner.tick()
        self.now += W.OUTAGE_LIMIT - 1
        self.assertIsNone(self.runner.hold)
        self.runner.tick()
        self.now += 1
        self.runner.tick()
        self.assertIn("did not answer", self.runner.hold)
        self.runner.read = self.c.read
        self.assertEqual("held", self.runner.tick()["status"])
        self.assertIn("did not answer", self.state()["runtime"]["message"])

    def test_uncertain_workload_write_still_holds_at_once(self):
        c = self.c
        c.lost = lambda m, p, b: p == c.dep_path
        self.assertEqual("held", self.runner.tick()["status"])
        c.lost = None
        self.assertEqual("held", self.runner.tick()["status"])
        self.assertEqual(1, sum(p == c.dep_path for _, p, _ in c.sent))

    def test_http_server_errors_are_transient_but_refusals_are_not(self):
        def error(code):
            raise urllib.error.HTTPError("/x", code, "x", {}, None)
        for code, transient in ((503, True), (429, True), (500, True), (404, False), (409, False), (403, False)):
            try:
                try: error(code)
                except Exception: raise Held("wrapped") from None
            except Held as held:
                self.assertEqual(transient, W.transient(held), code)
        self.assertFalse(W.transient(Held("A data volume has an unreviewed consumer")))
        self.assertFalse(W.transient(ValueError("bad")))

    def test_the_move_client_marks_an_unanswered_request_and_nothing_else(self):
        from test_self_data_kube import ClientTests
        case = ClientTests("test_response_from_another_origin_is_never_trusted"); case.setUp()
        self.addCleanup(case.doCleanups)
        for failure, transient in ((TimeoutError("private endpoint"), True),
                                   (urllib.error.URLError(ConnectionResetError()), True),
                                   (ValueError("private JSON"), False)):
            case.open.side_effect = failure
            with self.assertRaises(Held) as caught: case.client.read("/api/v1/nodes")
            self.assertEqual(transient, W.transient(caught.exception), failure)
            self.assertNotIn("private", str(caught.exception))


class HelperCleanupTests(unittest.TestCase):
    """C8: a helper DELETE that never reached the API is sent again, not a hold."""

    def setUp(self):
        from test_self_data_finish import CleanupTests
        self.fixture = CleanupTests("test_replaced_helper_is_retained"); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c = self.fixture.c

    def test_delete_that_never_took_effect_is_sent_again_pinned_to_its_uid(self):
        start = len(self.c.sent)
        self.c.reject = lambda method, path, body: method == "DELETE"
        with self.assertRaises(TimeoutError): self.fixture.finish()
        first = [(p, b["preconditions"]) for m, p, b in self.c.sent[start:] if m == "DELETE"]
        self.c.reject = None
        self.assertTrue(self.fixture.finish()["done"])
        again = [(p, b["preconditions"]) for m, p, b in self.c.sent[start:] if m == "DELETE" and p == first[0][0]]
        self.assertEqual([first[0], first[0]], again)
        self.assertNotIn(self.fixture.anchor.path, self.c.objects)

    def test_a_delete_already_under_way_is_waited_for_not_repeated(self):
        self.c.reject = lambda method, path, body: method == "DELETE"
        with self.assertRaises(TimeoutError): self.fixture.finish()
        self.c.reject = None
        path = [p for m, p, _ in self.c.sent if m == "DELETE"][-1]
        self.c.objects[path]["metadata"]["deletionTimestamp"] = "1970-01-01T00:16:40Z"
        count = len(self.c.sent)
        self.assertFalse(self.fixture.finish()["done"])
        self.assertEqual(count, len(self.c.sent))

    def test_the_cleanup_loop_retries_an_api_error_a_bounded_number_of_times(self):
        import server
        calls = []
        def finish(*_):
            calls.append(1)
            if len(calls) < 3: raise urllib.error.URLError("refused")
            return {"done": True}
        with mock.patch.object(server, "_self_data_fence", object()), \
             mock.patch.object(server.SELF_DATA_FINISH, "finish", side_effect=finish), \
             mock.patch.object(server.SELF_DATA_EXECUTE, "reconcile_completed") as reconcile, \
             mock.patch.object(server.time, "sleep") as sleep:
            server.finish_self_data_helpers()
        self.assertEqual(3, len(calls)); reconcile.assert_called_once()
        calls.clear()
        with mock.patch.object(server, "_self_data_fence", object()), \
             mock.patch.object(server.SELF_DATA_FINISH, "finish", side_effect=urllib.error.URLError("down")) as down, \
             mock.patch.object(server.time, "sleep"):
            server.finish_self_data_helpers()
        self.assertEqual(server.SELF_DATA_FINISH.ATTEMPTS, down.call_count)
        # Failures count only in a row: a pass that makes progress resets them.
        answers = [urllib.error.URLError("x")] * (server.SELF_DATA_FINISH.ATTEMPTS - 1) + [{"done": False}]
        answers = answers * 2 + [{"done": True}]
        def flaky(*_):
            answer = answers.pop(0)
            if isinstance(answer, Exception): raise answer
            return answer
        with mock.patch.object(server, "_self_data_fence", object()), \
             mock.patch.object(server.SELF_DATA_FINISH, "finish", side_effect=flaky), \
             mock.patch.object(server.SELF_DATA_EXECUTE, "reconcile_completed") as reconcile, \
             mock.patch.object(server.time, "sleep"):
            server.finish_self_data_helpers()
        self.assertEqual([], answers); reconcile.assert_called_once()


class HeldMoveRecoveryTests(unittest.TestCase):
    """C1, C2: a hold in phase prepare or start has a reviewed way out."""

    def setUp(self):
        self.c = Cluster()
        self.now = 1000

    def anchor(self):
        return self.c.fresh().load(**self.c.handle)

    def hold(self, message="Copy admission preflight expired before Homestead stopped; obtain a fresh review"):
        self.anchor().report("coordinator-uid", self.now, "held", message)

    def confirm(self, action):
        logs = lambda _: self.c.logs_text
        review = R.review(self.anchor(), self.c.read, logs, action, clock=lambda: self.now)
        return R.confirm(self.anchor(), self.c.read, logs, action, review["fingerprint"], clock=lambda: self.now)

    def runner(self):
        return W.Runner(self.c.read, self.c.send, lambda _: self.c.logs_text, self.c.admit,
                        namespace="lab", deployment="homestead", operation=self.c.handle["operation"],
                        anchor_uid=self.c.handle["uid"], worker_uid="coordinator-uid", clock=lambda: self.now)

    def writes(self):
        return [e["step"] for e in self.anchor().item()["ref"]["storage_writes"]]

    def test_hold_before_the_stop_returns_to_the_original_volume_without_any_write(self):
        self.hold()
        result = self.confirm("return-original")
        self.assertEqual("prepare", result["phase"])
        with tempfile.TemporaryDirectory() as directory:
            SHARED.write_json(os.path.join(directory, F.MARKER), A.pointer("lab", self.anchor().state, self.c.handle["uid"]), durable=True)
            pod = self.c.objects["/api/v1/namespaces/lab/pods/old-0"]
            pod["metadata"]["ownerReferences"][0]["name"] = "hs-rs"
            pod["spec"]["containers"] = [{"name": "homestead", "volumeMounts": [{"name": "data", "mountPath": directory}]}]
            fence = F.Fence(self.c.read, "lab", "homestead", "old-0", "homestead", directory)
            self.assertFalse(fence.inspect()["writable"])  # still read-only until the worker confirms
            sent = len(self.c.sent)
            view = self.runner().tick()
            self.assertEqual("cancelled", view["status"])
            self.assertEqual("done", self.anchor().state["phase"])
            self.assertEqual([], self.writes())
            self.assertFalse(any(p == self.c.dep_path for _, p, _ in self.c.sent[sent:]))
            self.assertEqual(2, self.c.objects[self.c.dep_path]["spec"]["replicas"])
            self.assertTrue(fence.inspect()["writable"])
            self.assertTrue(FIN.finish(fence, self.c.read, self.c.send)["done"])
            self.assertIsNone(F.read_marker(directory))
            for name in ("source", "target"):
                self.assertIn("/api/v1/namespaces/lab/persistentvolumeclaims/" + name, self.c.objects)

    def test_hold_before_the_stop_can_resume_through_every_check(self):
        self.hold("Current placement or capacity needs review before the data handoff can continue")
        self.confirm("resume")
        self.assertEqual("quiesce", self.runner().tick()["phase"])
        self.assertEqual(["stop"], self.writes())

    def test_untouched_return_is_refused_when_homestead_changed(self):
        self.hold()
        self.c.objects[self.c.dep_path]["spec"]["replicas"] = 3
        with self.assertRaises(Held): self.confirm("return-original")
        self.assertEqual("held", self.anchor().state["runtime"]["state"])

    def stopped_then_held(self):
        c = self.c
        c.reject = lambda m, p, b: p == c.anchor.path and json.loads(b["data"]["state.json"])["phase"] == "quiesce"
        with self.assertRaises(Held): c.step()
        c.reject = None
        self.assertEqual(("prepare", ["stop"]), (self.anchor().state["phase"], self.writes()))
        self.hold("The data handoff control write was not verified; inspect it before continuing. Nothing was retried")
        c.settle_stop()

    def test_hold_after_the_stop_resumes_without_a_second_stop(self):
        self.stopped_then_held()
        self.confirm("resume")
        self.assertEqual("quiesce", self.runner().tick()["phase"])
        self.assertEqual(1, sum(p == self.c.dep_path for _, p, _ in self.c.sent))

    def test_hold_after_the_stop_can_restart_homestead_on_its_original_volume(self):
        self.stopped_then_held()
        self.confirm("return-original")
        runner = self.runner()
        runner.tick()
        self.assertEqual(["stop", "recover-start"], self.writes())
        self.c.settle_start()
        for i in range(2):
            self.c.objects[f"/api/v1/namespaces/lab/pods/new-{i}"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "source"
        self.assertEqual("cancelled", runner.tick()["status"])

    def started_then_held(self, accepted_start):
        self.c.switching(); self.c.step(); self.c.settle_stop()
        self.assertEqual("start", self.anchor().state["phase"])
        if accepted_start:
            self.c.step()
            self.assertIn("start", self.writes())
        self.hold("Current placement or capacity needs review before the data handoff can continue")

    def test_hold_at_start_resumes_forward_and_never_back(self):
        for accepted_start in (False, True):
            with self.subTest(accepted_start=accepted_start):
                self.c = Cluster(); self.started_then_held(accepted_start)
                with self.assertRaisesRegex(Held, "Cutover"): self.confirm("return-original")
                self.confirm("resume")
                runner = self.runner()
                runner.tick()
                self.assertEqual(1, self.writes().count("start"))
                self.c.settle_start()
                self.assertEqual("done", runner.tick()["status"])
                self.assertEqual("target", self.c.objects[self.c.dep_path]["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_resume_at_start_needs_a_verified_copy_receipt(self):
        self.started_then_held(False)
        anchor = self.anchor(); state = anchor.state
        state["copy_receipt"] = {"state": "intent", "worker_uid": "coordinator-uid"}
        self.c.objects[anchor.path]["data"]["state.json"] = A._validate(state, "lab")
        with self.assertRaisesRegex(Held, "verified copy"): self.confirm("resume")

    def test_resume_at_start_is_refused_while_a_stranger_mounts_a_volume(self):
        self.started_then_held(False)
        from test_self_data_coordinator import obj
        self.c.put("/api/v1/namespaces/lab/pods/stranger", obj("Pod", "stranger", {"volumes": [{"persistentVolumeClaim": {"claimName": "target"}}]}))
        with self.assertRaises(Held): self.confirm("resume")

    def test_slow_start_says_so_without_holding(self):
        self.c.switching(); self.c.step(); self.c.settle_stop()
        runner = self.runner()
        runner.tick()  # the start request
        self.assertNotIn("has not become ready", runner.tick()["message"])
        lease = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/node1"
        self.now += W.READY_WARN
        self.c.objects[lease]["spec"]["renewTime"] = datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        view = runner.tick()
        self.assertEqual("running", view["status"])
        self.assertIn("has not become ready", view["message"])
        self.assertIsNone(runner.hold)


class FailedPreparationTests(unittest.TestCase):
    """C6: a failed preparation can be closed once what it made is gone."""

    def setUp(self):
        import homestead_capacity_review as SIGN
        import homestead_kube as K
        import homestead_operations as OPS
        import homestead_self_data_prepare as P
        from fakes.job_store import put
        from fakes.kube import Kube
        self.K, self.OPS, self.P = K, OPS, P
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(OPS, "DATA_DIR", directory.name), mock.patch.object(OPS, "WRITE_GUARD", None),
                      mock.patch.object(SIGN, "_key", lambda: b"close-fixture")):
            patch.start(); self.addCleanup(patch.stop)
        op = "d" * 24
        self.ref = {"operation": op, "namespace": "lab", "destination": "homestead-data-" + op, "storage_class": "local",
                    "access_mode": "ReadWriteOnce", "size": "1Gi", "node": "node-a", "image": "example.com/homestead@sha256:" + "e" * 64,
                    "source": {"name": "homestead-data", "uid": "source-uid", "shape": "x"},
                    "deployment": {"name": "homestead", "uid": "dep-uid", "shape": "y"},
                    "storage_writes": [{"step": "destination", "method": "POST", "state": "uncertain",
                                        "target": {"path": "/api/v1/namespaces/lab/persistentvolumeclaims/homestead-data-" + op}}],
                    "retain_resources": True}
        self.job = {"id": "prep-job", "kind": P.KIND, "title": "Prepare Homestead data volume", "status": "failed", "progress": 15,
                    "href": "/settings", "message": "Destination preparation could not verify its next step", "ref": self.ref}
        put(OPS, [self.job])
        pvc, pod = P.manifests(self.ref)
        self.kube = self.enterContext(Kube(pvc, pod))
        self.pvc, self.pod = self.kube.path_of(pvc), self.kube.path_of(pod)

    def close(self, body=None, *, start=False):
        return self.P.close_recovery(body or {"id": "prep-job"}, "admin", "lab", "homestead", self.OPS,
                                     lambda: ("homestead-data", True), start=start, read=self.K.kget)

    def remove(self, path):
        self.kube("DELETE", path)  # the fake itself: write guards other tests install are not under test here

    def test_failed_preparation_blocks_until_its_claim_and_pod_are_gone(self):
        self.assertEqual(["prep-job"], [j["id"] for j in self.P.blocking_jobs(self.OPS)])
        self.assertTrue(self.P.blocking_jobs(self.OPS)[0]["closable"])
        with self.assertRaisesRegex(Held, "still exists"): self.close()
        self.remove(self.pvc)
        with self.assertRaisesRegex(Held, "still exists"): self.close()
        self.remove(self.pod)
        before = self.OPS.stored()
        review = self.close()
        self.assertEqual(before, self.OPS.stored(), "the review writes nothing")
        self.assertIn(("Removed", self.ref["destination"]), [tuple(f) for f in review["facts"]])
        writes = len(self.kube.writes)
        self.close({"id": "prep-job", "capacity_token": review["capacity_token"], "confirm_close": True}, start=True)
        self.assertEqual(writes, len(self.kube.writes), "closing writes nothing to the cluster")
        self.assertEqual([], self.P.blocking_jobs(self.OPS))
        saved = self.OPS.stored()[0]
        self.assertFalse(saved["ref"]["retain_resources"])
        self.assertEqual("failed", saved["status"])
        self.assertIn("Recovery closed", saved["history"][-1]["m"])

    def test_a_same_name_object_that_is_not_this_preparations_is_left_alone(self):
        # The name was taken by someone else (the preparation then failed):
        # without this preparation's label it neither blocks nor is called ours.
        for path in (self.pvc, self.pod):
            obj = self.kube.object(path); self.remove(path)
            obj["metadata"]["labels"] = {"app": "someone-else"}
            obj["metadata"].pop("uid", None); obj["metadata"].pop("resourceVersion", None)
            self.kube.add(obj)
        review = self.close()
        self.assertIn("not this preparation's", review["detail"])
        self.assertNotIn("remove", review["detail"].lower())
        self.close({"id": "prep-job", "capacity_token": review["capacity_token"], "confirm_close": True}, start=True)
        self.assertEqual([], self.P.blocking_jobs(self.OPS))
        self.assertIsNotNone(self.kube.object(self.pvc), "the other claim is untouched")

    def test_a_claim_made_again_after_review_refuses_the_close(self):
        self.remove(self.pvc); self.remove(self.pod)
        review = self.close()
        pvc, _ = self.P.manifests(self.ref)
        self.kube.add(pvc)
        with self.assertRaisesRegex(Held, "still exists"):
            self.close({"id": "prep-job", "capacity_token": review["capacity_token"], "confirm_close": True}, start=True)
        self.assertEqual(1, len(self.P.blocking_jobs(self.OPS)))

    def test_an_unreadable_cluster_refuses_the_close(self):
        self.remove(self.pvc); self.remove(self.pod)
        self.kube.fail(self.pvc, 503, times=5)
        with self.assertRaises(Held): self.close()

    def test_a_running_or_succeeded_preparation_is_not_closable(self):
        from fakes.job_store import put
        for status in ("running", "succeeded"):
            put(self.OPS, [{**self.job, "status": status}])
            self.assertFalse(self.P.can_close_recovery(self.OPS.stored()[0]))


class SetupWithoutControlRecordTests(unittest.TestCase):
    """C7: a move whose setup never recorded its control record is bounded."""

    def setUp(self):
        import homestead_operations as OPS
        import homestead_self_data_execute as E
        import test_self_data_execute as execute_fixture
        self.OPS, self.E = OPS, E
        self.fixture = execute_fixture.ExecutionTests("test_no_new_service_or_vip_and_no_volume_delete"); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.started = self.stored()["started_at"]

    def stored(self):
        return next(i for i in self.OPS.stored() if i["id"] == self.fixture.job["id"])

    def at(self, seconds):
        import calendar, time
        return lambda: calendar.timegm(time.strptime(self.started, "%Y-%m-%dT%H:%M:%SZ")) + seconds

    def test_setup_that_never_recorded_its_record_fails_after_the_bound(self):
        item = self.stored()
        self.assertEqual("running", self.E.resolve(item, self.fixture.read, clock=self.at(self.E.SETUP_STALE - 1))[0])
        self.E._ACTIVE.add(item["id"])
        try:
            self.assertEqual("running", self.E.resolve(item, self.fixture.read, clock=self.at(self.E.SETUP_STALE + 1))[0])
        finally:
            self.E._ACTIVE.discard(item["id"])
        status, _, _ = self.E.resolve(item, self.fixture.read, clock=self.at(self.E.SETUP_STALE + 1))
        self.assertEqual("failed", status)
        self.assertIn("Setup stopped", item["ref"]["setup_error"])
        self.assertTrue(item["ref"]["retain_resources"], "closing it is a reviewed step")

    def test_a_late_setup_thread_never_creates_a_record_for_a_closed_job(self):
        from fakes.job_store import put
        items = self.OPS.stored()
        next(i for i in items if i["id"] == self.fixture.job["id"])["status"] = "failed"
        put(self.OPS, items)
        sent = len(self.fixture.cluster.sent)
        self.assertIsNone(self.fixture.task().run())
        self.assertFalse(any(m == "POST" for m, _, _ in self.fixture.cluster.sent[sent:]))
        self.assertNotIn(self.fixture.cluster.anchor.path, self.fixture.cluster.objects)

    def close(self, body=None, *, start=False):
        import homestead_self_data_prepare as P
        return P.close_recovery(body or {"id": self.fixture.job["id"]}, "admin", "lab", "homestead", self.OPS,
                                lambda: ("source", True), start=start, read=self.fixture.read)

    def test_failed_setup_without_a_record_can_be_closed_once_no_record_exists(self):
        import homestead_self_data_prepare as P
        self.fixture.cluster.lost = lambda method, path, body: method == "POST" and body.get("kind") == "ConfigMap"
        self.assertIsNone(self.fixture.task().run())
        self.fixture.cluster.lost = None
        item = self.stored()
        self.assertEqual("failed", self.E.resolve(item, self.fixture.read)[0])
        from fakes.job_store import put
        put(self.OPS, [{**item, "status": "failed"}])
        self.assertTrue(P.blocking_jobs(self.OPS)[0]["closable"])
        with self.assertRaisesRegex(Held, "control record still exists"): self.close()
        del self.fixture.cluster.objects[self.fixture.cluster.anchor.path]
        review = self.close()
        self.close({"id": item["id"], "capacity_token": review["capacity_token"], "confirm_close": True}, start=True)
        self.assertEqual([], P.blocking_jobs(self.OPS))


class CompletionWithoutAnchorUidTests(unittest.TestCase):
    """C7: an abandoned or finished move whose job never saved its control
    record's uid is still matched by its operation once cleanup proves it."""

    def test_completion_matches_a_job_without_its_anchor_uid(self):
        import homestead_operations as OPS
        import homestead_self_data_execute as E
        from fakes.job_store import put
        from test_self_data_finish import CleanupTests
        fixture = CleanupTests("test_replaced_helper_is_retained"); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.finish()
        items = OPS.stored(); items[0]["ref"].pop("anchor_uid"); put(OPS, items)
        self.assertTrue(E.reconcile_completed(OPS, fixture.directory, "lab", "homestead"))
        self.assertEqual("succeeded", OPS.stored()[0]["status"])
        self.assertFalse(OPS.stored()[0]["ref"]["retain_resources"])


class InvolvedHostTests(unittest.TestCase):
    """A reboot of a host the move does not use no longer holds it."""

    def reboot(self, c, name):
        c.objects["/api/v1/nodes/" + name]["status"]["nodeInfo"]["bootID"] = "new-boot"

    def test_reboot_of_an_unrelated_host_does_not_hold(self):
        c = Cluster(extra_nodes=("node-b",))
        self.reboot(c, "node-b")
        c.objects["/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/node-b"]["spec"]["renewTime"] = "1970-01-01T00:00:00Z"
        self.assertEqual("quiesce", c.step()["phase"])
        c.settle_stop(); c.step(); c.step()
        self.assertIn(c.job_path, c.objects)

    def test_reboot_of_a_host_the_move_uses_still_holds(self):
        for where in ("homestead", "volume"):
            with self.subTest(where=where):
                c = Cluster(extra_nodes=("node-b",))
                if where == "homestead":
                    c.objects["/api/v1/namespaces/lab/pods/old-1"]["spec"]["nodeName"] = "node-b"
                else:
                    c.objects["/api/v1/persistentvolumes/old-pv"]["spec"]["nodeAffinity"] = {"required": {"nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-b"]}]}]}}
                    c.anchor = c.fresh().load(**c.handle)  # shape pinned at setup; rebuild the plan's fact
                    from test_self_data_coordinator import fact
                    state = c.anchor.state; state["plan"]["source_pv"] = fact(c.objects["/api/v1/persistentvolumes/old-pv"])
                    state["pointer_receipt"] = A.pointer_digest("lab", state, c.handle["uid"])
                    c.objects[c.anchor.path]["data"]["state.json"] = A._validate(state, "lab")
                self.reboot(c, "node-b")
                with self.assertRaisesRegex(Held, "rebooted"): c.step()
                self.assertEqual(2, c.objects[c.dep_path]["spec"]["replicas"])

    def test_a_host_the_move_uses_that_was_not_reviewed_holds(self):
        c = Cluster()
        c.objects["/api/v1/namespaces/lab/pods/old-1"]["spec"]["nodeName"] = "node-c"
        with self.assertRaisesRegex(Held, "not reviewed"): c.step()


class RecoveryCommandTests(unittest.TestCase):
    """While Homestead is stopped, a hold names the exact recovery command."""

    def test_held_view_names_the_kubectl_command_for_this_worker(self):
        c = Cluster()
        c.fresh().load(**c.handle).report("coordinator-uid", 1000, "held", "A host heartbeat is stale")
        view = W.progress(c.fresh().load(**c.handle), 1000)
        command = "kubectl -n lab exec coordinator -- python3 /srv/homestead_self_data_recovery.py --action resume"
        self.assertIn(command, view["message"])
        self.assertEqual(command, view["recovery_command"])
        self.assertIn("return-original", view["message"])

    def test_after_cutover_it_offers_resume_only(self):
        c = Cluster(); c.switching(); c.step()
        c.fresh().load(**c.handle).report("coordinator-uid", 1000, "held", "Startup needs review")
        view = W.progress(c.fresh().load(**c.handle), 1000)
        self.assertIn("--action resume", view["message"])
        self.assertNotIn("return-original", view["message"])

    def test_a_running_move_has_no_command(self):
        c = Cluster(); c.step()
        self.assertNotIn("recovery_command", W.progress(c.fresh().load(**c.handle), 1000))


if __name__ == "__main__":
    unittest.main()
