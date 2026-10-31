"""A cross-cluster move whose errors do not clear ends, and starts its source again.

The source app is stopped on the other cluster while a move runs. A conflict,
a rate limit, the API server struggling or any other error on this side used
to say "Waiting: ..." every few seconds for ever, so the app stayed stopped.
Every error now counts towards the limit; at the limit the move fails with the
last error and the source runs again at the replica count it had. On the shared
fake API (tests/fakes/kube.py); both clusters are the one fake, the source in
namespace lab and the destination in namespace moved."""
import io
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_move as client
import homestead_move_actions as ACT
import homestead_move_engine as engine
import homestead_move_phases as PHASE
import homestead_move_source as source
import homestead_move_state as STATE
import homestead_operations as OPS
from fakes.kube import Kube

SOURCE = "/apis/apps/v1/namespaces/lab/deployments/frigate"
HERE = "/apis/apps/v1/namespaces/moved/deployments/frigate"
LIMIT = 3


def deployment(namespace, replicas, ready=0):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": namespace, "name": "frigate"},
            "spec": {"replicas": replicas}, "status": {"readyReplicas": ready}}


def http_error(code, message):
    return urllib.error.HTTPError("x", code, "error", {}, io.BytesIO(
        ('{"message": "%s"}' % message).encode()))


