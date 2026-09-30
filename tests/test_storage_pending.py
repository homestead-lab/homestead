import sys
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_storage_pending as PENDING
import homestead_updates as UPDATES
import homestead_operations as OPS

NOW = 1_790_000_000
STAMP = lambda seconds_ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - seconds_ago))


class Cluster:
    def __init__(self, nodes, registered=(), longhorn=True):
        self.nodes, self.registered, self.longhorn, self.sent = nodes, set(registered), longhorn, []

    def get(self, path):
        if path == "/api/v1/nodes":
            return {"items": self.nodes}
        if path.endswith("/csidrivers/driver.longhorn.io"):
            if self.longhorn:
                return {}
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if "/csinodes/" in path:
            name = path.rsplit("/", 1)[-1]
            return {"spec": {"drivers": [{"name": "driver.longhorn.io"}] if name in self.registered else []}}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    def send(self, method, path, body=None, ctype=None):
        self.sent.append((path.rsplit("/", 1)[-1], body, ctype))


def node(name, age, taints=None):
    spec = {"taints": taints} if taints is not None else {}
    return {"metadata": {"name": name, "creationTimestamp": STAMP(age), "resourceVersion": "7"}, "spec": spec}


TAINT = {"key": PENDING.KEY, "value": "longhorn", "effect": "PreferNoSchedule"}


class StoragePendingTests(unittest.TestCase):
    def run_tick(self, cluster):
        PENDING.bind(cluster.get, cluster.send)
        return PENDING.tick(now=NOW)

    def test_a_node_that_just_joined_is_steered_clear_of_until_longhorn_is_there(self):
        cluster = Cluster([node("old", 86400), node("new", 60)], registered={"old"})
        self.assertEqual(["new"], [name for name, _ in self.run_tick(cluster)])
        name, ops, ctype = cluster.sent[0]
        self.assertEqual("application/json-patch+json", ctype)
        self.assertEqual({"op": "test", "path": "/metadata/resourceVersion", "value": "7"}, ops[0],
                         "a taint Kubernetes adds meanwhile is never overwritten")
        self.assertEqual([TAINT], ops[1]["value"])

    def test_the_taint_comes_off_once_the_driver_registers_and_keeps_the_others(self):
        other = {"key": "node.kubernetes.io/not-ready", "effect": "NoSchedule"}
        cluster = Cluster([node("new", 120, [other, TAINT])], registered={"new"})
        self.run_tick(cluster)
        self.assertEqual([other], cluster.sent[0][1][1]["value"])

    def test_an_old_node_without_longhorn_is_left_alone_and_no_longhorn_means_no_waiting(self):
        self.assertEqual([], self.run_tick(Cluster([node("old", 86400)])))
        cluster = Cluster([node("new", 60, [TAINT])], longhorn=False)
        self.run_tick(cluster)
        self.assertEqual([], cluster.sent[0][1][1]["value"], "the installer's taint is lifted with nothing to wait for")

    def test_the_installer_taints_a_joining_node_from_its_first_moment(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-k3s.sh").read_text()
        self.assertIn('JOIN_TAINT="homestead.io/storage-pending=longhorn:PreferNoSchedule"', script)
        self.assertIn('install_k3s agent --node-taint "$JOIN_TAINT"', script)
        self.assertIn('install_k3s server --server "$URL" --node-taint "$JOIN_TAINT"', script)
        self.assertIn('JOIN_TAINT=""', script, "set -u: the first server has none")


class WaitingPodTests(unittest.TestCase):
    def test_longhorn_missing_on_a_node_is_said_plainly_and_is_not_a_failure_yet(self):
        pod = {"metadata": {"name": "homestead-1", "creationTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 300))},
               "spec": {"nodeName": "k3s-2"}}
        event = {"reason": "FailedAttachVolume", "lastTimestamp": "x",
                 "message": 'AttachVolume.Attach failed for volume "pvc-1" : CSINode k3s-2 does not contain driver driver.longhorn.io'}
        UPDATES.kget = lambda path: {"items": [event]}
        found = UPDATES._why_waiting("lab", pod)
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
