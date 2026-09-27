"""Conservative local NUMA fit, including pending and joint-plan demand.

No name-based release credit. A fit is a current observation, not a promise
that kubelet, memory manager or a device plugin will admit the real launcher.
"""
import math

import homestead_pod_resources as R


def demand(spec):
    values = {name: R.pod_request(spec, name) for name in R.resource_names(spec)
              if name in ("cpu", "memory") or name.startswith("hugepages-")}
    out = {"cpu": math.ceil(values.get("cpu", 0) / 1000), "memory": values.get("memory", 0), "pages": {}}
    for key, value in values.items():
        if key.startswith("hugepages-"):
            size = R.quantity(key[len("hugepages-"):])
            if not size or value % size:
                raise ValueError("Hugepage request does not match its page size")
            out["pages"][str(size)] = out["pages"].get(str(size), 0) + value
    return out


def options(evidence, spec, pods, host):
    result = {"options": [], "reason": evidence.get("reason") or "Local allocation evidence is unavailable"}
    if not evidence.get("verified") or pods is None:
        return result
    try:
        policy, physical, allocated = evidence["policy"], evidence["physical"], evidence["allocation"]
        local_devices = {name for name in R.resource_names(spec) if "/" in name} - {
            "devices.kubevirt.io/kvm", "devices.kubevirt.io/tun", "devices.kubevirt.io/vhost-net"}
        if local_devices or spec.get("resourceClaims") or allocated.get("dynamic_resources_present"):
            result["reason"] = "Device locality is not verified for this NUMA placement; CPU and memory counts alone cannot authorize it"
            return result
        if policy["cpuManagerPolicy"] != "static" or policy["memoryManagerPolicy"] != "Static":
            result["reason"] = "This VM needs kubelet Static CPU and memory management to verify local capacity"
            return result
        if len(physical["cells"]) > 1 and (policy["topologyManagerPolicy"] != "single-numa-node" or policy["topologyManagerScope"] != "pod"):
            result["reason"] = "Local placement cannot be verified: multi-NUMA hosts need pod-scoped single-numa-node topology management"
            return result
        cpu_options = policy.get("cpuManagerPolicyOptions") or {}
        # Known options don't change the available pool. Unknown/new allocation
        # algorithms must be reviewed rather than silently treated as defaults.
        if not isinstance(cpu_options, dict) or set(cpu_options) - {"full-pcpus-only", "distribute-cpus-across-numa", "align-by-socket", "distribute-cpus-across-cores", "strict-cpu-reservation", "prefer-align-cpus-by-uncorecache"} or any(v not in ("true", "false") for v in cpu_options.values()):
            result["reason"] = "Kubelet CPU allocation options are not supported by this placement check"
            return result
        requested = demand(spec)
        if not requested["cpu"] or not requested["pages"]:
            raise ValueError()
        free = set(allocated["unallocated_cpu_ids"])
        cells = {row["id"]: {"id": row["id"], "cpu": len(free.intersection(row["online_cpus"])),
                            "memory": 0, "pages": {}, "cores": row["cores"]} for row in physical["cells"]}
        if cpu_options.get("full-pcpus-only") == "true":
            for row in cells.values():
                row["cpu"] = sum(len(core) for core in row["cores"] if set(core) <= free)
        def book(rows, sign):
            for row in rows:
                # An ambiguous reservation is charged in full to every cell it
                # could occupy; never split bytes and invent extra free space.
                targets = row["nodes"]
                if not targets:
                    raise ValueError()
                size = None if row["type"] == "memory" else str(R.quantity(row["type"][len("hugepages-"):]))
                if sign == 1 and len(targets) != 1:
                    raise ValueError()  # capacity must have explicit locality
                for ident in targets:
                    cell = cells[ident]
                    if size is None:
                        cell["memory"] += sign * row["bytes"]
                    else:
                        cell["pages"][size] = cell["pages"].get(size, 0) + sign * row["bytes"]
        book(allocated["allocatable_memory"], 1)
        for pod in allocated["pods"]:
            for container in pod["containers"]:
                book(container["memory"], -1)
        for original in physical["cells"]:
            cell = cells[original["id"]]
            for size in requested["pages"]:
                counters = original["hugepages"].get(size)
                pool = physical["page_pools"].get(size)
                if counters is None or pool is None or size not in cell["pages"]:
                    cell["pages"][size] = 0
                    continue
                physical_free = max(0, counters["free"] - pool["reserved"]) * int(size)
                cell["pages"][size] = min(cell["pages"][size], physical_free)
        # Pending real pods are not guaranteed to appear in PodResources yet.
        # Charge their full possible demand to each candidate local cell. A
        # terminating/running pod stays in the actual allocation inventory; no
        # resources are released merely because Kubernetes says it is stopping.
        observed = {(pod["namespace"], pod["name"]): {c["name"] for c in pod["containers"]} for pod in allocated["pods"]}
        for pod in pods:
            node = pod.get("spec", {}).get("nodeName")
            if node and node != host or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            planned = pod.get("_homestead_numa")
            if planned:
                pending = planned["demand"]
                targets = [cells[planned["cell"]]]
            elif pod.get("status", {}).get("phase") != "Running":
                pending = demand(pod.get("spec") or {})
                qos = pod.get("status", {}).get("qosClass")
                if qos in ("Burstable", "BestEffort"):
                    pending["cpu"] = 0
                targets = list(cells.values())
            else:
                # A Running phase can precede a resource-manager observation.
                # Never assume an absent guaranteed container is free capacity.
                if pod.get("status", {}).get("qosClass") not in ("Burstable", "BestEffort"):
                    meta = pod.get("metadata") or {}
                    names = {c["name"] for c in pod.get("spec", {}).get("containers", [])}
                    if not names <= observed.get((meta.get("namespace"), meta.get("name")), set()):
                        result["reason"] = "A running workload is missing from the allocation snapshot; refresh after its allocation settles"
                        return result
                continue
            for cell in targets:
                cell["cpu"] -= pending["cpu"]
                cell["memory"] -= pending["memory"]
                for size, value in pending["pages"].items():
                    cell["pages"][size] = cell["pages"].get(size, 0) - value
        for cell in cells.values():
            if cpu_options.get("full-pcpus-only") == "true":
                widths = {len(core) for core in cell["cores"]}
                main = R.request(spec["containers"][0], "cpu") // 1000
                if len(widths) != 1 or main % next(iter(widths)):
                    continue
            if cell["cpu"] >= requested["cpu"] and cell["memory"] >= requested["memory"] and all(
                    cell["pages"].get(size, 0) >= value for size, value in requested["pages"].items()):
                result["options"].append({"cell": cell["id"], "demand": requested})
        result["reason"] = ("Dedicated CPUs and memory fit together on a local NUMA cell; kubelet makes the final allocation"
                            if result["options"] else "No NUMA cell has enough unallocated dedicated CPUs and local memory after reservations")
    except Exception:
        result["reason"] = "Local CPU/memory accounting is incomplete; placement cannot be verified"
    return result
