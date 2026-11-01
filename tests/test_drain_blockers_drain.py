"""A drain that can't finish says what holds the host, and waits out a
disruption budget whose copies are still starting.

The eviction subresource refuses with 429 while a budget allows no
disruption. Its Status body is the one Kubernetes builds in
pkg/registry/core/pod/storage/eviction.go (v1.34.0) checkAndDecrement:
errors.NewTooManyRequests("Cannot evict pod as it would violate the pod's
disruption budget.", 0) plus one StatusCause{Type: policyv1.DisruptionBudgetCause
("DisruptionBudget"), Message: "The disruption budget %s needs %d healthy pods
and has %d currently"}. StatusCause.Type is sent as "reason"
(apimachinery meta/v1 types.go), and a retryAfterSeconds of 0 is left out
(omitempty). The budget's status fields are those of policy/v1
PodDisruptionBudgetStatus (staging/src/k8s.io/api/policy/v1/types.go):
expectedPods, currentHealthy, desiredHealthy, disruptionsAllowed,
observedGeneration.

Longhorn's own instance-manager budget is held at 0 while its engines run, and
is refused a moment after its consumers leave; it is waited on while the
engines it lists still run on this host, and fails early once only engines
that run elsewhere, or their orphan records, remain."""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_kube as KUBE
import homestead_lifecycle as LIFECYCLE
import homestead_maintenance as MAINTENANCE
import homestead_node_actions as NODE_ACTIONS
import homestead_drain_blockers as BLOCKERS
import test_drain_blockers as SHAPES
from fakes.kube import Kube

NODE = "node-a"
# The instance-manager pod is owned by the InstanceManager of the fixtures;
# the fake collects a pod whose owner is not there.
IM_UID = SHAPES.instance_manager()["metadata"]["uid"]


def workload(name="app-a", ns="lab", node=NODE, labels=None):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": ns, "uid": f"uid-{name}", "labels": labels or {"app": name}},
            "spec": {"nodeName": node, "containers": [{"name": "app", "image": "example.com/app:1"}]},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}


def manager_pod(node=NODE, name="instance-manager-0a1b2c3d4e5f60718293a4b5c6d7e8f9"):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "longhorn-system", "uid": "uid-im-pod",
                         "labels": {"longhorn.io/component": "instance-manager",
                                    "longhorn.io/managed-by": "longhorn-manager", "longhorn.io/node": node},
                         "ownerReferences": [{"apiVersion": "longhorn.io/v1beta2", "kind": "InstanceManager",
                                              "name": name, "uid": IM_UID, "controller": True}]},
            "spec": {"nodeName": node, "containers": [{"name": "im", "image": "example.com/im:1"}]},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}


def budget(name, ns="lab", expected=2, healthy=1, desired=1, allowed=0, generation=1, observed=1):
    return {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
            "metadata": {"name": name, "namespace": ns, "uid": f"uid-pdb-{name}", "generation": generation},
            "spec": {"minAvailable": desired, "selector": {"matchLabels": {"app": "app-a"}}},
            "status": {"observedGeneration": observed, "expectedPods": expected, "currentHealthy": healthy,
                       "desiredHealthy": desired, "disruptionsAllowed": allowed}}


def too_many_requests(path, pdb=None, body=True, processing=False):
    """The eviction 429 as Kubernetes sends it (see the module docstring).
    processing: the one createTooManyRequestsError builds while the budget's
    latest change is not observed yet (retryAfterSeconds 10)."""
    name = pdb["metadata"]["name"]
    cause = (f"The disruption budget {name} is still being processed by the server." if processing else
             f"The disruption budget {name} needs {pdb['status']['desiredHealthy']} "
             f"healthy pods and has {pdb['status']['currentHealthy']} currently")
    details = {"causes": [{"reason": "DisruptionBudget", "message": cause}]}
    if processing:
        details["retryAfterSeconds"] = 10
    status = {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
              "message": "Cannot evict pod as it would violate the pod's disruption budget.",
              "reason": "TooManyRequests", "code": 429, "details": details}
    raw = json.dumps(status).encode() if body else b""
    return urllib.error.HTTPError(path, 429, "Too Many Requests", {}, io.BytesIO(raw))


