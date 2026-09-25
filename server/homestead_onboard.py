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
quorum or the last copy of a volume. The same works on k3s, RKE2 and plain
Kubernetes, each with what it leaves behind: k3s and RKE2 keep a node-password
Secret that stops a rebuilt host of the same name joining; volumes kept on the
host itself (k3s's local-path) die with it; apps pinned to it wait for it.
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


PASSWORD_SECRET = re.compile(r"^(.+)\.node-password\.(k3s|rke2)$")
HOSTNAME = "kubernetes.io/hostname"


def _distribution(node):
    kubelet = ((node.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "")
    try:
        harvester = any(g.get("name") == "harvesterhci.io" for g in (_get("/apis") or {}).get("groups", []))
    except Exception:
        harvester = False
    return ("harvester" if harvester else "k3s" if "+k3s" in kubelet
            else "rke2" if "+rke2" in kubelet else "kubernetes")


def _workloads():
    """Deployments and StatefulSets everywhere, with their pod specs."""
    out = []
    for kind, path in (("Deployment", "/apis/apps/v1/deployments"), ("StatefulSet", "/apis/apps/v1/statefulsets")):
        for item in _list(path):
            out.append((kind, item))
    return out


def _claim_users(namespace, claim, workloads):
    users = []
    for kind, item in workloads:
        if item["metadata"].get("namespace") != namespace:
            continue
        volumes = ((((item.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or [])
        if any((v.get("persistentVolumeClaim") or {}).get("claimName") == claim for v in volumes):
            users.append(item["metadata"]["name"])
    return users


def _lost_detail(volumes, workloads):
    """Longhorn volumes by the claim and apps a person knows them by."""
    by_name = {v["metadata"]["name"]: v for v in _list("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes")}
    rows = []
    for volume in volumes:
        k8s = ((by_name.get(volume) or {}).get("status") or {}).get("kubernetesStatus") or {}
        ns, claim = k8s.get("namespace", ""), k8s.get("pvcName", "")
        rows.append({"volume": volume, "namespace": ns, "claim": claim,
                     "users": _claim_users(ns, claim, workloads) if claim else []})
    return rows


def _pins_to(affinity, name):
    """A required node affinity that allows only this host."""
    terms = ((((affinity or {}).get("nodeAffinity") or {}).get("requiredDuringSchedulingIgnoredDuringExecution") or {})
             .get("nodeSelectorTerms") or [])
    if not terms:
        return False
    for term in terms:
        hosts = [e for e in term.get("matchExpressions") or [] if e.get("key") == HOSTNAME and e.get("operator") == "In"]
        if not hosts or hosts[0].get("values") != [name]:
            return False
    return True


def _pinned_volumes(name, workloads):
    """Volumes kept on the host itself - k3s's local-path, local volumes -
    whose data went with it."""
    rows = []
    for pv in _list("/api/v1/persistentvolumes"):
        spec = pv.get("spec") or {}
        claim = spec.get("claimRef") or {}
        # A volume's affinity is "required"; a pod's is the long name.
        required = (spec.get("nodeAffinity") or {}).get("required")
        if not claim or not _pins_to({"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": required}}, name):
            continue
        rows.append({"pv": pv["metadata"]["name"], "namespace": claim.get("namespace", ""), "claim": claim.get("name", ""),
                     "class": spec.get("storageClassName", ""), "size": (spec.get("capacity") or {}).get("storage", ""),
                     "users": _claim_users(claim.get("namespace", ""), claim.get("name", ""), workloads)})
    return rows


def _pinned_workloads(name, workloads):
    """Apps that may run only on this host, so wait for it."""
    rows = []
    for kind, item in workloads:
        pod = (((item.get("spec") or {}).get("template") or {}).get("spec") or {})
        if (pod.get("nodeSelector") or {}).get(HOSTNAME) == name or _pins_to(pod.get("affinity"), name):
            rows.append({"kind": kind, "namespace": item["metadata"]["namespace"], "name": item["metadata"]["name"]})
    return rows


def _unpin(row):
    plural = "deployments" if row["kind"] == "Deployment" else "statefulsets"
    path = f"/apis/apps/v1/namespaces/{row['namespace']}/{plural}/{row['name']}"
    item = _get(path)
    if not item:
        return False
    pod = item["spec"]["template"]["spec"]
    (pod.get("nodeSelector") or {}).pop(HOSTNAME, None)
    if not pod.get("nodeSelector"):
        pod.pop("nodeSelector", None)
    node_affinity = (pod.get("affinity") or {}).get("nodeAffinity") or {}
    required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    for term in required.get("nodeSelectorTerms") or []:
        term["matchExpressions"] = [e for e in term.get("matchExpressions") or [] if e.get("key") != HOSTNAME]
    if required:
        required["nodeSelectorTerms"] = [t for t in required.get("nodeSelectorTerms") or []
                                         if t.get("matchExpressions") or t.get("matchFields")]
        if not required["nodeSelectorTerms"]:
            node_affinity.pop("requiredDuringSchedulingIgnoredDuringExecution", None)
    item["metadata"].pop("managedFields", None)
    ksend("PUT", path, item)
    return True


def _recreate_empty(row):
    """A claim whose data was on a dead host, made again empty under its own
    name, so its app starts somewhere else - with no data, which was lost."""
    ns, claim = row["namespace"], row["claim"]
    path = f"/api/v1/namespaces/{ns}/persistentvolumeclaims"
    old = _get(f"{path}/{claim}")
    if old:
        _force_delete(f"{path}/{claim}")
        ksend("PATCH", f"{path}/{claim}", {"metadata": {"finalizers": None}}, "application/merge-patch+json") \
            if _get(f"{path}/{claim}") else None
    if _get(f"/api/v1/persistentvolumes/{row['pv']}"):
        _force_delete(f"/api/v1/persistentvolumes/{row['pv']}")
        if _get(f"/api/v1/persistentvolumes/{row['pv']}"):
            ksend("PATCH", f"/api/v1/persistentvolumes/{row['pv']}", {"metadata": {"finalizers": None}},
                  "application/merge-patch+json")
    if not old:
        return False
    for _ in range(15):
        if not _get(f"{path}/{claim}"):
            break
        time.sleep(1)
    spec = {key: value for key, value in (old.get("spec") or {}).items()
            if key in ("accessModes", "resources", "storageClassName", "volumeMode")}
    annotations = {k: v for k, v in (old["metadata"].get("annotations") or {}).items()
                   if not k.startswith(("pv.kubernetes.io/", "volume.kubernetes.io/", "volume.beta.kubernetes.io/"))}
    ksend("POST", path, {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                         "metadata": {"name": claim, "namespace": ns, "labels": old["metadata"].get("labels") or {},
                                      "annotations": annotations}, "spec": spec})
    return True


def _password_secret(name):
    for suffix in ("k3s", "rke2"):
        path = f"/api/v1/namespaces/kube-system/secrets/{name}.node-password.{suffix}"
        if _get(path):
            return path
    return ""


def _stop_steps(node):
    """How to take a node out of service on the host, for what runs it."""
    kubelet = ((node.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "")
    if "+k3s" in kubelet:
        return ("drain it (kubectl drain --ignore-daemonsets --delete-emptydir-data), run "
                "k3s-agent-uninstall.sh on it (k3s-uninstall.sh on a server)")
    try:
        harvester = any(g.get("name") == "harvesterhci.io" for g in (_get("/apis") or {}).get("groups", []))
    except Exception:
        harvester = True
    if "+rke2" in kubelet and not harvester:
        return "drain it (kubectl drain --ignore-daemonsets --delete-emptydir-data), run /usr/local/bin/rke2-uninstall.sh on it"
    return "put it in maintenance mode in Harvester, run /opt/rke2/bin/rke2-uninstall.sh on it"


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
                        f"{_stop_steps(node)}, power it off, and come back when it shows Not ready.")
    else:
        down = _age(since)
        if down is not None and down < 600:
            warnings.append(f"{name} stopped reporting {_plural(max(1, int(down // 60)), 'minute')} ago. "
                            f"It may only be rebooting; give it a few minutes.")
    distribution = _distribution(node)
    workloads = _workloads()
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
        elif distribution == "harvester":
            warnings.append("Harvester promotes a worker to take its control-plane place, when one is available.")
        if distribution == "kubernetes" and "etcd" in roles:
            warnings.append(f"Plain Kubernetes keeps {name}'s etcd member after the node goes, and still counts it "
                            f"toward quorum: remove it on another control-plane host with "
                            f"etcdctl member list, then etcdctl member remove <its id>.")
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
    pinned_volumes = _pinned_volumes(name, workloads)
    if pinned_volumes:
        warnings.append(f"{_plural(len(pinned_volumes), 'volume')} kept on {name} itself "
                        f"({', '.join(v['namespace'] + '/' + v['claim'] for v in pinned_volumes[:4])}"
                        f"{' …' if len(pinned_volumes) > 4 else ''}): their data went with the host. Gone for good makes "
                        f"each again, empty, so its app can start elsewhere.")
    pinned_workloads = _pinned_workloads(name, workloads)
    if pinned_workloads:
        warnings.append(f"{_plural(len(pinned_workloads), 'app')} may run only on {name} "
                        f"({', '.join(w['name'] for w in pinned_workloads[:4])}{' …' if len(pinned_workloads) > 4 else ''}) "
                        f"and wait for it. Gone for good lets them run anywhere.")
    pods, vmis, attachments, replicas = _stuck_on(name) if not ready else ([], [], [], [])
    annotations = node["metadata"].get("annotations") or {}
    machine = annotations.get("cluster.x-k8s.io/machine", "")
    machine_ns = annotations.get("cluster.x-k8s.io/cluster-namespace", "fleet-local")
    steps.append(f"Stop Longhorn scheduling new replicas to {name}")
    server = "etcd" in roles or bool({"control-plane", "master"} & set(roles))
    steps.append(f"Delete the Kubernetes node {name}" + (
        " (RKE2 removes its etcd membership)" if server and distribution in ("harvester", "rke2")
        else " (k3s removes its etcd membership)" if server and distribution == "k3s" else ""))
    if distribution == "harvester":
        steps.append(f"Delete its Cluster API machine {machine}" if machine
                     else "Delete any Cluster API machine left pointing at it")
    else:
        steps.append(f"Delete its node-password Secret, so a rebuilt host called {name} can join again"
                     if distribution in ("k3s", "rke2") else f"Nothing else of {name}'s is kept outside the node")
    steps.append(f"Delete Longhorn's record of {name} once it holds no replicas")
    vm_names = sorted({(v.get("metadata") or {}).get("name", "") for v in vmis})
    gone_steps = [
        f"Force-delete the {_plural(len(pods), 'pod')} still bound to it, so their workloads start elsewhere",
        (f"Force-stop the {_plural(len(vmis), 'VM')} it was running ({', '.join(vm_names[:5])}), "
         f"so each restarts on another host" if vmis else "No VMs are recorded as running there"),
        f"Release {_plural(len(attachments), 'volume attachment')}, so those volumes can attach on another node",
        f"Delete the {_plural(len(replicas), 'replica record')} Longhorn keeps for it, "
        f"so it rebuilds them from the remaining copies",
        "Finish its Cluster API machine's deletion if finalizers hold it" if distribution == "harvester"
        else f"Delete its node-password Secret" if distribution in ("k3s", "rke2") else "Nothing more to let go of",
        f"Let {_plural(len(pinned_workloads), 'app')} pinned to it run on any host" if pinned_workloads
        else "No apps are pinned to it",
        f"Make {_plural(len(pinned_volumes), 'volume')} kept on it again, empty, so their apps can start elsewhere"
        if pinned_volumes else "No volumes were kept on the host itself",
    ]
    return {"node": name, "ready": ready, "roles": roles, "since": since,
            "blockers": blockers, "warnings": warnings, "steps": steps, "gone_steps": gone_steps,
            "stuck": {"pods": len(pods), "vms": vm_names, "attachments": len(attachments),
                      "replicas": len(replicas)},
            "lost_volumes": lost, "rebuilt_volumes": degraded,
            "lost_detail": _lost_detail(lost, workloads) if lost else [],
            "pinned_volumes": pinned_volumes, "pinned_workloads": pinned_workloads, "distribution": distribution,
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
    if gone and plan["pinned_volumes"] and not accept_loss:
        raise ValueError(f"{_plural(len(plan['pinned_volumes']), 'volume')} kept on {name} would be made again empty; "
                         f"confirm that first")
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
    secret = _password_secret(name)
    if secret:
        _force_delete(secret)
        log.append(f"Deleted {name}'s node-password Secret, so a rebuilt {name} can join")
    if gone:
        unpinned = [row["name"] for row in plan["pinned_workloads"] if _unpin(row)]
        if unpinned:
            log.append(f"{', '.join(unpinned)} may run on any host now")
        remade = [f"{row['namespace']}/{row['claim']}" for row in plan["pinned_volumes"] if _recreate_empty(row)]
        if remade:
            log.append(f"Made {', '.join(remade)} again, empty: {'its' if len(remade) == 1 else 'their'} data was on {name}")
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
    passwords = []
    for secret in _list("/api/v1/namespaces/kube-system/secrets"):
        match = PASSWORD_SECRET.match(secret["metadata"]["name"])
        if match and match.group(1) not in names:
            passwords.append({"name": secret["metadata"]["name"], "node": match.group(1)})
    workloads = _workloads()
    gone_nodes = set()
    pinned_volumes, pinned_workloads = [], []
    for pv in _list("/api/v1/persistentvolumes"):
        terms = ((((pv.get("spec") or {}).get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms") or [])
        for term in terms:
            for expression in term.get("matchExpressions") or []:
                if expression.get("key") == HOSTNAME and len(expression.get("values") or []) == 1 \
                        and expression["values"][0] not in names:
                    gone_nodes.add(expression["values"][0])
    for gone_node in sorted(gone_nodes):
        pinned_volumes += [dict(row, node=gone_node) for row in _pinned_volumes(gone_node, workloads)]
    for kind, item in workloads:
        pod = (((item.get("spec") or {}).get("template") or {}).get("spec") or {})
        host = (pod.get("nodeSelector") or {}).get(HOSTNAME)
        if host and host not in names:
            pinned_workloads.append({"kind": kind, "namespace": item["metadata"]["namespace"],
                                     "name": item["metadata"]["name"], "node": host})
    attachments = [{"name": a["metadata"]["name"], "node": (a.get("spec") or {}).get("nodeName", "")}
                   for a in _list("/apis/storage.k8s.io/v1/volumeattachments")
                   if (a.get("spec") or {}).get("nodeName") and a["spec"]["nodeName"] not in names]
    return {"dead_nodes": dead, "stale_machines": stale_machines, "stale_longhorn": stale_longhorn,
            "passwords": passwords, "pinned_volumes": pinned_volumes, "pinned_workloads": pinned_workloads,
            "attachments": attachments}


def cleanup(kind, name, force=False):
    """Removes one leftover record the report found. `force` is for a host that is gone for good."""
    if kind in ("password", "pinned-volume", "pin", "attachment"):
        report = cleanup_report()
        if kind == "password":
            if name not in {row["name"] for row in report["passwords"]}:
                raise ValueError("that Secret belongs to a node that is still here")
            _force_delete(f"/api/v1/namespaces/kube-system/secrets/{name}")
            return {"ok": True, "message": f"Deleted {name}"}
        if kind == "attachment":
            if name not in {row["name"] for row in report["attachments"]}:
                raise ValueError("that attachment is on a node that is still here")
            _force_delete(f"/apis/storage.k8s.io/v1/volumeattachments/{name}")
            ksend("PATCH", f"/apis/storage.k8s.io/v1/volumeattachments/{name}", {"metadata": {"finalizers": None}},
                  "application/merge-patch+json") if _get(f"/apis/storage.k8s.io/v1/volumeattachments/{name}") else None
            return {"ok": True, "message": f"Released attachment {name}"}
        if kind == "pin":
            row = next((r for r in report["pinned_workloads"] if f"{r['namespace']}/{r['name']}" == name), None)
            if not row:
                raise ValueError("that app is not pinned to a host that is gone")
            _unpin(row)
            return {"ok": True, "message": f"{row['name']} may run on any host now"}
        row = next((r for r in report["pinned_volumes"] if f"{r['namespace']}/{r['claim']}" == name), None)
        if not row:
            raise ValueError("that volume is not kept on a host that is gone")
        if not force:
            raise ValueError(f"{name}'s data was on {row['node']}; confirm making it again empty")
        for pod in _list(f"/api/v1/namespaces/{row['namespace']}/pods"):
            volumes = (pod.get("spec") or {}).get("volumes") or []
            if any((v.get("persistentVolumeClaim") or {}).get("claimName") == row["claim"] for v in volumes):
                _force_delete(f"/api/v1/namespaces/{row['namespace']}/pods/{pod['metadata']['name']}")
        _recreate_empty(row)
        return {"ok": True, "message": f"Made {name} again, empty; its app can start on another host"}
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
