"""
Lifecycle actions: edit a workload, move it between hosts, cordon/drain a node,
reboot or shut a node down, and migrate VMs.

Everything destructive here is guarded. The guards are the point of this module —
the raw API calls are three lines each.
"""
import json
import os
import time
import urllib.error

# Rebooting a host needs a privileged pod that enters the host namespaces.
# That is a real escape hatch, so it is off unless the operator opts in on the
# Deployment with ENABLE_NODE_POWER=true.
NODE_POWER_ENABLED = os.environ.get("ENABLE_NODE_POWER", "").lower() in ("1", "true", "yes")

# injected by server.py so this module stays import-cycle free
kget = ksend = None
SYS_NS = set()
_cache = {}


def bind(_kget, _ksend, _sys_ns, _cache_ref):
    global kget, ksend, SYS_NS, _cache
    kget, ksend, SYS_NS, _cache = _kget, _ksend, _sys_ns, _cache_ref


def _bust(*keys):
    for k in list(_cache):
        if not keys or any(k.startswith(x) for x in keys):
            _cache.pop(k, None)


# --------------------------------------------------------------- edit
def edit_workload(cfg):
    """Patch an existing Deployment in place: image, resources, env, ports, gpu."""
    ns, name = cfg["ns"], cfg["name"]
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]

    if cfg.get("image"):
        c["image"] = cfg["image"]
    if "env" in cfg:
        c["env"] = [{"name": k, "value": str(v)} for k, v in (cfg["env"] or {}).items()] or None
        if c["env"] is None:
            c.pop("env", None)
    if cfg.get("cpu") or cfg.get("memory"):
        req = {}
        if cfg.get("cpu"):
            req["cpu"] = cfg["cpu"]
        if cfg.get("memory"):
            req["memory"] = cfg["memory"]
        c.setdefault("resources", {})["requests"] = req
    if "ports" in cfg:
        c["ports"] = [{"containerPort": int(p["container"]),
                       "name": (p.get("name") or f"p{p['container']}")[:15]}
                      for p in cfg["ports"]] or None
        if c["ports"] is None:
            c.pop("ports", None)
    if "replicas" in cfg:
        dep["spec"]["replicas"] = int(cfg["replicas"])
    if "gpu" in cfg:
        sel = spec.setdefault("nodeSelector", {})
        if cfg["gpu"]:
            sel["hardware/igpu"] = "true"
        else:
            sel.pop("hardware/igpu", None)
        if not sel:
            spec.pop("nodeSelector", None)

    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
        "harvui.io/editedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _bust("wl", "ov", "flow")
    return {"ok": True, "name": name}


# --------------------------------------------------------------- move
def move_workload(ns, name, node):
    """Pin a Deployment to a node (or unpin) and force it to reschedule.

    RWO volumes mean the pod must fully terminate before it can attach on the
    target, so this is Recreate + a rollout, not a live move.
    """
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    spec = dep["spec"]["template"]["spec"]
    sel = spec.setdefault("nodeSelector", {})
    if node:
        sel["kubernetes.io/hostname"] = node
    else:
        sel.pop("kubernetes.io/hostname", None)
    if not sel:
        spec.pop("nodeSelector", None)
    dep["spec"].setdefault("strategy", {})["type"] = "Recreate"
    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
        "harvui.io/movedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _bust("wl", "ov", "flow")
    return {"ok": True, "moved": name, "to": node or "any node"}


# --------------------------------------------------------------- node guards
def quorum_report():
    """How many etcd members we have, and whether we can afford to lose one."""
    nodes = kget("/api/v1/nodes").get("items", [])
    etcd, ready_etcd = [], []
    for n in nodes:
        labels = n["metadata"].get("labels", {})
        if "node-role.kubernetes.io/etcd" not in labels:
            continue
        name = n["metadata"]["name"]
        etcd.append(name)
        conds = {c["type"]: c["status"] for c in n["status"].get("conditions", [])}
        if conds.get("Ready") == "True":
            ready_etcd.append(name)
    total = len(etcd)
    need = total // 2 + 1
    return {"members": etcd, "ready": ready_etcd, "total": total,
            "quorum_needs": need, "can_lose": max(0, len(ready_etcd) - need)}


def node_action_check(node, action):
    """Refuse anything that would break etcd quorum. Returns (ok, reason, report)."""
    rep = quorum_report()
    is_etcd = node in rep["members"]
    if action in ("cordon", "uncordon"):
        return True, "", rep
    if not is_etcd:
        return True, "", rep
    if rep["can_lose"] < 1:
        return (False,
                f"Refusing: {len(rep['ready'])} of {rep['total']} etcd members are ready and "
                f"quorum needs {rep['quorum_needs']}. Taking {node} down would lose the cluster.",
                rep)
    return True, "", rep


