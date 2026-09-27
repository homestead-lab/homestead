"""Read-only kubelet PodResources v1 snapshots over its local Unix socket.

No checkpoint files, Kubernetes credentials, shell commands, TCP listener or
arbitrary RPC methods. Policy/host identity and NUMA fit are separate checks.
The protobuf schema is derived from Kubernetes v1.32.0; unknown fields fail
closed so newer pod-level allocations cannot silently disappear from counts.
"""
import os
import re
import stat
import time

MAX_BYTES = 4 * 1024**2
MAX_CPUS = 8192
MAX_ROWS = 8192
SOCKET = "/pod-resources/kubelet.sock"
DNS = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


class EvidenceError(ValueError):
    pass


def _known(message):
    if message.ByteSize() > MAX_BYTES:
        raise EvidenceError("Allocation response exceeds the size limit")
    stripped = type(message)()
    stripped.CopyFrom(message)
    stripped.DiscardUnknownFields()
    if stripped.SerializeToString(deterministic=True) != message.SerializeToString(deterministic=True):
        raise EvidenceError("Kubelet allocation protocol contains unsupported fields")


def _ids(values):
    if len(values) > MAX_CPUS or any(not 0 <= value < MAX_CPUS for value in values) or len(set(values)) != len(values):
        raise EvidenceError("Kubelet CPU/topology identifiers are invalid")
    return set(values)


def _memory(rows):
    if len(rows) > MAX_ROWS:
        raise EvidenceError("Kubelet memory allocation inventory exceeds its limit")
    result = []
    for row in rows:
        if row.memory_type != "memory" and not re.fullmatch(r"hugepages-[1-9][0-9]{0,12}(?:Ki|Mi|Gi|Ti|k|M|G|T)?", row.memory_type):
            raise EvidenceError("Kubelet memory allocation type is unsupported")
        if row.size > 2**53 - 1 or len(row.topology.nodes) > 256:
            raise EvidenceError("Kubelet memory allocation is out of range")
        nodes = sorted(_ids([node.ID for node in row.topology.nodes]))
        # Empty/multiple node sets stay explicit: never divide an allocation
        # across NUMA cells or invent locality the kubelet did not report.
        result.append({"type": row.memory_type, "bytes": row.size, "nodes": nodes})
    return sorted(result, key=lambda row: (row["type"], row["nodes"], row["bytes"]))


def normalize(allocatable, listing):
    """CPU IDs are allocator capacity minus *all* observed exclusive users.

    No resources are credited back by workload name. PodResources v1 names do
    not carry Pod UIDs and cannot authorize deleting or releasing a workload.
    """
    _known(allocatable)
    _known(listing)
    cpus = _ids(allocatable.cpu_ids)
    if len(listing.pod_resources) > 4096:
        raise EvidenceError("Kubelet pod allocation inventory exceeds its limit")
    taken, seen, rows, count = set(), set(), [], 0
    dynamic = False
    for pod in listing.pod_resources:
        key = (pod.namespace, pod.name)
        if any(not DNS.fullmatch(name) for name in key) or key in seen:
            raise EvidenceError("Kubelet pod allocation identity is invalid or duplicated")
        seen.add(key)
        count += len(pod.containers)
        if count > MAX_ROWS:
            raise EvidenceError("Kubelet container allocation inventory exceeds its limit")
        names, containers, pod_cpus = set(), [], set()
        for container in pod.containers:
            if not DNS.fullmatch(container.name) or container.name in names:
                raise EvidenceError("Kubelet container allocation identity is invalid or duplicated")
            names.add(container.name)
            allocated = _ids(container.cpu_ids)
            if not allocated <= cpus:
                raise EvidenceError("Kubelet CPU allocations do not match its allocatable pool")
            pod_cpus.update(allocated)
            dynamic |= bool(container.dynamic_resources)
            containers.append({"name": container.name, "cpu_ids": sorted(allocated), "memory": _memory(container.memory)})
        # Init/app containers in one Pod may legitimately reuse CPUs; two
        # different Pods cannot own the same exclusive CPU in this contract.
        if taken & pod_cpus:
            raise EvidenceError("Kubelet exclusive CPU allocations overlap across Pods")
        taken.update(pod_cpus)
        rows.append({"namespace": pod.namespace, "name": pod.name,
                     "containers": sorted(containers, key=lambda row: row["name"])})
    return {"allocatable_cpu_ids": sorted(cpus), "allocated_cpu_ids": sorted(taken),
            "unallocated_cpu_ids": sorted(cpus - taken), "allocatable_memory": _memory(allocatable.memory),
            "pods": sorted(rows, key=lambda row: (row["namespace"], row["name"])),
            "dynamic_resources_present": dynamic}


