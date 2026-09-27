import base64
import copy
import unittest
from unittest import mock

import test_numa_evidence as fixtures
import homestead_allocation_evidence as evidence
import homestead_allocation_probe as probe


class AllocationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.node, self.objects = fixtures.host(), fixtures.objects()
        self.pod = self.objects["/api/v1/namespaces/lab/pods/probe-1"]
        self.ds = self.objects["/apis/apps/v1/namespaces/lab/daemonsets/" + probe.NAMES.NODEPROBE]
        annotations = {probe.ANNOTATION: "/var/lib/kubelet/pod-resources", "homestead.io/allocation-key-uid": "key-uid"}
        container = {"name": "allocation", "image": "test:1", "command": ["python3", "helper"],
                     "env": [{"name": "NODE", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}]}
        self.ds["spec"] = {"template": {"metadata": {"annotations": annotations}, "spec": {"containers": [container]}}}
        self.pod["metadata"]["annotations"] = copy.deepcopy(annotations)
        self.pod["spec"]["containers"] = [copy.deepcopy(container)]
        self.pod["spec"]["containers"][0]["env"][0]["valueFrom"]["fieldRef"]["apiVersion"] = "v1"
        self.pod["status"].update(podIP="10.42.0.3", containerStatuses=[{"name": "allocation", "ready": True}])
        self.host = {"metadata": {"name": "node1", "uid": "node-uid", "resourceVersion": "3"},
                     "status": {"nodeInfo": {"bootID": fixtures.BOOT, "kubeletVersion": "v1.32.4+k3s1"}}}
        self.config = {"kubeletconfig": {"cpuManagerPolicy": "static", "memoryManagerPolicy": "Static",
                        "topologyManagerScope": "pod", "topologyManagerPolicy": "single-numa-node", "private": "secret-value"}}
        self.secret = {"metadata": {"name": probe.KEY_NAME, "namespace": "lab", "uid": "key-uid", "resourceVersion": "1",
                       "ownerReferences": [{"apiVersion": "apps/v1", "kind": "DaemonSet", "uid": "ds-uid"}]},
                       "data": {"key": base64.b64encode(("ab" * 32).encode()).decode()}}
        self.objects.update({"/api/v1/nodes/node1": self.host, "/api/v1/nodes/node1/proxy/configz": self.config,
                             "/api/v1/namespaces/lab/secrets/" + probe.KEY_NAME: self.secret})
        self.data = {"schema": 1, "protocol": "podresources.v1", "complete": True, "node": "node1", "pod_uid": "probe-uid",
                     "boot_id": fixtures.BOOT, "sampled_at": fixtures.NOW, "allocatable_cpu_ids": [0, 1], "allocated_cpu_ids": [],
                     "unallocated_cpu_ids": [0, 1], "allocatable_memory": [], "pods": [], "dynamic_resources_present": False}
        self.transport = mock.Mock(side_effect=lambda *args: copy.deepcopy(self.data))

    def inspect(self, read=None):
        return evidence.inspect(self.node, read or (lambda path: copy.deepcopy(self.objects[path])), transport=self.transport, now=fixtures.NOW)

    def test_authentication_provenance_policy_and_api_defaults(self):
        result = self.inspect()
        self.assertTrue(result["verified"], result)
        self.assertEqual("static", result["policy"]["cpuManagerPolicy"])
        self.assertNotIn("secret-value", str(result))
        self.assertNotIn("ab" * 32, str(result))
        self.assertEqual(3, len(result["dependencies"]))
        self.assertEqual("probe-uid", self.transport.call_args.args[2]["pod_uid"])

    def test_node_probe_secret_and_boot_replacements_are_rejected(self):
        for obj, key in ((self.host["metadata"], "uid"), (self.host["status"]["nodeInfo"], "bootID"),
                         (self.secret["metadata"], "uid"), (self.pod["metadata"]["annotations"], "homestead.io/allocation-key-uid")):
            before = obj[key]; obj[key] = "replaced"
            self.assertFalse(self.inspect()["verified"])
            obj[key] = before

    def test_unknown_build_or_unready_collector_never_calls_transport(self):
        self.host["status"]["nodeInfo"]["kubeletVersion"] = "v1.99.0-vendor"
        self.assertFalse(self.inspect()["verified"])
        self.transport.assert_not_called()
        self.host["status"]["nodeInfo"]["kubeletVersion"] = "v1.32.4+rke2r1"
        self.pod["status"]["containerStatuses"][0]["ready"] = False
        self.assertFalse(self.inspect()["verified"])
        self.transport.assert_not_called()

    def test_wrong_response_identity_age_and_cpu_arithmetic_are_unknown(self):
        for key, value in (("pod_uid", "old"), ("boot_id", "old"), ("node", "other"), ("sampled_at", fixtures.NOW - 6),
                           ("sampled_at", float("nan")), ("allocatable_cpu_ids", [0, 1, 2]), ("unallocated_cpu_ids", [0]),
                           ("allocated_cpu_ids", [0]), ("complete", False), ("pods", None)):
            before = self.data[key]; self.data[key] = value
            self.assertFalse(self.inspect()["verified"], (key, value))
            self.data[key] = before

    def test_policy_or_host_boot_changed_during_rpc_is_unknown(self):
        def change(*args):
            self.config["kubeletconfig"]["cpuManagerPolicy"] = "none"
            return self.data
        self.transport.side_effect = change
        self.assertFalse(self.inspect()["verified"])

    def test_private_errors_are_redacted(self):
        self.transport.side_effect = RuntimeError("secret-value")
        result = self.inspect()
        self.assertFalse(result["verified"])
        self.assertNotIn("secret-value", str(result))

    def test_duplicate_memory_capacity_and_cached_policy_cannot_authorize(self):
        row = {"type": "memory", "bytes": 1024, "nodes": [0]}
        self.data["allocatable_memory"] = [row, row]
        self.assertFalse(self.inspect()["verified"])
        self.data["allocatable_memory"] = []
        cached = mock.Mock(side_effect=AssertionError("cached reader must not be used"))
        cached.fresh = lambda path: copy.deepcopy(self.objects[path])
        self.assertTrue(self.inspect(cached)["verified"])
        cached.assert_not_called()

    def test_bad_response_mac_and_proxy_redirects_are_refused(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"complete":true}'
        response.headers = {"X-Homestead-Allocation": "not-authenticated"}
        opener = mock.Mock(); opener.open.return_value = response
        with mock.patch.object(evidence.urllib.request, "build_opener", return_value=opener) as build:
            with self.assertRaises(ValueError):
                evidence.exchange("10.42.0.3", "ab" * 32, evidence.AUTH.request("node", "pod"))
        self.assertIsInstance(build.call_args.args[1], evidence.NoRedirect)
        self.assertEqual({}, build.call_args.args[0].proxies)
        self.assertIsNone(evidence.NoRedirect().redirect_request(None, None, None, None, None, None))


if __name__ == "__main__":
    unittest.main()
