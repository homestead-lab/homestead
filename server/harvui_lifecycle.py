"""
Lifecycle actions: edit a workload, move it between hosts, cordon/drain a node,
reboot or shut a node down, and migrate VMs.

Everything destructive here is guarded. The guards are the point of this module —
the raw API calls are three lines each.
"""
import json
import hashlib
import copy
import os
import re
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
hardware_features = lambda: []


def dns_label(value, label):
    value = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError(f"{label} must use lowercase letters, numbers and dashes")
    return value


def bind(_kget, _ksend, _sys_ns, _cache_ref, _hardware_features=None):
    global kget, ksend, SYS_NS, _cache, hardware_features
    kget, ksend, SYS_NS, _cache = _kget, _ksend, _sys_ns, _cache_ref
    if _hardware_features:
        hardware_features = _hardware_features


def _bust(*keys):
    for k in list(_cache):
        if not keys or any(k.startswith(x) for x in keys):
            _cache.pop(k, None)


def seed_configs(ns, dep):
    """Return editable ConfigMap data consumed by Deployment init containers.

    A common container pattern seeds a persistent appdata volume from a
    ConfigMap before the main container starts.  The ConfigMap is therefore the
    authoritative value, even though users naturally discover the file in the
    mounted volume first.  Surface every ConfigMap mounted by an init container
    without making assumptions about its command or destination path.
    """
    spec = dep.get("spec", {}).get("template", {}).get("spec", {})
    sources = {
        v.get("name"): (v.get("configMap") or {}).get("name")
        for v in spec.get("volumes", []) or []
        if (v.get("configMap") or {}).get("name")
    }
    found = []
    seen = set()
    for init in spec.get("initContainers", []) or []:
        for mount in init.get("volumeMounts", []) or []:
            cm_name = sources.get(mount.get("name"))
            marker = (init.get("name", ""), cm_name)
            if not cm_name or marker in seen:
                continue
            seen.add(marker)
            cm = kget(f"/api/v1/namespaces/{ns}/configmaps/{cm_name}")
            for key, value in sorted((cm.get("data") or {}).items()):
                found.append({
                    "init_container": init.get("name", ""),
                    "config_map": cm_name,
                    "key": key,
                    "value": value,
                    "source_path": mount.get("mountPath", ""),
                    "command": " ".join((init.get("command") or []) + (init.get("args") or [])),
                })
    return found


def _save_seed_configs(ns, dep, requested):
    """Update only ConfigMap keys already wired to this Deployment's init containers."""
    if not requested:
        return
    allowed = {
        (x["init_container"], x["config_map"], x["key"])
        for x in seed_configs(ns, dep)
    }
    updates = {}
    for item in requested:
        marker = (item.get("init_container", ""), item.get("config_map", ""), item.get("key", ""))
        if marker not in allowed:
            raise ValueError("seed config is not attached to this workload")
        value = item.get("value", "")
        if not isinstance(value, str):
            raise ValueError("seed config value must be text")
        if len(value.encode("utf-8")) > 512 * 1024:
            raise ValueError("seed config value is too large (maximum 512 KiB)")
        updates.setdefault(item["config_map"], {})[item["key"]] = value
    for cm_name, values in updates.items():
        cm = kget(f"/api/v1/namespaces/{ns}/configmaps/{cm_name}")
        data = cm.setdefault("data", {})
        for key, value in values.items():
            if key not in data:
                raise ValueError("seed config key no longer exists")
            data[key] = value
        ksend("PUT", f"/api/v1/namespaces/{ns}/configmaps/{cm_name}", cm)