def snapshot(socket_path=SOCKET, *, timeout=3.0):
    """One bounded four-call observation, with no retry or mutation.

    A stable bracket is not an atomic reservation. GetAllocatableResources can
    be stale after CPU hotplug until kubelet restarts; consumers must verify
    policy, boot/online topology and ownership before planning placement.
    """
    result = {"schema": 1, "protocol": "podresources.v1", "complete": False,
              "reason": "Kubelet allocation service is unavailable or unsupported",
              "sampled_at": None, "allocatable_cpu_ids": None, "allocated_cpu_ids": None,
              "unallocated_cpu_ids": None, "allocatable_memory": None, "pods": None}
    try:
        if type(timeout) not in (int, float) or not 0 < timeout <= 5:
            raise EvidenceError("Allocation observation timeout is invalid")
        if not isinstance(socket_path, str) or not os.path.isabs(socket_path) or "\x00" in socket_path:
            raise EvidenceError("Allocation source must be a local Unix socket")
        source = os.lstat(socket_path)
        if not stat.S_ISSOCK(source.st_mode):
            raise EvidenceError("Allocation source is not a local Unix socket")
        # Lazy imports preserve the ordinary probe's stdlib-only execution.
        import grpc
        import podresources_pb2 as api
        deadline = time.monotonic() + timeout
        options = [("grpc.max_receive_message_length", MAX_BYTES), ("grpc.max_send_message_length", 1024),
                   ("grpc.enable_retries", 0)]
        with grpc.insecure_channel("unix:" + socket_path, options=options) as channel:
            allocated = channel.unary_unary("/v1.PodResourcesLister/GetAllocatableResources",
                request_serializer=api.AllocatableResourcesRequest.SerializeToString,
                response_deserializer=api.AllocatableResourcesResponse.FromString)
            listed = channel.unary_unary("/v1.PodResourcesLister/List",
                request_serializer=api.ListPodResourcesRequest.SerializeToString,
                response_deserializer=api.ListPodResourcesResponse.FromString)
            def call(method, request):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EvidenceError("Allocation observation timed out")
                return method(request, timeout=remaining, wait_for_ready=False)
            capacity_before = call(allocated, api.AllocatableResourcesRequest())
            users_before = call(listed, api.ListPodResourcesRequest())
            users_after = call(listed, api.ListPodResourcesRequest())
            capacity_after = call(allocated, api.AllocatableResourcesRequest())
        first, last = normalize(capacity_before, users_before), normalize(capacity_after, users_after)
        after = os.lstat(socket_path)
        if not stat.S_ISSOCK(after.st_mode) or (source.st_dev, source.st_ino) != (after.st_dev, after.st_ino) or first != last:
            raise EvidenceError("Kubelet allocations changed during observation; request a fresh review")
        result.update(last, complete=True, sampled_at=time.time(),
                      reason="Observed allocation snapshot, not a reservation; kubelet policy and host topology still need verification")
    except EvidenceError as error:
        result["reason"] = str(error)  # only static messages raised above
    except Exception:
        # gRPC statuses, socket errors and response bodies may contain private
        # host or workload details. Never reflect those through the helper.
        pass
    return result
