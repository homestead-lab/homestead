"""Two scripts on one host don't stop each other: each run has its own helper
pod, deletes only that one, and clears away pods of runs that already ended,
never one still running (#597). On the shared fake API (tests/fakes/kube.py)."""
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_hostrun as HOSTRUN
import homestead_kube as K
from fakes.kube import Kube

PODS = "/api/v1/namespaces/lab/pods"


def helper(name, node, phase):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "lab", "uid": f"uid-{name}",
                         "labels": {**HOSTRUN.NAMES.labels(HOSTRUN.TASK), HOSTRUN.NAMES.key("node"): node}},
            "spec": {"nodeName": node}, "status": {"phase": phase}}


class Exec:
    """Exec in a pod is not an API read or write: it only notes the pods it ran in."""
    def __init__(self):
        self.ran_in = []

    def __call__(self, namespace, pod, argv, **kwargs):
        self.ran_in.append(pod)
        return b"ok", ""


class RunPodTests(unittest.TestCase):
    def use(self, existing=()):
        kube = self.enterContext(Kube(*existing))

        def kubelet(method, path, body):
            body["status"] = {"phase": "Running"}   # the kubelet starts it straight away
        kube.react("POST", PODS, kubelet)
        self.addCleanup(HOSTRUN.bind, *(getattr(HOSTRUN, name, None) for name in ("kget", "ksend", "exec_in", "NS")))
        self.exec = Exec()
        HOSTRUN.bind(K.kget, K.ksend, self.exec, "lab")
        return kube

    def test_a_run_never_deletes_another_runs_helper(self):
        other = helper("homestead-host-node-2", "node-2", "Running")
        kube = self.use([other])
        HOSTRUN.run("node-2", "echo ok", sleep=lambda s: None)
        deleted = [path.split("?")[0] for method, path, _, _ in kube.writes if method == "DELETE"]
        self.assertNotIn(f"{PODS}/homestead-host-node-2", deleted)
        self.assertIsNotNone(kube.object(f"{PODS}/homestead-host-node-2"), "the other run's helper is still there")
        self.assertNotEqual("homestead-host-node-2", self.exec.ran_in[0])

    def test_each_run_has_its_own_helper_and_removes_it(self):
        kube = self.use()
        HOSTRUN.run("node-2", "echo one", sleep=lambda s: None)
        HOSTRUN.run("node-2", "echo two", sleep=lambda s: None)
        first, second = self.exec.ran_in
        self.assertNotEqual(first, second)
        deleted = [path.split("?")[0] for method, path, _, _ in kube.writes if method == "DELETE"]
        for name in (first, second):
            self.assertTrue(name.startswith("homestead-host-node-2-") and len(name) <= 63, name)
            self.assertIn(f"{PODS}/{name}", deleted)
        self.assertEqual([], K.kget(PODS)["items"])

    def test_helpers_of_runs_that_ended_are_cleared_away(self):
        ended = helper("homestead-host-node-2-dead01", "node-2", "Failed")
        done = helper("homestead-host-node-3-dead02", "node-3", "Succeeded")
        running = helper("homestead-host-node-3-live03", "node-3", "Running")
        kube = self.use([ended, done, running])
        HOSTRUN.run("node-2", "echo ok", sleep=lambda s: None)
        self.assertIsNone(kube.object(f"{PODS}/homestead-host-node-2-dead01"))
        self.assertIsNone(kube.object(f"{PODS}/homestead-host-node-3-dead02"))
        self.assertIsNotNone(kube.object(f"{PODS}/homestead-host-node-3-live03"))

    def test_a_long_node_name_still_gives_distinct_names_that_fit(self):
        node = "a-very-long-node-name-from-a-cloud-provider-eu-west-1-instance-0001"
        names = {HOSTRUN.pod_name(node) for _ in range(20)}
        self.assertEqual(20, len(names))
        self.assertTrue(all(len(n) <= 63 and n.startswith("homestead-host-") for n in names))


if __name__ == "__main__":
    unittest.main()