# --------------------------------------------------------------- edit / rename
def _wait_for_replicas(ns, name, desired, timeout=120):
    """Wait for a Deployment to reach an unambiguous ready or stopped state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        status = dep.get("status", {}) or {}
        if desired == 0:
            if (int(status.get("replicas", 0) or 0) == 0 and
                    int(status.get("readyReplicas", 0) or 0) == 0):
                return dep
        else:
            generation = int(dep.get("metadata", {}).get("generation", 0) or 0)
            observed = int(status.get("observedGeneration", 0) or 0)
            if (int(status.get("updatedReplicas", 0) or 0) >= desired and
                    int(status.get("readyReplicas", 0) or 0) >= desired and
                    int(status.get("availableReplicas", 0) or 0) >= desired and
                    (not generation or observed >= generation)):
                return dep
        time.sleep(2)
    state = "stop" if desired == 0 else f"reach {desired}/{desired} ready replicas"
    raise TimeoutError(f"timed out waiting for {name} to {state}")


def _renamed_deployment(dep, ns, new_name):
    """Clone a Deployment as a create-safe object with a non-overlapping selector."""
    cloned = copy.deepcopy(dep)
    metadata = cloned.setdefault("metadata", {})
    for key in ("uid", "resourceVersion", "generation", "creationTimestamp",
                "deletionTimestamp", "deletionGracePeriodSeconds", "managedFields",
                "selfLink", "finalizers", "ownerReferences"):
        metadata.pop(key, None)
    metadata["name"] = new_name
    metadata["namespace"] = ns
    annotations = metadata.setdefault("annotations", {})
    annotations.pop("deployment.kubernetes.io/revision", None)
    annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    annotations["homestead.io/renamed-from"] = dep.get("metadata", {}).get("name", "")
    annotations["homestead.io/renamed-at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    cloned.pop("status", None)

    spec = cloned.setdefault("spec", {})
    selector = spec.setdefault("selector", {}).setdefault("matchLabels", {})
    labels = spec.setdefault("template", {}).setdefault("metadata", {}).setdefault("labels", {})
    selector.pop("pod-template-hash", None)
    labels.pop("pod-template-hash", None)
    selector["homestead.io/workload"] = new_name
    labels["homestead.io/workload"] = new_name
    spec["replicas"] = 0
    return cloned


def _retarget_hpas(ns, old_name, new_name):
    """Keep HorizontalPodAutoscalers attached when their Deployment is renamed."""
    path = f"/apis/autoscaling/v2/namespaces/{ns}/horizontalpodautoscalers"
    try:
        items = kget(path).get("items", [])
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    changed = []
    for hpa in items:
        target = hpa.get("spec", {}).get("scaleTargetRef", {})
        if target.get("kind") != "Deployment" or target.get("name") != old_name:
            continue
        target["name"] = new_name
        hpa.pop("status", None)
        hpa_name = hpa["metadata"]["name"]
        ksend("PUT", f"{path}/{hpa_name}", hpa)
        changed.append(hpa_name)
    return changed


def rename_workload(ns, old_name, new_name, edited_dep):
    """Recreate a Deployment under a new Kubernetes name with rollback on failure."""
    new_name = dns_label(new_name, "workload name")
    if new_name == old_name:
        return {"ok": True, "name": old_name, "renamed": False}
    try:
        kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{new_name}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    else:
        raise ValueError(f"workload {new_name} already exists in {ns}")

    desired = int(edited_dep.get("spec", {}).get("replicas", 0) or 0)
    new_dep = _renamed_deployment(edited_dep, ns, new_name)
    new_created = False
    hpas = []
    try:
        ksend("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", new_dep)
        new_created = True
        # Move autoscaling control first so it cannot immediately undo the
        # deliberate scale-down of the old Deployment.
        hpas = _retarget_hpas(ns, old_name, new_name)
        ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{old_name}/scale",
              {"spec": {"replicas": 0}}, ctype="application/merge-patch+json")
        _wait_for_replicas(ns, old_name, 0)
        if desired:
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{new_name}/scale",
                  {"spec": {"replicas": desired}}, ctype="application/merge-patch+json")
            _wait_for_replicas(ns, new_name, desired)
        ksend("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{old_name}",
              {"propagationPolicy": "Foreground"})
    except Exception as exc:
        if hpas:
            try:
                _retarget_hpas(ns, new_name, old_name)
            except Exception:
                pass
        if new_created:
            try:
                ksend("DELETE", f"/apis/apps/v1/namespaces/{ns}/deployments/{new_name}",
                      {"propagationPolicy": "Foreground"})
            except Exception:
                pass
        try:
            ksend("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{old_name}/scale",
                  {"spec": {"replicas": desired}}, ctype="application/merge-patch+json")
        except Exception:
            pass
        raise RuntimeError(f"rename failed; {old_name} was restored: {exc}") from exc

    _bust("wl", "ov", "flow", "impact:")
    return {"ok": True, "name": new_name, "renamed": True,
            "renamed_from": old_name, "replicas": desired, "hpas": hpas}


def edit_workload(cfg):
    """Patch an existing Deployment in place: image, resources, env, ports, gpu."""
    ns, name = cfg["ns"], cfg["name"]
    dep = kget(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]

    if "container_name" in cfg:
        container_name = dns_label(cfg.get("container_name"), "container name")
        if any(other is not c and other.get("name") == container_name for other in spec.get("containers", [])):
            raise ValueError(f"container {container_name} already exists in {name}")
        c["name"] = container_name
    if "pod_hostname" in cfg:
        pod_hostname = (cfg.get("pod_hostname") or "").strip()
        if pod_hostname:
            spec["hostname"] = dns_label(pod_hostname, "pod hostname")
        else:
            spec.pop("hostname", None)

    if "seed_configs" in cfg:
        _save_seed_configs(ns, dep, cfg.get("seed_configs") or [])

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
    if "hardware" in cfg or "gpu" in cfg:
        wanted = set(cfg.get("hardware") or [])
        if cfg.get("gpu"):
            wanted.add("igpu")
        devices = {}
        for f in hardware_features():
            slug = f["id"].replace("_", "-")[:50].strip("-")
            vn = f"hw-{slug}-{hashlib.sha1(f['id'].encode()).hexdigest()[:6]}"
            devices[f["id"]] = (f["label"], vn, f["host_path"],
                                 f["container_path"], f["path_type"])
        unknown = wanted - set(devices)
        if unknown:
            raise ValueError("unknown hardware feature(s): " + ", ".join(sorted(unknown)))
        sel = spec.setdefault("nodeSelector", {})
        mounts = c.setdefault("volumeMounts", [])
        volumes = spec.setdefault("volumes", [])
        managed_names = {v[1] for v in devices.values()} | {"dri", "coral", "coral-usb"}
        mounts[:] = [m for m in mounts if m.get("name") not in managed_names]
        volumes[:] = [v for v in volumes if v.get("name") not in managed_names]
        for hw, (label, vn, host_path, container_path, typ) in devices.items():
            if hw in wanted:
                sel[label] = "true"
                c.setdefault("securityContext", {})["privileged"] = True
                mounts.append({"name": vn, "mountPath": container_path})
                volumes.append({"name": vn, "hostPath": {"path": host_path, "type": typ}})
            else:
                sel.pop(label, None)
        # Clear legacy labels after the matching configurable feature is removed.
        known_labels = {f["label"] for f in hardware_features()}
        for label in list(sel):
            if label.startswith(("hardware/", "hardware.harvui.io/")) and label not in known_labels:
                sel.pop(label, None)
        if not mounts: c.pop("volumeMounts", None)
        if not volumes: spec.pop("volumes", None)
        if not sel: spec.pop("nodeSelector", None)
        ann = dep["metadata"].setdefault("annotations", {})
        if wanted: ann["harvui.io/hardware"] = ",".join(sorted(wanted))
        else: ann.pop("harvui.io/hardware", None)
    if "icon" in cfg:
        ann = dep["metadata"].setdefault("annotations", {})
        if cfg["icon"]:
            ann["harvui.io/icon"] = cfg["icon"]
            ann["harvui.io/icon-source"] = cfg.get("icon_source", cfg["icon"])
        else:
            ann.pop("harvui.io/icon", None)
            ann.pop("harvui.io/icon-source", None)

    dep["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
        "harvui.io/editedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    workload_name = dns_label(cfg.get("workload_name") or name, "workload name")
    if workload_name != name:
        return rename_workload(ns, name, workload_name, dep)
    out = ksend("PUT", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", dep)
    _bust("wl", "ov", "flow", "impact:")
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
    _bust("nodes", "ov", "node:", "impact:")
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
    _bust("wl", "ov", "nodes", "flow", "impact:")
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
            "host namespaces, so it ships off. Set ENABLE_NODE_POWER=true on the Homestead "
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
