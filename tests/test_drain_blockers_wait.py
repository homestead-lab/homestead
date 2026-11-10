"""How long a drain waits, and what it says when it stops (#1306, part 2).

An instance-manager is held only by the engines it still lists: an orphan
record whose process has gone no longer counts, and when Longhorn removes
engine-instance orphan records itself (orphan-resource-auto-deletion), the
drain waits for it rather than stopping early. Longhorn is read every
ENGINES_EVERY seconds while it waits, not every round. A plain 429 is tried
again after its Retry-After, and a drain that runs out of time on one says
so. A budget's SyncFailed message is cut to a readable length.

Through the same drain and fake API server as test_drain_blockers_drain."""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_drain_blockers as BLOCKERS
import homestead_node_actions as NODE_ACTIONS
import test_drain_blockers as SHAPES
import test_drain_blockers_drain as DRAIN
from fakes.kube import Kube

NODE = DRAIN.NODE


def throttled(path, retry_after=None):
    """The API server's own 429 (API priority and fairness): a Status with
    no budget cause, and a Retry-After header."""
    status = {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
              "message": "Too many requests, please try again later.", "reason": "TooManyRequests",
              "code": 429, "details": {"retryAfterSeconds": retry_after or 1}}
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError(path, 429, "Too Many Requests", headers, io.BytesIO(json.dumps(status).encode()))


def auto_deletion(value):
    return {"apiVersion": "longhorn.io/v1beta2", "kind": "Setting",
            "metadata": {"name": "orphan-resource-auto-deletion", "namespace": "longhorn-system"}, "value": value}


class Drain(unittest.TestCase):
    setUp = DRAIN.DrainNamesItsBlockerTests.setUp
    run_drain = DRAIN.DrainNamesItsBlockerTests.run_drain
    refuse = DRAIN.DrainNamesItsBlockerTests.refuse

    def instance_manager_held(self, *extra, engines=("vol-a-e-0",)):
        im_pod = DRAIN.manager_pod()
        im_budget = DRAIN.budget(im_pod["metadata"]["name"], ns="longhorn-system", expected=1, healthy=1, desired=1)
        objects = [DRAIN.workload(), im_pod, im_budget, SHAPES.instance_manager(NODE, engines=list(engines)),
                   SHAPES.engine("vol-a", "node-b"), SHAPES.engine_orphan("vol-a", 1), *extra]
        return im_pod, im_budget, objects

    def throttle(self, kube, pod, times, retry_after=None):
        asked = []

        def respond(method, path, sent):
            asked.append(self.clock.now)
            if len(asked) <= times:
                raise throttled(path, retry_after)
            return None
        kube.react("POST", DRAIN.eviction(pod), respond)
        return asked


class InstanceManagerTests(Drain):
    def test_an_orphan_record_whose_process_has_gone_does_not_stop_the_drain_early(self):
        # The instance-manager lists no engines; an orphan record for vol-a is
        # still there. Whatever holds it now, it is not that record: the drain
        # waits as it does for any instance-manager, and goes on when it lets go.
        im_pod, im_budget, objects = self.instance_manager_held(engines=())
        with Kube(*objects) as kube:
            asked = self.refuse(kube, im_pod, im_budget, times=30)    # about 60 s
            result = self.run_drain(kube)
        self.assertTrue(result["ok"], result)
        self.assertEqual(31, len(asked))
        self.assertGreater(self.clock.now, BLOCKERS.STALE_GRACE)

    def test_a_listed_engine_with_its_orphan_record_still_stops_the_drain_early(self):
        im_pod, im_budget, objects = self.instance_manager_held()
        with Kube(*objects) as kube:
            self.refuse(kube, im_pod, im_budget, times=10_000)
            with self.assertRaises(ValueError) as caught:
                self.run_drain(kube, power=True)
        self.assertLess(self.clock.now, NODE_ACTIONS.LONGHORN_DRAIN_WAIT)
        self.assertIn("orphan record", str(caught.exception))

    def test_when_longhorn_removes_instance_orphans_itself_the_drain_waits_for_it(self):
        im_pod, im_budget, objects = self.instance_manager_held(auto_deletion("replica-data;instance"))
        with Kube(*objects) as kube:
            asked = self.refuse(kube, im_pod, im_budget, times=30)
            result = self.run_drain(kube)
        self.assertTrue(result["ok"], result)
        self.assertEqual(31, len(asked))

    def test_auto_deletion_of_replica_data_alone_does_not_count(self):
        for value in ("", "replica-data"):
            self.setUp()
            im_pod, im_budget, objects = self.instance_manager_held(auto_deletion(value))
            with self.subTest(value), Kube(*objects) as kube:
                self.refuse(kube, im_pod, im_budget, times=10_000)
                with self.assertRaises(ValueError):
                    self.run_drain(kube, power=True)
                self.assertLess(self.clock.now, NODE_ACTIONS.LONGHORN_DRAIN_WAIT)

    def test_longhorn_is_read_every_ten_seconds_while_the_drain_waits(self):
        im_pod = DRAIN.manager_pod()
        im_budget = DRAIN.budget(im_pod["metadata"]["name"], ns="longhorn-system", expected=1, healthy=1, desired=1)
        reads = []
        with Kube(DRAIN.workload(), im_pod, im_budget, SHAPES.instance_manager(NODE, engines=["vol-a-e-0"]),
                  SHAPES.engine("vol-a", NODE)) as kube:
            kube.react("GET", BLOCKERS.INSTANCE_MANAGERS, lambda method, path, body: reads.append(path))
            self.refuse(kube, im_pod, im_budget, times=30)    # about 60 s
            self.assertTrue(self.run_drain(kube)["ok"])
        # About 60 s of waiting, read every 10 s: about 6 reads, not 30.
        self.assertLessEqual(len(reads), 7, reads)
        self.assertGreaterEqual(len(reads), 5, reads)


