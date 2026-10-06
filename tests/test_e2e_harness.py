"""The release suite's harness riding out moments a person would too: a
review that went stale, the data move's progress server still holding the
address, and a Service made just before its answer was lost."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

E2E = Path(__file__).resolve().parent / "e2e"
sys.path.insert(0, str(E2E))
from harness import api as API  # noqa: E402
from harness.api import HomesteadError  # noqa: E402
from scenarios import network as NETWORK  # noqa: E402
from scenarios import power as POWER  # noqa: E402


class ProgressServerTests(unittest.TestCase):
    def test_the_move_progress_server_refusing_a_login_is_asked_again(self):
        client = API.Homestead(["http://192.0.2.10"])
        answers = iter([(405, {"error": API.PROGRESS_ONLY}), (200, {"ok": True})])
        with mock.patch.object(client, "_once", side_effect=lambda *a: next(answers)), mock.patch("time.sleep"):
            self.assertEqual({"ok": True}, client.post("/api/auth/login", {"username": "admin"}))

    def test_any_other_refused_method_still_fails(self):
        client = API.Homestead(["http://192.0.2.10"])
        with mock.patch.object(client, "_once", return_value=(405, {"error": "Method not allowed"})):
            with self.assertRaises(HomesteadError):
                client.post("/api/auth/login", {})


class StaleReviewTests(unittest.TestCase):
    def plan(self, token):
        return {"node": "node-2", "action": "reboot", "review_token": token, "blockers": []}

    def test_a_review_whose_impact_changed_is_reviewed_again_and_sent(self):
        sent = []

        def post(path, body):
            sent.append(body["review_token"])
            if len(sent) == 1:
                raise HomesteadError(409, {"error": POWER.CHANGED}, path)
            return {"operation": {"id": "op-1"}}
        ctx = SimpleNamespace(api=SimpleNamespace(post=post, get=lambda path: self.plan("fresh")))
        with mock.patch("time.sleep"):
            self.assertEqual("op-1", POWER._send(ctx, self.plan("stale")))
        self.assertEqual(["stale", "fresh"], sent)

    def test_other_conflicts_still_fail(self):
        def post(path, body):
            raise HomesteadError(409, {"error": "quorum would be lost"}, path)
        ctx = SimpleNamespace(api=SimpleNamespace(post=post, get=None))
        with self.assertRaises(HomesteadError):
            POWER._send(ctx, self.plan("t"))


class ExposeTests(unittest.TestCase):
    def ctx(self, service):
        def post(path, body):
            raise HomesteadError(400, {"error": "Service lab/e2e-web already exists"}, path)
        return SimpleNamespace(api=SimpleNamespace(post=post), kube=SimpleNamespace(get=lambda *a: service))

    def service(self, workload="e2e-web", vip="192.0.2.120", managed="true"):
        return {"metadata": {"labels": {"homestead.io/managed": managed},
                             "annotations": {"homestead.io/workload": workload, "kube-vip.io/loadbalancerIPs": vip}},
                "spec": {"type": "LoadBalancer"}}

    def test_homesteads_own_service_made_before_its_answer_was_lost_counts(self):
        self.assertEqual({"vip": "192.0.2.120"}, NETWORK._expose(self.ctx(self.service()), "e2e-web", "192.0.2.120", 80, 8080))

    def test_a_service_on_another_address_or_not_homesteads_fails(self):
        for service in (self.service(vip="192.0.2.121"), self.service(managed="false"), self.service(workload="other")):
            with self.assertRaises(AssertionError):
                NETWORK._expose(self.ctx(service), "e2e-web", "192.0.2.120", 80, 8080)


if __name__ == "__main__":
    unittest.main()
