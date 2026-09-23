"""Adding a Harvester host, and taking a dead one back out.

Adding one is Harvester's own installer, run from its ISO. An unattended
install would need the new machine's install disk and network card named in
advance - details nobody has until the machine is in front of them - so instead
of a config file Homestead gives a guide: the ISO for this cluster's version,
and each installer screen with the answer this cluster wants, read from the
cluster itself where it can be. Nothing is served to the new host, and the join
token never passes through Homestead: the guide says where to read it.

The second half removes a node that is gone - following Harvester's own order:
Longhorn stops scheduling to it, the Kubernetes node goes, then the leftover
Cluster API machine and Longhorn node - after checking it will not cost etcd
quorum or the last copy of a volume.
"""
import calendar
import json
import re
import time
import urllib.error

kget = ksend = None
NS = "lab"
OPS = None

RELEASES = "https://releases.rancher.com/harvester"
TOKEN_FILE = "/etc/rancher/rancherd/config.yaml"
MANAGEMENT_ROLES = {"control-plane", "etcd", "master"}


def bind(_kget, _ksend, namespace, operations=None):
    global kget, ksend, NS, OPS
    kget, ksend, NS, OPS = _kget, _ksend, namespace, operations


def _get(path):
    try:
        return kget(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


# ------------------------------------------------------------ the guide
def _setting(name):
    """A Harvester setting's value, which is often JSON inside a string."""
    try:
        setting = _get(f"/apis/harvesterhci.io/v1beta1/settings/{name}") or {}
    except Exception:
        return ""
    return setting.get("value") or setting.get("default") or ""


def _json(text):
    try:
        value = json.loads(text) if text else {}
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def next_hostname(names):
    """The name after the last numbered one: harvester-3 after harvester-1 and -2."""
    numbered = [re.fullmatch(r"(.*?)(\d+)", name) for name in names]
    numbered = [m for m in numbered if m]
    if not numbered:
        return ""
    prefix = max(numbered, key=lambda m: int(m.group(2))).group(1)
    width = len(numbered[0].group(2))
    taken = {int(m.group(2)) for m in numbered if m.group(1) == prefix}
    number = max(taken) + 1
    return f"{prefix}{number:0{width}d}"


def guide():
    """What the Harvester installer asks when joining this cluster, and this cluster's answers."""
    setting = _get("/apis/harvesterhci.io/v1beta1/settings/server-version") or {}
    version = str(setting.get("value") or (setting.get("status") or {}).get("value") or "").lstrip("v")
    vip = str(((_get("/api/v1/namespaces/harvester-system/configmaps/vip") or {}).get("data") or {}).get("ip") or "")
    nodes, arches = [], set()
    for node in (_get("/api/v1/nodes") or {}).get("items", []):
        info = (node.get("status") or {}).get("nodeInfo") or {}
        arches.add(info.get("architecture") or "amd64")
        if not version:
            match = re.search(r"Harvester\s+v?([0-9][^\s]*)", info.get("osImage", ""))
            version = match.group(1) if match else ""
        address = next((a["address"] for a in (node.get("status") or {}).get("addresses") or []
                        if a.get("type") == "InternalIP"), "")
        roles = _roles(node)
        nodes.append({"name": node["metadata"]["name"], "ip": address, "ready": _ready(node),
                      "management": bool(MANAGEMENT_ROLES & set(roles))})
    arch = "arm64" if arches == {"arm64"} else "amd64"
    base = f"{RELEASES}/v{version}" if version else ""
    ntp = _json(_setting("ntp-servers")).get("ntpServers") or []
    proxy = _json(_setting("http-proxy"))
    management = [n for n in nodes if n["management"]]
    return {
        "version": version, "arch": arch,
        "iso": f"{base}/harvester-v{version}-{arch}.iso" if base else "",
        "checksums": f"{base}/harvester-v{version}-{arch}.sha512" if base else "",
        "vip": vip, "ntp": [str(x) for x in ntp],
        "proxy": str(proxy.get("httpProxy") or proxy.get("httpsProxy") or ""),
        "hostname": next_hostname([n["name"] for n in nodes]),
        "nodes": sorted(nodes, key=lambda n: n["name"]),
        "management_count": len(management),
        "token_file": TOKEN_FILE,
        "token_command": f"sudo grep '^token:' {TOKEN_FILE}",
        "token_host": next((n["ip"] for n in management if n["ready"] and n["ip"]), ""),
    }


def tidy_old_plans():
    """Join plans from 2.8.68-2.8.71 kept a join token in a Secret each; they are gone now."""
    removed = 0
    try:
        secrets = kget(f"/api/v1/namespaces/{NS}/secrets").get("items", [])
        pods = kget(f"/api/v1/namespaces/{NS}/pods?labelSelector=app%3Dhomestead-pxe").get("items", [])
    except Exception:
        return 0
    for item in secrets:
        name = item["metadata"]["name"]
        if name.startswith("homestead-onboard-"):
            try:
                ksend("DELETE", f"/api/v1/namespaces/{NS}/secrets/{name}")
                removed += 1
            except Exception:
                pass
    for pod in pods:
        try:
            ksend("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod['metadata']['name']}")
            removed += 1
        except Exception:
            pass
    return removed


# ============================================================ cleanup
def _ready(node):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (node.get("status") or {}).get("conditions", []))


