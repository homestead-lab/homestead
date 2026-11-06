"""The restart review names what would keep the host from draining.

Before anything is cordoned, the review lists what homestead_drain_blockers
finds on the host - orphan records, engines that run elsewhere, a pod stuck
terminating, an attachment left behind, a list it couldn't read - and a
person acknowledges each one ("drain anyway") before the normal cordon and
drain start. A forced action sends no drain, so it skips them, and the check
after the drain does not compare them: what a person accepted is not asked
again, and a new one found then does not stop power.

Reads go through the real kget over the shared fake API server
(tests/fakes/kube.py), with its field selectors and list answers."""
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import homestead_drain_blockers as BLOCKERS
import homestead_kube as K
import homestead_power as power
from fakes.kube import Kube
from test_drain_blockers import attachment, claim, engine, engine_orphan, instance_manager, pod

ERROR = "acknowledge each drain blocker (or remove the orphan records) before host power control"


def host(name, uid):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "uid": uid},
            "status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"bootID": f"boot-{name}"}}}


def orphans_here():
    """Today's case: three engine orphans on node-a whose engines run on node-b."""
    volumes = ["vol-a", "vol-b", "vol-c"]
    return ([engine_orphan(volume, n) for n, volume in enumerate(volumes, 1)]
            + [engine(volume, "node-b") for volume in volumes]
            + [instance_manager("node-a", engines=[f"{v}-e-0" for v in volumes])])


def stuck_pod():
    """A finished pod still terminating an hour past its grace period, its volume attached here."""
    return [pod(deleting_at=time.time() - 3600), claim(), attachment()]


class DrainBlockerReviewTests(unittest.TestCase):
    def setUp(self):
        self.kube = Kube(host("node-a", "uid-a"), host("node-b", "uid-b"), *orphans_here())
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        power.bind(K.kget, lambda node: {"workloads": [], "stranded": []},
                   lambda: {"members": ["node-a", "node-b", "node-c"], "can_lose": 1}, lambda: True)

    def post(self, **body):
        """The route's answer: (code, body), with the job's start stubbed out."""
        plan = power.plan("node-a", "reboot", force=body.get("force") is True)
        request = {"node": "node-a", "action": "reboot", "confirm": "node-a",
                   "review_token": plan["review_token"], **body}
        with mock.patch.object(server.POWER_JOBS, "active_power_job", return_value=None), \
                mock.patch.object(server.POWER_JOBS, "send_reviewed_power", return_value={"ok": True}) as send:
            reply = server.POWER_JOBS._send_power(types.SimpleNamespace(body=request))
        return reply.code, reply.body, send

    def test_review_lists_drain_blockers_and_the_token_follows_them(self):
        plan = power.plan("node-a", "reboot")
        self.assertEqual(["engine-orphans"], [row["kind"] for row in plan["drain_blockers"]])
        self.assertEqual("engine-orphans:node-a", plan["drain_blockers"][0]["id"])
        self.assertTrue(plan["drain_blockers"][0]["fix"])
        self.assertTrue(plan["requires_blocker_ack"])
        # Acknowledged, not blocking: they never enter the stops Force overrides.
        self.assertTrue(plan["ready"], plan["blockers"])
        self.assertEqual([], plan["overridable"])
        for obj in stuck_pod():
            self.kube.add(obj)
        later = power.plan("node-a", "reboot")
        self.assertEqual(["engine-orphans", "stuck-pod"], sorted(row["kind"] for row in later["drain_blockers"]))
        self.assertNotEqual(plan["review_token"], later["review_token"], "a new blocker is a new review")
        # The same stuck pod a few minutes on is the same review: the time it
        # has been stuck is said, not compared.
        with mock.patch.object(BLOCKERS.time, "time", return_value=time.time() + 600):
            self.assertEqual(later["review_token"], power.plan("node-a", "reboot")["review_token"])

    def test_power_is_refused_until_each_blocker_is_acknowledged(self):
        for obj in stuck_pod():
            self.kube.add(obj)
        ids = [row["id"] for row in power.plan("node-a", "reboot")["drain_blockers"]]
        self.assertEqual(2, len(ids))
        for given in (None, [], ids[:1], ids + ["stuck-pod:apps/other"], "yes"):
            body = {} if given is None else {"allow_drain_blockers": given}
            code, answer, send = self.post(**body)
            self.assertEqual((409, ERROR), (code, answer.get("error")), given)
            self.assertIn("drain_blockers", answer["plan"], "the browser shows the review again")
            send.assert_not_called()
        code, _, send = self.post(allow_drain_blockers=list(reversed(ids)))
        self.assertEqual(202, code)
        # Drain anyway is the normal cordon and drain, not Force.
        plan, forced = send.call_args.args
        self.assertFalse(forced)
        self.assertTrue(send.call_args.kwargs["background"])
        self.assertEqual(sorted(ids), sorted(row["id"] for row in plan["drain_blockers"]))

    def test_force_ignores_drain_blockers(self):
        forced = power.plan("node-a", "reboot", force=True)
        self.assertEqual([], forced["drain_blockers"])
        self.assertFalse(forced["requires_blocker_ack"])
        code, _, send = self.post(force=True)
        self.assertEqual(202, code)
        self.assertTrue(send.call_args.args[1], "a forced action sends no drain")

    def test_recheck_after_drain_does_not_compare_drain_blockers(self):
        # Reviewed with nothing in the way; a stuck pod and new orphan records
        # turn up while it drains. What the drain itself got past does not
        # stop power (the spec's call 2).
        for obj in orphans_here():
            if obj["kind"] in ("Orphan", "InstanceManager"):
                self.kube.discard(self.kube.path_of(obj))
        original = power.plan("node-a", "reboot")
        self.assertEqual([], original["drain_blockers"])
        for obj in stuck_pod() + [engine_orphan("vol-a", 1), engine_orphan("vol-d", 4)]:
            self.kube.add(obj)
        self.assertEqual([], power.plan("node-a", "reboot", after_drain=True)["drain_blockers"])
        power.recheck_after_drain(original, clock=lambda: 0, sleep=lambda _: self.fail("no wait"))

    def test_a_check_that_fails_is_said_and_still_needs_its_acknowledgement(self):
        with mock.patch.object(BLOCKERS, "for_host", side_effect=KeyError("status")):
            plan = power.plan("node-a", "reboot")
        self.assertEqual(["unread"], [row["kind"] for row in plan["drain_blockers"]])
        self.assertTrue(plan["requires_blocker_ack"])
        self.assertIn("couldn't check", plan["drain_blockers"][0]["what"])

    def test_the_reads_go_through_the_reviews_own_reader(self):
        seen = []
        power.bind(lambda path, **k: seen.append(path) or K.kget(path, **k), power.impact, power.quorum,
                   power.power_enabled)
        power.plan("node-a", "reboot")
        self.assertIn(BLOCKERS.ORPHANS, seen)
        self.assertTrue(any(p.startswith("/api/v1/pods?") and "node-a" in p for p in seen), seen)


if __name__ == "__main__":
    unittest.main()
