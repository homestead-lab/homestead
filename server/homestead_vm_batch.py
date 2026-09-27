"""Joint VM admission, including created controllers waiting for launchers.

Read-only. Prepared manifests stay in memory; the returned plan contains no
cloud-init or Secret data. A fitting example is not enforced placement.
"""
import copy

import homestead_batch_capacity as BATCH
import homestead_vm_capacity as CAPACITY
import homestead_vm_claims as CLAIMS
import homestead_vm_resources as RESOURCES
import homestead_place as PLACE


def contains(actual, expected):
    """Permit API defaults, never a changed submitted value/list membership."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and contains(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(contains(a, e) for a, e in zip(actual, expected))
    return actual == expected


def plan(prepared, read, nodes, *, created=None, threshold=88):
    if not prepared or len(prepared) > 9:
        raise ValueError("A VM cluster batch must have between one and nine VMs")
    namespace = prepared[0]["namespace"]
    if any(item["namespace"] != namespace for item in prepared):
        raise ValueError("A VM batch must use one namespace")
    if len({item["name"] for item in prepared}) != len(prepared):
        raise ValueError("VM batch names must be unique")
    created = created or {}
    pods = copy.deepcopy(CAPACITY._items(read, "/api/v1/pods"))
    configurations = CAPACITY._items(read, "/apis/kubevirt.io/v1/kubevirts")
    if len(configurations) != 1 or configurations[0].get("metadata", {}).get("deletionTimestamp"):
        raise ValueError("A single active KubeVirt configuration is required for VM batch admission")
    configuration = (configurations[0].get("spec") or {}).get("configuration") or {}
    kubevirt_version = RESOURCES.CPU.observed_version(configurations[0])
    claims, entries, warnings, blockers = {}, [], set(), []
    headroom = {}
    for item in prepared:
        vm, name = item["vm"], item["name"]
        definitions = CLAIMS.plans(vm, read, item["claims"], item["downloads"])
        if set(claims) & set(definitions):
            raise ValueError("VM batch disks must have distinct names")
        claims.update(definitions)
        model = RESOURCES.project(vm, configuration, read=read, kubevirt_version=kubevirt_version)
        blockers.extend(f"{name}: {text}" for text in model["blockers"])
        warnings.update(model["warnings"])
        count = 1
        receipt = created.get(name)
        if receipt:
            current = read(f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{name}")
            meta = current.get("metadata") or {}
            if (meta.get("uid") != receipt.get("uid") or meta.get("deletionTimestamp") or
                    not contains(current.get("spec"), vm["spec"])):
                raise ValueError(f"Created VM {name} changed during the batch; inspect it before creating more VMs")
            vmi = CAPACITY._optional(read, f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}")
            owned, known = RESOURCES.launchers(current, vmi, pods)
            if vmi and not known:
                raise ValueError(f"Created VM {name} launcher ownership cannot be verified")
            if vmi and any(not all(RESOURCES.identity(obj).values()) for obj in [vmi, *owned]):
                raise ValueError(f"Created VM {name} launcher identity/version is incomplete")
            if vmi and (vmi.get("metadata", {}).get("deletionTimestamp") or
                        ((vmi.get("status") or {}).get("migrationState") or {}).get("startTimestamp")):
                raise ValueError(f"Created VM {name} is terminating or migrating; stop and review the remaining batch")
            scheduled = [pod for pod in owned if pod.get("spec", {}).get("nodeName") and
                         not pod.get("metadata", {}).get("deletionTimestamp")]
            if len(scheduled) > 1 or any(pod.get("metadata", {}).get("deletionTimestamp") for pod in owned):
                raise ValueError(f"Created VM {name} launcher transition is not stable")
            count = 0 if scheduled else 1
            for pod in scheduled:
                host = pod["spec"]["nodeName"]
                estimate = max(model["memory_estimate_bytes"], PLACE._pod_memory(pod["spec"])[0])
                headroom[host] = headroom.get(host, 0) + max(0, estimate - PLACE._pod_request(pod["spec"], "memory")) / 1024**3
            # Replace only verified, unassigned launchers with a synthetic
            # demand. No launcher yet still means one VM owed, not free RAM.
            pending = {pod["metadata"]["uid"] for pod in owned if not pod.get("spec", {}).get("nodeName")}
            pods = [pod for pod in pods if pod.get("metadata", {}).get("uid") not in pending]
        else:
            single = CAPACITY.plan(vm, read, nodes, action="create", planned_claims=definitions, warning_percent=threshold)
            blockers.extend(f"{name}: {text}" for text in single["blockers"])
            warnings.update(single["warnings"])
        entries.append({"name": name, "deployment": model["manifest"], "replicas": count,
                        "workload_kind": "vm", "memory_estimate_bytes": model["memory_estimate_bytes"]})
    nodes = copy.deepcopy(nodes)
    for node in nodes:
        node["batch_starting_headroom_gb"] = headroom.get(node["name"], 0)
    result = BATCH.plan(entries, namespace, pods, nodes, claims, threshold, read=read)
    result["blockers"] = sorted(set(blockers))
    if blockers:
        result.update(blocked=True, status="blocked")
    result["warnings"] = sorted(set(result["warnings"]) | warnings | {
        "Image importers and launcher sidecars can need additional resources; RAM requests are lower bounds and version-uncertain IO-thread CPU uses conservative planning, not guaranteed capacity.",
        "Three guest servers provide guest-level quorum only. Shared physical hosts/storage can still fail together; placement examples are not enforced."})
    result["vm_count"] = len(prepared)
    result["created_count"] = len(created)
    return result
