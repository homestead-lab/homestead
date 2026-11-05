import copy, json, sys, time, unittest, urllib.error
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_longhorn as LH
import homestead_longhorn_target as BACKUP_TARGET
import homestead_revert as REVERT

API = REVERT.API


class FakeReclass:
    def __init__(self, cluster):
        self.cluster = cluster
        self.started = []

    def consumers(self, ns, claim):
        return [{"kind": "Deployment", "name": "paperless", "replicas": 1, "running": True}]

    def _using_pods(self, ns, claim, helpers=True):
        return [{"metadata": {"name": "paperless-abc"}}] if self.cluster.pod_running else []

    def _start(self, ns, ref):
        self.started.append([c["name"] for c in ref["consumers"]])

    def _started(self, ns, ref):
        return []


class Crash(BaseException):
    """The pod dies mid-step: nothing after this point is saved."""


class RevertTests(unittest.TestCase):
    """Snapshots could be taken and deleted but never rolled back to."""

    def setUp(self):
        self.state, self.pod_running, self.sent, self.calls = "attached", True, [], []
        self.objects = {
            f"{API}/volumes/pvc-1": {"status": {"kubernetesStatus": {"namespace": "lab", "pvcName": "paperless-data"}}},
            f"{API}/snapshots/snap-a": {"spec": {"volume": "pvc-1"}, "status": {"creationTime": "2026-09-24T02:00:00Z",
                                                                                  "readyToUse": True}},
            f"{API}/replicas": {"items": [{"spec": {"volumeName": "pvc-1", "nodeID": "node2"},
                                           "status": {"currentState": "stopped"}}]},
        }
        self.reclass = FakeReclass(self)
        self.failing = {}       # Longhorn API action -> the error it raises
        REVERT.bind(self.get, self.send, self.reclass, lambda ns, name: name == "homestead", self.lh_call)
        reach = mock.patch.object(REVERT.ACCESS, "reachable", return_value="")
        self.reachable = reach.start()
        self.addCleanup(reach.stop)
        pause = mock.patch.object(REVERT, "RETRY_AFTER", 0)
        pause.start()
        self.addCleanup(pause.stop)
        self.made_anyway = False    # a snapshotCreate that fails after Longhorn made it
        self.saved = []             # what the job store durably holds, at each checkpoint
        keep = mock.patch.object(REVERT.OPS, "checkpoint", lambda item: self.saved.append(copy.deepcopy(item)))
        keep.start()
        self.addCleanup(keep.stop)

    def lh_call(self, method, path, body=None):
        self.calls.append((path, body))
        action = path.split("action=")[1] if "action=" in path else ""
        if action == "snapshotCreate" and (action not in self.failing or self.made_anyway):
            self.objects[f"{API}/snapshots/{body['name']}"] = {"spec": {"volume": "pvc-1"}}
        if action in self.failing:
            raise self.failing[action]
        return {}

    def head_after(self, parent, known=()):
        """Longhorn's engine shows the volume head on top of this snapshot,
        in a chain that lists these snapshots too."""
        chain = {name: {"parent": ""} for name in known}
        chain["volume-head"] = {"parent": parent}
        self.objects[f"{API}/engines?labelSelector=longhornvolume%3Dpvc-1"] = {"items": [
            {"status": {"snapshots": chain}}]}

    def actions(self):
        return [path.split("action=")[1] for path, _ in self.calls]

    def _stopped_and_detached(self):
        item = {"ref": {"volume": "pvc-1", "snapshot": "snap-a", "namespace": "lab", "claim": "paperless-data",
                        "consumers": REVERT.plan("pvc-1", "snap-a")["consumers"], "phase": "stopping",
                        "since": time.time()}}
        REVERT.resolve(item)
        self.pod_running, self.state = False, "detached"
        return item

    def get(self, path):
        if path == f"{API}/volumes/pvc-1":
            self.objects[path]["status"]["state"] = self.state
        if path in self.objects:
            return self.objects[path]
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))

    def test_the_plan_names_what_stops(self):
        plan = REVERT.plan("pvc-1", "snap-a")
        self.assertTrue(plan["ready"])
        self.assertEqual(("lab", "paperless-data", ["paperless"]),
                         (plan["namespace"], plan["claim"], [c["name"] for c in plan["consumers"]]))

    def test_it_stops_waits_attaches_keeps_the_present_reverts_and_starts_again(self):
        item = {"ref": {"volume": "pvc-1", "snapshot": "snap-a", "namespace": "lab", "claim": "paperless-data",
                        "consumers": REVERT.plan("pvc-1", "snap-a")["consumers"], "phase": "stopping",
                        "since": time.time()}}
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status)
        self.assertIn(("PATCH", "/apis/apps/v1/namespaces/lab/deployments/paperless", {"spec": {"replicas": 0}}),
                      self.sent)
        self.assertEqual([], self.calls, "nothing is attached while the app still has it")
        self.pod_running, self.state = False, "detached"
        REVERT.resolve(item)
        self.assertEqual("/volumes/pvc-1?action=attach", self.calls[0][0])
        self.assertTrue(self.calls[0][1]["disableFrontend"])
        self.assertEqual("node2", self.calls[0][1]["hostId"])
        self.state = "attached"
        REVERT.resolve(item)
        actions = [path.split("action=")[1] for path, _ in self.calls]
        self.assertEqual(["attach", "snapshotCreate", "snapshotRevert", "detach"], actions)
        self.assertEqual("snap-a", self.calls[2][1]["name"])
        self.state = "detached"
        REVERT.resolve(item)
        self.assertEqual([["paperless"]], self.reclass.started)
        status, _, message = REVERT.resolve(item)
        self.assertEqual("succeeded", status)
        self.assertIn(item["ref"]["kept"], message)

    def test_a_volume_that_will_not_detach_is_put_back_untouched(self):
        item = {"ref": {"volume": "pvc-1", "snapshot": "snap-a", "namespace": "lab", "claim": "paperless-data",
                        "consumers": REVERT.plan("pvc-1", "snap-a")["consumers"], "phase": "stopping",
                        "since": time.time() - 3600}}
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertIn("Nothing was changed", message)
        self.assertEqual([["paperless"]], self.reclass.started)
        self.assertEqual([], self.calls)

    def test_an_unreachable_api_refuses_before_anything_stops(self):
        self.reachable.return_value = "Connection refused"
        self.pod_running = False
        ops = mock.Mock()
        with self.assertRaisesRegex(ValueError, "Nothing was changed"):
            REVERT.start("pvc-1", "snap-a", ops)
        ops.start.assert_not_called()
        self.assertEqual([], [s for s in self.sent if s[0] != "GET"])
        self.assertEqual([], self.calls)

    def test_a_connection_failure_on_attach_is_tried_three_times_then_puts_the_app_back(self):
        self.failing["attach"] = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        item = self._stopped_and_detached()
        self.assertEqual("running", REVERT.resolve(item)[0])
        self.assertEqual("running", REVERT.resolve(item)[0])
        self.assertEqual([], self.reclass.started)
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertIn("Connection refused", message)
        self.assertIn("Nothing was changed", message)
        self.assertEqual([["paperless"]], self.reclass.started)
        self.assertEqual(3, self.actions().count("attach"))
        self.assertNotIn("snapshotCreate", self.actions())
        self.assertNotIn("detach", self.actions(), "the API is not answering: let go through Kubernetes")
        self.assertIn(("PATCH", f"{API}/volumeattachments/pvc-1",
                       {"spec": {"attachmentTickets": {"homestead-revert": None}}}), self.sent)

    def test_a_brief_blip_on_attach_is_ridden_out(self):
        self.failing["attach"] = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        item = self._stopped_and_detached()
        self.assertEqual("running", REVERT.resolve(item)[0])
        del self.failing["attach"]
        REVERT.resolve(item)
        self.state = "attached"
        REVERT.resolve(item)
        self.assertEqual(["attach", "attach", "snapshotCreate", "snapshotRevert", "detach"], self.actions())
        self.assertTrue(item["ref"]["reverted"])

    def test_a_longhorn_refusal_on_attach_is_not_tried_again(self):
        self.failing["attach"] = REVERT.Refused("Longhorn refused it: no")
        item = self._stopped_and_detached()
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertEqual(["attach"], self.actions())
        self.assertIn("Nothing was changed", message)
        self.assertEqual([["paperless"]], self.reclass.started)

    def test_a_volume_with_no_attachment_record_is_not_reported_as_held(self):
        self.failing["attach"] = REVERT.Refused("Longhorn refused it: no")
        item = self._stopped_and_detached()
        def send(method, path, body=None, **kw):
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        REVERT.ksend = send
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertNotIn("could not let go", message)

    def test_a_refused_revert_puts_the_app_back_and_names_the_kept_snapshot(self):
        self.failing["snapshotRevert"] = REVERT.Refused("Longhorn refused it: busy")
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertEqual(["attach", "snapshotCreate", "snapshotRevert", "detach"], self.actions())
        self.assertEqual([["paperless"]], self.reclass.started)
        self.assertIn(item["ref"]["kept"], message)
        self.assertIn("Nothing was rolled back", message)

    def test_a_revert_whose_answer_is_lost_but_went_through_carries_on(self):
        self.failing["snapshotRevert"] = urllib.error.URLError(TimeoutError("timed out"))
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        self.head_after("before-rollback-0")          # not yet
        status, _, message = REVERT.resolve(item)
        self.assertEqual("running", status)
        self.assertEqual("confirming", item["ref"]["phase"])
        self.assertNotIn("detach", self.actions(), "never let go while Longhorn may still be reverting")
        self.assertEqual([], self.reclass.started)
        self.assertEqual("running", REVERT.resolve(item)[0])
        self.head_after("snap-a", known=[item["ref"]["kept"], "snap-a"])
        status, _, _ = REVERT.resolve(item)
        self.assertEqual("running", status)
        self.assertTrue(item["ref"]["reverted"])
        self.assertEqual("detaching", item["ref"]["phase"])
        self.assertEqual(["attach", "snapshotCreate", "snapshotRevert", "detach"], self.actions())

    def test_a_revert_whose_outcome_stays_unknown_keeps_the_app_stopped(self):
        self.failing["snapshotRevert"] = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        REVERT.resolve(item)
        item["ref"]["since"] = time.time() - 3600       # Longhorn's engine never said
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertEqual([], self.reclass.started, "starting the app could meet a half-reverted volume")
        self.assertNotIn("detach", self.actions())
        self.assertFalse([s for s in self.sent if "volumeattachments" in s[1]])
        for words in ("maintenance mode", "homestead-revert", "paperless", item["ref"]["kept"]):
            self.assertIn(words, message)

    def test_a_chain_that_has_not_caught_up_is_not_taken_as_reverted(self):
        """The engine's snapshot list lags: just after the kept snapshot is
        made it can still show the head on the chosen (newest) snapshot."""
        self.failing["snapshotRevert"] = urllib.error.URLError(TimeoutError("timed out"))
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        REVERT.resolve(item)
        self.head_after("snap-a", known=["snap-a"])     # no kept snapshot in it yet
        self.assertEqual("running", REVERT.resolve(item)[0])
        self.assertFalse(item["ref"].get("reverted"))
        item["ref"]["since"] = time.time() - 3600
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertEqual([], self.reclass.started)
        self.assertIn("maintenance mode", message)

    def test_a_revert_whose_answer_is_lost_and_did_not_happen_puts_the_app_back(self):
        self.failing["snapshotRevert"] = urllib.error.URLError(TimeoutError("timed out"))
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        REVERT.resolve(item)
        self.head_after(item["ref"]["kept"], known=[item["ref"]["kept"], "snap-a"])
        item["ref"]["since"] = time.time() - 3600
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertEqual([["paperless"]], self.reclass.started)
        self.assertIn("Nothing was rolled back", message)

    def test_a_snapshot_made_before_its_answer_was_lost_is_kept(self):
        self.failing["snapshotCreate"] = urllib.error.URLError(TimeoutError("timed out"))
        self.made_anyway = True
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        REVERT.resolve(item)
        self.assertEqual(["attach", "snapshotCreate", "snapshotRevert", "detach"], self.actions())
        self.assertTrue(item["ref"]["kept"].startswith("before-rollback-"))

    def test_a_snapshot_that_cannot_be_made_is_tried_three_times(self):
        self.failing["snapshotCreate"] = urllib.error.URLError(TimeoutError("timed out"))
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        self.assertEqual("running", REVERT.resolve(item)[0])
        self.assertEqual("running", REVERT.resolve(item)[0])
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertEqual(3, self.actions().count("snapshotCreate"))
        self.assertEqual(1, len({body["name"] for path, body in self.calls if "snapshotCreate" in path}),
                         "one name, so a retry can't leave two")
        self.assertNotIn("snapshotRevert", self.actions())
        self.assertIn("Nothing was changed", message)
        self.assertEqual([["paperless"]], self.reclass.started)

    def test_put_back_still_starts_the_app_when_detach_fails(self):
        self.failing["snapshotCreate"] = REVERT.Refused("Longhorn refused it: busy")
        self.failing["detach"] = urllib.error.URLError("timed out")
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertEqual([["paperless"]], self.reclass.started)
        self.assertIn(("PATCH", f"{API}/volumeattachments/pvc-1",
                       {"spec": {"attachmentTickets": {"homestead-revert": None}}}), self.sent)
        self.assertIn("Nothing was changed", message)

    def test_put_back_names_the_ticket_when_it_cannot_be_released(self):
        self.failing["snapshotCreate"] = REVERT.Refused("Longhorn refused it: busy")
        self.failing["detach"] = urllib.error.URLError("timed out")
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        sent = self.send
        def send(method, path, body=None, **kw):
            if "volumeattachments" in path:
                raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
            return sent(method, path, body, **kw)
        REVERT.ksend = send
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertEqual([["paperless"]], self.reclass.started)
        self.assertIn("homestead-revert", message)

    def test_put_back_finishes_and_says_so_when_the_app_cannot_be_started(self):
        self.failing["snapshotCreate"] = REVERT.Refused("Longhorn refused it: busy")
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        def start(ns, ref):
            raise urllib.error.HTTPError("/apis/apps", 409, "conflict", {}, None)
        self.reclass._start = start
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertIn("detach", self.actions())
        self.assertIn("could not start", message)
        self.assertIn("paperless", message)

    def test_a_failed_detach_after_the_revert_does_not_revert_again(self):
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        self.failing["detach"] = urllib.error.URLError("timed out")
        sent = self.send
        def send(method, path, body=None, **kw):
            if "volumeattachments" in path:
                raise urllib.error.URLError("timed out")
            return sent(method, path, body, **kw)
        REVERT.ksend = send
        status, _, _ = REVERT.resolve(item)
        self.assertEqual("running", status)
        self.assertEqual("detaching", item["ref"]["phase"])
        self.assertTrue(item["ref"]["attached"])
        del self.failing["detach"]
        REVERT.ksend = sent
        REVERT.resolve(item)
        self.assertEqual(["attach", "snapshotCreate", "snapshotRevert", "detach", "detach"], self.actions())
        self.assertFalse(item["ref"]["attached"])
        self.assertEqual([], self.reclass.started, "nothing starts while the volume is still held for the rollback")

    def test_a_detach_that_keeps_failing_is_tried_three_times_then_starts_things_and_says_so(self):
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        self.failing["detach"] = urllib.error.URLError("timed out")
        def send(method, path, body=None, **kw):
            raise urllib.error.URLError("timed out")
        REVERT.ksend = send
        REVERT.resolve(item)                            # reverted; the first detach fails
        self.assertEqual("running", REVERT.resolve(item)[0])
        self.state = "detached"
        REVERT.resolve(item)
        self.assertEqual(3, self.actions().count("detach"))
        self.assertEqual([["paperless"]], self.reclass.started)
        status, _, message = REVERT.resolve(item)
        self.assertEqual("succeeded", status)
        self.assertIn("homestead-revert", message)

    def _to_the_revert(self):
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.state = "attached"
        return item

    def test_a_server_error_on_the_revert_is_confirmed_not_assumed(self):
        """A 5xx does not prove nothing happened: the engine can time out
        while the replicas carry on reverting."""
        self.failing["snapshotRevert"] = REVERT.ServerError("Longhorn failed: context deadline exceeded")
        item = self._to_the_revert()
        status, _, _ = REVERT.resolve(item)
        self.assertEqual("running", status)
        self.assertEqual("confirming", item["ref"]["phase"])
        self.assertEqual([], self.reclass.started)

    def test_a_connection_refused_before_the_revert_was_sent_puts_the_app_back(self):
        self.failing["snapshotRevert"] = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        item = self._to_the_revert()
        status, _, message = REVERT.resolve(item)
        self.assertEqual("failed", status)
        self.assertIn("Nothing was rolled back", message)
        self.assertEqual([["paperless"]], self.reclass.started)

    def test_http_answers_are_told_apart(self):
        for code, kind in ((400, REVERT.Refused), (409, REVERT.Refused), (500, REVERT.ServerError), (503, REVERT.ServerError)):
            error = urllib.error.HTTPError("http://x", code, "x", {}, None)
            error.read = lambda: b"busy"
            with self.subTest(code=code), mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(kind):
                    REVERT._backend("POST", "/volumes/pvc-1?action=snapshotRevert", {})

    def test_cancel_is_refused_once_the_revert_may_be_running(self):
        self.failing["snapshotRevert"] = urllib.error.URLError(TimeoutError("timed out"))
        item = self._to_the_revert()
        REVERT.resolve(item)
        self.assertEqual("confirming", item["ref"]["phase"])
        plan = REVERT.cancel_plan(item)
        self.assertFalse(plan["can"])
        self.assertIn("being confirmed", plan["why_not"])
        with self.assertRaisesRegex(ValueError, "being confirmed"):
            REVERT.cancel_run(item, {})
        self.assertNotIn("detach", self.actions())
        self.assertFalse([s for s in self.sent if "volumeattachments" in s[1]])
        self.assertEqual([], self.reclass.started)

    def test_cancel_before_the_revert_still_puts_things_back(self):
        item = self._stopped_and_detached()
        REVERT.resolve(item)
        self.assertTrue(REVERT.cancel_plan(item).get("mode") == "rollback")
        REVERT.cancel_run(item, {})
        self.assertEqual([["paperless"]], self.reclass.started)

    def test_the_revert_is_recorded_before_it_is_sent(self):
        item = self._to_the_revert()
        sent = []
        def lh_call(method, path, body=None):
            if "snapshotRevert" in path:
                sent.append(copy.deepcopy(self.saved[-1]["ref"]))
            return self.lh_call(method, path, body)
        REVERT.lh_call = lh_call
        REVERT.resolve(item)
        self.assertTrue(sent[0].get("revert_sent"), "a new leader must know the revert may have gone out")
        self.assertEqual(item["ref"]["kept"], sent[0].get("kept"))

    def test_a_restart_while_the_revert_was_out_confirms_and_never_sends_it_again(self):
        item = self._to_the_revert()
        def crash(method, path, body=None):
            if "snapshotRevert" in path:
                raise Crash()
            return self.lh_call(method, path, body)
        REVERT.lh_call = crash
        with self.assertRaises(Crash):
            REVERT.resolve(item)
        resumed = {"ref": copy.deepcopy(self.saved[-1]["ref"])}    # the next leader reads the store
        REVERT.lh_call = self.lh_call
        self.calls.clear()
        status, _, _ = REVERT.resolve(resumed)
        self.assertEqual("running", status)
        self.assertEqual("confirming", resumed["ref"]["phase"])
        self.assertEqual([], self.calls, "no new snapshot, no second revert")
        self.head_after("snap-a", known=[resumed["ref"]["kept"], "snap-a"])
        REVERT.resolve(resumed)
        self.assertTrue(resumed["ref"]["reverted"])

    def test_a_resumed_revert_is_confirmed_even_when_late_and_not_attached(self):
        """The step began long ago and Longhorn reports something other than
        attached: still, a revert that may have gone out is only confirmed."""
        item = self._to_the_revert()
        def crash(method, path, body=None):
            if "snapshotRevert" in path:
                raise Crash()
            return self.lh_call(method, path, body)
        REVERT.lh_call = crash
        with self.assertRaises(Crash):
            REVERT.resolve(item)
        resumed = {"ref": copy.deepcopy(self.saved[-1]["ref"])}
        resumed["ref"]["since"] = time.time() - 3600
        self.state = "attaching"
        REVERT.lh_call = self.lh_call
        self.calls.clear()
        status, _, _ = REVERT.resolve(resumed)
        self.assertEqual("running", status)
        self.assertEqual("confirming", resumed["ref"]["phase"])
        self.assertEqual([], self.calls)
        self.assertFalse([s for s in self.sent if "volumeattachments" in s[1]])
        self.assertEqual([], self.reclass.started)

    def test_a_restart_while_the_snapshot_was_out_keeps_the_one_made(self):
        item = self._to_the_revert()
        def crash(method, path, body=None):
            if "snapshotCreate" in path:
                self.objects[f"{API}/snapshots/{body['name']}"] = {"spec": {"volume": "pvc-1"}}
                raise Crash()
            return self.lh_call(method, path, body)
        REVERT.lh_call = crash
        with self.assertRaises(Crash):
            REVERT.resolve(item)
        resumed = {"ref": copy.deepcopy(self.saved[-1]["ref"])}
        REVERT.lh_call = self.lh_call
        self.calls.clear()
        REVERT.resolve(resumed)
        self.assertEqual(["snapshotRevert", "detach"], self.actions(), "the snapshot Longhorn made is the kept one")
        self.assertEqual(self.saved[0]["ref"]["keep"], resumed["ref"]["kept"])

    def test_a_restart_before_the_snapshot_was_made_makes_it_under_the_same_name(self):
        item = self._to_the_revert()
        def crash(method, path, body=None):
            if "snapshotCreate" in path:
                raise Crash()
            return self.lh_call(method, path, body)
        REVERT.lh_call = crash
        with self.assertRaises(Crash):
            REVERT.resolve(item)
        resumed = {"ref": copy.deepcopy(self.saved[-1]["ref"])}
        name = resumed["ref"]["keep"]
        REVERT.lh_call = self.lh_call
        self.calls.clear()
        REVERT.resolve(resumed)
        self.assertEqual(["snapshotCreate", "snapshotRevert", "detach"], self.actions())
        self.assertEqual(name, self.calls[0][1]["name"])

    def test_a_detach_longhorn_refuses_is_not_tried_again(self):
        item = self._to_the_revert()
        self.failing["detach"] = REVERT.Refused("Longhorn refused it: no")
        def send(method, path, body=None, **kw):
            raise urllib.error.URLError("timed out")
        REVERT.ksend = send
        REVERT.resolve(item)
        self.state = "detached"
        REVERT.resolve(item)
        self.assertEqual(1, self.actions().count("detach"))
        self.assertEqual([["paperless"]], self.reclass.started)

    def test_homesteads_own_data_is_refused(self):
        self.reclass.consumers = lambda ns, claim: [{"kind": "Deployment", "name": "homestead", "replicas": 1}]
        self.pod_running = False
        with self.assertRaisesRegex(ValueError, "Homestead's own data"):
            REVERT.start("pvc-1", "snap-a", None)


