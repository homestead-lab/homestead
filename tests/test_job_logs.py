"""Every job can show its log: the steps it has taken, and where it has one,
the output of what does its work - a Job's pod, a k3s node's console."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_joblogs as JL
import homestead_operations as OPS


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = OPS.DATA_DIR, dict(OPS.RESOLVERS), dict(OPS.LOGGERS)
        OPS.DATA_DIR = self.tmp.name
        self.steps = iter([("running", 10, "0 of 2 VMs running"), ("running", 10, "0 of 2 VMs running"),
                           ("running", 50, "2 of 2 VMs running"), ("succeeded", 100, "The cluster is up")])
        OPS.RESOLVERS["thing"] = lambda item: next(self.steps)

    def tearDown(self):
        OPS.DATA_DIR = self.saved[0]
        OPS.RESOLVERS.clear(); OPS.RESOLVERS.update(self.saved[1])
        OPS.LOGGERS.clear(); OPS.LOGGERS.update(self.saved[2])
        self.tmp.cleanup()

    def test_each_step_a_job_takes_is_kept_once_with_its_time(self):
        op = OPS.start("thing", "Build", {}, "/", {}, "Starting 2 VMs")["id"]
        for _ in range(4):
            OPS.list_operations()
        log = OPS.log(op)
        self.assertEqual(["Starting 2 VMs", "0 of 2 VMs running", "2 of 2 VMs running", "The cluster is up"],
                         [h["m"] for h in log["history"]])
        self.assertTrue(all(h["t"] for h in log["history"]))
        self.assertNotIn("history", OPS.list_operations()[0], "the tray's poll stays small")

    def test_a_reader_that_fails_says_so_rather_than_failing_the_log(self):
        op = OPS.start("thing", "Build", {}, "/", {})["id"]
        OPS.LOGGERS["thing"] = lambda item: 1 / 0
        self.assertIn("could not be read", OPS.log(op)["sources"][0]["note"])


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.objects, self.logs = {}, {}

        def get(path):
            base, _, query = path.partition("?")
            key = (base, query.split("labelSelector=")[-1] if "labelSelector" in query else "")
            if key in self.objects:
                return self.objects[key]
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)

        def text(path):
            for key, value in self.logs.items():
                if path.startswith(key):
                    return value
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        JL.bind(get, text)

    def pod(self, name, containers=("compute",), phase="Running"):
        return {"metadata": {"name": name, "creationTimestamp": "2026-09-25T12:00:00Z"},
                "spec": {"containers": [{"name": c} for c in containers]}, "status": {"phase": phase}}

    def test_a_k3s_node_shows_its_console_and_a_node_not_running_says_so(self):
        sel = lambda vm: JL._q(f"vm.kubevirt.io/name={vm}")
        self.objects[("/api/v1/namespaces/lab/pods", sel("c-server-1"))] = {"items": [
            self.pod("virt-launcher-c-server-1-abcde", ("compute", "guest-console-log"))]}
        self.objects[("/api/v1/namespaces/lab/pods", sel("c-agent-1"))] = {"items": []}
        self.logs["/api/v1/namespaces/lab/pods/virt-launcher-c-server-1-abcde/log?tailLines=200&container=guest-console-log"] = \
            "[INFO]  Installing k3s to /usr/local/bin/k3s\n"
        out = JL.k3s_cluster({"ref": {"namespace": "lab", "nodes": [
            {"name": "c-server-1", "role": "server", "address": "192.0.2.60"},
            {"name": "c-agent-1", "role": "agent", "address": "192.0.2.61"}]}})
        self.assertIn("Installing k3s", out[0]["text"])
        self.assertEqual("the VM is not running yet", out[1]["note"])

    def test_an_older_kubevirt_says_where_to_look_instead(self):
        self.objects[("/api/v1/namespaces/lab/pods", JL._q("vm.kubevirt.io/name=c-server-1"))] = {"items": [
            self.pod("virt-launcher-c-server-1-abcde")]}
        out = JL.k3s_cluster({"ref": {"namespace": "lab", "nodes": [
            {"name": "c-server-1", "role": "server", "address": "192.0.2.60"}]}})
        self.assertIn("open c-server-1's console", out[0]["note"])

    def test_an_old_kubevirt_is_named_as_the_reason(self):
        self.objects[("/api/v1/namespaces/lab/pods", JL._q("vm.kubevirt.io/name=c-server-1"))] = {"items": [
            self.pod("virt-launcher-c-server-1-abcde")]}
        self.objects[("/apis/kubevirt.io/v1/kubevirts", "")] = {"items": [
            {"status": {"observedKubeVirtVersion": "v1.0.1-150500.8.6.1"}}]}
        out = JL.k3s_cluster({"ref": {"namespace": "lab", "nodes": [
            {"name": "c-server-1", "role": "server", "address": "192.0.2.60"}]}})
        self.assertIn("KubeVirt v1.0.1-150500.8.6.1 keeps no console output", out[0]["note"])

    def test_a_vm_made_before_it_asked_says_new_clusters_will(self):
        self.objects[("/api/v1/namespaces/lab/pods", JL._q("vm.kubevirt.io/name=c-server-1"))] = {"items": [
            self.pod("virt-launcher-c-server-1-abcde")]}
        self.objects[("/apis/kubevirt.io/v1/kubevirts", "")] = {"items": [
            {"status": {"observedKubeVirtVersion": "v1.4.0"}}]}
        self.objects[("/apis/kubevirt.io/v1/namespaces/lab/virtualmachines/c-server-1", "")] = {
            "spec": {"template": {"spec": {"domain": {"devices": {}}}}}}
        out = JL.k3s_cluster({"ref": {"namespace": "lab", "nodes": [
            {"name": "c-server-1", "role": "server", "address": "192.0.2.60"}]}})
        self.assertIn("clusters made from now on do", out[0]["note"])

    def test_a_job_shows_its_newest_pods_output(self):
        self.objects[("/api/v1/namespaces/lab/pods", JL._q("job-name=homestead-import-frigate"))] = {"items": [
            self.pod("homestead-import-frigate-x1")]}
        self.logs["/api/v1/namespaces/lab/pods/homestead-import-frigate-x1/log"] = "==> step 1/2 config\n"
        out = JL.job_output("lab", "homestead-import-frigate", "Copy")
        self.assertEqual(("Copy", "==> step 1/2 config\n"), (out[0]["title"], out[0]["text"]))

    def test_every_kind_that_runs_a_pod_has_a_reader(self):
        ops = type("Ops", (), {"LOGGERS": {}})()
        JL.register(ops)
        for kind in ("import", "protect-run", "helm", "restructure", "reclass", "self-data-move",
                     "vm-disk-import", "image-cleanup", "deployment", "image-update", "k3s-cluster"):
            self.assertIn(kind, ops.LOGGERS)


if __name__ == "__main__":
    unittest.main()
