"""Homestead's own data move: a pod that never stops ends in a hold after 10 minutes,
not in a move that says "running" for ever with Homestead at 0 (stuck-jobs C5).

Each test drives the real worker, coordinator and recovery on the shared fake API
(tests/fakes/kube.py, tests/fakes/self_data_move.py) with a clock the test moves."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_self_data_anchor as A
import homestead_self_data_coordinator as C
import homestead_self_data_recovery as R
from homestead_storage_journal import Held
from fakes.kube import Kube
from fakes.self_data_move import Move, NS


class DeadlineTest(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube())
        self.m = Move(self.kube)
        self.run_ = self.m.runner()

    def tick(self):
        return self.run_.tick()

    def load(self):
        return A.Anchor(self.m.read, self.m.send, NS, "homestead").load(**self.m.handle)

    def review(self, action):
        return R.review(self.load(), self.m.read, lambda _: self.m.logs_text, action, clock=lambda: self.m.now)

    def confirm(self, action):
        R.confirm(self.load(), self.m.read, lambda _: self.m.logs_text, action, self.review(action)["fingerprint"], clock=lambda: self.m.now)

    def writes(self):
        return len([w for w in self.kube.writes if w[0] != "GET" and w[1] != self.m.anchor.path])

    def pod_stuck(self):
        """Scaled to zero, but its pod stays Terminating (a hung volume unmount)."""
        dep = self.kube.object(self.m.dep_path)
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 0, "terminatingReplicas": 1}
        self.kube.add(dep)
        rs = self.kube.object(self.m.rs_path)
        rs["spec"]["replicas"] = 0
        rs["status"] = {"observedGeneration": 1, "replicas": 0}
        self.kube.add(rs)
        pod = self.kube.object(self.m.old_pod)
        pod["metadata"]["deletionTimestamp"] = "2020-01-01T00:00:00Z"
        self.kube.add(pod)


class QuiesceTests(DeadlineTest):
    def setUp(self):
        super().setUp()
        self.assertEqual("quiesce", self.tick()["phase"])  # stop sent
        self.pod_stuck()
        self.tick()

    def test_ten_minutes_and_not_a_second_less(self):
        self.assertEqual(600, C.RELEASE_LIMIT)
        self.m.pass_time(599)
        self.assertNotEqual("held", self.tick()["status"])
        self.assertIsNone(self.run_.hold)
        self.m.pass_time(1)
        self.assertEqual("held", self.tick()["status"])

    def test_the_hold_names_the_pod_and_is_durable(self):
        before = self.writes()
        self.m.pass_time(600)
        view = self.tick()
        self.assertIn("homestead-old", view["message"])
        self.assertIn("10 minutes", view["message"])
        self.assertIn("recovery_command", view)
        self.assertEqual("held", self.load().state["runtime"]["state"])
        self.assertIn("homestead-old", self.load().state["runtime"]["message"])
        self.assertEqual(before, self.writes(), "nothing was forced or deleted")
        self.assertIsNotNone(self.kube.object(self.m.old_pod))

    def test_a_new_coordinator_keeps_the_same_clock(self):
        self.m.pass_time(400)
        self.tick()
        self.m.pod_lost(self.m.first_pod)
        pod = self.m.start_pod("node-b")
        replacement = self.m.runner(pod)
        self.assertNotEqual("held", replacement.tick()["status"])
        self.m.pass_time(200)
        self.assertEqual("held", replacement.tick()["status"])

    def test_preview_names_the_stuck_pod_and_writes_nothing(self):
        self.m.pass_time(600)
        self.tick()
        puts = len(self.kube.writes)
        for action in ("resume", "return-original"):
            with self.assertRaisesRegex(Held, "homestead-old"):
                self.review(action)
        self.assertEqual(puts, len(self.kube.writes))

    def stuck_pod_goes(self):
        self.kube.discard(self.m.old_pod)
        dep = self.kube.object(self.m.dep_path)
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 0}
        self.kube.add(dep)

    def test_once_it_is_gone_recovery_offers_going_back_to_the_original_volume(self):
        self.m.pass_time(600)
        self.tick()
        self.stuck_pod_goes()
        self.assertEqual("held", self.tick()["status"], "a hold stays until a person reviews it")
        self.assertEqual("source", self.review("return-original")["source"])
        self.confirm("return-original")
        self.tick()
        self.assertEqual(["stop", "recover-start"], self.m.steps())
        dep = self.kube.object(self.m.dep_path)
        self.assertEqual(1, dep["spec"]["replicas"])
        self.assertEqual("source", dep["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])

    def test_resume_starts_a_new_ten_minutes(self):
        self.m.pass_time(600)
        self.tick()
        self.stuck_pod_goes()
        self.confirm("resume")
        self.assertNotEqual("held", self.tick()["status"])
        self.assertEqual("copy", self.load().state["phase"])
        self.assertNotIn("waits", self.load().state)

    def test_a_failed_pod_left_from_before_the_move_holds_with_its_name(self):
        """The ReplicaSet never removes a Failed pod, so nothing but a person will."""
        pod = self.kube.object(self.m.old_pod)
        pod["metadata"].pop("deletionTimestamp")
        pod["status"]["phase"] = "Failed"
        self.kube.add(pod)
        self.m.pass_time(600)
        self.assertIn("homestead-old", self.tick()["message"])


class NoFalseHoldTests(DeadlineTest):
    def test_a_quick_stop_never_records_a_wait_and_a_long_copy_never_holds(self):
        m = self.m
        self.tick(); m.stopped(); self.tick()
        self.assertNotIn("waits", self.load().state)
        self.tick()
        m.pass_time(3600)
        self.assertNotEqual("held", self.tick()["status"])
        self.assertEqual("copy", self.load().state["phase"])

    def test_a_slow_restart_on_the_new_volume_says_so_and_does_not_hold(self):
        """A hold would keep the new pods unready: this wait is not bounded by a hold."""
        m = self.m
        self.tick(); m.stopped(); self.tick(); self.tick(); m.copy_finished(); self.tick(); self.tick()
        m.copy_released(); self.tick(); self.tick(); m.stopped(); self.tick()
        self.assertIn("start", m.steps())
        self.tick()
        m.pass_time(3600)
        self.assertNotEqual("held", self.tick()["status"])


class ReleaseTests(DeadlineTest):
    def setUp(self):
        super().setUp()
        m = self.m
        self.tick(); m.stopped(); self.tick(); self.tick(); m.copy_finished(); self.tick()
        self.tick()  # the release is sent; the copy pod does not go
        self.assertIn("release-copy", m.steps())

    def test_a_copy_pod_that_never_releases_holds_with_its_name(self):
        self.tick()
        self.m.pass_time(599)
        self.assertNotEqual("held", self.tick()["status"])
        self.m.pass_time(1)
        view = self.tick()
        self.assertEqual("held", view["status"])
        self.assertIn("copy-pod", view["message"])
        self.assertEqual("verify", view["phase"])

    def test_preview_waits_for_the_copy_pod_then_offers_both_ways(self):
        self.tick()
        self.m.pass_time(600)
        self.tick()
        for action in ("resume", "return-original"):
            with self.assertRaisesRegex(Held, "copy"):
                self.review(action)
        self.m.copy_released()
        self.assertEqual("return-original", self.review("return-original")["action"])
        self.confirm("resume")
        self.assertEqual("switch", self.tick()["phase"])

    def test_going_back_after_a_stuck_release_starts_the_original_volume(self):
        self.tick()
        self.m.pass_time(600)
        self.tick()
        self.m.copy_released()
        self.confirm("return-original")
        self.tick()
        self.assertIn("recover-start", self.m.steps())
        self.assertEqual("source", self.kube.object(self.m.dep_path)["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"])


class RecoveryWaitTests(DeadlineTest):
    def test_going_back_does_not_wait_for_ever_either(self):
        self.tick()
        self.pod_stuck()
        self.tick()
        self.m.pass_time(600)
        self.tick()
        self.kube.discard(self.m.old_pod)
        dep = self.kube.object(self.m.dep_path)
        dep["status"] = {"observedGeneration": dep["metadata"]["generation"], "replicas": 0}
        self.kube.add(dep)
        self.confirm("return-original")
        path = self.kube.add({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "homestead-late", "namespace": NS,
            "ownerReferences": [{"kind": "ReplicaSet", "uid": self.m.rs_uid, "name": "hs-rs", "controller": True}]},
            "spec": {}, "status": {"phase": "Failed"}})
        self.tick()
        self.m.pass_time(600)
        view = self.tick()
        self.assertEqual("held", view["status"])
        self.assertIn("homestead-late", view["message"])
        self.kube.discard(path)
        self.confirm("return-original")
        self.tick()
        self.assertIn("recover-start", self.m.steps())


if __name__ == "__main__":
    unittest.main()
