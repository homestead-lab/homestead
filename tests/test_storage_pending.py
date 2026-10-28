import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_storage_pending as PENDING
import homestead_updates as UPDATES
import homestead_update_rollouts as ROLLOUTS
import homestead_operations as OPS
from fakes.kube import Kube

NOW = 1_790_000_000
STAMP = lambda seconds_ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - seconds_ago))


def node(name, age, taints=None):
    spec = {"taints": taints} if taints is not None else {}
    return {"apiVersion": "v1", "kind": "Node",
            "metadata": {"name": name, "creationTimestamp": STAMP(age), "resourceVersion": "7"}, "spec": spec}


def cluster(nodes, registered=(), longhorn=True):
    """The nodes, Longhorn's CSI driver (unless longhorn is False) and a CSINode
    for each node that lists the driver for those in registered."""
    objects = list(nodes)
    if longhorn:
        objects.append({"apiVersion": "storage.k8s.io/v1", "kind": "CSIDriver", "metadata": {"name": PENDING.DRIVER}})
    for each in nodes:
        name = each["metadata"]["name"]
        objects.append({"apiVersion": "storage.k8s.io/v1", "kind": "CSINode", "metadata": {"name": name},
                        "spec": {"drivers": [{"name": PENDING.DRIVER}] if name in registered else []}})
    return objects


TAINT = {"key": PENDING.KEY, "value": "longhorn", "effect": "PreferNoSchedule"}


class StoragePendingTests(unittest.TestCase):
    """On the shared fake API (tests/fakes/kube.py)."""

    def start(self, *args, **kwargs):
        kube = self.enterContext(Kube(*cluster(*args, **kwargs)))
        for name, value in (("kget", PENDING.kget), ("ksend", PENDING.ksend)):
            self.addCleanup(setattr, PENDING, name, value)
        PENDING.bind(K.kget, K.ksend)
        return kube

    def sent(self, kube):
        return [(path.rsplit("/", 1)[-1], body, ctype) for _, path, body, ctype in kube.writes]

    def test_a_node_that_just_joined_is_steered_clear_of_until_longhorn_is_there(self):
        kube = self.start([node("old", 86400), node("new", 60)], registered={"old"})
        self.assertEqual(["new"], [name for name, _ in PENDING.tick(now=NOW)])
        name, ops, ctype = self.sent(kube)[0]
        self.assertEqual("application/json-patch+json", ctype)
        self.assertEqual({"op": "test", "path": "/metadata/resourceVersion", "value": "7"}, ops[0],
                         "a taint Kubernetes adds meanwhile is never overwritten")
        self.assertEqual([TAINT], ops[1]["value"])
        self.assertEqual([TAINT], kube.object("/api/v1/nodes/new")["spec"]["taints"])

    def test_the_taint_comes_off_once_the_driver_registers_and_keeps_the_others(self):
        other = {"key": "node.kubernetes.io/not-ready", "effect": "NoSchedule"}
        kube = self.start([node("new", 120, [other, TAINT])], registered={"new"})
        PENDING.tick(now=NOW)
        self.assertEqual([other], self.sent(kube)[0][1][1]["value"])
        self.assertEqual([other], kube.object("/api/v1/nodes/new")["spec"]["taints"])

    def test_an_old_node_without_longhorn_is_left_alone_and_no_longhorn_means_no_waiting(self):
        self.start([node("old", 86400)])
        self.assertEqual([], PENDING.tick(now=NOW))
        kube = self.start([node("new", 60, [TAINT])], longhorn=False)
        PENDING.tick(now=NOW)
        self.assertEqual([], self.sent(kube)[0][1][1]["value"], "the installer's taint is lifted with nothing to wait for")

    def test_the_installer_taints_a_joining_node_from_its_first_moment(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-k3s.sh").read_text()
        self.assertIn('JOIN_TAINT="homestead.io/storage-pending=longhorn:PreferNoSchedule"', script)
        self.assertIn('install_k3s agent --node-taint "$JOIN_TAINT"', script)
        self.assertIn('install_k3s server --server "$URL" --node-taint "$JOIN_TAINT"', script)
        self.assertIn('JOIN_TAINT=""', script, "set -u: the first server has none")

    def test_a_tiebreaker_joins_with_its_own_taint_and_label_and_no_storage_wait(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-k3s.sh").read_text()
        self.assertIn('--tiebreaker) TIEBREAKER=1; shift; continue ;;', script)
        self.assertIn('JOIN_TAINT="node-role/tiebreaker=true:NoSchedule"; JOIN_LABEL="node-role/tiebreaker=true"', script)
        self.assertIn('install_k3s server --server "$URL" --node-taint "$JOIN_TAINT" ${JOIN_LABEL:+--node-label "$JOIN_LABEL"}', script)
        self.assertIn('[ "$TIEBREAKER" = 1 ] && [ "$MODE" = agent ] && fail', script)


class WaitingPodTests(unittest.TestCase):
    def test_longhorn_missing_on_a_node_is_said_plainly_and_is_not_a_failure_yet(self):
        pod = {"metadata": {"name": "homestead-1", "creationTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 300))},
               "spec": {"nodeName": "k3s-2"}}
        event = {"reason": "FailedAttachVolume", "lastTimestamp": "x",
                 "message": 'AttachVolume.Attach failed for volume "pvc-1" : CSINode k3s-2 does not contain driver driver.longhorn.io'}
        UPDATES.kget = lambda path: {"items": [event]}
        found = ROLLOUTS._why_waiting("lab", pod)
        self.assertIn("Longhorn is not running on k3s-2 yet", found["message"])
        self.assertFalse(found["stuck"], "five minutes after a join is not stuck")
        self.assertNotIn("hint", found)

    def test_the_rollout_job_says_what_the_new_pod_waits_for_not_the_old_pods_count(self):
        self.addCleanup(setattr, OPS, "deployment_progress", OPS.deployment_progress)
        OPS.deployment_progress = lambda ns, name: {"phase": "progressing", "desired": 1, "ready": 1,
            "pods": [{"name": "old", "node": "k3s-1"}, {"name": "new", "node": "k3s-2", "blocked": "Longhorn is not running on k3s-2 yet"}]}
        status, _, message = OPS._deployment({"ref": {"namespace": "lab", "name": "homestead"}})
        self.assertEqual("running", status)
        self.assertEqual("New pod waiting on k3s-2: Longhorn is not running on k3s-2 yet", message)


if __name__ == "__main__":
    unittest.main()
