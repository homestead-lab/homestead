"""A host job that stops before power takes off the cordon it put on (#1289).

Restart, Shut down and Maintenance cordon the host, then drain it. When the
job stops before power is sent (or before maintenance opens), the cordon it
put on comes off, so the host takes new apps again and a second attempt
starts clean, instead of reading the job's cordon as a person's. A cordon that
was there before the job's own cordon stays, as does one on a host that was
replaced since, and one the job cannot read or change.

Through the routes and the job's own record, on the fake API server: the real
review, cordon, job store and history file. Only the drain's answer is given
(a refusal, as a disruption budget gives)."""
import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: F401  (binds the job store's resolvers and the modules)
import homestead_history as HISTORY
import homestead_lifecycle as LC
import homestead_node_actions as NODE_ACTIONS
import homestead_operations as OPS
import homestead_os_rollout as ROLLOUT
import homestead_power as POWER
import homestead_power_jobs as JOBS
from fakes.kube import Kube

NODE = "/api/v1/nodes/node-a"
QUORUM = {"members": ["node-a", "node-b", "node-c"], "can_lose": 1, "ready": ["node-a", "node-b", "node-c"], "total": 3,
          "quorum_needs": 2}


def host(name, uid=None, cordoned=False):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "uid": uid or f"uid-{name}"},
            "spec": {"unschedulable": cordoned},
            "status": {"conditions": [{"type": "Ready", "status": "True"}], "nodeInfo": {"bootID": "boot-1"}}}


def refused(*args, **kwargs):
    """The drain's answer when a disruption budget allows no eviction."""
    return {"evicted": [], "skipped": ["kubevirt/virt-api-1 (HTTP 429: the disruption budget allows no disruption)"]}


