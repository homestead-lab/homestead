"""Wire-level tests use a temporary fake kubelet socket, never a live host."""
import os
import sys
import tempfile
import time
import unittest
from concurrent import futures
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/probe"))
import allocation
import grpc
import podresources_pb2 as api


def fixtures():
    capacity = api.AllocatableResourcesResponse(cpu_ids=[0, 1, 2, 3])
    listing = api.ListPodResourcesResponse()
    pod = listing.pod_resources.add(namespace="lab", name="guest")
    pod.containers.add(name="compute", cpu_ids=[0, 1])
    return capacity, listing


class AllocationNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.capacity, self.listing = fixtures()

    def normalize(self):
        return allocation.normalize(self.capacity, self.listing)

    def test_subtracts_all_users_without_workload_name_credit(self):
        result = self.normalize()
        self.assertEqual([0, 1], result["allocated_cpu_ids"])
        self.assertEqual([2, 3], result["unallocated_cpu_ids"])

    def test_init_container_reuse_is_counted_once(self):
        self.listing.pod_resources[0].containers.add(name="init", cpu_ids=[0])
        self.assertEqual([2, 3], self.normalize()["unallocated_cpu_ids"])

    def test_cross_pod_overlap_is_not_accepted(self):
        pod = self.listing.pod_resources.add(namespace="lab", name="other")
        pod.containers.add(name="compute", cpu_ids=[1])
        with self.assertRaisesRegex(allocation.EvidenceError, "overlap"):
            self.normalize()

    def test_out_of_pool_allocation_is_unknown(self):
        self.listing.pod_resources[0].containers[0].cpu_ids.append(8)
        with self.assertRaisesRegex(allocation.EvidenceError, "allocatable pool"):
            self.normalize()

    def test_duplicate_and_out_of_range_ids_are_invalid(self):
        for ids in ([0, 0], [-1], [8192]):
            with self.subTest(ids=ids):
                self.capacity.cpu_ids[:] = ids
                with self.assertRaises(allocation.EvidenceError):
                    self.normalize()

    def test_duplicate_identity_is_not_merged(self):
        self.listing.pod_resources.add().CopyFrom(self.listing.pod_resources[0])
        with self.assertRaisesRegex(allocation.EvidenceError, "identity"):
            self.normalize()

    def test_unknown_top_level_and_nested_fields_fail_closed(self):
        for target in (self.capacity, self.listing, self.listing.pod_resources[0],
                       self.listing.pod_resources[0].containers[0]):
            with self.subTest(type=type(target).__name__):
                target.MergeFromString(b"\xa0\x06\x01")  # unknown field 100
                with self.assertRaisesRegex(allocation.EvidenceError, "unsupported fields"):
                    self.normalize()
                target.DiscardUnknownFields()

    def test_multi_node_memory_is_not_divided_or_assumed_local(self):
        memory = self.capacity.memory.add(memory_type="hugepages-2Mi", size=4194304)
        memory.topology.nodes.add(ID=0)
        memory.topology.nodes.add(ID=1)
        self.assertEqual([{"type": "hugepages-2Mi", "bytes": 4194304, "nodes": [0, 1]}],
                         self.normalize()["allocatable_memory"])

    def test_missing_memory_locality_stays_explicit(self):
        self.capacity.memory.add(memory_type="memory", size=1024)
        self.assertEqual([], self.normalize()["allocatable_memory"][0]["nodes"])

    def test_unknown_memory_type_and_oversized_inventory_are_rejected(self):
        self.capacity.memory.add(memory_type="future", size=1024)
        with self.assertRaises(allocation.EvidenceError):
            self.normalize()
        self.capacity.ClearField("memory")
        with mock.patch.object(allocation, "MAX_BYTES", 1):
            with self.assertRaisesRegex(allocation.EvidenceError, "size limit"):
                self.normalize()

    def test_dra_is_explicit_but_not_claimed_supported(self):
        self.listing.pod_resources[0].containers[0].dynamic_resources.add(claim_name="private-claim")
        result = self.normalize()
        self.assertTrue(result["dynamic_resources_present"])
        self.assertNotIn("private-claim", str(result))

    def test_absent_socket_and_invalid_options_never_become_free_capacity(self):
        for path, timeout in (("/does/not/exist", 1), ("relative", 1), ("/abs", True),
                              ("/abs", float("nan")), ("/abs", 6)):
            result = allocation.snapshot(path, timeout=timeout)
            self.assertFalse(result["complete"])
            self.assertIsNone(result["unallocated_cpu_ids"])

    def test_regular_file_is_not_contacted(self):
        with tempfile.NamedTemporaryFile() as handle:
            result = allocation.snapshot(handle.name)
        self.assertFalse(result["complete"])
        self.assertIn("not a local Unix socket", result["reason"])


