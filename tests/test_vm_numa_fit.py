import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_vm_numa_fit as fit

GIB = 1024**3


def snapshot():
    return {"verified": True, "reason": "verified", "dependencies": {}, "host": {"uid": "host"},
            "policy": {"fingerprint": "policy", "cpuManagerPolicy": "static", "memoryManagerPolicy": "Static",
                       "topologyManagerPolicy": "single-numa-node", "topologyManagerScope": "pod"},
            "physical": {"page_pools": {"2097152": {"reserved": 0}}, "cells": [
                {"id": n, "online_cpus": list(range(n * 4, n * 4 + 4)), "cores": [[n * 4, n * 4 + 1], [n * 4 + 2, n * 4 + 3]],
                 "hugepages": {"2097152": {"free": 2048, "total": 2048, "surplus": 0}}} for n in (0, 1)]},
            "allocation": {"unallocated_cpu_ids": list(range(8)), "pods": [], "allocatable_memory": [
                {"type": kind, "bytes": 4 * GIB, "nodes": [node]} for node in (0, 1) for kind in ("memory", "hugepages-2Mi")]}}


def spec():
    return {"containers": [{"name": "vm", "resources": {"requests": {"cpu": "2", "memory": "128Mi", "hugepages-2Mi": "2Gi"}}}]}


class NUMAFitTests(unittest.TestCase):
    def setUp(self):
        self.evidence, self.spec = snapshot(), spec()

    def options(self, pods=None):
        return fit.options(self.evidence, self.spec, pods or [], "node1")

    def test_cpu_and_pages_must_fit_together(self):
        self.assertEqual(2, len(self.options()["options"]))
        self.evidence["allocation"]["unallocated_cpu_ids"] = [0, 1]
        self.evidence["physical"]["cells"][0]["hugepages"]["2097152"]["free"] = 0
        self.assertEqual([], self.options()["options"])

    def test_manager_allocations_and_physical_reservations_are_independent_caps(self):
        self.evidence["allocation"]["pods"] = [{"containers": [{"memory": [{"type": "hugepages-2Mi", "bytes": 3 * GIB, "nodes": [0, 1]}]}]}]
        self.assertEqual([], self.options()["options"])
        self.evidence["allocation"]["pods"] = []
        self.evidence["physical"]["page_pools"]["2097152"]["reserved"] = 1025
        self.assertEqual([], self.options()["options"])

    def test_manager_capacity_without_locality_is_not_divided(self):
        self.evidence["allocation"]["allocatable_memory"][0]["nodes"] = [0, 1]
        self.assertEqual([], self.options()["options"])

    def test_pending_and_previously_planned_vms_consume_local_capacity(self):
        first = self.options()["options"][0]
        pending = {"spec": {**spec(), "nodeName": "node1"}, "status": {"phase": "Pending"}, "_homestead_numa": first}
        self.assertEqual(2, len(self.options([pending])["options"]))
        self.assertEqual([1], [o["cell"] for o in self.options([pending, pending])["options"]])
        unassigned = {"spec": spec(), "status": {"phase": "Pending"}}
        self.assertEqual([], self.options([unassigned] * 3)["options"])

    def test_smt_requires_complete_free_cores_when_configured(self):
        self.evidence["policy"]["cpuManagerPolicyOptions"] = {"full-pcpus-only": "true"}
        self.evidence["allocation"]["unallocated_cpu_ids"] = [0, 2, 4, 6]
        self.assertEqual([], self.options()["options"])

    def test_policy_unknowns_and_incomplete_evidence_do_not_allow_start(self):
        for key, value in (("cpuManagerPolicy", "none"), ("memoryManagerPolicy", "None"),
                           ("topologyManagerScope", "container"), ("topologyManagerPolicy", "best-effort"),
                           ("cpuManagerPolicyOptions", {"future": "true"})):
            original = copy.deepcopy(self.evidence["policy"])
            self.evidence["policy"][key] = value
            self.assertEqual([], self.options()["options"])
            self.evidence["policy"] = original
        self.evidence["verified"] = False
        self.assertEqual([], self.options()["options"])

    def test_input_is_unchanged(self):
        before = copy.deepcopy(self.evidence), copy.deepcopy(self.spec)
        self.options()
        self.assertEqual(before, (self.evidence, self.spec))

    def test_running_guaranteed_workload_missing_from_inventory_is_unknown(self):
        pod = {"metadata": {"name": "running", "namespace": "lab"}, "spec": {**spec(), "nodeName": "node1"},
               "status": {"phase": "Running", "qosClass": "Guaranteed"}}
        result = self.options([pod])
        self.assertEqual([], result["options"])
        self.assertIn("missing", result["reason"])

    def test_device_locality_cannot_be_inferred_from_device_counts(self):
        self.spec["containers"][0]["resources"]["requests"]["vendor/gpu"] = "1"
        self.assertIn("Device locality", self.options()["reason"])
        self.assertEqual([], self.options()["options"])
