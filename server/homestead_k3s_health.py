"""Read-only, identity-bound verification of reviewed guest-cluster batches.

Guest TLS credentials never leave the guest. KubeVirt executes the pinned probe
through its guest agent and publishes readiness in the authenticated host API.
This is a readiness observation, not attestation against a compromised guest or
proof of future failover. No SSH, API-port heuristics, writes or automatic retries.
"""
import base64
import copy
import datetime
import hashlib
from pathlib import Path
import re
import time
import urllib.parse
import uuid

import homestead_guest_readiness as GUEST

SCRIPT = "/usr/local/lib/homestead/guest-readiness.py"


def digest(value):
    return hashlib.sha256(GUEST.canonical(value)).hexdigest()


def prepare(namespace, built, review_id):
    if not re.fullmatch(r"[a-f0-9]{32}", str(review_id or "")):
        raise ValueError("Guest verification requires the frozen review identity")
    members = [{key: row[key] for key in ("name", "role", "address")} for row in built["nodes"]]
    for row in members:
        row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"homestead-guest:{namespace}:{review_id}:{row['name']}"))
    nodes = []
    for row in members:
        config = {"version": 1, "name": row["name"], "role": row["role"], "members": members,
                  "setup": built["setup"], "kubevirt": built["kubevirt"]}
        nodes.append({"name": row["name"], "uuid": row["uuid"], "config": config,
                      "probe": {"exec": {"command": ["/usr/bin/python3", SCRIPT, digest(config)]},
                                "initialDelaySeconds": 15, "periodSeconds": 30, "timeoutSeconds": 25,
                                "successThreshold": 1, "failureThreshold": 1}})
    return {"version": 1, "nodes": nodes}


def cloud_files(node):
    """Public probe/config files only; JSON is also valid YAML for cloud-init."""
    source = Path(GUEST.__file__).read_bytes()
    return [{"path": path, "owner": "root:root", "permissions": "0600", "encoding": "b64",
             "content": base64.b64encode(content).decode()}
            for path, content in ((SCRIPT, source), (GUEST.CONFIG, GUEST.canonical(node["config"])))]


def pin(prepared, health):
    """Attach the reviewed probe before admission; record only seed data's hash."""
    node = next(row for row in health["nodes"] if row["name"] == prepared["name"])
    spec = prepared["vm"]["spec"]["template"]["spec"]
    spec["readinessProbe"] = copy.deepcopy(node["probe"])
    spec["domain"].setdefault("firmware", {})["uuid"] = node["uuid"]
    seed = next(row for row in prepared["secrets"] if row["metadata"]["name"] == prepared["secret_name"])
    node["seed"] = {"name": prepared["secret_name"], "digest": digest(seed["data"])}


def _identity(obj, kind, namespace, name, uid=None):
    meta = obj.get("metadata") or {}
    return (obj.get("kind") == kind and meta.get("namespace", "") == namespace and meta.get("name") == name
            and bool(meta.get("uid")) and (uid is None or meta["uid"] == uid) and not meta.get("deletionTimestamp"))


def _matches(spec, node):
    volumes = [row for row in spec.get("volumes", []) if row.get("name") == "cloudinit"]
    seed = (volumes[0].get("cloudInitNoCloud") or {}) if len(volumes) == 1 else {}
    return (spec.get("readinessProbe") == node["probe"]
            and spec.get("domain", {}).get("firmware", {}).get("uuid") == node["uuid"]
            and seed.get("secretRef") == {"name": node["seed"]["name"]}
            and seed.get("networkDataSecretRef") == {"name": node["seed"]["name"]}
            and not any(key in seed for key in ("userData", "userDataBase64", "networkData", "networkDataBase64")))


def _host_fresh(read, name, now):
    if not name:
        return False
    node = read("/api/v1/nodes/" + urllib.parse.quote(name, safe=""))
    lease = read("/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/" + urllib.parse.quote(name, safe=""))
    meta, state = lease.get("metadata") or {}, lease.get("spec") or {}
    renewed = datetime.datetime.fromisoformat(state.get("renewTime", "").replace("Z", "+00:00"))
    return (_identity(node, "Node", "", name) and GUEST.condition(node, "Ready")
            and _identity(lease, "Lease", "kube-node-lease", name) and state.get("holderIdentity") == name
            and any(owner.get("kind") == "Node" and owner.get("uid") == node["metadata"]["uid"]
                    for owner in meta.get("ownerReferences", []))
            and renewed.tzinfo is not None and -30 <= now - renewed.timestamp() <= 90)