@unittest.skipIf(os.name == "nt", "gRPC Unix sockets require Linux; CI exercises the real transport")
class AllocationSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="podres-")
        self.addCleanup(self.temp.cleanup)
        self.socket = str(Path(self.temp.name) / "kubelet.sock")
        self.capacity, self.listing = fixtures()
        self.calls = []
        self.responses = {}
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        methods = {}
        for name, request in (("List", api.ListPodResourcesRequest),
                              ("GetAllocatableResources", api.AllocatableResourcesRequest)):
            def invoke(req, context, name=name):
                self.calls.append(name)
                handler = self.responses.get(name)
                if handler:
                    return handler(context)
                return self.listing if name == "List" else self.capacity
            methods[name] = grpc.unary_unary_rpc_method_handler(invoke,
                request_deserializer=request.FromString,
                response_serializer=lambda value: value if isinstance(value, bytes) else value.SerializeToString())
        self.server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("v1.PodResourcesLister", methods),))
        self.assertNotEqual(0, self.server.add_insecure_port("unix:" + self.socket))
        self.server.start()
        self.addCleanup(lambda: self.server.stop(0).wait(5))

    def test_stable_bracket_uses_only_four_read_only_calls(self):
        result = allocation.snapshot(self.socket)
        self.assertTrue(result["complete"], result)
        self.assertEqual([2, 3], result["unallocated_cpu_ids"])
        self.assertEqual(["GetAllocatableResources", "List", "List", "GetAllocatableResources"], self.calls)
        self.assertLess(abs(result["sampled_at"] - time.time()), 2)

    def test_changed_allocations_require_new_observation(self):
        def change(context):
            if self.calls.count("List") == 2:
                self.listing.pod_resources[0].containers[0].cpu_ids.append(2)
            return self.listing
        self.responses["List"] = change
        result = allocation.snapshot(self.socket)
        self.assertFalse(result["complete"])
        self.assertIn("changed", result["reason"])
        self.assertIsNone(result["pods"])

    def test_unimplemented_rpc_is_redacted_and_not_retried(self):
        self.responses["GetAllocatableResources"] = lambda ctx: ctx.abort(grpc.StatusCode.UNIMPLEMENTED, "private-host-data")
        result = allocation.snapshot(self.socket)
        self.assertFalse(result["complete"])
        self.assertNotIn("private-host-data", str(result))
        self.assertEqual(["GetAllocatableResources"], self.calls)

    def test_malformed_and_oversized_wire_responses_are_unknown(self):
        for payload in (b"\x0a\xff", b"x" * (allocation.MAX_BYTES + 1)):
            with self.subTest(size=len(payload)):
                self.responses["List"] = lambda ctx: payload
                result = allocation.snapshot(self.socket)
                self.assertFalse(result["complete"])
                self.assertIsNone(result["allocated_cpu_ids"])

    def test_total_deadline_is_bounded(self):
        def stall(context):
            time.sleep(.25)
            return self.capacity
        self.responses["GetAllocatableResources"] = stall
        started = time.monotonic()
        result = allocation.snapshot(self.socket, timeout=.05)
        self.assertFalse(result["complete"])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(["GetAllocatableResources"], self.calls)

    def test_socket_replacement_invalidates_snapshot(self):
        original = os.lstat(self.socket)
        replacement = mock.Mock(st_mode=original.st_mode, st_dev=original.st_dev, st_ino=original.st_ino + 1)
        with mock.patch.object(allocation.os, "lstat", side_effect=[original, replacement]):
            result = allocation.snapshot(self.socket)
        self.assertFalse(result["complete"])
        self.assertIn("changed", result["reason"])

    def test_symlink_socket_is_rejected(self):
        link = str(Path(self.temp.name) / "link.sock")
        os.symlink(self.socket, link)
        result = allocation.snapshot(link)
        self.assertFalse(result["complete"])
        self.assertEqual([], self.calls)