def _not_ready_since(node):
    for c in (node.get("status") or {}).get("conditions", []):
        if c.get("type") == "Ready" and c.get("status") != "True":
            return c.get("lastTransitionTime", "")
    return ""


def _roles(node):
    labels = node["metadata"].get("labels") or {}
    return sorted(k.split("/", 1)[1] for k in labels
                  if k.startswith("node-role.kubernetes.io/") and labels[k] in ("true", ""))


def _replicas_by_node():
    try:
        replicas = kget("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas").get("items", [])
    except Exception:
        return {}, {}
    by_volume, by_node = {}, {}
    for r in replicas:
        spec, status = r.get("spec") or {}, r.get("status") or {}
        node, volume = spec.get("nodeID", ""), spec.get("volumeName", "")
        healthy = status.get("currentState") == "running" and not spec.get("failedAt")
        by_volume.setdefault(volume, []).append((node, healthy))
        by_node.setdefault(node, set()).add(volume)
    return by_volume, by_node


def _age(stamp):
    """Seconds since a Kubernetes timestamp, or None when there is none."""
    try:
        return time.time() - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _list(path):
    try:
        return kget(path).get("items", [])
    except Exception:
        return []


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def _stuck_on(name):
    """What a dead host still holds: pods, VMs, volume attachments and replica records."""
    pods = [p for p in _list(f"/api/v1/pods?fieldSelector=spec.nodeName%3D{name}")
            if (p.get("spec") or {}).get("nodeName") == name]
    vmis = [v for v in _list("/apis/kubevirt.io/v1/virtualmachineinstances")
            if (v.get("status") or {}).get("nodeName") == name]
    attachments = [a for a in _list("/apis/storage.k8s.io/v1/volumeattachments")
                   if (a.get("spec") or {}).get("nodeName") == name]
    replicas = [r for r in _list("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas")
                if (r.get("spec") or {}).get("nodeID") == name]
    return pods, vmis, attachments, replicas