class ThrottledTests(Drain):
    def test_a_plain_429_is_tried_again_after_its_retry_after(self):
        pod = DRAIN.workload()
        with Kube(pod) as kube:
            asked = self.throttle(kube, pod, 3, retry_after=10)
            result = self.run_drain(kube)
        self.assertTrue(result["ok"], result)
        self.assertEqual(4, len(asked))
        self.assertTrue(all(later - earlier >= 10 for earlier, later in zip(asked, asked[1:])), asked)

    def test_a_retry_after_past_the_drains_deadline_still_ends_it_on_time(self):
        pod = DRAIN.workload()
        with Kube(pod) as kube:
            self.throttle(kube, pod, 10_000, retry_after=3600)
            with self.assertRaises(ValueError):
                self.run_drain(kube, power=True)
        self.assertLess(self.clock.now, 130)

    def test_a_drain_that_runs_out_of_time_on_throttling_says_so(self):
        pod = DRAIN.workload()
        with Kube(pod) as kube:
            self.throttle(kube, pod, 10_000)
            with self.assertRaises(ValueError) as caught:
                self.run_drain(kube, power=True)
        message = str(caught.exception)
        self.assertIn("The API server asked Homestead to slow down", message)
        self.assertIn("lab/app-a", message)
        self.assertNotIn("Pods have not left the host", message)


class RefusalWordsTests(unittest.TestCase):
    def test_a_long_sync_failed_message_is_cut_short(self):
        pod = DRAIN.workload()
        pdb = DRAIN.sync_failed(DRAIN.budget("keep-one", expected=2, healthy=2, desired=1, allowed=0))
        pdb["status"]["conditions"][0]["message"] = "x" * 2000
        with Kube(pdb):
            said = BLOCKERS.refusal(DRAIN.too_many_requests("eviction", pdb), pod)
        self.assertIn("x" * 200 + "…", said["why"])
        self.assertNotIn("x" * 201, said["why"])

    def test_disrupted_pods_alone_make_a_budget_passing(self):
        # Nothing starting, nothing to process, healthy equal to desired: only
        # disruptedPods says Kubernetes has not caught up with an eviction.
        pod = DRAIN.workload()
        pdb = DRAIN.budget("keep-one", expected=1, healthy=1, desired=1, allowed=0)
        with Kube(pdb):
            self.assertFalse(BLOCKERS.refusal(DRAIN.too_many_requests("eviction", pdb), pod)["passing"])
        pdb["status"]["disruptedPods"] = {"app-z": "2026-11-01T12:00:00Z"}
        with Kube(pdb):
            self.assertTrue(BLOCKERS.refusal(DRAIN.too_many_requests("eviction", pdb), pod)["passing"])


if __name__ == "__main__":
    unittest.main()
