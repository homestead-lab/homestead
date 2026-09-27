"""Fresh authenticated allocation observations; never an allocation/reservation."""
import base64
import hashlib
import ipaddress
import json
import math
import re
import time
import urllib.request
from urllib.parse import quote

import homestead_allocation_auth as AUTH
import homestead_allocation_probe as PROBE
import homestead_numa_evidence as PHYSICAL


def _contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _contains(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(_contains(a, e) for a, e in zip(actual, expected))
    return actual == expected


def _allocation(data, physical):
    cells = {cell["id"] for cell in physical["cells"]}
    online = {cpu for cell in physical["cells"] for cpu in cell["online_cpus"]}
    available, allocated, free = (PHYSICAL._cpus(data.get(key)) for key in (
        "allocatable_cpu_ids", "allocated_cpu_ids", "unallocated_cpu_ids"))
    if not allocated <= available <= online or free != available - allocated:
        raise ValueError("Allocation CPU inventory disagrees with current host topology")
    def memory(rows):
        if not isinstance(rows, list) or len(rows) > 8192:
            raise ValueError()
        out = []
        for row in rows:
            name = row.get("type")
            if name != "memory" and (not isinstance(name, str) or not re.fullmatch(r"hugepages-[1-9][0-9]{0,12}(?:Ki|Mi|Gi|Ti|k|M|G|T)?", name)):
                raise ValueError()
            nodes = PHYSICAL._cpus(row.get("nodes"))
            if not nodes <= cells:
                raise ValueError()
            out.append({"type": name, "bytes": PHYSICAL._number(row.get("bytes")), "nodes": sorted(nodes)})
        return out
    pods, used, identities, total = [], set(), set(), 0
    if not isinstance(data.get("pods"), list) or len(data["pods"]) > 4096 or type(data.get("dynamic_resources_present")) is not bool:
        raise ValueError()
    for pod in data["pods"]:
        identity = pod.get("namespace"), pod.get("name")
        if any(not isinstance(name, str) or not PHYSICAL.DNS.fullmatch(name) for name in identity) or identity in identities:
            raise ValueError()
        identities.add(identity)
        if not isinstance(pod.get("containers"), list):
            raise ValueError()
        total += len(pod["containers"])
        if total > 8192:
            raise ValueError()
        names, containers, pod_cpus = set(), [], set()
        for container in pod["containers"]:
            name = container.get("name")
            if not isinstance(name, str) or not PHYSICAL.DNS.fullmatch(name) or name in names:
                raise ValueError()
            names.add(name)
            cpus = PHYSICAL._cpus(container.get("cpu_ids"))
            if not cpus <= allocated:
                raise ValueError()
            pod_cpus.update(cpus)
            containers.append({"name": name, "cpu_ids": sorted(cpus), "memory": memory(container.get("memory"))})
        if used & pod_cpus:
            raise ValueError()
        used.update(pod_cpus)
        pods.append({"namespace": identity[0], "name": identity[1], "containers": containers})
    if used != allocated:
        raise ValueError()
    capacity = memory(data.get("allocatable_memory"))
    keys = [(row["type"], tuple(row["nodes"])) for row in capacity]
    if len(set(keys)) != len(keys):
        raise ValueError("Allocation CPU/memory capacity is duplicated")
    return {"allocatable_cpu_ids": sorted(available), "allocated_cpu_ids": sorted(allocated),
            "unallocated_cpu_ids": sorted(free), "pods": pods, "allocatable_memory": capacity,
            "dynamic_resources_present": data["dynamic_resources_present"], "sampled_at": data["sampled_at"]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def exchange(ip, key, request):
    address = ipaddress.ip_address(ip)
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
        raise ValueError("Invalid allocation endpoint")
    host = f"[{address}]" if address.version == 6 else str(address)
    body = AUTH.encode(request)
    req = urllib.request.Request(f"http://{host}:9101/snapshot", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Homestead-Allocation": AUTH.signature(key, body)})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=5) as response:
        raw = response.read(AUTH.MAX_BODY + 1)
        if len(raw) > AUTH.MAX_BODY or not AUTH.verify(key, raw, response.headers.get("X-Homestead-Allocation"), reply_to=body):
            raise ValueError("Allocation response is unauthenticated or too large")
    return json.loads(raw)


def policy(node, configz):
    version = ((node.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "")
    match = re.fullmatch(r"v1\.(\d+)\.\d+(?:\+(?:k3s\d+|rke2r\d+))?", version)
    if not match or not 28 <= int(match[1]) <= 36:
        raise ValueError("Kubelet allocation policy is not verified for this version")
    config = configz.get("kubeletconfig")
    if not isinstance(config, dict):
        raise ValueError("Kubelet resource-manager policy is unavailable")
    result = {"version": version}
    for key in ("cpuManagerPolicy", "cpuManagerPolicyOptions", "memoryManagerPolicy", "topologyManagerPolicy",
                "topologyManagerScope", "topologyManagerPolicyOptions", "reservedSystemCPUs", "reservedMemory", "featureGates"):
        result[key] = config.get(key)
    if result["cpuManagerPolicy"] not in ("none", "static") or result["topologyManagerPolicy"] not in (
            "none", "best-effort", "restricted", "single-numa-node"):
        raise ValueError("Kubelet CPU/topology-manager policy is unrecognized")
    # Return only the known public policy shape; don't reflect arbitrary configz
    # fields (which can include paths/authentication settings) in a review.
    if len(AUTH.encode(result)) > 16384:
        raise ValueError("Kubelet resource-manager policy exceeds its limit")
    result["fingerprint"] = hashlib.sha256(AUTH.encode(result)).hexdigest()
    return result


def inspect(node, read, *, transport=None, now=None):
    read = getattr(read, "fresh", read)
    result = {"verified": False, "reason": "VM allocation checks are unavailable; check the node probe",
              "dependencies": {}, "host": None, "physical": None, "allocation": None, "policy": None}
    try:
        physical = PHYSICAL.inspect(node, read, now=now)
        if not physical["verified"]:
            result["reason"] = physical["reason"]
            return result
        source = node["temps"]["numa_source"]["pod"]
        pod_path = f"/api/v1/namespaces/{quote(source['namespace'], safe='')}/pods/{quote(source['name'], safe='')}"
        ds_path = f"/apis/apps/v1/namespaces/{quote(source['namespace'], safe='')}/daemonsets/{PROBE.NAMES.NODEPROBE}"
        node_path = "/api/v1/nodes/" + quote(node["name"], safe="")
        policy_path = node_path + "/proxy/configz"
        pod, ds, host = read(pod_path), read(ds_path), read(node_path)
        if host["metadata"]["uid"] != node["uid"] or host["metadata"].get("deletionTimestamp") or host["status"]["nodeInfo"]["bootID"] != node["boot_id"]:
            raise ValueError("Host identity changed; refresh the review")
        if PHYSICAL._identity(pod) != physical["dependencies"][pod_path] or PHYSICAL._identity(ds) != physical["dependencies"][ds_path]:
            raise ValueError("Node probe changed; refresh the review")
        template = ds["spec"]["template"]
        annotations = template.get("metadata", {}).get("annotations") or {}
        PROBE.socket_directory(annotations.get(PROBE.ANNOTATION))
        for key in (PROBE.ANNOTATION, "homestead.io/allocation-key-uid"):
            if not annotations.get(key) or (pod["metadata"].get("annotations") or {}).get(key) != annotations[key]:
                raise ValueError("Allocation collector rollout is not complete")
        desired = [c for c in template["spec"]["containers"] if c.get("name") == PROBE.CONTAINER]
        actual = [c for c in pod["spec"]["containers"] if c.get("name") == PROBE.CONTAINER]
        if len(desired) != 1 or len(actual) != 1 or not _contains(actual[0], desired[0]):
            raise ValueError("Allocation collector configuration is changing")
        if not any(c.get("name") == PROBE.CONTAINER and c.get("ready") is True for c in pod["status"].get("containerStatuses", [])):
            raise ValueError("Allocation collector is not ready")
        secret_path = f"/api/v1/namespaces/{quote(source['namespace'], safe='')}/secrets/{PROBE.KEY_NAME}"
        secret = read(secret_path)
        identity = PHYSICAL._identity(secret)
        if identity["uid"] != annotations["homestead.io/allocation-key-uid"] or not any(
                row.get("uid") == ds["metadata"]["uid"] and row.get("kind") == "DaemonSet" and row.get("apiVersion") == "apps/v1"
                for row in secret["metadata"].get("ownerReferences", [])):
            raise ValueError("Allocation authentication identity changed")
        key = base64.b64decode(secret["data"]["key"], validate=True).decode("ascii")
        AUTH.key_bytes(key)
        config = policy(host, read(policy_path))
        stamp = time.time() if now is None else now
        request = AUTH.request(node["name"], source["uid"], now=stamp)
        data = (transport or exchange)(pod["status"]["podIP"], key, request)
        finished = time.time() if now is None else now
        if data.get("complete") is not True or data.get("schema") != 1 or data.get("protocol") != "podresources.v1":
            raise ValueError("Kubelet did not return a complete supported allocation snapshot")
        if any(data.get(field) != value for field, value in (("node", node["name"]), ("pod_uid", source["uid"]), ("boot_id", node["boot_id"]))):
            raise ValueError("Allocation sample belongs to another host or probe")
        sampled = data.get("sampled_at")
        if type(sampled) not in (int, float) or not math.isfinite(sampled) or not stamp - 5 <= sampled <= finished + 5 or finished - stamp > 10:
            raise ValueError("Allocation sample is stale or its clock is inconsistent")
        data = _allocation(data, physical["sample"])
        after_host = read(node_path)
        if after_host["metadata"]["uid"] != host["metadata"]["uid"] or after_host["metadata"].get("deletionTimestamp") or after_host["status"]["nodeInfo"]["bootID"] != node["boot_id"] or policy(after_host, read(policy_path)) != config or PHYSICAL._identity(read(pod_path)) != PHYSICAL._identity(pod) or PHYSICAL._identity(read(ds_path)) != PHYSICAL._identity(ds) or PHYSICAL._identity(read(secret_path)) != identity:
            raise ValueError("Allocation policy or probe changed during the check")
        # Sample details stay internal: public diagnostics expose counts, not
        # every Pod name or allocation. Admission validates memory accounting.
        result.update(verified=True, reason="Current host allocation and policy verified; placement still needs checking",
                      dependencies={**physical["dependencies"], secret_path: identity}, host=physical["host"],
                      physical=physical["sample"], allocation=data, policy=config)
    except ValueError as error:
        # Reflect only this module's explicitly raised messages, not a JSON,
        # socket, decoding or API exception that may include private content.
        known = ("Host identity changed", "Node probe changed", "Allocation collector", "Allocation authentication identity",
                 "Kubelet did not return", "Allocation sample", "Allocation CPU", "Allocation policy", "Kubelet allocation policy",
                 "Kubelet resource-manager policy", "Kubelet CPU/topology-manager")
        if str(error).startswith(known):
            result["reason"] = str(error)
    except Exception:
        pass
    return result