def eviction(pod):
    meta = pod["metadata"]
    return f"/api/v1/namespaces/{meta['namespace']}/pods/{meta['name']}/eviction"


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DrainNamesItsBlockerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.phases = []

    def run_drain(self, kube, power=False):
        with mock.patch.object(LIFECYCLE, "kget", KUBE.kget), \
                mock.patch.object(LIFECYCLE, "ksend", KUBE.ksend), \
                mock.patch.object(LIFECYCLE, "_bust", lambda *a: None), \
                mock.patch.object(LIFECYCLE, "NODE_POWER_ENABLED", True), \
                mock.patch.object(NODE_ACTIONS, "node_action_check", return_value=(True, "", {})), \
                mock.patch.object(NODE_ACTIONS, "set_cordon"), \
                mock.patch.object(NODE_ACTIONS, "_send_power", return_value={"ok": True}) as send, \
                mock.patch.object(NODE_ACTIONS.time, "monotonic", self.clock.monotonic), \
                mock.patch.object(NODE_ACTIONS.time, "sleep", self.clock.sleep):
            here = [p for p in MAINTENANCE.items(KUBE.kget, "/api/v1/pods") if p["spec"].get("nodeName") == NODE]
            reviewed = [list(row) for row in MAINTENANCE.pod_snapshot(here)]
            try:
                if power:
                    return NODE_ACTIONS.node_power(NODE, "reboot", reviewed_pods=reviewed, before_send=lambda: None,
                                                   progress=lambda *a, **k: self.phases.append(a))
                return NODE_ACTIONS.drain(NODE, include_system=True, reviewed_pods=reviewed, wait=True,
                                          progress=lambda *a, **k: self.phases.append(a))
            finally:
                self.sent = send.called

    def refuse(self, kube, pod, pdb, times, then=None, body=True):
        """Answer pod's eviction 429 the first times; then let it through."""
        asked = []

        def answer(method, path, sent):
            asked.append(path)
            if len(asked) <= times:
                if then and len(asked) == times:
                    then()
                raise too_many_requests(path, pdb, body=body)
            return None
        kube.react("POST", eviction(pod), answer)
        return asked

    def test_a_budget_whose_copies_are_starting_is_waited_out(self):
        pod, pdb = workload(), budget("keep-ready", expected=2, healthy=1)
        with Kube(pod, pdb) as kube:
            asked = self.refuse(kube, pod, pdb, times=2)
            result = self.run_drain(kube)
        self.assertTrue(result["ok"])
        self.assertEqual(3, len(asked))
        self.assertEqual(["lab/app-a"], result["evicted"])
        said = " ".join(row[2] for row in self.phases)
        self.assertIn("lab/keep-ready", said)
        self.assertIn("1 of 2 copies", said)

    def test_a_budget_behind_after_the_first_eviction_is_waited_out(self):
        # Two copies under minAvailable 1, both on this host. The first
        # eviction goes through: Kubernetes' checkAndDecrement sets
        # disruptionsAllowed to 0 and records the pod in disruptedPods, but
        # currentHealthy stays 2 until the budget controller resyncs. The
        # second eviction, a moment later, is refused with "needs 1 healthy
        # pods and has 2 currently"; once the controller catches up it goes.
        first = workload("app-a", labels={"app": "app-a"})
        second = workload("app-b", labels={"app": "app-a"})
        pdb = budget("keep-one", expected=2, healthy=2, desired=1, allowed=1)
        with Kube(first, second, pdb) as kube:
            pdb_path = kube.path_of(pdb)

            def evicted(method, path, sent):
                current = kube.object(pdb_path)
                current["status"].update(disruptionsAllowed=0, disruptedPods={"app-a": "2026-11-01T12:00:00Z"})
                kube.add(current)
                return None
            kube.react("POST", eviction(first), evicted)
            asked = self.refuse(kube, second, pdb, times=3)
            result = self.run_drain(kube)
        self.assertTrue(result["ok"], result)
        self.assertEqual(4, len(asked))
        self.assertEqual(["lab/app-a", "lab/app-b"], sorted(result["evicted"]))
        said = " ".join(row[2] for row in self.phases)
        self.assertIn("lab/keep-one", said)
        self.assertNotIn("none is starting", said)

    def test_a_budget_with_nothing_starting_fails_at_once_naming_it(self):
        pod, pdb = workload(), budget("keep-ready", expected=1, healthy=1, desired=1, allowed=0)
        with Kube(pod, pdb) as kube:
            self.refuse(kube, pod, pdb, times=99)
            with self.assertRaises(ValueError) as caught:
                self.run_drain(kube, power=True)
        message = str(caught.exception)
        self.assertIn("lab/keep-ready", message)
        self.assertIn("Host remains cordoned", message)
        self.assertIn("Power was not sent", message)
        self.assertLess(self.clock.now, BLOCKERS.BUDGET_WAIT)
        self.assertEqual(0, self.clock.now)
        self.assertFalse(self.sent)

    def test_a_passing_refusal_gives_up_after_the_wait(self):
        pod, pdb = workload(), budget("keep-ready", expected=2, healthy=1)
        with Kube(pod, pdb) as kube:
            self.refuse(kube, pod, pdb, times=10_000)
            with self.assertRaises(ValueError) as caught:
                self.run_drain(kube, power=True)
        message = str(caught.exception)
        self.assertGreaterEqual(self.clock.now, BLOCKERS.BUDGET_WAIT)
        self.assertIn("lab/keep-ready", message)
        self.assertIn("still waiting", message)
        self.assertIn("Host remains cordoned", message)
        self.assertIn("Power was not sent", message)
        self.assertFalse(self.sent)

    def test_instance_manager_refused_while_engines_detach_keeps_waiting(self):
        im_pod = manager_pod()
        im_budget = budget(im_pod["metadata"]["name"], ns="longhorn-system", expected=1, healthy=1, desired=1)
        # Normal detach: the engine still runs on this host, then leaves.
        im = SHAPES.instance_manager(NODE, engines=["vol-a-e-0"])
        objects = [workload(), im_pod, im_budget, im, SHAPES.engine("vol-a", NODE)]
        with Kube(*objects) as kube:
            im_path = kube.path_of(im)

            def detached():
                current = kube.object(im_path)
                current["status"]["instanceEngines"] = {}
                kube.add(current)
            # Longer than the early-failure grace: engines still here are progress.
            asked = self.refuse(kube, im_pod, im_budget, times=40, then=detached)
            result = self.run_drain(kube)
        self.assertTrue(result["ok"])
        self.assertEqual(41, len(asked))
        self.assertIn("longhorn-system/" + im_pod["metadata"]["name"], result["evicted"])
        self.assertLess(self.clock.now, NODE_ACTIONS.LONGHORN_DRAIN_WAIT)

    def test_instance_manager_fails_early_when_only_stale_engines_remain(self):
        im_pod = manager_pod()
        im_budget = budget(im_pod["metadata"]["name"], ns="longhorn-system", expected=1, healthy=1, desired=1)
        # Today's case: the engine runs on node-b now; node-a's instance-manager
        # still lists it, and Longhorn recorded the leftover as an orphan.
        objects = [workload(), im_pod, im_budget, SHAPES.instance_manager(NODE, engines=["vol-a-e-0", "vol-b-e-0"]),
                   SHAPES.engine("vol-a", "node-b"), SHAPES.engine_orphan("vol-a", 1)]
        with Kube(*objects) as kube:
            self.refuse(kube, im_pod, im_budget, times=10_000)
            with self.assertRaises(ValueError) as caught:
                self.run_drain(kube, power=True)
        message = str(caught.exception)
        self.assertLess(self.clock.now, NODE_ACTIONS.LONGHORN_DRAIN_WAIT)
        self.assertIn("orphan record", message)
        self.assertIn("vol-a", message)
        self.assertIn("vol-b-e-0 (no engine record)", message)
        self.assertIn("review the restart again", message)
        self.assertIn("Host remains cordoned", message)
        self.assertIn("Power was not sent", message)
        self.assertFalse(self.sent)

    def test_unreadable_refusal_falls_back_to_todays_behaviour(self):
        # Instance-manager: waited on as today, for LONGHORN_DRAIN_WAIT.
        im_pod = manager_pod()
        im_budget = budget(im_pod["metadata"]["name"], ns="longhorn-system", expected=1, healthy=1, desired=1)
        with Kube(workload(), im_pod, im_budget, SHAPES.instance_manager(NODE)) as kube:
            kube.fail(BLOCKERS.INSTANCE_MANAGERS, 500, times=10_000)
            kube.fail(BLOCKERS.ORPHANS, 500, times=10_000)
            self.refuse(kube, im_pod, im_budget, times=10_000, body=False)
            with self.assertRaises(ValueError) as caught:
                self.run_drain(kube, power=True)
        self.assertGreaterEqual(self.clock.now, NODE_ACTIONS.LONGHORN_DRAIN_WAIT)
        message = str(caught.exception)
        self.assertIn("after 5 minutes. Longhorn still prevents eviction", message)
        # What could not be read is said, not left out, with today's advice.
        self.assertIn("Homestead couldn't check Longhorn instance-managers: HTTP 500", message)
        self.assertIn("Homestead couldn't check Longhorn orphan records: HTTP 500", message)
        self.assertIn("inspect Longhorn volumes, disruption budgets and pod events", message)
        # Any other pod: refused at once as today, and the message says why is unknown.
        self.clock, pod, pdb = Clock(), workload(), budget("keep-ready", expected=2, healthy=1)
        with Kube(pod, pdb) as kube:
            kube.fail("/apis/policy/v1/namespaces/lab/poddisruptionbudgets/keep-ready", 500, times=10_000)
            self.refuse(kube, pod, pdb, times=10_000, body=False)
            with self.assertRaises(ValueError) as caught:
                self.run_drain(kube, power=True)
        message = str(caught.exception)
        self.assertEqual(0, self.clock.now)
        self.assertIn("drain was refused for lab/app-a (HTTP 429", message)
        self.assertIn("couldn't read why", message)


    def test_a_stale_only_moment_shorter_than_the_grace_keeps_waiting(self):
        # An engine that just moved is listed on the old instance-manager for
        # a reconcile or two: that alone must not fail a healthy drain.
        im_pod = manager_pod()
        im_budget = budget(im_pod["metadata"]["name"], ns="longhorn-system", expected=1, healthy=1, desired=1)
        objects = [workload(), im_pod, im_budget, SHAPES.instance_manager(NODE, engines=["vol-a-e-0"]),
                   SHAPES.engine("vol-a", "node-b")]
        with Kube(*objects) as kube:
            asked = self.refuse(kube, im_pod, im_budget, times=10)  # about 20 s
            result = self.run_drain(kube)
        self.assertTrue(result["ok"])
        self.assertEqual(11, len(asked))
        self.assertLess(self.clock.now, BLOCKERS.STALE_GRACE)

    def test_malformed_longhorn_data_mid_wait_is_waited_on_as_unread(self):
        im_pod = manager_pod()
        im_budget = budget(im_pod["metadata"]["name"], ns="longhorn-system", expected=1, healthy=1, desired=1)
        im = SHAPES.instance_manager(NODE)
        im["status"]["instanceEngines"] = [1, "vol-a-e-0"]  # not the map Longhorn writes
        with Kube(workload(), im_pod, im_budget, im) as kube:
            asked = self.refuse(kube, im_pod, im_budget, times=20)
            result = self.run_drain(kube)
        self.assertTrue(result["ok"])
        self.assertEqual(21, len(asked))


