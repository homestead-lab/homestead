import copy
import unittest

import test_vm_resources as fixtures
import homestead_numa_evidence as evidence
import homestead_names as names

NOW = 1000
BOOT = "12345678-1234-1234-1234-123456789abc"


def host():
    return {"name": "node1", "uid": "node-uid", "boot_id": BOOT, "temps": {
        "numa_source": {"pod": {"namespace": "lab", "name": "probe-1", "uid": "probe-uid"}, "received_at": NOW},
        "numa": {"schema": 1, "complete": True, "node": "node1", "boot_id": BOOT, "sampled_at": NOW,
                 "cells": [{"id": 0, "online_cpus": [0, 1], "cores": [[0, 1]],
                            "hugepages": {"2097152": {"total": 2048, "free": 1024, "surplus": 0}}}],
                 "page_pools": {"2097152": {"reserved": 4}}}}}


def objects():
    ds = {"metadata": {"name": names.NODEPROBE, "namespace": "lab", "uid": "ds-uid", "resourceVersion": "1"}}
    pod = fixtures.child(ds, "DaemonSet", "probe-1", "probe-uid")
    pod["metadata"]["ownerReferences"][0]["apiVersion"] = "apps/v1"
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    return {"/api/v1/namespaces/lab/pods/probe-1": pod,
            "/apis/apps/v1/namespaces/lab/daemonsets/" + names.NODEPROBE: ds}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.node = host()
        self.sample = self.node["temps"]["numa"]
        self.source = self.node["temps"]["numa_source"]
        self.objects = objects()
        self.pod = self.objects["/api/v1/namespaces/lab/pods/probe-1"]
        self.ds = self.objects["/apis/apps/v1/namespaces/lab/daemonsets/" + names.NODEPROBE]

    def inspect(self):
        return evidence.inspect(self.node, lambda path: self.objects[path], now=NOW)

    def test_verified_means_physical_only_with_identity_dependencies(self):
        original = copy.deepcopy(self.node)
        self.sample["private"] = "session-secret"
        result = self.inspect()
        self.assertTrue(result["verified"], result)
        self.assertIn("remain unverified", result["reason"])
        self.assertEqual("node-uid", result["host"]["uid"])
        self.assertEqual(2, len(result["dependencies"]))
        self.assertNotIn("session-secret", str(result))
        self.sample.pop("private")
        self.assertEqual(original, self.node)

    def test_wrong_host_boot_or_missing_identity_is_unknown(self):
        for target, key in ((self.node, "uid"), (self.node, "boot_id"), (self.sample, "node"), (self.sample, "boot_id")):
            saved = target[key]
            target[key] = "" if key == "uid" else "different"
            self.assertFalse(self.inspect()["verified"])
            target[key] = saved

    def test_clock_age_invalid_numbers_and_future_samples(self):
        for target, key in ((self.sample, "sampled_at"), (self.source, "received_at")):
            for value in (NOW - 61, NOW + 6, None, True, "1000", float("nan"), float("inf")):
                target[key] = value
                self.assertFalse(self.inspect()["verified"], value)
            target[key] = NOW
        self.sample["sampled_at"] = NOW - 60
        self.assertTrue(self.inspect()["verified"])

    def test_current_pod_not_labels_or_name_alone(self):
        for key, value in (("uid", "replacement"), ("deletionTimestamp", "now"), ("resourceVersion", "")):
            original = copy.deepcopy(self.pod["metadata"])
            self.pod["metadata"][key] = value
            self.assertFalse(self.inspect()["verified"])
            self.pod["metadata"] = original
        self.pod["spec"]["nodeName"] = "elsewhere"
        self.assertFalse(self.inspect()["verified"])
        self.pod["spec"]["nodeName"] = "node1"
        self.pod["status"]["conditions"] = []
        self.assertFalse(self.inspect()["verified"])

    def test_current_daemonset_owner_and_namespace_must_match(self):
        for key, value in (("uid", "replacement"), ("namespace", "elsewhere"), ("deletionTimestamp", "now")):
            original = copy.deepcopy(self.ds["metadata"])
            self.ds["metadata"][key] = value
            self.assertFalse(self.inspect()["verified"])
            self.ds["metadata"] = original
        self.pod["metadata"]["ownerReferences"] *= 2
        self.assertFalse(self.inspect()["verified"])

    def test_unsupported_schema_and_incomplete_payload(self):
        for value in (2, True, "1", None):
            self.sample["schema"] = value
            self.assertFalse(self.inspect()["verified"])
        self.sample["schema"] = 1
        self.sample["complete"] = "true"
        self.assertFalse(self.inspect()["verified"])

    def test_invalid_topology_never_becomes_empty_free_capacity(self):
        original = copy.deepcopy(self.sample["cells"])
        for key, value in (("id", -1), ("online_cpus", [0, 0]), ("online_cpus", [True]),
                           ("cores", [[0], [0, 1]]), ("cores", [[0]]), ("cores", "private-data")):
            self.sample["cells"][0][key] = value
            result = self.inspect()
            self.assertFalse(result["verified"])
            self.assertIsNone(result["sample"])
            self.assertNotIn("private-data", str(result))
            self.sample["cells"] = copy.deepcopy(original)
        self.sample["cells"] *= 2
        self.assertFalse(self.inspect()["verified"])

    def test_bad_page_counters_are_unknown_and_missing_pool_is_not_invented(self):
        for value in (True, -1, 2**53, "private-data", 9999):
            self.sample["cells"][0]["hugepages"]["2097152"]["free"] = value
            self.assertFalse(self.inspect()["verified"])
        self.sample["cells"][0]["hugepages"]["2097152"]["free"] = 1
        self.sample["page_pools"] = {}
        self.assertFalse(self.inspect()["verified"])

    def test_read_failures_never_echo_private_response_bodies(self):
        def read(path):
            raise RuntimeError("private-response")
        result = evidence.inspect(self.node, read, now=NOW)
        self.assertFalse(result["verified"])
        self.assertNotIn("private-response", str(result))


if __name__ == "__main__":
    unittest.main()