def removal_plan(name):
    """What removing a node would do, and whether it is safe - before anything changes."""
    node = _get(f"/api/v1/nodes/{name}")
    if not node:
        raise ValueError(f"there is no node called {name}")
    nodes = (_get("/api/v1/nodes") or {}).get("items", [])
    ready = _ready(node)
    roles = _roles(node)
    since = _not_ready_since(node)
    blockers, warnings, steps = [], [], []
    etcd = [n for n in nodes if "etcd" in _roles(n)]
    control = [n for n in nodes if {"control-plane", "master"} & set(_roles(n))]
    if ready:
        blockers.append(f"{name} is Ready. A running node re-registers itself, so it is not removed from here: "
                        f"put it in maintenance mode in Harvester, run /opt/rke2/bin/rke2-uninstall.sh on it, "
                        f"power it off, and come back when it shows Not ready.")
    else:
        down = _age(since)
        if down is not None and down < 600:
            warnings.append(f"{name} stopped reporting {_plural(max(1, int(down // 60)), 'minute')} ago. "
                            f"It may only be rebooting; give it a few minutes.")
    if "etcd" in roles or {"control-plane", "master"} & set(roles):
        others_ready = sum(1 for n in etcd if n["metadata"]["name"] != name and _ready(n))
        remaining = len(etcd) - 1
        if len([n for n in control if n["metadata"]["name"] != name]) == 0:
            blockers.append(f"{name} is the only control-plane node; removing it ends the cluster.")
        elif remaining and others_ready < remaining // 2 + 1:
            blockers.append(f"Only {others_ready} of the other {remaining} etcd members are ready, "
                            f"so the cluster would not have quorum without {name}. Bring them back first.")
        elif remaining < 3:
            warnings.append(f"The cluster keeps {_plural(remaining, 'etcd member')}; "
                            f"one more failure would stop it. Add a node soon.")
        else:
            warnings.append("Harvester promotes a worker to take its control-plane place, when one is available.")
    by_volume, by_node = _replicas_by_node()
    lost, degraded = [], []
    for volume in sorted(by_node.get(name, ())):
        healthy_elsewhere = [n for n, ok in by_volume.get(volume, []) if ok and n != name]
        (degraded if healthy_elsewhere else lost).append(volume)
    if lost:
        warnings.append(f"{_plural(len(lost), 'volume')} with no healthy copy anywhere but {name}: "
                        f"{', '.join(lost[:6])}{' …' if len(lost) > 6 else ''}. "
                        f"Removing the node gives up on that data.")
    if degraded:
        warnings.append(f"{_plural(len(degraded), 'volume')} will rebuild the copy {name} held, "
                        f"on the remaining nodes.")
    pods, vmis, attachments, replicas = _stuck_on(name) if not ready else ([], [], [], [])
    annotations = node["metadata"].get("annotations") or {}
    machine = annotations.get("cluster.x-k8s.io/machine", "")
    machine_ns = annotations.get("cluster.x-k8s.io/cluster-namespace", "fleet-local")
    steps.append(f"Stop Longhorn scheduling new replicas to {name}")
    steps.append(f"Delete the Kubernetes node {name} (RKE2 removes its etcd membership)")
    steps.append(f"Delete its Cluster API machine {machine}" if machine
                 else "Delete any Cluster API machine left pointing at it")
    steps.append(f"Delete Longhorn's record of {name} once it holds no replicas")
    vm_names = sorted({(v.get("metadata") or {}).get("name", "") for v in vmis})
    gone_steps = [
        f"Force-delete the {_plural(len(pods), 'pod')} still bound to it, so their workloads start elsewhere",
        (f"Force-stop the {_plural(len(vmis), 'VM')} it was running ({', '.join(vm_names[:5])}), "
         f"so each restarts on another host" if vmis else "No VMs are recorded as running there"),
        f"Release {_plural(len(attachments), 'volume attachment')}, so those volumes can attach on another node",
        f"Delete the {_plural(len(replicas), 'replica record')} Longhorn keeps for it, "
        f"so it rebuilds them from the remaining copies",
        "Finish its Cluster API machine's deletion if finalizers hold it",
    ]
    return {"node": name, "ready": ready, "roles": roles, "since": since,
            "blockers": blockers, "warnings": warnings, "steps": steps, "gone_steps": gone_steps,
            "stuck": {"pods": len(pods), "vms": vm_names, "attachments": len(attachments),
                      "replicas": len(replicas)},
            "lost_volumes": lost, "rebuilt_volumes": degraded,
            "machine": {"name": machine, "namespace": machine_ns}, "ok": not blockers}


