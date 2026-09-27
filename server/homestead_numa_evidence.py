"""Validate physical NUMA telemetry provenance and freshness, not allocation.

The backend records the source Pod separately from its HTTP response. The
control plane and host root remain trusted; this is not remote attestation.
Only normalized counters/topology leave this module, never arbitrary payloads.
"""
import math
import re
import time
from urllib.parse import quote

import homestead_names as NAMES

MAX_AGE = 60
DNS = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


def _identity(obj):
    meta = obj.get("metadata") or {}
    if not all(isinstance(meta.get(key), str) and meta[key] for key in ("name", "namespace", "uid", "resourceVersion")) or meta.get("deletionTimestamp"):
        raise ValueError()
    return {key: meta[key] for key in ("namespace", "name", "uid", "resourceVersion")}


def _number(value, *, maximum=2**53 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError()
    return value


def _cpus(value):
    if not isinstance(value, list) or len(value) > 8192:
        raise ValueError()
    result = [_number(cpu, maximum=8191) for cpu in value]
    if len(set(result)) != len(result):
        raise ValueError()
    return set(result)


def inspect(node, read, *, now=None):
    result = {"verified": False, "reason": "NUMA topology is unavailable, stale or not bound to a current host/probe identity",
              "dependencies": {}, "host": None, "sample": None}
    now = time.time() if now is None else now
    try:
        uid, name, boot = node.get("uid"), node.get("name"), node.get("boot_id")
        if not all(isinstance(value, str) and value for value in (uid, name, boot)):
            raise ValueError()
        payload = node.get("temps") or {}
        sample, source = payload.get("numa"), payload.get("numa_source")
        if not isinstance(sample, dict) or not isinstance(source, dict) or sample.get("complete") is not True or type(sample.get("schema")) is not int or sample["schema"] != 1:
            raise ValueError()
        sampled, received = sample.get("sampled_at"), source.get("received_at")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (sampled, received, now)):
            raise ValueError()
        if not -5 <= now - sampled <= MAX_AGE or not 0 <= now - received <= MAX_AGE or sampled > received + 5:
            raise ValueError()
        if sample.get("node") != name or sample.get("boot_id") != boot:
            raise ValueError()
        ref = source.get("pod") or {}
        if any(not isinstance(ref.get(key), str) or not DNS.fullmatch(ref[key]) for key in ("namespace", "name")) or not ref.get("uid"):
            raise ValueError()
        base = f"/api/v1/namespaces/{quote(ref['namespace'], safe='')}/pods/{quote(ref['name'], safe='')}"
        pod = read(base)
        pod_id = _identity(pod)
        if any(pod_id[key] != ref[key] for key in ("namespace", "name", "uid")):
            raise ValueError()
        if (pod.get("spec") or {}).get("nodeName") != name or (pod.get("status") or {}).get("phase") != "Running" or not any(
                row.get("type") == "Ready" and row.get("status") == "True" for row in (pod.get("status") or {}).get("conditions") or []):
            raise ValueError()
        owners = [owner for owner in (pod.get("metadata") or {}).get("ownerReferences") or [] if owner.get("controller") is True]
        if len(owners) != 1 or owners[0].get("kind") != "DaemonSet" or owners[0].get("apiVersion", "").split("/")[0] != "apps" or owners[0].get("name") != NAMES.NODEPROBE:
            raise ValueError()
        path = f"/apis/apps/v1/namespaces/{quote(ref['namespace'], safe='')}/daemonsets/{NAMES.NODEPROBE}"
        daemonset = read(path)
        ds_id = _identity(daemonset)
        if ds_id["namespace"] != ref["namespace"] or ds_id["name"] != NAMES.NODEPROBE or ds_id["uid"] != owners[0].get("uid"):
            raise ValueError()

        rows, pools = sample.get("cells"), sample.get("page_pools")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 256 or not isinstance(pools, dict) or len(pools) > 32:
            raise ValueError()
        normalized_pools = {}
        for size, pool in pools.items():
            if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", size) or int(size) > 2**53 - 1:
                raise ValueError()
            normalized_pools[size] = {"reserved": _number(pool.get("reserved"))}
        ids, online, cells = set(), set(), []
        for row in rows:
            cell_id = _number(row.get("id"), maximum=8191)
            cpus = _cpus(row.get("online_cpus"))
            cores, pages = row.get("cores"), row.get("hugepages")
            if cell_id in ids or online & cpus or not isinstance(cores, list) or len(cores) > 8192 or not isinstance(pages, dict) or set(pages) - set(pools):
                raise ValueError()
            ids.add(cell_id)
            online.update(cpus)
            covered, groups = set(), []
            for core in cores:
                group = _cpus(core)
                if not group or covered & group or not group <= cpus:
                    raise ValueError()
                covered.update(group)
                groups.append(sorted(group))
            if covered != cpus:
                raise ValueError()
            normalized_pages = {}
            for size, counters in pages.items():
                values = {key: _number(counters.get(key)) for key in ("total", "free", "surplus")}
                if values["free"] > values["total"] + values["surplus"]:
                    raise ValueError()
                normalized_pages[size] = values
            cells.append({"id": cell_id, "online_cpus": sorted(cpus), "cores": groups, "hugepages": normalized_pages})
        if not online:
            raise ValueError()
        result.update(verified=True, reason="Physical topology verified; dedicated CPU ownership and local page allocation remain unverified",
                      dependencies={base: pod_id, path: ds_id}, host={"name": name, "uid": uid, "boot_id": boot},
                      sample={"cells": sorted(cells, key=lambda cell: cell["id"]), "page_pools": normalized_pools,
                              "sampled_at": sampled, "received_at": received})
    except Exception:
        # API error bodies and untrusted fields can contain private data.
        pass
    return result