def set_cordon(node, unschedulable):
    ksend("PATCH", f"/api/v1/nodes/{node}",
          {"spec": {"unschedulable": bool(unschedulable)}},
          ctype="application/merge-patch+json")
    _bust("nodes", "ov", "node:")
    return {"ok": True, "node": node, "cordoned": bool(unschedulable)}


def drain(node, grace=30, include_system=False):
    """Evict workload pods off a node. DaemonSets and mirror pods are skipped
    because the scheduler will simply recreate them on the same node."""
    pods = kget("/api/v1/pods").get("items", [])
    evicted, skipped = [], []
    for p in pods:
        if p["spec"].get("nodeName") != node:
            continue
        ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
        owners = p["metadata"].get("ownerReferences") or []
        if any(o.get("kind") == "DaemonSet" for o in owners):
            skipped.append(f"{ns}/{name} (daemonset)")
            continue
        if p["metadata"].get("annotations", {}).get("kubernetes.io/config.mirror"):
            skipped.append(f"{ns}/{name} (static)")
            continue
        if ns in SYS_NS and not include_system:
            skipped.append(f"{ns}/{name} (system)")
            continue
        try:
            ksend("POST", f"/api/v1/namespaces/{ns}/pods/{name}/eviction",
                  {"apiVersion": "policy/v1", "kind": "Eviction",
                   "metadata": {"name": name, "namespace": ns},
                   "deleteOptions": {"gracePeriodSeconds": int(grace)}})
            evicted.append(f"{ns}/{name}")
        except urllib.error.HTTPError as e:
            skipped.append(f"{ns}/{name} (HTTP {e.code})")
    _bust("wl", "ov", "nodes", "flow")
    return {"ok": True, "node": node, "evicted": evicted, "skipped": skipped}


def node_power(node, action, drain_first=True):
    """Reboot or shut down a host.

    Kubernetes cannot do this. We schedule a one-shot privileged pod pinned to
    the node that enters the host's namespaces and asks systemd. The pod is the
    only way in without SSH credentials.
    """
    if not NODE_POWER_ENABLED:
        raise PermissionError(
            "Host power control is disabled. It needs a privileged helper pod that enters the "
            "host namespaces, so it ships off. Set ENABLE_NODE_POWER=true on the harvui "
            "Deployment to turn it on. Cordon and drain work regardless.")
    if action not in ("reboot", "poweroff"):
        raise ValueError("action must be reboot or poweroff")
    ok, why, rep = node_action_check(node, action)
    if not ok:
        raise PermissionError(why)

    steps = []
    set_cordon(node, True)
    steps.append("cordoned")
    if drain_first:
        d = drain(node)
        steps.append(f"drained {len(d['evicted'])} pod(s)")

    cmd = "systemctl reboot" if action == "reboot" else "systemctl poweroff"
    pod_name = f"harvui-{action}-{node.split('.')[0][-12:]}-{int(time.time()) % 100000}"
    body = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": "lab",
                     "labels": {"harvui.io/task": "node-power"}},
        "spec": {
            "nodeName": node, "hostPID": True, "hostIPC": True, "hostNetwork": True,
            "restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
            "tolerations": [{"operator": "Exists"}],
            "containers": [{
                "name": "power",
                "image": "busybox",
                "command": ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--",
                            "sh", "-c", f"sleep 3; {cmd}"],
                "securityContext": {"privileged": True},
            }],
        },
    }
    ksend("POST", "/api/v1/namespaces/lab/pods", body)
    steps.append(f"scheduled {action} helper ({pod_name})")
    _bust()
    return {"ok": True, "node": node, "action": action, "steps": steps, "quorum": rep}


# --------------------------------------------------------------- VM actions
def vm_migrate(ns, name, target=None):
    """Live-migrate a running VM. KubeVirt picks the target unless one is given."""
    body = {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachineInstanceMigration",
            "metadata": {"generateName": f"{name}-mig-", "namespace": ns},
            "spec": {"vmiName": name}}
    if target:
        body["spec"]["addedNodeSelector"] = {"kubernetes.io/hostname": target}
    out = ksend("POST", f"/apis/kubevirt.io/v1/namespaces/{ns}/virtualmachineinstancemigrations", body)
    _bust("flow", "ov")
    return {"ok": True, "migration": out.get("metadata", {}).get("name", ""), "vm": name}


def vm_power(ns, name, action):
    if action not in ("start", "stop", "restart"):
        raise ValueError("bad action")
    ksend("PUT", f"/apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachines/{name}/{action}", {})
    _bust("flow", "ov")
    return {"ok": True, "vm": name, "action": action}