class ProtectionHealthTests(unittest.TestCase):
    """A failing backup job said so only on its own page."""

    def test_failed_jobs_and_an_unreachable_target_are_issues(self):
        import server
        jobs = [{"name": "daily-backup", "task": "backup", "covers": 3, "last_failed": True},
                {"name": "weekly-trim", "task": "filesystem-trim", "covers": 3, "last_failed": True},
                {"name": "daily-snapshot", "task": "snapshot", "covers": 0, "last_failed": True}]
        issues = server.OVERVIEW_ROUTES.protection_issues(jobs, {"configured": True, "available": False, "url": "nfs://nas:/b",
                                                 "reason": "connection refused"})
        self.assertEqual(["daily-backup", "backup target"], [i["name"] for i in issues])
        self.assertIn("connection refused", issues[1]["reason"])
        self.assertIn("no backup target", server.OVERVIEW_ROUTES.protection_issues(jobs[:1], {"configured": False})[1]["reason"])
        result = server.CLUSTER.classify_cluster_health([], [], [], protection=issues)
        self.assertEqual("degraded", result["health"])


class HarvesterTargetTests(unittest.TestCase):
    """On Harvester the target is Harvester's setting, or Harvester resets it."""

    def setUp(self):
        self.sent = []
        self.setting = {"metadata": {"name": "backup-target"}, "value": ""}

        def get(path):
            if path == BACKUP_TARGET.HARVESTER_TARGET:
                return self.setting
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        saved = (LH.kget, LH.ksend, LH._cache, LH.STORAGE_CLASS, LH.DEFAULT_NS)
        self.addCleanup(lambda: LH.bind(*saved))   # put back what server.py bound
        LH.bind(get, lambda m, p, b=None, **k: self.sent.append((m, p, b)), {})

    def test_nfs_and_s3_are_written_to_harvesters_setting(self):
        BACKUP_TARGET.set_backup_target("nfs://192.0.2.177:/mnt/user/backups", poll="5m")
        method, path, body = self.sent[-1]
        self.assertEqual(("PUT", BACKUP_TARGET.HARVESTER_TARGET), (method, path))
        self.assertEqual({"type": "nfs", "endpoint": "nfs://192.0.2.177:/mnt/user/backups",
                          "refreshIntervalInSeconds": 300}, json.loads(body["value"]))
        BACKUP_TARGET.set_backup_target("s3://homelab@us-east-1", keys={"access_key": "AK", "secret_key": "SK",
                                                             "endpoint": "http://192.0.2.20:9000"})
        value = json.loads(self.sent[-1][2]["value"])
        self.assertEqual(("s3", "homelab", "us-east-1", "http://192.0.2.20:9000", "AK"),
                         (value["type"], value["bucketName"], value["bucketRegion"], value["endpoint"], value["accessKeyId"]))
        self.assertFalse(any("backuptargets" in p for _, p, _ in self.sent), "Longhorn's own is left to Harvester")

    def test_keys_are_kept_when_none_are_typed_and_a_path_is_refused(self):
        self.setting["value"] = json.dumps({"type": "s3", "accessKeyId": "AK", "secretAccessKey": "SK", "endpoint": "http://x:9000"})
        BACKUP_TARGET.set_backup_target("s3://homelab@us-east-1", secret="whatever")
        value = json.loads(self.sent[-1][2]["value"])
        self.assertEqual(("AK", "SK", "http://x:9000"), (value["accessKeyId"], value["secretAccessKey"], value["endpoint"]))
        with self.assertRaisesRegex(ValueError, "leave any path off"):
            BACKUP_TARGET.set_backup_target("s3://homelab@us-east-1/longhorn", keys={"access_key": "A", "secret_key": "S"})

    def test_harvester_validation_explains_the_failure_without_disclosing_keys(self):
        import io
        from unittest import mock
        for payload in ({"message": 'connection refused; access=AKID secret=secret"key old=OLDKEY'},
                        {"message": json.dumps({"secretAccessKey": 'secret"key'})}):
            self.setting["value"] = json.dumps({"secretAccessKey": "OLDKEY"})
            error = urllib.error.HTTPError("target", 422, "Unprocessable Entity", {},
                                           io.BytesIO(json.dumps(payload).encode()))
            with mock.patch.object(LH, "ksend", side_effect=error):
                with self.assertRaises(ValueError) as caught:
                    BACKUP_TARGET.set_backup_target("s3://homelab@us-east-1", keys={
                        "access_key": "AKID", "secret_key": 'secret"key', "endpoint": "http://store:9070"})
            message = str(caught.exception)
            self.assertIn("Harvester refused the backup target (HTTP 422)", message)
            self.assertNotIn("AKID", message)
            self.assertNotIn("OLDKEY", message)
            self.assertNotIn("secret\\\"key", message)
            self.assertNotIn('secret"key', message)
            if "connection refused" in payload["message"]:
                self.assertIn("connection refused", message)

    def test_a_non_json_harvester_error_keeps_the_http_reason(self):
        import io
        from unittest import mock
        error = urllib.error.HTTPError("target", 503, "Service Unavailable", {}, io.BytesIO(b"<html>unavailable</html>"))
        with mock.patch.object(LH, "ksend", side_effect=error):
            with self.assertRaisesRegex(ValueError, "Harvester refused.*HTTP 503.*Service Unavailable"):
                BACKUP_TARGET.set_backup_target("nfs://nas:/backups")


    def test_a_secret_named_alone_gives_its_keys_to_harvesters_setting(self):
        # Backup storage for a move names the Secret it wrote; Harvester's
        # setting has nowhere to put a name, so the keys are read from it.
        import base64
        enc = lambda v: base64.b64encode(v.encode()).decode()
        secret = {"data": {"AWS_ACCESS_KEY_ID": enc("homestead"), "AWS_SECRET_ACCESS_KEY": enc("s3cret"),
                           "AWS_ENDPOINTS": enc("http://192.0.2.20:9010")}}

        def get(path):
            if path == BACKUP_TARGET.HARVESTER_TARGET:
                return self.setting
            if path.endswith("/secrets/homestead-backup-credentials"):
                return secret
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        LH.bind(get, lambda m, p, b=None, **k: self.sent.append((m, p, b)), {})
        BACKUP_TARGET.set_backup_target("s3://homestead-backups@us-east-1/", secret="homestead-backup-credentials")
        value = json.loads(self.sent[-1][2]["value"])
        self.assertEqual(("homestead", "s3cret", "http://192.0.2.20:9010", "homestead-backups"),
                         (value["accessKeyId"], value["secretAccessKey"], value["endpoint"], value["bucketName"]))


if __name__ == "__main__":
    unittest.main()