class RefusalTests(unittest.TestCase):
    def test_a_refusal_reads_the_budget_named_in_the_cause(self):
        pod, pdb = workload(), budget("keep-ready", expected=3, healthy=1, desired=2)
        with Kube(pdb):
            said = BLOCKERS.refusal(too_many_requests("eviction", pdb), pod)
        self.assertEqual({"budget": "lab/keep-ready", "passing": True, "starting": 2, "expected": 3},
                         {k: said[k] for k in ("budget", "passing", "starting", "expected")})
        self.assertIn("1 of 3 copies", said["why"])

    def test_an_unreadable_refusal(self):
        pod, pdb = workload(), budget("keep-ready")
        with Kube():
            for error in (too_many_requests("eviction", pdb, body=False),
                          urllib.error.HTTPError("eviction", 429, "x", {}, None),
                          too_many_requests("eviction", pdb)):  # the budget is not there to read
                said = BLOCKERS.refusal(error, pod)
                self.assertEqual({"budget": None, "passing": False, "why": "couldn't read why"},
                                 {k: said[k] for k in ("budget", "passing", "why")})

    def test_a_budget_the_controller_has_not_caught_up_with_is_passing(self):
        # Right after an eviction: disruptedPods names the evicted pod, or
        # currentHealthy is still above desiredHealthy with nothing allowed.
        pod = workload()
        for pdb in (budget("keep-one", expected=2, healthy=2, desired=1, allowed=0),
                    budget("keep-one", expected=2, healthy=1, desired=1, allowed=0)):
            if pdb["status"]["currentHealthy"] == 1:
                pdb["status"]["disruptedPods"] = {"app-z": "2026-11-01T12:00:00Z"}
            with self.subTest(pdb["status"]), Kube(pdb):
                said = BLOCKERS.refusal(too_many_requests("eviction", pdb), pod)
            self.assertTrue(said["passing"], said)
            self.assertIn("lab/keep-one", said["why"])
            self.assertNotIn("none is starting", said["why"])

    def test_copies_not_yet_healthy_are_said_so(self):
        pod, pdb = workload(), budget("keep-ready", expected=2, healthy=1, desired=1)
        with Kube(pdb):
            said = BLOCKERS.refusal(too_many_requests("eviction", pdb), pod)
        self.assertIn("1 not yet healthy", said["why"])
        self.assertNotIn("still starting", said["why"])

    def test_a_budget_not_yet_processed_is_passing(self):
        # observedGeneration < generation: Kubernetes answers "is still being
        # processed" and the refusal clears on its own.
        pod, pdb = workload(), budget("keep-ready", expected=1, healthy=1, desired=1, generation=3, observed=2)
        with Kube(pdb):
            said = BLOCKERS.refusal(too_many_requests("eviction", pdb, processing=True), pod)
        self.assertEqual({"budget": "lab/keep-ready", "passing": True, "starting": 0},
                         {k: said[k] for k in ("budget", "passing", "starting")})
        self.assertIn("lab/keep-ready", said["why"])

    def test_a_partly_unread_host_says_what_was_found_and_what_could_not_be_checked(self):
        with Kube(SHAPES.fixture("orphan_replica_data")) as kube:
            kube.fail(BLOCKERS.CLAIMS, 500, times=10)
            said = BLOCKERS.stall_reason(NODE)
        self.assertIn("other Longhorn orphan record", said)
        self.assertIn("Homestead couldn't check the volume claims: HTTP 500", said)
        self.assertIn("review the restart again", said)

    def test_stall_reason_is_empty_when_nothing_is_found(self):
        with Kube():
            self.assertEqual("", BLOCKERS.stall_reason(NODE))


if __name__ == "__main__":
    unittest.main()