def status(item, read, *, limit=2700, now=None):
    """Only the caller persists observations, under its operations-store lock."""
    now = time.time() if now is None else now
    ref, ready, observations, hosts = item["ref"], 0, [], {}
    health = ref["guest_health"]
    nodes = health.get("nodes") or []
    if health.get("version") != 1 or [row["name"] for row in nodes] != [row["name"] for row in ref["nodes"]] or not nodes:
        return "failed", item.get("progress", 10), "Guest verification contract is unavailable; inspect the retained batch"
    pending = ""
    for row in nodes:
        name, ns = row["name"], ref["namespace"]
        try:
            receipt = next(entry["identity"] for entry in ref["created"] if entry["name"] == name)
            path = f"/apis/kubevirt.io/v1/namespaces/{urllib.parse.quote(ns, safe='')}/"
            vm = read(path + "virtualmachines/" + urllib.parse.quote(name, safe=""))
            if not _identity(vm, "VirtualMachine", ns, name, receipt["uid"]):
                return "failed", item.get("progress", 10), f"{name}'s VM identity changed or is deleting; resources are retained"
            if not _matches(vm.get("spec", {}).get("template", {}).get("spec", {}), row):
                return "failed", item.get("progress", 10), f"{name}'s reviewed verification settings changed; resources are retained"
            seed_resource = {"apiVersion": "v1", "kind": "Secret", "namespace": ns, "name": row["seed"]["name"]}
            seed_writes = [entry for entry in ref["writes"] if entry["resource"] == seed_resource]
            if not seed_writes or seed_writes[-1]["phase"] != "accepted":
                return "failed", item.get("progress", 10), f"{name} has no confirmed bootstrap Secret receipt; resources are retained"
            seed = read(f"/api/v1/namespaces/{urllib.parse.quote(ns, safe='')}/secrets/" + urllib.parse.quote(row["seed"]["name"], safe=""))
            if (not _identity(seed, "Secret", ns, row["seed"]["name"], seed_writes[-1]["identity"]["uid"])
                    or digest(seed.get("data")) != row["seed"]["digest"]):
                return "failed", item.get("progress", 10), f"{name}'s bootstrap Secret changed; resources are retained"
            vmi = read(path + "virtualmachineinstances/" + urllib.parse.quote(name, safe=""))
            if not (_identity(vmi, "VirtualMachineInstance", ns, name) and
                    any(owner.get("controller") is True and owner.get("apiVersion") == "kubevirt.io/v1"
                        and owner.get("kind") == "VirtualMachine" and owner.get("name") == name and owner.get("uid") == receipt["uid"]
                        for owner in vmi["metadata"].get("ownerReferences", []))):
                pending = f"Waiting for {name}'s owned VM instance"
                continue
            state = vmi.get("status") or {}
            host = state.get("nodeName")
            if host not in hosts:
                hosts[host] = _host_fresh(read, host, now)
            if not (_matches(vmi.get("spec") or {}, row) and state.get("phase") == "Running"
                    and GUEST.condition(vmi, "AgentConnected") and GUEST.condition(vmi, "Ready")
                    and not any(entry.get("type") == "Paused" and entry.get("status") != "False" for entry in state.get("conditions", []))
                    and hosts[host]):
                pending = f"Waiting for {name}'s guest-agent readiness check and fresh host heartbeat"
                continue
            ready += 1
            observations.append({"name": name, "vm_uid": receipt["uid"], "vmi_uid": vmi["metadata"]["uid"]})
        except Exception:
            # API errors may contain seed data. Neither raw errors nor credentials
            # belong in public jobs. Unknown/missing observations are never ready.
            pending = f"Waiting for verified observations of {name}; check its console and /var/log/homestead-k3s.log"
    if ready == len(nodes):
        ref.update(phase="verified-ready", retain_resources=False,
                   guest_observation={"at": now, "instances": observations})
        return "succeeded", 100, "Guest bootstrap checks passed for the expected nodes and requested components; guest services and server APIs are ready. Ongoing cluster health and failover need separate monitoring."
    if now - float(ref.get("started") or now) > limit:
        return "failed", item.get("progress", 10), f"Guest verification timed out ({ready}/{len(nodes)} ready). {pending}. Resources are retained; inspect the batch outcome."
    return "running", 10 + int(80 * ready / len(nodes)), f"Guest checks: {ready}/{len(nodes)} ready. {pending}"