def req(body=None, **query):
    return types.SimpleNamespace(body=body or {}, query={k: [v] for k, v in query.items()}, path="/api/node/power")


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.kube = self.enterContext(Kube(host("node-a"), host("node-b"), host("node-c"),
                                           groups=("longhorn.io", "kubevirt.io")))
        impact = {"workloads": [], "stranded": []}
        self.before_job = []      # what happens between the review and the job (run_now)
        for target, name, value in (
                (OPS, "DATA_DIR", tmp.name), (HISTORY, "DATA_DIR", tmp.name), (ROLLOUT, "DATA_DIR", tmp.name),
                (POWER, "impact", lambda node: impact), (POWER, "quorum", lambda: QUORUM),
                (POWER, "power_enabled", lambda: True), (LC, "NODE_POWER_ENABLED", True),
                (NODE_ACTIONS, "quorum_report", lambda: QUORUM),
                (JOBS, "POD_NAME", "homestead-a"), (JOBS.LEADER, "is_leader", mock.Mock(return_value=True)),
                (POWER, "SETTLE", 0), (POWER, "WORKER_GONE", lambda pod, restarts=None: False),
                (JOBS, "_power_in_background", self.run_now)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_now(self, operation_id, power_plan, force, resumed=False):
        for step in self.before_job:
            step()
        try:
            JOBS.run_power_job(operation_id, power_plan, force, resumed)
        except (JOBS.PowerNotSent, OPS.Superseded):
            pass

    def send(self, action="reboot", drain=refused):
        review = JOBS._plan(req(node="node-a", action=action))
        body = {"node": "node-a", "confirm": "node-a", "action": action, "choices": {},
                "review_token": review["review_token"], "allow_drain_blockers": [],
                **({"reason": "Drive swap", "hours": 1} if action == "maintenance" else {})}
        with mock.patch.object(NODE_ACTIONS, "drain", drain):
            reply = JOBS._send_power(req(body))
        self.assertEqual(202, reply.code, reply.body)
        return next(i for i in OPS.stored() if i["id"] == reply.body["operation"]["id"])

    def cordoned(self):
        return bool(self.kube.object(NODE)["spec"].get("unschedulable"))

    def cordon_by_hand(self):
        found = self.kube.object(NODE)
        found["spec"]["unschedulable"] = True
        self.kube.add(found)


class OwnCordonTests(Case):
    def test_a_restart_stopped_at_the_drain_takes_off_its_own_cordon(self):
        item = self.send()
        self.assertEqual("failed", item["status"])
        self.assertFalse(self.cordoned(), "the job's own cordon comes off")
        self.assertTrue(item["message"].startswith("Nothing was sent. The host was uncordoned"), item["message"])
        self.assertNotIn("stays cordoned", item["message"])
        self.assertNotIn("still cordoned", item["message"])
        self.assertIn("The drain was refused for kubevirt/virt-api-1", item["message"])
        self.assertTrue(item["message"].endswith("before trying again."), item["message"])
        self.assertTrue(item["ref"]["uncordoned"])
        self.assertFalse(JOBS._plan(req(node="node-a", action="reboot"))["cordoned"],
                         "a second attempt starts clean: the host is not cordoned before it")

    def test_a_shutdown_stopped_at_the_check_after_the_drain_takes_off_its_own_cordon(self):
        with mock.patch.object(POWER, "recheck_after_drain",
                               side_effect=ValueError("The host stays cordoned, and nothing was sent: A volume changed")):
            item = self.send(action="poweroff", drain=lambda *a, **k: {"evicted": [], "skipped": []})
        self.assertFalse(self.cordoned())
        self.assertIn("A volume changed", item["message"])
        self.assertNotIn("stays cordoned", item["message"])

    def test_maintenance_stopped_at_the_drain_takes_off_its_own_cordon(self):
        item = self.send(action="maintenance")
        self.assertEqual("failed", item["status"])
        self.assertIsNone(HISTORY.declared_for("node-a"))
        self.assertFalse(self.cordoned())
        self.assertTrue(item["message"].startswith("Maintenance was not started. The host was uncordoned"), item["message"])
        self.assertNotIn("power", item["message"].lower())
        self.assertNotIn("stays cordoned", item["message"])

    def test_maintenance_that_could_not_be_recorded_keeps_the_cordon(self):
        # Whether the event was written is not known, so the host stays safe.
        with mock.patch.object(HISTORY, "maintenance_declare", side_effect=OSError("disk full")):
            item = self.send(action="maintenance", drain=lambda *a, **k: {"evicted": [], "skipped": []})
        self.assertEqual("failed", item["status"])
        self.assertTrue(self.cordoned())

    def test_a_resumed_job_still_knows_the_cordon_is_its_own(self):
        class Evicted(BaseException):
            """The replica running the job goes mid-drain: nothing is recorded as failed."""

        def evicted(*args, **kwargs):
            raise Evicted()
        with self.assertRaises(Evicted):
            self.send(drain=evicted)
        item = next(i for i in OPS.stored() if i["ref"].get("node") == "node-a")
        self.assertEqual("draining", item["ref"]["phase"])
        self.assertTrue(self.cordoned())
        with mock.patch.object(NODE_ACTIONS, "drain", refused):
            with self.assertRaises(JOBS.PowerNotSent):
                JOBS.run_power_job(item["id"], item["ref"]["plan"], resumed=True)
        self.assertFalse(self.cordoned(), "the cordon found on resuming is the job's own from its first run")


class OtherCordonTests(Case):
    def test_a_host_cordoned_before_the_review_stays_cordoned(self):
        self.cordon_by_hand()
        item = self.send()
        self.assertEqual("failed", item["status"])
        self.assertTrue(self.cordoned())
        self.assertIn("The host stays cordoned, as it was before this job", item["message"])
        self.assertNotIn("uncordoned", item["message"])

    def test_a_cordon_put_on_after_the_review_and_before_the_jobs_own_stays(self):
        self.before_job.append(self.cordon_by_hand)
        item = self.send()
        self.assertTrue(self.cordoned())
        self.assertFalse(item["ref"]["cordoned_before"], "the review saw no cordon")
        self.assertIn("The host stays cordoned, as it was before this job", item["message"])

    def test_a_job_stopped_before_its_cordon_leaves_a_cordon_put_on_since(self):
        self.before_job.append(self.cordon_by_hand)
        with mock.patch.object(NODE_ACTIONS, "node_action_check", return_value=(False, "Quorum changed", QUORUM)):
            item = self.send()
        self.assertEqual("failed", item["status"])
        self.assertTrue(self.cordoned())

    def test_a_host_replaced_during_the_job_keeps_its_cordon(self):
        def replaced(*args, **kwargs):
            self.kube.add(host("node-a", uid="uid-new", cordoned=True))
            return refused()
        item = self.send(drain=replaced)
        self.assertTrue(self.cordoned())
        self.assertIn("The host was replaced during this job, so its cordon was left alone", item["message"])

    def test_an_uncordon_that_fails_says_the_host_stays_cordoned(self):
        self.kube.fail(NODE, 500, method="PATCH", skip=1, times=5)    # the job's cordon goes through
        item = self.send()
        self.assertTrue(self.cordoned())
        self.assertIn("The host stays cordoned, because uncordoning it failed", item["message"])
        self.assertNotIn("uncordoned,", item["message"])

    def test_a_send_that_may_have_gone_out_leaves_the_cordon(self):
        self.kube.fail("/api/v1/namespaces/lab/pods", 500, method="POST")
        item = self.send(drain=lambda *a, **k: {"evicted": [], "skipped": []})
        self.assertIn("uncertain", item["message"])
        self.assertTrue(self.cordoned())


def _raised_texts(module):
    """The words of every message module raises: a string, the constant parts
    of an f-string, or the strings of a + of them."""
    def text(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return (text(node.left) or "") + (text(node.right) or "")
        return None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
            words = text(node.exc.args[0])
            if words:
                yield words


class CordonWordsTests(unittest.TestCase):
    def test_every_cordon_sentence_the_drain_and_rechecks_raise_is_taken_out(self):
        """The job says what became of the cordon itself, so each "stays
        cordoned" sentence the drain or a recheck raises must be one
        _CORDON_WORDS removes. Reworded there, this fails, instead of a job
        saying "was uncordoned" and "stays cordoned" at once."""
        found = [t for module in (NODE_ACTIONS, POWER) for t in _raised_texts(module) if "stays cordoned" in t]
        self.assertGreaterEqual(len(found), 7, "the drain's and the rechecks' messages were found")
        for words in found:
            cleaned = words
            for old, new in JOBS._CORDON_WORDS:
                cleaned = cleaned.replace(old, new)
            self.assertNotIn("stays cordoned", cleaned, words)
            self.assertNotIn("othing was sent", cleaned, words)


if __name__ == "__main__":
    unittest.main()