def _force_delete(path):
    try:
        ksend("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
        return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def remove_node(name, accept_loss=False, gone=False):
    """Removes a dead node in Harvester's documented order, reporting each step.

    `gone` is for a host that will never come back: what it still holds - pods,
    VMs, volume attachments, replica records, a machine held by finalizers - is
    let go of too, rather than left for controllers that wait on the host.
    """
    plan = removal_plan(name)
    if plan["blockers"]:
        raise ValueError(plan["blockers"][0])
    if plan["lost_volumes"] and not accept_loss:
        raise ValueError(f"{_plural(len(plan['lost_volumes']), 'volume')} would lose the only copy; confirm that first")
    log = []
    lh = f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/{name}"
    if _get(lh):
        ksend("PATCH", lh, {"spec": {"allowScheduling": False}}, "application/merge-patch+json")
        log.append(f"Longhorn stopped scheduling to {name}")
    pods, vmis, attachments, replicas = _stuck_on(name) if gone else ([], [], [], [])
    if gone:
        stopped = sum(_force_delete(f"/apis/kubevirt.io/v1/namespaces/{v['metadata']['namespace']}/"
                                    f"virtualmachineinstances/{v['metadata']['name']}") for v in vmis)
        if stopped:
            log.append(f"Force-stopped the {_plural(stopped, 'VM')} it was running")
        deleted = sum(_force_delete(f"/api/v1/namespaces/{p['metadata']['namespace']}/pods/{p['metadata']['name']}")
                      for p in pods)
        if deleted:
            log.append(f"Force-deleted {_plural(deleted, 'pod')} bound to {name}")
        released = sum(_force_delete(f"/apis/storage.k8s.io/v1/volumeattachments/{a['metadata']['name']}")
                       for a in attachments)
        if released:
            log.append(f"Released {_plural(released, 'volume attachment')}")
    ksend("DELETE", f"/api/v1/nodes/{name}")
    log.append(f"Deleted node {name}")
    removed = _delete_machines_for(name, plan["machine"])
    log.extend(f"Deleted Cluster API machine {m}" for m in removed)
    if gone:
        for machine_name in removed:
            if _unstick_machine(machine_name):
                log.append(f"Cleared the finalizers holding machine {machine_name}")
        dropped = sum(_force_delete(f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas/"
                                    f"{r['metadata']['name']}") for r in replicas)
        if dropped:
            log.append(f"Deleted {_plural(dropped, 'replica record')}; Longhorn rebuilds them from the remaining copies")
    try:
        ksend("DELETE", lh)
        log.append(f"Deleted Longhorn's record of {name}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            log.append(f"Longhorn kept its record of {name} for now: it still lists replicas there. "
                       f"Clean up again once they have rebuilt elsewhere, or force it if the host is gone for good.")
    if OPS:
        OPS.start("cluster-cleanup", f"Removed {name}", {"kind": "Node", "name": name},
                  "/system/cluster", {"log": log}, "; ".join(log))
    return {"ok": True, "node": name, "log": log}


def _machines():
    return _list("/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines")


def _unstick_machine(machine_name):
    """A machine whose deletion waits on a host that will never answer: let it go."""
    path = f"/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines/{machine_name}"
    machine = _get(path)
    if not machine or not machine["metadata"].get("deletionTimestamp"):
        return False
    ksend("PATCH", path, {"metadata": {"finalizers": None}}, "application/merge-patch+json")
    return True


def _delete_machines_for(name, known):
    removed = []
    for machine in _machines():
        meta = machine["metadata"]
        node_ref = ((machine.get("status") or {}).get("nodeRef") or {}).get("name", "")
        if node_ref == name or meta["name"] == known.get("name"):
            try:
                ksend("DELETE", f"/apis/cluster.x-k8s.io/v1beta1/namespaces/{meta['namespace']}/machines/{meta['name']}")
                removed.append(meta["name"])
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
    return removed


def cleanup_report():
    """What is left over: dead nodes, machines with no node, Longhorn nodes with no node."""
    nodes = (_get("/api/v1/nodes") or {}).get("items", [])
    names = {n["metadata"]["name"] for n in nodes}
    dead = [{"name": n["metadata"]["name"], "roles": _roles(n), "since": _not_ready_since(n)}
            for n in nodes if not _ready(n)]
    stale_machines = []
    for machine in _machines():
        node_ref = ((machine.get("status") or {}).get("nodeRef") or {}).get("name", "")
        phase = (machine.get("status") or {}).get("phase", "")
        deleting = bool(machine["metadata"].get("deletionTimestamp"))
        orphaned = (node_ref and node_ref not in names) or (not node_ref and phase in ("Failed", "Deleting"))
        if orphaned or (deleting and node_ref not in names):
            stale_machines.append({"name": machine["metadata"]["name"], "node": node_ref, "phase": phase,
                                   "stuck": deleting, "created": machine["metadata"].get("creationTimestamp", "")})
    lh_nodes = _list("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes")
    _, by_node = _replicas_by_node()
    stale_longhorn = [{"name": n["metadata"]["name"], "replicas": len(by_node.get(n["metadata"]["name"], ()))}
                      for n in lh_nodes if n["metadata"]["name"] not in names]
    return {"dead_nodes": dead, "stale_machines": stale_machines, "stale_longhorn": stale_longhorn}


def cleanup(kind, name, force=False):
    """Removes one leftover record the report found. `force` is for a host that is gone for good."""
    if kind == "machine":
        stale = {m["name"]: m for m in cleanup_report()["stale_machines"]}
        if name not in stale:
            raise ValueError("that machine is not a leftover; it still has a node")
        if stale[name]["stuck"]:
            if not force:
                raise ValueError(f"{name} is already being deleted and is waiting on its host; "
                                 f"if that host is gone for good, force it")
            _unstick_machine(name)
            return {"ok": True, "message": f"Cleared the finalizers holding machine {name}"}
        ksend("DELETE", f"/apis/cluster.x-k8s.io/v1beta1/namespaces/fleet-local/machines/{name}")
        if force:
            _unstick_machine(name)
        return {"ok": True, "message": f"Deleted leftover machine {name}"}
    if kind == "longhorn":
        stale = {n["name"]: n for n in cleanup_report()["stale_longhorn"]}
        if name not in stale:
            raise ValueError("that Longhorn node still has a Kubernetes node")
        path = f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/{name}"
        if stale[name]["replicas"]:
            if not force:
                raise ValueError(f"Longhorn still lists {_plural(stale[name]['replicas'], 'replica')} on {name}; "
                                 f"let them rebuild elsewhere, or force it if the host is gone for good")
            for replica in _list("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas"):
                if (replica.get("spec") or {}).get("nodeID") == name:
                    _force_delete(f"/apis/longhorn.io/v1beta2/namespaces/longhorn-system/replicas/"
                                  f"{replica['metadata']['name']}")
        # Longhorn refuses to delete a node that still allows scheduling.
        ksend("PATCH", path, {"spec": {"allowScheduling": False}}, "application/merge-patch+json")
        ksend("DELETE", path)
        return {"ok": True, "message": f"Deleted Longhorn's record of {name}"}
    raise ValueError("unknown cleanup")