class MoveGivesUpTests(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube(deployment("lab", 3), deployment("moved", 0, ready=3)))
        data = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(OPS, "DATA_DIR", data))
        self.enterContext(mock.patch.object(engine, "after_finish", None))
        self.enterContext(mock.patch.object(STATE, "MAX_TRANSIENT", LIMIT))
        self.releases = 0
        self.source_down = False
        # What the source's network page lists: its Services and their addresses.
        self.source_addresses = ["192.0.2.240"]
        self.network_down = False

        def remote(name, path, body=None):
            route, _, query = path.partition("?")
            if route == "/api/move/source-status":
                if self.source_down:
                    raise client.Unreachable("could not reach shed")
                ask = dict(urllib.parse.parse_qsl(query))
                return source.in_namespace(ask["namespace"], source.status, ask["kind"], ask["name"])
            if path == "/api/move/source" and body["action"] == "release":
                self.releases += 1
                if self.source_down:
                    raise client.Unreachable("could not reach shed")
                return source.in_namespace(body["namespace"], source.release, body["kind"], body["name"])
            if path == "/api/network" and body is None:
                if self.network_down:
                    raise client.Unreachable("could not reach shed")
                return {"services": [{"namespace": "lab", "name": "frigate", "type": "LoadBalancer",
                                      "external_ips": list(self.source_addresses)}]}
            raise AssertionError(path)

        self.enterContext(mock.patch.object(client, "remote", remote))
        source.bind(K.kget, K.ksend, None, "lab")
        # Activity: a retry opens a fresh entry there.
        activity = types.SimpleNamespace(start=lambda *args, **kwargs: {"id": "op-2"})
        engine.bind(K.kget, K.ksend, None, client, None, activity, data, "moved")
        source.in_namespace("lab", source.quiesce, "container", "frigate")
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"], "stopped for the move")

    def start(self, phase, flags=None, **fields):
        move = {**fields, "id": "0123456789ab", "status": "running", "phase": phase, "progress": 80,
                "cluster": "shed", "kind": "container", "name": "frigate", "namespace": "moved",
                "source_namespace": "lab", "claims": [], "origin": {"replicas": 3},
                "op": "op-1", "created_at": STATE._now(),
                "flags": dict({"quiesce_requested": True, "quiesced": True}, **(flags or {}))}
        STATE._store(move)
        return move["id"]

    def failing(self, *errors, phase="creating"):
        """The phase raises each error in turn, the last one for ever. An HTTP
        error's body reads once, so those are made afresh: a function makes one."""
        errors = list(errors)

        def handler(move):
            error = errors.pop(0) if len(errors) > 1 else errors[0]
            raise error() if callable(error) else error
        return mock.patch.dict(PHASE.HANDLERS, {phase: handler})

    def tick(self, times):
        for _ in range(times):
            PHASE.tick_all()
        return STATE._find("0123456789ab")

    def assert_gave_up_and_source_runs(self, said):
        move = self.tick(LIMIT)
        self.assertEqual("running", move["status"], "the limit is not reached yet")
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"])
        move = self.tick(1)
        self.assertEqual("failed", move["status"])
        self.assertIn(said, move["message"])
        self.assertIn("frigate runs again on shed", move["message"])
        self.assertEqual(3, self.kube.object(SOURCE)["spec"]["replicas"], "back at its earlier replica count")
        self.assertEqual(1, self.releases)
        self.tick(3)
        self.assertEqual(1, self.releases, "a failed move is not ticked again")

    def test_a_rate_limit_that_never_clears_fails_the_move_and_starts_the_source(self):
        self.start("creating")
        with self.failing(lambda: http_error(429, "too many requests")):
            self.assert_gave_up_and_source_runs("HTTP 429: too many requests")

    def test_a_struggling_api_server_fails_the_move_and_starts_the_source(self):
        self.start("creating")
        with self.failing(lambda: http_error(503, "etcd leader changed")):
            self.assert_gave_up_and_source_runs("HTTP 503: etcd leader changed")

    def test_any_other_lasting_error_fails_the_move_and_starts_the_source(self):
        self.start("creating")
        with self.failing(RuntimeError("socket closed")):
            self.assert_gave_up_and_source_runs("socket closed")

    def test_an_unreachable_source_waited_out_too_long_starts_it_again_when_it_answers(self):
        self.start("creating")
        with self.failing(client.Unreachable("could not reach shed")):
            self.assert_gave_up_and_source_runs("could not reach shed")

    def test_the_count_is_of_errors_in_a_row_whatever_their_kind(self):
        self.start("creating")
        with self.failing(http_error(409, "changed"), http_error(429, "slow down"),
                          RuntimeError("socket closed"), client.Unreachable("could not reach shed")):
            move = self.tick(LIMIT + 1)
        self.assertEqual("failed", move["status"])
        self.assertIn("could not reach shed", move["message"], "the last error is the one shown")

    def test_a_source_that_cannot_be_started_again_is_said_so_and_cancel_still_puts_it_back(self):
        self.start("creating")
        self.source_down = True
        with self.failing(RuntimeError("socket closed")):
            move = self.tick(LIMIT + 1)
        self.assertEqual("failed", move["status"])
        self.assertIn("frigate may still be stopped on shed", move["message"])
        self.assertIn("Cancel", move["message"])
        self.assertTrue(STATE._source_held(move), "Cancel is still the way to put it back")
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"])

    def test_a_move_given_up_with_its_source_running_again_is_not_retried_from_old_backups(self):
        self.start("creating")
        with self.failing(RuntimeError("socket closed")):
            move = self.tick(LIMIT + 1)
        with self.assertRaises(ValueError) as caught:
            ACT.retry(move["id"])
        self.assertIn("may run again on shed", str(caught.exception))
        ACT.abandon(move["id"])
        self.assertEqual("cancelled", STATE._find(move["id"])["status"])

    def assert_gave_up_and_source_stays_stopped(self, move):
        self.assertEqual("failed", move["status"])
        self.assertIn("frigate may already be running here, so it stays stopped on shed", move["message"])
        self.assertIn("Cancel", move["message"])
        self.assertEqual(0, self.releases, "never two copies, one on each cluster, both serving and writing")
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"])
        self.assertTrue(STATE._source_held(move), "Cancel is still the way to put it back")

    def test_once_started_here_the_source_is_not_started_too(self):
        self.start("starting", {"start_requested": time.time(), "started": time.time()})
        with self.failing(RuntimeError("socket closed"), phase="starting"):
            self.assert_gave_up_and_source_stays_stopped(self.tick(LIMIT + 1))

    def test_a_start_whose_reply_was_lost_counts_as_started(self):
        # The start is applied here, but every reply is lost: it looks unanswered.
        self.start("starting")
        self.kube.add(deployment("moved", 0, ready=0))
        self.kube.fail(HERE, 503, times=100, method="PATCH", applied=True)
        move = self.tick(LIMIT + 1)
        self.assertEqual(3, self.kube.object(HERE)["spec"]["replicas"], "the start did happen")
        self.assertNotIn("started", move["flags"], "no reply ever came")
        self.assert_gave_up_and_source_stays_stopped(move)

    def test_a_start_asked_before_a_retry_still_keeps_the_source_stopped(self):
        self.start("starting", {"start_requested": time.time() - STATE.START_GRACE - 60,
                                "started": time.time() - STATE.START_GRACE - 60})
        self.kube.add(deployment("moved", 3, ready=0))
        self.assertEqual("failed", self.tick(1)["status"], "not ready after the grace")
        ACT.retry("0123456789ab")
        with self.failing(RuntimeError("socket closed"), phase="starting"):
            move = self.tick(LIMIT + 1)
        self.assertIn("start_requested", move["flags"], "a retry never forgets a start was asked for")
        self.assert_gave_up_and_source_stays_stopped(move)

    def test_a_give_up_in_the_starting_phase_keeps_the_source_stopped_even_with_no_start_recorded(self):
        # A move stored by an older release, part-way through starting, has no start_requested.
        self.start("starting")
        with self.failing(RuntimeError("socket closed"), phase="starting"):
            self.assert_gave_up_and_source_stays_stopped(self.tick(LIMIT + 1))

    def service_here(self, address="192.0.2.240", kind="LoadBalancer", ours=True, assigned=False):
        """A Service in the destination; ours carries this move's id, as _creating stamps it.
        The address is asked for (annotation) or, once kube-vip has it, assigned (status)."""
        annotations = {"homestead.io/move-id": "0123456789ab"} if ours else {}
        status = {}
        if assigned:
            status = {"loadBalancer": {"ingress": [{"ip": address}]}}
        else:
            annotations["kube-vip.io/loadbalancerIPs"] = address
        self.kube.add({"apiVersion": "v1", "kind": "Service",
                       "metadata": {"namespace": "moved", "name": "frigate", "annotations": annotations},
                       "spec": {"type": kind, "ports": [{"port": 5000}]}, "status": status})

    def give_up_in_creating(self, mode="shared", **service):
        move_id = self.start("creating")
        stored = STATE._find(move_id)
        stored["address_mode"] = mode
        STATE._store(stored)
        if service is not None:
            self.service_here(**service)
        with self.failing(RuntimeError("socket closed")):
            return self.tick(LIMIT + 1)

    def assert_address_clash_keeps_the_source_stopped(self, move, said="192.0.2.240"):
        self.assertEqual("failed", move["status"])
        self.assertIn(f"its address may be announced by both clusters ({said})", move["message"])
        self.assertIn("Cancel removes it here", move["message"])
        self.assertEqual(0, self.releases)
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"])

    def assert_source_runs_again(self, move):
        self.assertIn("frigate runs again on shed", move["message"])
        self.assertEqual(3, self.kube.object(SOURCE)["spec"]["replicas"])

    def test_a_specific_address_equal_to_the_sources_keeps_the_source_stopped(self):
        self.assert_address_clash_keeps_the_source_stopped(self.give_up_in_creating("manual"))

    def test_an_address_assigned_here_equal_to_one_of_the_sources_keeps_the_source_stopped(self):
        self.source_addresses = ["192.0.2.10", "192.0.2.240"]
        self.assert_address_clash_keeps_the_source_stopped(self.give_up_in_creating("automatic", assigned=True))

    def test_this_clusters_shared_address_differing_from_the_sources_starts_the_source_again(self):
        self.assert_source_runs_again(self.give_up_in_creating("shared", address="192.0.2.250"))

    def test_an_address_read_that_fails_on_either_side_keeps_the_source_stopped(self):
        self.network_down = True
        move = self.give_up_in_creating("shared", address="192.0.2.250")
        self.assert_address_clash_keeps_the_source_stopped(move, said="the addresses could not be compared")
        self.setUp()
        self.kube.fail("/api/v1/namespaces/moved/services", 500, times=100, method="GET")
        move = self.give_up_in_creating("shared", address="192.0.2.250")
        self.assert_address_clash_keeps_the_source_stopped(move, said="the addresses could not be compared")

    def test_no_load_balancer_of_this_move_here_starts_the_source_again_without_asking_the_source(self):
        self.network_down = True
        for service in ({"kind": "ClusterIP"}, {"ours": False}):
            with self.subTest(service=service):
                self.setUp()
                self.network_down = True
                self.assert_source_runs_again(self.give_up_in_creating("manual", **service))

    def test_a_release_whose_reply_was_lost_still_refuses_retry(self):
        # The source runs again, but this side never hears so.
        self.start("creating")
        self.kube.fail(SOURCE, 503, times=100, method="PATCH", applied=True)
        with self.failing(RuntimeError("socket closed")):
            move = self.tick(LIMIT + 1)
        self.assertEqual("failed", move["status"])
        self.assertEqual(3, self.kube.object(SOURCE)["spec"]["replicas"], "the release did happen")
        self.assertNotIn("released_on_give_up", move["flags"])
        self.assertIn("release_requested", move["flags"])
        with self.assertRaises(ValueError) as caught:
            ACT.retry(move["id"])
        self.assertIn("may run again on shed", str(caught.exception))

    def test_a_source_running_again_by_hand_is_never_joined_by_a_start_here(self):
        self.start("starting")
        self.kube.add(deployment("moved", 0, ready=0))
        # Someone puts it back on the source by hand: its move mark is gone and it runs.
        source.in_namespace("lab", source.release, "container", "frigate")
        move = self.tick(1)
        self.assertEqual("failed", move["status"])
        self.assertIn("frigate is not stopped for this move on shed", move["message"])
        self.assertEqual(0, self.kube.object(HERE)["spec"]["replicas"], "no start was sent here")

    def test_a_source_whose_state_cannot_be_read_is_never_joined_by_a_start_here(self):
        self.start("starting")
        self.kube.add(deployment("moved", 0, ready=0))
        self.source_down = True
        move = self.tick(LIMIT + 1)
        self.assertEqual("failed", move["status"])
        self.assertEqual(0, self.kube.object(HERE)["spec"]["replicas"], "no start was sent here")
        self.assert_gave_up_and_source_stays_stopped(move)

    def made_here(self, pod_owned=True):
        """The started copy here, stamped by this move, and one pod of it."""
        here = deployment("moved", 3, ready=3)
        here["metadata"]["annotations"] = {"homestead.io/move-id": "0123456789ab"}
        here["spec"]["selector"] = {"matchLabels": {"app": "frigate"}}
        self.kube.add(here)
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "namespace": "moved", "name": "frigate-1", "labels": {"app": "frigate"}}, "status": {"phase": "Running"}}
        if pod_owned:
            uid = self.kube.object(HERE)["metadata"]["uid"]
            pod["metadata"]["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "Deployment",
                                                   "name": "frigate", "uid": uid}]
        self.kube.add(pod)

    def given_up_with_the_copy_here_started(self, pod_owned=True):
        self.start("starting", {"start_requested": time.time(), "started": time.time()})
        self.made_here(pod_owned)
        with self.failing(RuntimeError("socket closed"), phase="starting"):
            move = self.tick(LIMIT + 1)
        self.assert_gave_up_and_source_stays_stopped(move)
        return move

    def test_cancel_after_a_give_up_removes_the_copy_here_before_starting_the_source(self):
        move = self.given_up_with_the_copy_here_started()
        ACT.abandon(move["id"])
        self.assertEqual("cancelled", STATE._find(move["id"])["status"])
        self.assertIsNone(self.kube.object(HERE))
        self.assertEqual(3, self.kube.object(SOURCE)["spec"]["replicas"])
        writes = [(method, path) for method, path, _, _ in self.kube.writes]
        started_there = len(writes) - 1 - writes[::-1].index(("PATCH", SOURCE))  # the first was the stop
        self.assertLess(writes.index(("DELETE", HERE)), started_there,
                        "the copy here is gone before the source starts")

    def test_cancel_waits_for_the_copys_pods_here_to_go_before_starting_the_source(self):
        move = self.given_up_with_the_copy_here_started(pod_owned=False)
        ACT.abandon(move["id"])
        move = STATE._find(move["id"])
        self.assertEqual("failed", move["status"])
        self.assertIn("still running here", move["message"])
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"])
        self.kube.discard("/api/v1/namespaces/moved/pods/frigate-1")
        PHASE.tick_all()  # a failed cancellation is tried again by the engine
        self.assertEqual("cancelled", STATE._find(move["id"])["status"])
        self.assertEqual(3, self.kube.object(SOURCE)["spec"]["replicas"])

    def test_cancel_whose_removal_here_fails_keeps_the_source_stopped(self):
        move = self.given_up_with_the_copy_here_started()
        self.kube.fail(HERE, 500, times=100, method="DELETE")
        with self.assertRaises(Exception):
            ACT.abandon(move["id"])
        move = STATE._find(move["id"])
        self.assertEqual("failed", move["status"])
        self.assertIn("frigate stays stopped on shed until it is gone", move["message"])
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"])
        self.assertTrue(STATE._source_held(move))

    def test_a_replaced_source_is_never_joined_by_a_start_here(self):
        self.start("starting", source_uid="uid-of-the-one-that-was-stopped")
        self.kube.add(deployment("moved", 0, ready=0))
        move = self.tick(1)
        self.assertEqual("failed", move["status"])
        self.assertIn("frigate is not stopped for this move on shed", move["message"])
        self.assertEqual(0, self.kube.object(HERE)["spec"]["replicas"], "no start was sent here")

    def test_a_source_held_by_another_transfer_is_never_joined_by_a_start_here(self):
        held = self.kube.object(SOURCE)
        held["metadata"]["annotations"][source.NAMES.key(source.OWNER)] = "ba9876543210"
        self.kube.add(held)
        self.start("starting")
        self.kube.add(deployment("moved", 0, ready=0))
        move = self.tick(1)
        self.assertEqual("failed", move["status"])
        self.assertEqual(0, self.kube.object(HERE)["spec"]["replicas"], "no start was sent here")

    def test_an_address_not_yet_assigned_here_counts_as_a_clash(self):
        move = self.give_up_in_creating("automatic", address="")
        self.assert_address_clash_keeps_the_source_stopped(move, said="Service frigate has no address yet")

    def test_cancel_finds_the_copys_pods_by_name_when_its_selector_is_unknown(self):
        move = self.given_up_with_the_copy_here_started(pod_owned=False)
        here = self.kube.object(HERE)
        del here["spec"]["selector"]
        self.kube.add(here)
        self.kube.discard("/api/v1/namespaces/moved/pods/frigate-1")
        self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {
            "namespace": "moved", "name": "frigate-7d9c-x2x", "labels": {"run": "it"}}, "status": {"phase": "Running"}})
        ACT.abandon(move["id"])
        move = STATE._find(move["id"])
        self.assertEqual("failed", move["status"])
        self.assertIn("pod frigate-7d9c-x2x is still running here", move["message"])
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"])
        self.kube.discard("/api/v1/namespaces/moved/pods/frigate-7d9c-x2x")
        PHASE.tick_all()
        self.assertEqual("cancelled", STATE._find(move["id"])["status"])

    def test_cancel_in_starting_waits_for_the_copy_even_with_no_start_recorded(self):
        # A move stored by an older release, part-way through starting.
        self.start("starting")
        self.made_here(pod_owned=False)
        with self.failing(RuntimeError("socket closed"), phase="starting"):
            move = self.tick(LIMIT + 1)
        ACT.abandon(move["id"])
        move = STATE._find(move["id"])
        self.assertEqual("failed", move["status"])
        self.assertIn("pod frigate-1 is still running here", move["message"])
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"])

    def test_a_transient_error_then_success_still_lands_the_move(self):
        self.start("starting")
        self.kube.fail(HERE, 503, times=2, method="PATCH")
        move = self.tick(1)
        self.assertEqual("running", move["status"])
        self.assertIn("Waiting", move["message"])
        move = self.tick(LIMIT)
        self.assertEqual("succeeded", move["status"], move["message"])
        self.assertEqual(3, self.kube.object(HERE)["spec"]["replicas"])
        self.assertEqual(0, self.releases, "a landed move leaves its source stopped")
        self.assertEqual(0, self.kube.object(SOURCE)["spec"]["replicas"])

    def test_retry_from_starting_starts_the_app_here_again(self):
        # The start was sent long ago and the app never became ready: failed.
        self.start("starting", {"started": time.time() - STATE.START_GRACE - 60})
        self.kube.add(deployment("moved", 0, ready=0))
        move = self.tick(1)
        self.assertEqual("failed", move["status"])
        self.assertIn("not ready after", move["message"])
        # Someone fixes the app; retry sends the start again and waits afresh.
        self.kube.add(deployment("moved", 0, ready=3))
        ACT.retry(move["id"])
        move = self.tick(1)
        self.assertEqual("succeeded", move["status"], move["message"])
        self.assertEqual(3, self.kube.object(HERE)["spec"]["replicas"], "the start was sent again")
        self.assertIn("start_requested", move["flags"])


if __name__ == "__main__":
    unittest.main()
